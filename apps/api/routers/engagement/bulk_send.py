"""Engagement package: bulk_send."""

import asyncio
import inspect
import uuid
from fastapi import HTTPException
from typing import Optional, List, Any, Dict, Tuple, Callable
import json
import os
from datetime import datetime, timezone
from core.pipeline_trace import span as _trace_span
from core.queue import enqueue
from core.queue_tasks import PROVISION_JOBDIVA_BATCH

from .claims import LAUNCH_BLOCK_DUPLICATE_OTHER_JOB, LAUNCH_BLOCK_DUPLICATE_PERSON_THIS_JOB, LAUNCH_BLOCK_DUPLICATE_UNVERIFIED, _claim_rows_for_send, _launch_block_reason_text, _map_duplicate_interviews, _pairbot_rejected_ids, _person_duplicate_reason, _release_send_claim
from .common import EXTERNAL_INTERVIEW_API_URL, _parse_json_list, logger
from .pairbot import _confirm_failed_candidates_with_pairbot, _stamp_engage_status_batch
from .payloads import _extract_pair_error_message, _merge_employer_signals, _validate_pair_payload_contacts, is_candidate_excluded_from_pair
from . import common
from . import models
from . import pairbot
from . import payloads


# Per candidate (any row under either job key): already sent, or blocked.
# Params: claimed_session_id twice ('' = none), candidate ids, job key, alt key.
_ENGAGE_IDEMPOTENCY_SQL = """
    SELECT candidate_id,
           bool_or(
               (
                   COALESCE(data ->> 'engage_status', '') NOT IN ('', 'failed')
                   AND NOT (
                       data ->> 'engage_status' = 'queued'
                       AND %s <> ''
                       AND COALESCE(data ->> 'engage_session_id', '') = %s
                   )
               )
               OR COALESCE(data ->> 'engage_interview_id', '') <> ''
           ) AS already_sent,
           MAX(NULLIF(data ->> 'engage_block_reason', '')) AS block_reason
    FROM sourced_candidates
    WHERE candidate_id = ANY(%s)
      AND (jobdiva_id = %s OR jobdiva_id = %s)
    GROUP BY candidate_id
"""


def _is_production() -> bool:
    return os.getenv("ENVIRONMENT", "").strip().lower() in {"production", "prod"}


def _audit_payload_json(payload_obj: Any) -> str:
    """Serialize a launch payload for an audit row, without résumé text."""
    return json.dumps(payloads._payload_without_resume_text(payload_obj))


def _apply_handoff_fields(payload_obj: dict, request, recruiter_email: Optional[str]) -> None:
    """Only the launch's recruiter can join, so a handoff without one is
    refused by Pair-bot (422); send the flag only when there is someone to
    hand off to. Handoff is not enabled in production yet (the UI hides the
    toggle there), so the flag is dropped server-side too."""
    if request.recruiter_handoff and _is_production():
        logger.warning("recruiter_handoff requested in production; launching without handoff")
        return
    if request.recruiter_handoff and recruiter_email:
        payload_obj["recruiter_handoff"] = True
        if request.handoff_timeout_sec:
            payload_obj["handoff_timeout_sec"] = request.handoff_timeout_sec
    elif request.recruiter_handoff:
        logger.warning("recruiter_handoff requested without a recruiter_email; launching without handoff")


async def _send_bulk_interview_core(
    request: models.SendBulkInterviewRequest,
    precomputed_signals: Optional[Dict[str, Dict[str, Any]]] = None,
    launch_id: Optional[str] = None,
    batch_idx: Optional[int] = None,
    *,
    claimed_session_id: Optional[str] = None,
    provision_inline: bool = True,
    bulk_id_for: Optional[Callable[..., Any]] = None,
    recruiter_email: Optional[str] = None,
):
    """
    Send the (potentially edited) interview payload to the PAIR bulk-interviews API.
    Saves the request and response to engage_interview_audit for traceability.

    Every path takes the send claim right before the POST (rows → 'sending';
    only the ids it wins are posted, the rest come back in
    skipped_already_sent; a DB error posts nothing). If no answer comes back
    from Pairbot, the held rows are released ('failed', or 'queued' for the
    claiming session's own rows).

    Launch-session hooks (all default to the legacy behaviour):
      claimed_session_id  rows 'queued' with this data.engage_session_id are
                          sendable (every other non-empty engage_status still
                          reads as already sent), the idempotency read fails
                          CLOSED — nothing is posted if it errors — and only
                          ids with a saved row are ever posted (a row-less id
                          is excluded; legacy calls still post it unclaimed).
      provision_inline    False = skip the fire-and-forget JobDiva
                          provisioning; the caller runs provision_sent_candidates.
      bulk_id_for         ``(batch_idx, posted_candidate_ids) -> bulk_id`` (or an
                          awaitable of it), called right before the POST with the
                          exact source_candidate_ids of the payload in resume
                          order; its id is the client bulk_id. If it raises,
                          nothing is posted.
    """
    # The fire-once ledger's "this launch" boundary (launch_effects_ledger).
    _core_started_at = datetime.now(timezone.utc)
    # Pool-leak safety net for the audit-write block below: if any exception
    # fires between `conn = _get_db_connection()` and the explicit close, the
    # outer try's finally returns the slot. The explicit close stays on the
    # happy path so the slot frees before the post-success email tasks run;
    # _PooledConnection.close() is idempotent (re-close is a no-op).
    conn = None
    cur = None
    try:
        # Parse the payload
        try:
            payload_obj = json.loads(request.payload)
            jd_block = payload_obj.get("jd", {})
            job_id_from_payload = jd_block.get("job_id") or jd_block.get("jobdiva_id") or "unknown"
            print(f"DEBUG: send_bulk_interview called for job {job_id_from_payload}")
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON format in payload")

        _validate_pair_payload_contacts(payload_obj)

        # Fetch job metadata (recruiter emails) and defense-in-depth outreach check
        if job_id_from_payload and job_id_from_payload != "unknown":
            try:
                _stop_conn = common._get_db_connection()
                try:
                    _stop_cur = _stop_conn.cursor()
                    _stop_cur.execute("""
                        SELECT outreach_stopped_at, recruiter_emails FROM monitored_jobs
                        WHERE job_id = %s OR jobdiva_id = %s
                        LIMIT 1
                    """, (str(job_id_from_payload), str(job_id_from_payload)))
                    _stop_row = _stop_cur.fetchone()
                    _stop_cur.close()
                    if _stop_row:
                        if _stop_row[0] is not None:
                            raise HTTPException(
                                status_code=409,
                                detail="Job activity has been stopped. Cannot launch new candidates.",
                            )
                        # Inject recruiter emails into PAIR payload so PAIR can send notifications (e.g. 20-hour report)
                        if _stop_row[1]:
                            recruiter_emails = [
                                str(email).strip()
                                for email in _parse_json_list(_stop_row[1])
                                if str(email).strip()
                            ]
                            payload_obj.setdefault("jd", {})["recruiter_emails"] = recruiter_emails
                finally:
                    _stop_conn.close()
            except HTTPException:
                raise
            except Exception as _stop_check_err:
                logger.warning(f"Could not check job metadata for job {job_id_from_payload}: {_stop_check_err}")

        # Idempotency: drop candidates that already have a *successful* engage_status
        # so retries or staged-launch races don't create duplicate interviews on the
        # PAIR side. Crucially, 'failed' is NOT treated as already-sent — a candidate
        # PAIR accepted but returned no interview_id for (or that erred) must remain
        # retryable, otherwise a transient miss would silently skip them forever.
        # We keep the skipped candidate_ids in skipped_already_sent so the caller
        # can show them as already-launched in the UI.
        #
        # Also already sent: a row holding an interview id (the voice webhook
        # can leave engage_status 'failed' = an interview Fail), and a row
        # 'queued' by a launch session other than ``claimed_session_id``.
        # Rows with data.engage_block_reason (a Pairbot duplicate) are never
        # re-posted; they come back as exclusions. This read is an early,
        # advisory filter: the send claim right before the POST
        # (_claim_rows_for_send) is what guarantees one post per person.
        skipped_already_sent: List[str] = []
        blocked_records: List[Dict[str, str]] = []
        if request.real_candidate_ids:
            try:
                _idem_conn = common._get_db_connection()
                try:
                    _idem_cur = _idem_conn.cursor()
                    _idem_cur.execute(
                        _ENGAGE_IDEMPOTENCY_SQL,
                        (
                            str(claimed_session_id or ""),
                            str(claimed_session_id or ""),
                            list(request.real_candidate_ids),
                            str(job_id_from_payload or ""),
                            str(payload_obj.get("jd", {}).get("jobdiva_id") or ""),
                        ),
                    )
                    for _idem_row in _idem_cur.fetchall() or []:
                        if _idem_row[1]:
                            skipped_already_sent.append(_idem_row[0])
                        elif _idem_row[2]:
                            blocked_records.append({
                                "candidate_id": str(_idem_row[0]),
                                "name": "",
                                "reason": _launch_block_reason_text(str(_idem_row[2])),
                            })
                    _idem_cur.close()
                finally:
                    _idem_conn.close()
            except Exception as _idem_err:
                logger.warning(f"engage idempotency check failed for job {job_id_from_payload}: {_idem_err}")
                if claimed_session_id:
                    # A launch session never posts blind: fail CLOSED. Its
                    # rows stay 'queued' for the session to retry.
                    return {
                        "success": False,
                        "message": "Launch idempotency check failed; nothing was sent.",
                        "data": [],
                        "skipped_already_sent": [],
                        "excluded_candidates": [],
                        "employer_unverified": [],
                        "raw_response": {},
                    }

        if skipped_already_sent or blocked_records:
            skip_set = set(skipped_already_sent) | {b["candidate_id"] for b in blocked_records}
            kept_indices = [
                idx for idx, cid in enumerate(request.real_candidate_ids) if cid not in skip_set
            ]
            request.real_candidate_ids = [request.real_candidate_ids[i] for i in kept_indices]
            existing_resumes = payload_obj.get("resumes") if isinstance(payload_obj, dict) else None
            if isinstance(existing_resumes, list) and len(existing_resumes) >= max(kept_indices, default=-1) + 1:
                payload_obj["resumes"] = [existing_resumes[i] for i in kept_indices]
            logger.info(
                f"engage idempotency: skipped {len(skipped_already_sent)} already-sent candidates for job {job_id_from_payload}"
                + (f" (+{len(blocked_records)} blocked)" if blocked_records else "")
            )

        excluded_records: List[Dict[str, str]] = list(blocked_records)
        unverified_employer_records: List[Dict[str, str]] = []
        if request.real_candidate_ids:
            excluded_candidate_ids = set()
            try:
                # (candidate_id, name, hydrated candidate) — rows are collected
                # first and gated after the DB connection is released, because
                # the employer-resolution pass between the two can await
                # resume fetches + LLM parses for a while.
                _gate_rows: List[Tuple[str, str, Dict[str, Any]]] = []
                _excl_conn = common._get_db_connection()
                try:
                    _excl_cur = _excl_conn.cursor()
                    _excl_cur.execute(
                        """
                        SELECT customer_name
                        FROM monitored_jobs
                        WHERE job_id = %s OR jobdiva_id = %s
                        LIMIT 1
                        """,
                        (str(job_id_from_payload or ""), str(job_id_from_payload or "")),
                    )
                    _excl_client_row = _excl_cur.fetchone()
                    _excl_client = (
                        str(_excl_client_row[0])
                        if _excl_client_row and _excl_client_row[0]
                        else ""
                    )

                    _excl_cur.execute(
                        """
                        SELECT candidate_id, data, name, headline, resume_text, source
                        FROM sourced_candidates
                        WHERE candidate_id = ANY(%s)
                          AND (jobdiva_id = %s OR jobdiva_id = %s)
                        """,
                        (
                            list(request.real_candidate_ids),
                            str(job_id_from_payload or ""),
                            str(payload_obj.get("jd", {}).get("jobdiva_id") or ""),
                        ),
                    )
                    _excl_rows = _excl_cur.fetchall() or []
                    # Employment history lives in candidate_enhanced_info, not
                    # in `data` — without this the gate matches an empty blob.
                    _excl_signals = payloads._fetch_stored_employer_signals(
                        _excl_conn, [str(r[0]) for r in _excl_rows]
                    )
                    for _row in _excl_rows:
                        _cdata = _row[1] or {}
                        if isinstance(_cdata, str):
                            try:
                                _cdata = json.loads(_cdata)
                            except Exception:
                                _cdata = {}
                        _cdata = _merge_employer_signals(
                            _cdata,
                            headline=str(_row[3] or ""),
                            signals=_excl_signals.get(str(_row[0])),
                        )
                        _cdata.setdefault("candidate_id", str(_row[0]))
                        # Stored resume + source ride along so the resolution
                        # pass can parse without re-fetching from JobDiva.
                        if _row[4] and not _cdata.get("resume_text"):
                            _cdata["resume_text"] = str(_row[4])
                        if _row[5] and not _cdata.get("source"):
                            _cdata["source"] = str(_row[5])
                        _gate_rows.append((str(_row[0]), str(_row[2] or ""), _cdata))
                    _excl_cur.close()
                finally:
                    _excl_conn.close()

                # Employer resolution (policy 2026-09-02): any candidate whose
                # employer data is missing or not confident gets their resume
                # fetched + parsed NOW, before outreach, with JobDiva's own
                # profile work history attached as corroboration. Fails open —
                # a resolution error gates on stored signals, as before.
                try:
                    from services.employer_resolution import employer_verification_state
                except Exception:  # noqa: BLE001
                    employer_verification_state = None  # type: ignore[assignment]
                _resolved_signals: Dict[str, Dict[str, Any]] = {}
                # Fix 1: /engage/launch resolves once for the whole pool and
                # passes the result in. Every id the launch pass covered is a
                # key (empty dict = nothing resolved), so only rows it missed
                # are resolved here. Legacy /engage/send-bulk passes None.
                if precomputed_signals is not None:
                    _resolved_signals = {
                        _cid: precomputed_signals[_cid]
                        for _cid, _, _ in _gate_rows
                        if _cid in precomputed_signals
                    }
                    _to_resolve = [
                        c for _cid, _, c in _gate_rows if _cid not in precomputed_signals
                    ]
                else:
                    _to_resolve = [c for _, _, c in _gate_rows]
                if _to_resolve:
                    try:
                        from services.employer_resolution import resolve_employer_signals
                        async with _trace_span("employer_gate", scope="batch", n=len(_to_resolve)):
                            _resolved_signals.update(await asyncio.wait_for(
                                resolve_employer_signals(_to_resolve),
                                timeout=240.0
                            ) or {})
                    except Exception as _res_err:  # noqa: BLE001
                        logger.warning(
                            "employer resolution failed for job %s (gating on stored signals): %s",
                            job_id_from_payload, _res_err,
                        )

                _gate_rows = [
                    (
                        _cid,
                        _cname,
                        _merge_employer_signals(_cdata, signals=_resolved_signals[_cid])
                        if _resolved_signals.get(_cid) else _cdata,
                    )
                    for _cid, _cname, _cdata in _gate_rows
                ]
                # Launch-level freshness stamps ride on the precomputed entry.
                if precomputed_signals is not None:
                    for _cid, _, _cdata in _gate_rows:
                        _ts = (precomputed_signals.get(_cid) or {}).get("resume_updated_at")
                        if _ts and not _cdata.get("resume_updated_at"):
                            _cdata["resume_updated_at"] = _ts
                # Resume freshness (phase 3): stamp JobDiva's resume timestamp
                # onto resume-verified candidates so a years-old "Present"
                # classifies as verified_stale, not verified. Advisory only —
                # never blocks; fails open.
                try:
                    from services.employer_resolution import stamp_resume_freshness
                    # Hard 20s cap: freshness is advisory, and this fetch sits
                    # on the launch path — a slow JobDiva must cost seconds,
                    # not a batch-long stall (TimeoutError lands in the
                    # except below and the launch proceeds unstamped).
                    _fresh_rows = [
                        c for _cid, _, c in _gate_rows
                        if precomputed_signals is None or _cid not in precomputed_signals
                    ]
                    if _fresh_rows:
                        await asyncio.wait_for(
                            stamp_resume_freshness(_fresh_rows),
                            timeout=20.0,
                        )
                except Exception as _fresh_err:  # noqa: BLE001
                    logger.warning(
                        "resume-freshness check failed for job %s: %s",
                        job_id_from_payload, _fresh_err,
                    )

                for _cid, _cname, _cdata in _gate_rows:
                    _is_excl, _excl_reason = is_candidate_excluded_from_pair(
                        _cdata, _excl_client
                    )
                    if _is_excl:
                        logger.info(
                            "send_bulk_interview skipping excluded candidate %s reason: %s",
                            _cid,
                            _excl_reason,
                        )
                        excluded_candidate_ids.add(_cid)
                        excluded_records.append({
                            "candidate_id": _cid,
                            "name": _cname,
                            "reason": _excl_reason,
                        })
                        continue
                    # Not excluded — but say HOW WELL we know that. An
                    # unverified employer means the client/no-contact checks
                    # had nothing to judge, not that the candidate is clean.
                    if employer_verification_state is not None:
                        _emp_state = employer_verification_state(_cdata)
                        if _emp_state != "verified":
                            _unv_record = {
                                "candidate_id": _cid,
                                "name": _cname,
                                "employer_verification": _emp_state,
                            }
                            if _cdata.get("resume_updated_at"):
                                _unv_record["resume_updated_at"] = str(
                                    _cdata["resume_updated_at"]
                                )
                            unverified_employer_records.append(_unv_record)
                            logger.info(
                                "launch_employer_unverified candidate=%s job=%s state=%s client=%r",
                                _cid, job_id_from_payload, _emp_state, _excl_client,
                            )
            except Exception as _excl_err:
                logger.warning(
                    f"candidate exclusion check failed for job {job_id_from_payload}: {_excl_err}"
                )

            if excluded_candidate_ids:
                kept_indices = [
                    idx
                    for idx, cid in enumerate(request.real_candidate_ids)
                    if cid not in excluded_candidate_ids
                ]
                request.real_candidate_ids = [
                    request.real_candidate_ids[i] for i in kept_indices
                ]
                existing_resumes = (
                    payload_obj.get("resumes")
                    if isinstance(payload_obj, dict)
                    else None
                )
                if (
                    isinstance(existing_resumes, list)
                    and len(existing_resumes) >= max(kept_indices, default=-1) + 1
                ):
                    payload_obj["resumes"] = [existing_resumes[i] for i in kept_indices]

        if not request.real_candidate_ids:
            return {
                "success": True,
                "message": "All requested candidates were already launched or excluded; nothing to send.",
                "data": [],
                "skipped_already_sent": skipped_already_sent,
                "excluded_candidates": excluded_records,
                "employer_unverified": unverified_employer_records,
                "raw_response": {},
            }

        is_success = False
        response_data = {}

        if request.dry_run:
            logger.info("🧪 Dry run enabled: Skipping external PAIR API call.")
            is_success = True
            response_data = {
                "status": "success",
                "message": "DRY RUN: External call skipped, but local processing continued.",
                "data": [
                    {"interview_id": f"dry_run_{cid}", "candidate_id": cid}
                    for cid in request.real_candidate_ids
                ]
            }
        else:
            # Send claim (every launch path — see "Per-candidate launch
            # claims"): atomically move the rows about to be posted to
            # 'sending' and post ONLY what this call won. The idempotency read
            # above is advisory (minutes of employer resolution can pass since
            # it ran); an id that lost the claim was sent, claimed or blocked
            # by another path meanwhile and comes back as already sent. A DB
            # error here posts nothing.
            _send_keys = (
                str(job_id_from_payload or ""),
                str(payload_obj.get("jd", {}).get("jobdiva_id") or ""),
            )
            # Contacts of the payload's résumés, for the person gate's view of
            # ids that have no saved row (legacy posts those unclaimed).
            _gate_fallback: Dict[str, Dict[str, Any]] = {}
            for _r in (payload_obj.get("resumes") if isinstance(payload_obj, dict) else None) or []:
                if isinstance(_r, dict) and str(_r.get("source_candidate_id") or "").strip():
                    _gate_fallback[str(_r["source_candidate_id"]).strip()] = {
                        "email": _r.get("email"), "phone": _r.get("phone"),
                        "profile_url": _r.get("linkedin_url") or _r.get("linkedin") or _r.get("profile_url"),
                    }
            try:
                _send_claimed, _send_with_rows, _send_dups = _claim_rows_for_send(
                    _send_keys, list(request.real_candidate_ids), claimed_session_id, _gate_fallback,
                )
            except Exception as _claim_err:  # noqa: BLE001
                logger.error(
                    "engage_send_claim_failed job=%s candidates=%d err=%s — nothing sent",
                    job_id_from_payload, len(request.real_candidate_ids), _claim_err,
                )
                return {
                    "success": False,
                    "message": "Launch claim failed; nothing was sent.",
                    "data": [],
                    "skipped_already_sent": skipped_already_sent,
                    "excluded_candidates": excluded_records,
                    "employer_unverified": unverified_employer_records,
                    "raw_response": {},
                }
            _send_keep = set(_send_claimed)
            # Person gate: another id of a person already held for this job.
            # Never posted, never "row-less", reported as an exclusion.
            for _c, (_other, _kind) in _send_dups.items():
                excluded_records.append({
                    "candidate_id": _c, "name": "", "reason": _person_duplicate_reason(_other, _kind),
                })
            _rowless = [
                str(c) for c in request.real_candidate_ids
                if str(c) not in _send_with_rows and str(c) not in _send_dups
            ]
            if _rowless:
                if claimed_session_id:
                    # A session posts only rows it holds — never a row-less id.
                    for _c in _rowless:
                        excluded_records.append({
                            "candidate_id": _c, "name": "", "reason": "not saved for this job",
                        })
                else:
                    # Legacy: no saved row to claim (nor for a session to
                    # race on) — posted as before.
                    _send_keep.update(_rowless)
                logger.warning(
                    "engage_send_claim_rowless job=%s candidates=%d session=%s",
                    job_id_from_payload, len(_rowless), bool(claimed_session_id),
                )
            _send_lost = [
                str(c) for c in request.real_candidate_ids
                if str(c) not in _send_keep and str(c) in _send_with_rows
            ]
            if _send_lost:
                skipped_already_sent.extend(_send_lost)
                logger.warning(
                    "engage_send_claim_lost job=%s candidates=%d (sent or claimed by another launch meanwhile)",
                    job_id_from_payload, len(_send_lost),
                )
            request.real_candidate_ids = [
                c for c in request.real_candidate_ids if str(c) in _send_keep
            ]
            _send_resumes = payload_obj.get("resumes") if isinstance(payload_obj, dict) else None
            if isinstance(_send_resumes, list):
                if claimed_session_id:
                    payload_obj["resumes"] = [
                        r for r in _send_resumes
                        if isinstance(r, dict)
                        and str(r.get("source_candidate_id") or "").strip() in _send_keep
                    ]
                elif _send_lost or _send_dups:
                    _send_drop = set(_send_lost) | set(_send_dups)
                    payload_obj["resumes"] = [
                        r for r in _send_resumes
                        if not (
                            isinstance(r, dict)
                            and str(r.get("source_candidate_id") or "").strip() in _send_drop
                        )
                    ]
            if not request.real_candidate_ids:
                return {
                    "success": True,
                    "message": "All requested candidates were already launched or excluded; nothing to send.",
                    "data": [],
                    "skipped_already_sent": skipped_already_sent,
                    "excluded_candidates": excluded_records,
                    "employer_unverified": unverified_employer_records,
                    "raw_response": {},
                }
            # The rows this call now holds in 'sending'. Until Pairbot answers,
            # any exit gives them back (_release_send_claim): a session's own
            # rows to 'queued', others to 'failed'.
            _send_held = [c for c in request.real_candidate_ids if str(c) in set(_send_claimed)]

            # Send to external PAIR API
            external_url = f"{EXTERNAL_INTERVIEW_API_URL}/api/bulk-interviews"
            try:
                # Client-generate bulk_id so requests are idempotent and lookups
                # succeed even if the connection drops after PairBot accepts but
                # before the 202 response arrives — without this, a client-side
                # retry has no id to look up and would create a second bulk.
                client_bulk_id = str(uuid.uuid4())
                _launch_idem_key: Optional[str] = None
                if launch_id is not None and batch_idx is not None:
                    # Fix 3: deterministic per (launch, batch, ids) so a re-run of
                    # the same async batch reuses Pairbot's bulk instead of
                    # creating a duplicate.
                    _launch_idem_key = launch._launch_batch_idempotency_key(
                        launch_id, batch_idx, request.real_candidate_ids
                    )
                    client_bulk_id = str(uuid.uuid5(uuid.NAMESPACE_URL, _launch_idem_key))
                if bulk_id_for is not None:
                    # Launch session: the runner owns the bulk id (deterministic
                    # per session/attempt/batch; Pairbot honours a client bulk_id)
                    # and records the exact posted ids before anything leaves this
                    # process. If that fails, nothing is sent.
                    _posted_ids = [
                        str(r.get("source_candidate_id") or "")
                        for r in (payload_obj.get("resumes") or [])
                        if isinstance(r, dict)
                    ]
                    try:
                        _forced_bulk_id = bulk_id_for(batch_idx, _posted_ids)
                        if inspect.isawaitable(_forced_bulk_id):
                            _forced_bulk_id = await _forced_bulk_id
                    except Exception as _bid_err:  # noqa: BLE001
                        logger.error(
                            "launch_bulk_id_for_failed batch_idx=%s candidates=%d err=%s — not sending",
                            batch_idx, len(_posted_ids), _bid_err,
                        )
                        _release_send_claim(_send_keys, _send_held, claimed_session_id)
                        _send_held = []
                        return {
                            "success": False,
                            "message": f"Could not record the batch before sending ({type(_bid_err).__name__}); nothing was sent.",
                            "data": [],
                            "skipped_already_sent": skipped_already_sent,
                            "excluded_candidates": excluded_records,
                            "employer_unverified": unverified_employer_records,
                            "raw_response": {},
                        }
                    if _forced_bulk_id:
                        client_bulk_id = str(_forced_bulk_id)
                if isinstance(payload_obj, dict):
                    payload_obj["bulk_id"] = client_bulk_id
                    # Server-resolved recruiter only (never a request field,
                    # so /engage/send-bulk-interview callers cannot set it).
                    if recruiter_email:
                        payload_obj["recruiter_email"] = recruiter_email
                    _apply_handoff_fields(payload_obj, request, recruiter_email)
                logger.info(f"📤 Sending bulk interview to {external_url} with bulk_id={client_bulk_id}")

                async with _trace_span("pairbot_post", batch_idx=batch_idx):
                    response = await pairbot._post_to_pairbot(external_url, payload_obj)
            except BaseException:
                # No answer from Pairbot (transport error, timeout, cancel).
                # Pre-claim behaviour for legacy rows ('failed' = retryable,
                # as a failed POST always left them); a session keeps its rows
                # 'queued' and reconciles by-job before any re-post.
                if _send_held:
                    _release_send_claim(_send_keys, _send_held, claimed_session_id)
                raise

            response_data = {}
            try:
                if response.content:
                    response_data = response.json()
                else:
                    response_data = {"message": "Success (Empty response)"}
            except Exception:
                response_data = {"message": f"Raw: {response.text[:100]}"}

            # PAI-155: 202 Accepted means interviews are creating in background.
            # Wait on SSE for creation_completed before writing audit/engage_status.
            is_success = 200 <= response.status_code < 300
            logger.info(f"📥 PAIR API response status: {response.status_code}")
            if is_success and response.status_code == 202:
                bulk_id = str(response_data.get("bulk_id") or client_bulk_id).strip()
                if not bulk_id:
                    bulk_id = client_bulk_id
                job_id_alt = str(payload_obj.get("jd", {}).get("jobdiva_id") or "")
                # Mark processing so rank-list shows work in progress if the
                # stream is slow; overwritten to sent/failed after creation.
                # The bulk id rides in the same UPDATE (data.engage_bulk_id) so
                # a resume knows which bulk to reconcile a 'processing' row on.
                try:
                    _stamp_engage_status_batch(
                        list(request.real_candidate_ids),
                        "processing",
                        job_id_value=str(job_id_from_payload or ""),
                        job_id_alt=job_id_alt,
                        bulk_id=bulk_id,
                    )
                except Exception as _proc_err:
                    logger.warning(
                        "engage_processing_stamp_failed bulk_id=%s err=%s",
                        bulk_id,
                        _proc_err,
                    )
                logger.info(
                    "pairbot_async_accepted bulk_id=%s waiting_for_creation candidates=%d",
                    bulk_id,
                    len(request.real_candidate_ids),
                )
                try:
                    if _launch_idem_key is not None:
                        launch._launch_batch_set_bulk(
                            _launch_idem_key, bulk_id, launch_id=launch_id,
                            idx=batch_idx, candidate_ids=list(request.real_candidate_ids),
                        )
                    async with _trace_span("pairbot_wait_creation", batch_idx=batch_idx):
                        if _launch_idem_key is not None:
                            # Webhook (POST /webhooks/pairbot/creation) first,
                            # SSE wait as the fallback — whichever lands first.
                            creation_event = await launch._wait_for_creation_webhook_or_sse(bulk_id)
                        else:
                            creation_event = await pairbot._wait_for_pairbot_creation(bulk_id)
                except Exception as wait_err:
                    # A dead stream or a wait timeout is NOT proof creation
                    # failed — PairBot already accepted the bulk and may
                    # still be working. Check PairBot's own record before
                    # stamping anyone `failed`; a retry on a false failure
                    # would create a duplicate bulk for candidates PairBot
                    # already has interviews for.
                    logger.error(
                        "engage_wait_for_creation_error bulk_id=%s candidates=%d err=%s "
                        "— confirming with PairBot before stamping failed",
                        bulk_id,
                        len(request.real_candidate_ids),
                        wait_err,
                    )
                    confirmed_failed = await _confirm_failed_candidates_with_pairbot(
                        bulk_id, job_id_alt, list(request.real_candidate_ids),
                        job_keys=(str(job_id_from_payload or ""), job_id_alt),
                    )
                    if confirmed_failed:
                        try:
                            _stamp_engage_status_batch(
                                confirmed_failed,
                                "failed",
                                job_id_value=str(job_id_from_payload or ""),
                                job_id_alt=job_id_alt,
                            )
                        except Exception as _fail_stamp_err:
                            logger.error(
                                "engage_failed_stamp_after_wait_error bulk_id=%s err=%s",
                                bulk_id,
                                _fail_stamp_err,
                            )
                    else:
                        # PairBot confirms it's still working, or the check
                        # itself couldn't tell — leave as 'processing'. There
                        # is no automatic reconciler any more, so this needs
                        # manual follow-up (dashboard/logs on bulk_id) if it
                        # never resolves.
                        logger.error(
                            "engage_wait_for_creation_error_unconfirmed bulk_id=%s "
                            "candidates=%d left as 'processing' — PairBot did not "
                            "confirm these failed; needs manual follow-up if it "
                            "never resolves",
                            bulk_id,
                            len(request.real_candidate_ids),
                        )
                    raise wait_err
                response_data = {
                    **response_data,
                    "bulk_id": bulk_id,
                    "data": creation_event.get("data") or [],
                    "total_created": creation_event.get("total_created"),
                    "failed_interviews": creation_event.get("failed_interviews"),
                    "message": creation_event.get("message")
                    or response_data.get("message")
                    or "Interviews created",
                }
                logger.info(
                    "pairbot_creation_completed bulk_id=%s created=%s",
                    bulk_id,
                    response_data.get("total_created"),
                )

        # Pairbot DuplicateInterview answers: resolved against by-job BEFORE
        # the audit connection is taken (a network call must not hold a pool
        # slot). Proven ones (by-job lists an interview under the candidate's
        # own id) are written 'sent' by reconcile_with_pairbot and reported as
        # already launched; proven-but-unwritten ones (its DB write failed) are
        # written by the loop below, in this call's own transaction. The rest
        # — twins of a person this job already interviews, other-job
        # duplicates, unverifiable ones — are failed + blocked below.
        dup_proven: Dict[str, str] = {}
        dup_unrecorded: Dict[str, str] = {}
        dup_blocked: Dict[str, str] = {}
        dup_items: Dict[str, Dict[str, Any]] = {}
        if is_success and isinstance(response_data, dict) and not request.dry_run:
            try:
                _created_ids = {
                    str(item.get("source_candidate_id") or "").strip()
                    for item in (response_data.get("data") or [])
                    if isinstance(item, dict) and item.get("interview_id")
                }
                _dups = _map_duplicate_interviews(
                    response_data.get("failed_interviews"), payload_obj,
                    request.real_candidate_ids, _created_ids,
                )
                dup_items = {
                    _cid: {k: item.get(k) for k in ("error_type", "error", "existing_interview_id", "resume_index")}
                    for _cid, item in _dups.items()
                }
                dup_existing = {
                    _cid: str(item.get("existing_interview_id") or "").strip() for _cid, item in _dups.items()
                }
                if dup_existing:
                    rec = await pairbot.reconcile_with_pairbot(
                        (str(job_id_from_payload or ""), str(payload_obj.get("jd", {}).get("jobdiva_id") or "")),
                        list(dup_existing),
                        existing_interview_ids=dup_existing,
                    )
                    dup_proven = dict(rec.get("sent") or {})
                    dup_unrecorded = dict(rec.get("unrecorded") or {})
                    _missing = set(rec.get("missing") or [])
                    _twins = set(rec.get("duplicate_of") or {})
                    for _cid in dup_existing:
                        if _cid in dup_proven or _cid in dup_unrecorded:
                            continue
                        if _cid in _twins:
                            dup_blocked[_cid] = LAUNCH_BLOCK_DUPLICATE_PERSON_THIS_JOB
                        elif _cid in _missing:
                            dup_blocked[_cid] = LAUNCH_BLOCK_DUPLICATE_OTHER_JOB
                        else:
                            dup_blocked[_cid] = LAUNCH_BLOCK_DUPLICATE_UNVERIFIED
                    logger.warning(
                        "pairbot_duplicate_interviews bulk_id=%s duplicates=%d proven_this_job=%d "
                        "unrecorded=%d blocked=%d",
                        response_data.get("bulk_id", "?"), len(dup_existing), len(dup_proven),
                        len(dup_unrecorded), len(dup_blocked),
                    )
            except Exception as _dup_err:  # noqa: BLE001
                # Mapping failed: unplaced rows fall through as plain failures,
                # as before. Anything reconcile already proved stays proven
                # (it is written 'sent'; the loop must not overwrite it).
                logger.error("pairbot_duplicate_mapping_failed err=%s", _dup_err)
                dup_blocked = {}

        # Save audit log for each candidate
        conn = common._get_db_connection()
        cur = conn.cursor()

        def _write_candidate_engage_status(
            candidate_id: str,
            status_value: str,
            job_id_value: str,
            interview_id_value: str = "",
            response_fragment: Optional[Dict[str, Any]] = None,
            block_reason: Optional[str] = None,
        ) -> None:
            """Write-through status sync for rank-list source of truth.

            Rank-list reads engage_status from sourced_candidates.data, so we
            must update that blob whenever engage send state changes.
            ``block_reason`` also stamps data.engage_block_reason (never
            re-posted by any launch path).
            """
            now_iso = datetime.now(timezone.utc).isoformat()
            cur.execute(
                """
                UPDATE sourced_candidates
                SET data =
                    jsonb_set(
                        jsonb_set(
                            jsonb_set(
                                COALESCE(data, '{}'::jsonb),
                                '{engage_status}',
                                to_jsonb(%s::text),
                                true
                            ),
                            '{engage_updated_at}',
                            to_jsonb(%s::text),
                            true
                        ),
                        '{engage_interview_id}',
                        to_jsonb(%s::text),
                        true
                    ),
                    updated_at = CURRENT_TIMESTAMP
                WHERE candidate_id = %s
                  AND (
                    jobdiva_id = %s
                    OR jobdiva_id = %s
                  )
                """,
                (
                    status_value,
                    now_iso,
                    interview_id_value,
                    candidate_id,
                    str(job_id_value or ""),
                    str(job_id_from_payload or ""),
                ),
            )

            # Preserve last external response snippet for support/debugging.
            if response_fragment is not None:
                cur.execute(
                    """
                    UPDATE sourced_candidates
                    SET data = jsonb_set(
                            COALESCE(data, '{}'::jsonb),
                            '{engage_last_response}',
                            %s::jsonb,
                            true
                        ),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE candidate_id = %s
                      AND (
                        jobdiva_id = %s
                        OR jobdiva_id = %s
                      )
                    """,
                    (
                        json.dumps(response_fragment),
                        candidate_id,
                        str(job_id_value or ""),
                        str(job_id_from_payload or ""),
                    ),
                )

            if block_reason:
                cur.execute(
                    """
                    UPDATE sourced_candidates
                    SET data = jsonb_set(
                            COALESCE(data, '{}'::jsonb),
                            '{engage_block_reason}',
                            to_jsonb(%s::text),
                            true
                        ),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE candidate_id = %s
                      AND (
                        jobdiva_id = %s
                        OR jobdiva_id = %s
                      )
                    """,
                    (
                        block_reason,
                        candidate_id,
                        str(job_id_value or ""),
                        str(job_id_from_payload or ""),
                    ),
                )

        interview_results = []

        if is_success and isinstance(response_data, dict):
            # Extract interview data from response
            data_list = response_data.get("data", [])
            if not isinstance(data_list, list):
                data_list = [response_data] if response_data else []

            # Index Pairbot's creation_completed data[] by source_candidate_id
            # (PAI-155: Pairbot always echoes it). candidate_email is never
            # present in Pairbot response items, so the email index is only
            # populated when Pairbot happens to include it (legacy/fallback).
            interview_by_source_id: Dict[str, Dict[str, Any]] = {}
            interview_by_email: Dict[str, Dict[str, Any]] = {}
            for item in data_list:
                if not isinstance(item, dict):
                    continue
                src = str(item.get("source_candidate_id") or "").strip()
                if src and src not in interview_by_source_id:
                    interview_by_source_id[src] = item
                item_email = str(item.get("candidate_email") or "").lower().strip()
                if item_email and item_email not in interview_by_email:
                    interview_by_email[item_email] = item

            # Log any interviews Pairbot explicitly failed on its side.
            failed_interviews_from_pairbot = response_data.get("failed_interviews") or []
            # A candidate Pairbot named in failed_interviews was rejected (a
            # definite 'failed', retryable). One it is silent about — neither
            # created in data[] nor failed — may well have an interview whose
            # echo did not match: parked 'processing' (in flight for every
            # launch path, never re-posted) until Pairbot's by-job record
            # settles it (settle_parked_legacy_rows / the session's settle).
            pairbot_rejected_ids = _pairbot_rejected_ids(
                failed_interviews_from_pairbot, payload_obj, request.real_candidate_ids,
            )
            if failed_interviews_from_pairbot:
                failed_ids = [item.get("candidate_id") or item.get("source_candidate_id") or "?" for item in failed_interviews_from_pairbot]
                logger.warning(
                    "pairbot_failed_interviews bulk_id=%s count=%d sample=%s",
                    response_data.get("bulk_id", "?"),
                    len(failed_interviews_from_pairbot),
                    failed_ids[:5],
                )

            # Build email lookup from the sent payload resumes (keyed by
            # source_candidate_id == candidate_id).  Used as email fallback
            # when Pairbot response items don't carry source_candidate_id.
            # IMPORTANT: source_candidate_id in generate-payload is always set
            # to the candidate's own candidate_id, so we look up by candidate_id
            # directly — this is position-independent and alignment-safe after
            # idempotency/exclusion filtering.
            payload_resumes = payload_obj.get("resumes") or []
            candidate_id_to_email: Dict[str, str] = {
                str(r.get("source_candidate_id") or "").strip(): str(r.get("email") or "").lower().strip()
                for r in payload_resumes
                if isinstance(r, dict) and r.get("source_candidate_id")
            }

            # ── TRIGGER PROVISIONING (JobDiva Application) ─────────────
            # Background-fire batch provisioning so it doesn't block the
            # HTTP response. The batch function fetches the applicants list
            # ONCE and creates all missing candidates concurrently under
            # _PROVISION_CONCURRENCY, which is far more efficient than the
            # old per-candidate approach (N JobDiva round-trips → 1).
            # A launch session (provision_inline=False) runs it itself, awaited
            # and claim-stamped (provision_sent_candidates). Rows Pairbot held
            # as a duplicate (another job's, or a twin of a person this job
            # already interviews) were not launched here: not provisioned.
            _batch_cids = [c for c in request.real_candidate_ids if c not in dup_blocked]
            if _batch_cids and provision_inline:
                # Durable when ARQ_ENABLED (the worker runs it and retries
                # after a crash); otherwise spawned in-process as before.
                await enqueue(PROVISION_JOBDIVA_BATCH, [str(c) for c in _batch_cids], str(job_id_from_payload))

            for idx, candidate_id in enumerate(request.real_candidate_ids):
                if str(candidate_id) in dup_proven:
                    # Pairbot already has this person's interview for THIS job
                    # (by-job proof); reconcile wrote 'sent' + id + audit row.
                    skipped_already_sent.append(candidate_id)
                    continue
                if str(candidate_id) in dup_unrecorded:
                    # Proven the same way, but reconcile's write failed: record
                    # it here ('sent' + the existing id + an audit row).
                    _existing_iid = dup_unrecorded[str(candidate_id)]
                    _job_resolved = payload_obj.get("jd", {}).get("jobdiva_id") or payload_obj.get("jd", {}).get("job_id", "")
                    _dup_item = dict(dup_items.get(str(candidate_id)) or {}, reconciled_from="pairbot_by_job")
                    cur.execute("""
                        INSERT INTO engage_interview_audit
                            (candidate_id, jobdiva_id, interview_id, payload, response, status)
                        VALUES (%s, %s, %s, %s, %s, %s)
                    """, (
                        candidate_id,
                        _job_resolved,
                        _existing_iid,
                        _audit_payload_json(payload_obj),
                        json.dumps(_dup_item),
                        "Initiated",
                    ))
                    _write_candidate_engage_status(
                        candidate_id=candidate_id,
                        status_value="sent",
                        job_id_value=_job_resolved,
                        interview_id_value=_existing_iid,
                        response_fragment=_dup_item,
                    )
                    skipped_already_sent.append(candidate_id)
                    continue
                if str(candidate_id) in dup_blocked:
                    _reason = dup_blocked[str(candidate_id)]
                    _dup_item = dup_items.get(str(candidate_id)) or {}
                    _job_resolved = payload_obj.get("jd", {}).get("jobdiva_id") or payload_obj.get("jd", {}).get("job_id", "")
                    cur.execute("""
                        INSERT INTO engage_interview_audit
                            (candidate_id, jobdiva_id, interview_id, payload, response, status)
                        VALUES (%s, %s, %s, %s, %s, %s)
                    """, (
                        candidate_id,
                        _job_resolved,
                        "",
                        _audit_payload_json(payload_obj),
                        json.dumps(_dup_item),
                        "duplicate_this_job" if _reason == LAUNCH_BLOCK_DUPLICATE_PERSON_THIS_JOB
                        else "duplicate_other_job",
                    ))
                    _write_candidate_engage_status(
                        candidate_id=candidate_id,
                        status_value="failed",
                        job_id_value=_job_resolved,
                        interview_id_value="",
                        response_fragment=_dup_item,
                        block_reason=_reason,
                    )
                    excluded_records.append({
                        "candidate_id": str(candidate_id),
                        "name": "",
                        "reason": _launch_block_reason_text(_reason),
                    })
                    continue
                # Direct lookup by candidate_id == source_candidate_id (always
                # identical — see generate-payload).  Falls back to email index
                # for legacy Pairbot responses that include candidate_email.
                # NOTE: positional fallback (data_list[idx]) is intentionally
                # absent — Pairbot may omit skipped candidates from data[], so a
                # position-based grab would assign the wrong interview_id.
                submitted_source_id = str(candidate_id)
                submitted_email = candidate_id_to_email.get(str(candidate_id), "")
                interview_info: Dict[str, Any] = {}
                if submitted_source_id in interview_by_source_id:
                    interview_info = interview_by_source_id.pop(submitted_source_id, {}) or {}
                elif submitted_email:
                    interview_info = interview_by_email.pop(submitted_email, {}) or {}

                interview_id = str(interview_info.get("interview_id") or "")
                candidate_name = interview_info.get("candidate_name", "")
                candidate_email = interview_info.get("candidate_email", submitted_email)

                if not interview_id:
                    # Equal lengths + no match = keying broke for the whole batch, not a skip.
                    _log = logger.error if len(data_list) == len(request.real_candidate_ids) else logger.warning
                    _log(
                        "engagement_no_interview_match candidate_id=%s submitted_source_id=%s submitted_email=%s response_data_count=%d request_count=%d",
                        candidate_id,
                        submitted_source_id,
                        submitted_email,
                        len(data_list),
                        len(request.real_candidate_ids),
                    )

                # Extract job_id from payload (prefer reference jobdiva_id for UI consistency)
                job_id_resolved = payload_obj.get("jd", {}).get("jobdiva_id") or payload_obj.get("jd", {}).get("job_id", "")

                unconfirmed = (
                    not interview_id
                    and not request.dry_run
                    and str(candidate_id) not in pairbot_rejected_ids
                )
                if interview_id:
                    audit_status, engage_status = "Initiated", "sent"
                elif unconfirmed:
                    audit_status, engage_status = "unconfirmed", "processing"
                else:
                    audit_status, engage_status = "failed", "failed"

                cur.execute("""
                    INSERT INTO engage_interview_audit
                        (candidate_id, jobdiva_id, interview_id, candidate_name, candidate_email, payload, response, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    candidate_id,
                    job_id_resolved,
                    interview_id,
                    candidate_name,
                    candidate_email,
                    _audit_payload_json(payload_obj),
                    json.dumps(interview_info),
                    audit_status
                ))

                _write_candidate_engage_status(
                    candidate_id=candidate_id,
                    status_value=engage_status,
                    job_id_value=job_id_resolved,
                    interview_id_value=interview_id,
                    response_fragment=interview_info,
                )

                interview_results.append({
                    "candidate_id": candidate_id,
                    "interview_id": interview_id,
                    "candidate_name": candidate_name,
                    "candidate_email": candidate_email,
                    "links": interview_info.get("links", {}),
                    "session_token": interview_info.get("session_token", ""),
                    "created_at": interview_info.get("created_at", ""),
                    # Parked 'processing': Pairbot was silent about this one.
                    **({"unconfirmed": True} if unconfirmed else {}),
                })

            # Any items left in interview_by_source_id were created by Pairbot
            # but couldn't be matched back to a submitted candidate_id.  This
            # can happen if source_candidate_id was not echoed or was mutated.
            if interview_by_source_id:
                logger.warning(
                    "pairbot_unmatched_created_interviews bulk_id=%s count=%d orphaned_ids=%s",
                    response_data.get("bulk_id", "?"),
                    len(interview_by_source_id),
                    list(interview_by_source_id.keys())[:10],
                )
        else:
            # Still log the failed attempt
            for candidate_id in request.real_candidate_ids:
                job_id_resolved = payload_obj.get("jd", {}).get("jobdiva_id") or payload_obj.get("jd", {}).get("job_id", "")
                cur.execute("""
                    INSERT INTO engage_interview_audit
                        (candidate_id, jobdiva_id, payload, response, status)
                    VALUES (%s, %s, %s, %s, %s)
                """, (
                    candidate_id,
                    job_id_resolved,
                    _audit_payload_json(payload_obj),
                    json.dumps(response_data),
                    "failed"
                ))

                _write_candidate_engage_status(
                    candidate_id=candidate_id,
                    status_value="failed",
                    job_id_value=job_id_resolved,
                    interview_id_value="",
                    response_fragment=response_data if isinstance(response_data, dict) else {"response": response_data},
                )

        conn.commit()
        cur.close()
        conn.close()

        # T2 (job's first interview created) for every launch path; the stamp
        # is COALESCE'd in launch_timing, so per-batch calls are idempotent.
        if not request.dry_run and (
            any(r.get("interview_id") for r in interview_results) or dup_unrecorded
        ):
            _t2_job_key = (
                payload_obj.get("jd", {}).get("jobdiva_id")
                or payload_obj.get("jd", {}).get("job_id", "")
                or job_id_from_payload
            )
            try:
                await asyncio.to_thread(pairbot._stamp_first_launch, str(_t2_job_key or ""))
            except Exception as _t2_err:  # noqa: BLE001
                logger.warning("launch T2 stamp failed job=%s: %s", _t2_job_key, _t2_err)

        if is_success:
            # People this call actually created an interview for — not every
            # row it answered for (a 'failed' / parked row is not a launch).
            _created_count = sum(1 for r in interview_results if r.get("interview_id"))
            # ── Applicant sync + launch email (non-blocking) ───────────────
            # Manual rankings Screen sends create the interview only; wizard
            # Launch/re-source flows pass notify_recruiters=True. The
            # first-launch effects (applicant sync, job-posting email) go
            # through the fire-once ledger like every other launch path.
            # (Pre-fix, this gate was `is_initial_launch or dry_run` — but
            # dry_run also skipped the pairbot call, so wizard launches never
            # actually created interviews.)
            if request.is_initial_launch or request.notify_recruiters:
                send_job_posting = (
                    request.send_job_posting_email
                    if request.send_job_posting_email is not None
                    else request.is_initial_launch
                )
                launch.fire_launch_side_effects(
                    job_id_from_payload,
                    sent_count=_created_count,
                    notify_recruiters=bool(request.notify_recruiters),
                    send_job_posting_email=bool(send_job_posting),
                    is_initial_launch=bool(request.is_initial_launch),
                    app_base_url=request.app_base_url,
                    launch_started_at=_core_started_at,
                )
            try:
                from core.newrelic import record_custom_event
                record_custom_event("EngageSendBulkInterview", {
                    "success": True,
                    "bulk_id": response_data.get("bulk_id"),
                    "count": _created_count,
                    "answered_count": len(interview_results),
                    "skipped_count": len(skipped_already_sent),
                })
            except Exception:
                pass
            return {
                "success": True,
                "message": "Interview(s) sent successfully",
                "bulk_id": response_data.get("bulk_id"),
                "data": interview_results,
                "skipped_already_sent": skipped_already_sent,
                "excluded_candidates": excluded_records,
                "employer_unverified": unverified_employer_records,
                "raw_response": response_data
            }
        else:
            try:
                from core.newrelic import record_custom_event
                record_custom_event("EngageSendBulkInterview", {
                    "success": False,
                    "status_code": response.status_code,
                })
            except Exception:
                pass
            return {
                "success": False,
                "message": _extract_pair_error_message(response_data, response.status_code),
                "data": [],
                "skipped_already_sent": skipped_already_sent,
                "excluded_candidates": excluded_records,
                "employer_unverified": unverified_employer_records,
                "raw_response": response_data
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ send-bulk-interview failed: {e}", exc_info=True)
        return {
            "success": False,
            "message": f"Server error: {str(e)}",
            "data": []
        }
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# Imported last: these sibling modules import from this one at load time,
# and the names below are only looked up at call time.
from . import launch  # noqa: E402
