"""Engagement package: pairbot."""

import asyncio
from typing import Optional, List, Any, Dict, Tuple, Sequence
import psycopg2.extras
import json
import httpx
from datetime import datetime, timezone, timedelta
from services.pair_auth import get_pair_auth_headers
from core.pipeline_trace import span as _trace_span
from core.email import (
    notify_pair_launched,
    notify_job_posting,
)
from services.notifications_service import create_pair_launched_notification

from .common import EXTERNAL_INTERVIEW_API_URL, _parse_json_list, logger
from . import common

# ---------------------------------------------------------------------------
# Helper: fire PAIR launch notifications (background task)
# Fires Email #1 (launch confirmation) + Email #2 (job posting request)
# from a single DB query so we don't hit monitored_jobs twice.
# ---------------------------------------------------------------------------
async def _send_pair_launch_email(
    *, job_id: str, candidate_count: int, send_job_posting: bool = True, app_base_url: str = "",
    relaunch: bool = False,
) -> None:
    """
    Fetches job metadata from monitored_jobs and fires launch emails.

    Email #1 (PAIR launch confirmation to recruiters) always fires.
    Email #2 (job posting team request) is gated on `send_job_posting` —
    callers pass False on re-launches so we don't re-spam the posting team
    every time a recruiter sources another batch of candidates.
    ``relaunch`` (the job's first interview predates this launch) words Email
    #1 as "more candidates launched" instead of a new campaign.

    Runs inside asyncio.create_task() so failures are fully isolated.
    """
    if not job_id:
        return
    try:
        conn = common._get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT job_id, jobdiva_id, title, enhanced_title, customer_name,
                   city, state, location_type, bot_introduction,
                   jobdiva_description, ai_description, recruiter_notes,
                   selected_job_boards, recruiter_emails
            FROM monitored_jobs
            WHERE job_id = %s OR jobdiva_id = %s
            ORDER BY (job_id ~ '^[0-9]+$') DESC, created_at DESC
            LIMIT 1
        """, (job_id, job_id))
        row = cur.fetchone()
        cur.close()
        conn.close()

        if not row:
            logger.warning("📧 _send_pair_launch_email: job '%s' not found in monitored_jobs", job_id)
            return

        recruiter_emails: list = _parse_json_list(row.get("recruiter_emails", []))
        job_boards: list       = _parse_json_list(row.get("selected_job_boards", []))

        jobdiva_id    = str(row.get("jobdiva_id") or "")
        job_title      = row.get("enhanced_title") or row.get("title", "")
        customer_name  = row.get("customer_name", "Unknown")
        location       = f"{row.get('city', 'TBD')}, {row.get('state', '')}"
        db_job_id     = str(row.get("job_id") or job_id)
        clean_emails  = [str(e) for e in recruiter_emails if e]
        ai_desc       = row.get("ai_description") or ""

        # ── Email #1: PAIR Launch Confirmation ───────────────────────────────
        # Isolated in its own try: a failure here must not also skip the
        # in-app notification below — they're independent channels, and the
        # in-app one no longer needs the email to have succeeded first (dedup
        # is now a DB-level partial index, not call-order).
        try:
            await asyncio.to_thread(
                notify_pair_launched,
                jobdiva_id=jobdiva_id,
                job_title=job_title,
                customer_name=customer_name,
                candidate_count=candidate_count,
                recruiter_emails=clean_emails,
                job_id=db_job_id,
                app_base_url=app_base_url,
                relaunch=relaunch,
            )
        except Exception as email_err:
            logger.warning(
                "📧 PAIR launch email failed for job %s (in-app notification still proceeds): %s",
                db_job_id, email_err, exc_info=True,
            )

        # In-app companion to Email #1 — same recipients, same trigger point,
        # job-level (see services/notifications_service.py).
        await create_pair_launched_notification(
            job_id=db_job_id,
            jobdiva_id=jobdiva_id,
            job_title=job_title,
            candidate_count=candidate_count,
            recruiter_emails=clean_emails,
        )

        # ── Email #2: Job Posting Request (skipped on re-launch or no boards) ──
        if send_job_posting and job_boards:
            await asyncio.to_thread(
                notify_job_posting,
                jobdiva_id=jobdiva_id,
                job_title=job_title,
                recruiter_emails=clean_emails,
                job_boards=job_boards,
                ai_description=ai_desc,
                job_id=db_job_id,
                app_base_url=app_base_url,
            )
        else:
            logger.info(
                "📧 Skipping job-posting email for job %s (not requested or no job boards selected)",
                jobdiva_id or job_id,
            )

    except Exception as exc:
        logger.warning("📧 _send_pair_launch_email failed silently: %s", exc, exc_info=True)


async def _post_to_pairbot(
    url: str,
    payload_obj: Dict[str, Any],
    *,
    max_attempts: int = 3,
    base_backoff_seconds: float = 2.0,
    timeout_seconds: float = 60.0,
) -> httpx.Response:
    """POST to pairbot with retries on network error / 5xx.

    PAI-155: Pairbot bulk-create now returns 202 quickly; do NOT retry on
    TimeoutException (a timed-out create may still be running server-side,
    and retrying would duplicate work / orphan bulk_jds).
    """
    last_exc: Optional[BaseException] = None
    last_response: Optional[httpx.Response] = None
    headers = get_pair_auth_headers(json_content_type=True)
    for attempt in range(max_attempts):
        try:
            async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                response = await client.post(
                    url,
                    json=payload_obj,
                    headers=headers,
                )
            if response.status_code < 500:
                return response
            last_response = response
            logger.warning(
                "pairbot_5xx attempt=%d/%d status=%d",
                attempt + 1, max_attempts, response.status_code,
            )
        except httpx.TimeoutException as exc:
            # Do not retry timeouts — Pairbot may still be creating.
            logger.warning(
                "pairbot_timeout attempt=%d/%d msg=%s (no retry)",
                attempt + 1, max_attempts, str(exc),
            )
            raise
        except (httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            last_exc = exc
            logger.warning(
                "pairbot_transport_error attempt=%d/%d type=%s msg=%s",
                attempt + 1, max_attempts, type(exc).__name__, str(exc),
            )
        if attempt < max_attempts - 1:
            await asyncio.sleep(base_backoff_seconds * (2 ** attempt))

    if last_response is not None:
        return last_response
    raise last_exc if last_exc is not None else RuntimeError("pairbot call failed")


def _stamp_engage_status_batch(
    candidate_ids: List[str],
    status_value: str,
    *,
    job_id_value: str = "",
    job_id_alt: str = "",
    bulk_id: Optional[str] = None,
) -> None:
    """Batch-write engage_status for many candidates in one connection.

    With ``bulk_id`` the Pairbot bulk the rows were posted in is written as
    data.engage_bulk_id in the SAME statement, so a row can never read
    'processing' without saying which bulk to reconcile it against."""
    if not candidate_ids:
        return
    conn = common._get_db_connection()
    try:
        cur = conn.cursor()
        if bulk_id:
            cur.execute(
                """
                UPDATE sourced_candidates
                SET data = jsonb_set(
                        jsonb_set(
                            COALESCE(data, '{}'::jsonb),
                            '{engage_status}',
                            to_jsonb(%s::text),
                            true
                        ),
                        '{engage_bulk_id}',
                        to_jsonb(%s::text),
                        true
                    ),
                    updated_at = CURRENT_TIMESTAMP
                WHERE candidate_id = ANY(%s)
                  AND (jobdiva_id = %s OR jobdiva_id = %s)
                """,
                (
                    status_value,
                    str(bulk_id),
                    list(candidate_ids),
                    str(job_id_value or ""),
                    str(job_id_alt or ""),
                ),
            )
        else:
            cur.execute(
                """
                UPDATE sourced_candidates
                SET data = jsonb_set(
                        COALESCE(data, '{}'::jsonb),
                        '{engage_status}',
                        to_jsonb(%s::text),
                        true
                    ),
                    updated_at = CURRENT_TIMESTAMP
                WHERE candidate_id = ANY(%s)
                  AND (jobdiva_id = %s OR jobdiva_id = %s)
                """,
                (
                    status_value,
                    list(candidate_ids),
                    str(job_id_value or ""),
                    str(job_id_alt or ""),
                ),
            )
        conn.commit()
        cur.close()
    finally:
        conn.close()


async def _wait_for_pairbot_creation(
    bulk_id: str,
    *,
    timeout_seconds: float = 600.0,
) -> Dict[str, Any]:
    """PAI-155: wait on Pairbot SSE until interviews are created.

    Pairbot returns 202 + bulk_id immediately; interview rows arrive later via
    status=creation_completed (with data[]). Outreach progress uses the same
    stream but Curate only needs creation_completed here.

    Uses a connect timeout plus a background pump into a queue. Waiting on
    the queue (not aiter.__anext__) keeps the SSE generator alive across idle
    gaps so a quiet-but-alive stream is not closed before timeout_seconds.
    """
    stream_url = f"{EXTERNAL_INTERVIEW_API_URL}/api/bulk-interviews/{bulk_id}/stream"
    headers: Dict[str, str] = get_pair_auth_headers()

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    # read=None: Pairbot may emit nothing until creation_completed; the overall
    # deadline is enforced by wait_for on the queue, not the httpx read timeout.
    client_timeout = httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0)
    async with httpx.AsyncClient(timeout=client_timeout) as client:
        async with client.stream("GET", stream_url, headers=headers) as response:
            if response.status_code >= 400:
                body = (await response.aread())[:300]
                raise RuntimeError(
                    f"Pairbot stream failed status={response.status_code} body={body!r}"
                )
            chunks: "asyncio.Queue[Optional[str]]" = asyncio.Queue()
            pump_error: List[Exception] = []

            async def _pump_chunks() -> None:
                try:
                    async for chunk in response.aiter_text():
                        await chunks.put(chunk)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    pump_error.append(exc)
                finally:
                    await chunks.put(None)

            pump = asyncio.create_task(_pump_chunks())
            buffer = ""
            try:
                while True:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        raise TimeoutError(
                            f"Timed out waiting for Pairbot creation_completed bulk_id={bulk_id}"
                        )
                    try:
                        chunk = await asyncio.wait_for(
                            chunks.get(),
                            timeout=min(60.0, remaining),
                        )
                    except asyncio.TimeoutError:
                        # No bytes yet; generator is still alive on the pump.
                        continue
                    if chunk is None:
                        break
                    # SSE allows LF or CRLF frame terminators.
                    buffer += chunk.replace("\r\n", "\n").replace("\r", "\n")
                    while "\n\n" in buffer:
                        raw_event, buffer = buffer.split("\n\n", 1)
                        data_lines = [
                            line[5:].strip()
                            for line in raw_event.split("\n")
                            if line.startswith("data:")
                        ]
                        if not data_lines:
                            continue
                        try:
                            event = json.loads("".join(data_lines))
                        except Exception:
                            logger.warning(
                                "pairbot_stream_parse_error bulk_id=%s raw=%r",
                                bulk_id,
                                data_lines,
                            )
                            continue
                        status = str(event.get("status") or "")
                        if status == "creation_completed":
                            return event if isinstance(event, dict) else {"status": status}
                        if status == "creation_failed":
                            raise RuntimeError(
                                event.get("message")
                                or f"Pairbot creation_failed for bulk_id={bulk_id}"
                            )
                        # Ignore accepted/creating/processing/completed (outreach).
            finally:
                if not pump.done():
                    pump.cancel()
                    try:
                        await pump
                    except (asyncio.CancelledError, Exception):
                        pass
            if pump_error:
                raise pump_error[0]
    raise TimeoutError(f"Pairbot stream ended without creation_completed bulk_id={bulk_id}")


async def _fetch_pairbot_job_interviews(job_key: str) -> Dict[str, Any]:
    """Pairbot's job-scoped record: ``GET /api/interviews/by-job/{job_key}``.

    Returns its ``data`` block — ``{bulk_in_progress, in_progress_details,
    interviews: [{interview_id, source_candidate_id, jobdiva_id, status, …}]}``
    (newest interview first). Pairbot matches the key against both the
    jobdiva ref and the job id of the bulk / interview. Raises on a non-200
    or a transport error: a failed check must never read as "nothing exists".
    """
    headers = get_pair_auth_headers()
    url = f"{EXTERNAL_INTERVIEW_API_URL}/api/interviews/by-job/{job_key}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, headers=headers)
    if resp.status_code != 200:
        raise RuntimeError(f"Pairbot by-job check failed status={resp.status_code}")
    body = resp.json()
    data = body.get("data") if isinstance(body, dict) else None
    return data if isinstance(data, dict) else {}


def _pairbot_interview_index(
    payload: Dict[str, Any],
) -> Tuple[Dict[str, str], set, Dict[str, str]]:
    """(source_candidate_id -> newest interview id, every listed
    source_candidate_id, every listed interview id -> the source_candidate_id
    it is listed under ('' when none)) from a by-job payload."""
    by_source: Dict[str, str] = {}
    listed_sources: set = set()
    listed_interviews: Dict[str, str] = {}
    interviews = payload.get("interviews") if isinstance(payload, dict) else None
    for iv in interviews if isinstance(interviews, list) else []:
        if not isinstance(iv, dict):
            continue
        src = str(iv.get("source_candidate_id") or "").strip()
        iid = str(iv.get("interview_id") or "").strip()
        if iid:
            listed_interviews.setdefault(iid, src)
        if not src:
            continue
        listed_sources.add(src)
        # Pairbot lists newest first; the first id seen per candidate wins.
        if iid and src not in by_source:
            by_source[src] = iid
    return by_source, listed_sources, listed_interviews


def _job_key_list(job_keys: Any) -> List[str]:
    """Distinct non-empty job keys, order kept. A bare string is one key."""
    if isinstance(job_keys, str):
        job_keys = (job_keys,)
    out: List[str] = []
    for key in job_keys or ():
        k = str(key or "").strip()
        if k and k not in out:
            out.append(k)
    return out


def _stamp_first_launch(job_key: str) -> None:
    """T2 (first interview created) on monitored_jobs via
    services.launch_timing.stamp_first_launch — COALESCE there, so calling it
    for every batch that created an interview is idempotent. Fail-open, and a
    no-op until that module exists."""
    if not job_key:
        return
    try:
        from services.launch_timing import stamp_first_launch
    except ImportError:
        return
    try:
        stamp_first_launch(str(job_key))
    except Exception as exc:  # noqa: BLE001 — timing must never fail a launch
        logger.warning("launch_timing.stamp_first_launch failed job=%s: %s", job_key, exc)


def _record_reconciled_interviews(job_keys: Sequence[str], found: Dict[str, str]) -> List[str]:
    """Write interviews Pairbot's by-job record proves exist for THIS job.

    ``job_keys`` are every spelling sourced_candidates.jobdiva_id may hold for
    the job — ``(job_id, jobdiva_ref)``; the audit row goes under the LAST
    non-empty key (the ref, as the launch core writes it). Per candidate:
    engage_status 'sent' + engage_interview_id (+ engage_updated_at, and any
    engage_block_reason dropped) on rows that have no interview id yet and are
    still in a launch state (''/failed/queued/processing/sending) — a row the
    voice webhook already moved on (in_progress, passed, an interview Fail) is
    never pulled back to 'sent'. The audit row is inserted only when none
    exists for (candidate_id, job, interview_id); a transaction-scoped
    advisory lock on (candidate_id, interview_id) makes that check-then-insert
    exclusive, so two concurrent reconciles cannot both insert. Candidates are
    written in id order (no lock-order deadlock between two reconciles). One
    transaction: it raises and commits nothing on a DB error. Returns the
    candidate ids for which anything was written.
    """
    keys = _job_key_list(job_keys)
    if not keys or not found:
        return []
    audit_job = keys[-1]
    now_iso = datetime.now(timezone.utc).isoformat()
    written: List[str] = []
    conn = common._get_db_connection()
    try:
        cur = conn.cursor()
        for cid, interview_id in sorted(found.items(), key=lambda kv: str(kv[0])):
            cid, interview_id = str(cid), str(interview_id or "").strip()
            if not cid or not interview_id:
                continue
            cur.execute(
                """
                UPDATE sourced_candidates
                SET data = (COALESCE(data, '{}'::jsonb) - 'engage_block_reason')
                           || jsonb_build_object(
                                'engage_status', 'sent',
                                'engage_interview_id', %s::text,
                                'engage_updated_at', %s::text
                           ),
                    updated_at = CURRENT_TIMESTAMP
                WHERE candidate_id = %s
                  AND jobdiva_id = ANY(%s)
                  AND COALESCE(data->>'engage_interview_id', '') = ''
                  AND LOWER(COALESCE(data->>'engage_status', ''))
                      IN ('', 'failed', 'queued', 'processing', 'sending')
                RETURNING candidate_id
                """,
                (interview_id, now_iso, cid, keys),
            )
            updated = bool(cur.fetchall())
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"engage_audit:{cid}:{interview_id}",),
            )
            cur.execute(
                """
                INSERT INTO engage_interview_audit
                    (candidate_id, jobdiva_id, interview_id, response, status)
                SELECT %s, %s, %s, %s::jsonb, 'Initiated'
                WHERE NOT EXISTS (
                    SELECT 1 FROM engage_interview_audit
                    WHERE candidate_id = %s
                      AND jobdiva_id = ANY(%s)
                      AND interview_id = %s
                )
                RETURNING id
                """,
                (
                    cid, audit_job, interview_id,
                    json.dumps({"interview_id": interview_id, "reconciled_from": "pairbot_by_job"}),
                    cid, keys, interview_id,
                ),
            )
            inserted = bool(cur.fetchall())
            if updated or inserted:
                written.append(cid)
        conn.commit()
        cur.close()
    finally:
        conn.close()
    if written:
        logger.info(
            "pairbot_reconcile_recorded job=%s candidates=%d", audit_job, len(written),
        )
        _stamp_first_launch(audit_job)
    return written


async def _confirm_failed_candidates_with_pairbot(
    bulk_id: str,
    job_id: str,
    candidate_ids: List[str],
    *,
    job_keys: Optional[Sequence[str]] = None,
) -> List[str]:
    """After `_wait_for_pairbot_creation` errors out (timeout, dropped SSE
    stream, or any other wait failure), check PairBot's own record before
    concluding a candidate's interview was never created.

    PairBot already accepted the bulk with a 202; our stream dying or our
    600s wait expiring does NOT mean creation failed — PairBot may still be
    working, or may have finished right after we stopped listening. This is
    the only check before stamping `failed`.

    Candidates PairBot DOES list with an interview id are written through as
    `sent` + interview id + audit row (`_record_reconciled_interviews`,
    fail-open) — they used to be discarded and stayed 'processing' forever.
    ``job_keys`` are the job spellings to write under (default ``(job_id,)``).

    Returns only the candidate_ids PairBot itself confirms have no interview,
    and only once PairBot reports it is done processing the bulk. Everything
    else — including a failed check itself — comes back empty, so a real
    success already in progress is never overwritten to `failed`.
    """
    if not job_id or not candidate_ids:
        return []
    try:
        payload = await _fetch_pairbot_job_interviews(job_id)
    except Exception as exc:
        logger.error(
            "pairbot_status_check_after_wait_error_failed bulk_id=%s err=%s", bulk_id, exc
        )
        return []
    by_source, found_ids, _ = _pairbot_interview_index(payload)
    found = {str(c): by_source[str(c)] for c in candidate_ids if by_source.get(str(c))}
    if found:
        try:
            await asyncio.to_thread(_record_reconciled_interviews, job_keys or (job_id,), found)
        except Exception as exc:  # noqa: BLE001 — rows stay 'processing'; reconcile later
            logger.error(
                "pairbot_confirm_found_write_failed bulk_id=%s candidates=%d err=%s",
                bulk_id, len(found), exc,
            )
    if payload.get("bulk_in_progress"):
        # Still creating — nothing is a confirmed failure yet.
        return []
    return [cid for cid in candidate_ids if str(cid) not in found_ids]


async def _fetch_pairbot_bulk_status(bulk_id: str, *, timeout_seconds: float = 15.0) -> str:
    """The durable create status of one Pairbot bulk: the ``status`` of the
    FIRST event of ``GET /api/bulk-interviews/{bulk_id}/stream`` — 'creating'
    while its interviews are still being created, 'creation_completed' /
    'creation_failed' once done (Pairbot keeps that 24 h). A bulk Pairbot has
    no create record for (never accepted, or expired) answers 'completed'
    (the legacy outreach snapshot). Raises on a non-2xx, a transport error or
    no event within the timeout."""
    stream_url = f"{EXTERNAL_INTERVIEW_API_URL}/api/bulk-interviews/{bulk_id}/stream"
    headers: Dict[str, str] = get_pair_auth_headers()

    async def _first_event() -> str:
        client_timeout = httpx.Timeout(connect=10.0, read=timeout_seconds, write=10.0, pool=10.0)
        async with httpx.AsyncClient(timeout=client_timeout) as client:
            async with client.stream("GET", stream_url, headers=headers) as response:
                if response.status_code >= 400:
                    raise RuntimeError(f"Pairbot bulk stream failed status={response.status_code}")
                buffer = ""
                async for chunk in response.aiter_text():
                    buffer += chunk.replace("\r\n", "\n").replace("\r", "\n")
                    while "\n\n" in buffer:
                        raw_event, buffer = buffer.split("\n\n", 1)
                        data_lines = [
                            line[5:].strip() for line in raw_event.split("\n") if line.startswith("data:")
                        ]
                        if not data_lines:
                            continue
                        event = json.loads("".join(data_lines))
                        if isinstance(event, dict):
                            return str(event.get("status") or "")
        raise RuntimeError(f"Pairbot bulk stream ended without an event bulk_id={bulk_id}")

    return await asyncio.wait_for(_first_event(), timeout=timeout_seconds + 5.0)


async def reconcile_with_pairbot(
    job_keys: Sequence[str],
    candidate_ids: Sequence[str],
    *,
    session_id: Optional[str] = None,
    existing_interview_ids: Optional[Dict[str, str]] = None,
    own_bulk_ids: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Settle candidates against Pairbot's job-scoped record (by-job).

    One by-job read per distinct job key (``(job_id, jobdiva_ref)``; Pairbot
    matches either). A candidate counts as launched for THIS job only when
    by-job lists an interview under its OWN source_candidate_id; that is
    written through (`_record_reconciled_interviews`: 'sent' + interview id +
    an audit row via INSERT … WHERE NOT EXISTS).

    ``existing_interview_ids`` ({cid: interview id} from a DuplicateInterview
    answer): when by-job lists that interview for this job under ANOTHER
    source id, the candidate is a second PAIR id (a twin) for a person this
    job already interviews. It goes to ``duplicate_of`` and is never written
    as sent — one interview must not count as two launched candidates.

    Returns::

        {"sent": {cid: interview_id},        # proven AND recorded (committed)
         "unrecorded": {cid: interview_id},  # proven; the DB write failed
         "duplicate_of": {cid: {"interview_id", "source_candidate_id"}},
         "missing": [cid],                   # safe to treat as never created
         "bulk_in_progress": bool,           # Pairbot is creating SOME bulk —
         "in_progress_bulk_id": str|None,    #   its check is not job-scoped
         "own_bulks_creating": [bulk_id],    # of own_bulk_ids; a failed read counts
         "error": str|None}

    ``missing`` (by-job answered and lists no interview for them) is filled
    only when re-posting them is safe: every by-job read succeeded AND no
    bulk is being created — or, given ``own_bulk_ids`` (the caller's bulks
    for these candidates), none of those is still creating (each one's
    durable status) and by-job's in-progress bulk is not one of them.
    Pairbot dedupes only within one bulk, so a re-POST while our own bulk is
    still creating calls the person twice. A bulk Pairbot has not registered
    yet (its POST still in flight) does not read as creating: never pass a
    bulk posted moments ago.

    Fail-safe: when any by-job read fails, ``error`` is set and ``missing`` is
    empty — the caller must not re-POST anything on an error. A failed DB
    write sets ``error`` and reports the proven ids under ``unrecorded``.
    """
    result: Dict[str, Any] = {
        "sent": {}, "unrecorded": {}, "duplicate_of": {}, "missing": [],
        "bulk_in_progress": False, "in_progress_bulk_id": None,
        "own_bulks_creating": [], "error": None,
    }
    keys = _job_key_list(job_keys)
    ids = [str(c) for c in dict.fromkeys(candidate_ids or []) if str(c).strip()]
    if not ids:
        return result
    if not keys:
        result["error"] = "no job key to reconcile against"
        return result
    own = [str(b).strip() for b in dict.fromkeys(own_bulk_ids or []) if str(b or "").strip()]

    by_source: Dict[str, str] = {}
    listed_sources: set = set()
    listed_interviews: Dict[str, str] = {}
    fetch_errors: List[str] = []
    async with _trace_span("pairbot_reconcile", n=len(ids)):
        for key in keys:
            try:
                payload = await _fetch_pairbot_job_interviews(key)
            except Exception as exc:  # noqa: BLE001
                fetch_errors.append(f"by-job {type(exc).__name__}: {str(exc)[:160]}")
                continue
            b, srcs, ivs = _pairbot_interview_index(payload)
            for src, iid in b.items():
                by_source.setdefault(src, iid)
            listed_sources |= srcs
            for iid, src in ivs.items():
                listed_interviews.setdefault(iid, src)
            if payload.get("bulk_in_progress"):
                result["bulk_in_progress"] = True
                details = payload.get("in_progress_details")
                if isinstance(details, dict) and details.get("bulk_id") and not result["in_progress_bulk_id"]:
                    result["in_progress_bulk_id"] = str(details["bulk_id"])
        if result["bulk_in_progress"] and own and not fetch_errors:
            for bulk_id in own:
                try:
                    status = await _fetch_pairbot_bulk_status(bulk_id)
                except Exception as exc:  # noqa: BLE001 — unknown reads as still creating
                    logger.warning("pairbot_bulk_status_failed bulk_id=%s err=%s", bulk_id, exc)
                    status = "creating"
                if status == "creating":
                    result["own_bulks_creating"].append(bulk_id)

    sent: Dict[str, str] = {cid: by_source[cid] for cid in ids if by_source.get(cid)}
    duplicate_of: Dict[str, Dict[str, str]] = {}
    for cid, existing in (existing_interview_ids or {}).items():
        cid, existing = str(cid), str(existing or "").strip()
        if cid in ids and cid not in sent and existing and existing in listed_interviews:
            # Listed for this job, and not under cid (cid would be in `sent`).
            duplicate_of[cid] = {
                "interview_id": existing,
                "source_candidate_id": listed_interviews[existing],
            }
    result["duplicate_of"] = duplicate_of

    errors = list(fetch_errors)
    if sent:
        try:
            await asyncio.to_thread(_record_reconciled_interviews, keys, sent)
            result["sent"] = sent
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "pairbot_reconcile_write_failed session=%s candidates=%d err=%s",
                session_id, len(sent), exc,
            )
            errors.append(f"write {type(exc).__name__}: {str(exc)[:160]}")
            result["unrecorded"] = sent
    missing_is_safe = not fetch_errors and (
        not result["bulk_in_progress"]
        or (
            bool(own)
            and not result["own_bulks_creating"]
            and result["in_progress_bulk_id"] not in own
        )
    )
    if missing_is_safe:
        result["missing"] = [
            c for c in ids if c not in sent and c not in duplicate_of and c not in listed_sources
        ]
    if errors:
        result["error"] = "; ".join(errors)
    logger.info(
        "pairbot_reconcile session=%s job=%s asked=%d sent=%d unrecorded=%d twins=%d missing=%d "
        "in_progress=%s own_creating=%d error=%s",
        session_id, keys[-1], len(ids), len(result["sent"]), len(result["unrecorded"]),
        len(duplicate_of), len(result["missing"]), result["bulk_in_progress"],
        len(result["own_bulks_creating"]), bool(result["error"]),
    )
    return result


# --- Settling parked legacy rows -----------------------------------------------
# A launch outside a session can leave a row 'processing' with no recorded
# outcome: Pairbot's 2xx was silent about it (the core parks it instead of
# calling it failed), or the creation wait ended unconfirmed. 'processing' is
# in flight for every launch path — never re-posted — so nothing would ever
# settle it. This sweep (launch-session housekeeping, one worker per tick)
# asks Pairbot's by-job record about rows parked > _LEGACY_SETTLE_AFTER_S:
#   listed under the row's own id   → 'sent' + audit row (reconcile writes it),
#                                     then provisioned to JobDiva
#   listed under another PAIR id    → failed + blocked duplicate_person_this_job
#   missing (and nothing creating)  → still parked; after _LEGACY_SETTLE_GIVE_UP_S
#                                     → 'failed' (retryable): its echo may be
#                                     broken, so one 'missing' is not proof
#   by-job unreadable               → still parked, checked again later
# A row 'sending' > 30 min with no session (the process died mid-POST) is
# settled the same way. Each row is checked at most every
# _LEGACY_SETTLE_EVERY_S across the fleet (conditional claim).
_LEGACY_SETTLE_AFTER_S = 600
_LEGACY_SENDING_AFTER_S = 1800
_LEGACY_SETTLE_EVERY_S = 600
_LEGACY_SETTLE_GIVE_UP_S = 24 * 3600

_LEGACY_PARKED_CLAIM_SQL = """
    WITH due AS (
        SELECT id FROM sourced_candidates
        WHERE COALESCE(data->>'engage_session_id', '') = ''
          AND COALESCE(data->>'engage_interview_id', '') = ''
          AND (
                (LOWER(COALESCE(data->>'engage_status', '')) = 'processing'
                 AND updated_at < now() - make_interval(secs => %(after)s))
             OR (LOWER(COALESCE(data->>'engage_status', '')) = 'sending'
                 AND updated_at < now() - make_interval(secs => %(sending_after)s))
          )
          AND COALESCE(data->>'engage_settle_checked_at', '') < %(checked_before)s
        ORDER BY id
        LIMIT %(limit)s
        FOR UPDATE SKIP LOCKED
    )
    UPDATE sourced_candidates sc
    SET data = COALESCE(sc.data, '{}'::jsonb) || jsonb_build_object('engage_settle_checked_at', %(now)s::text)
    FROM due
    WHERE sc.id = due.id
    RETURNING sc.jobdiva_id, sc.candidate_id, COALESCE(sc.data->>'engage_updated_at', ''), sc.updated_at
"""

_LEGACY_SETTLE_FAIL_SQL = """
    UPDATE sourced_candidates
    SET data = COALESCE(data, '{}'::jsonb) || jsonb_build_object(
            'engage_status', 'failed',
            'engage_updated_at', %(now)s::text,
            'engage_last_response', jsonb_build_object('settled', %(why)s::text))
        || CASE WHEN %(block)s <> '' THEN jsonb_build_object('engage_block_reason', %(block)s::text)
                ELSE '{}'::jsonb END,
        updated_at = CURRENT_TIMESTAMP
    WHERE jobdiva_id = ANY(%(keys)s)
      AND candidate_id = ANY(%(ids)s)
      AND LOWER(COALESCE(data->>'engage_status', '')) IN ('processing', 'sending')
      AND COALESCE(data->>'engage_session_id', '') = ''
      AND COALESCE(data->>'engage_interview_id', '') = ''
    RETURNING candidate_id
"""


def _claim_parked_legacy_rows(limit: int) -> List[Tuple[str, str, str, Any]]:
    now = datetime.now(timezone.utc)
    params = {
        "after": _LEGACY_SETTLE_AFTER_S,
        "sending_after": _LEGACY_SENDING_AFTER_S,
        "checked_before": (now - timedelta(seconds=_LEGACY_SETTLE_EVERY_S)).isoformat(),
        "limit": int(limit),
        "now": now.isoformat(),
    }
    conn = common._get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(_LEGACY_PARKED_CLAIM_SQL, params)
        rows = [(str(r[0] or ""), str(r[1] or ""), str(r[2] or ""), r[3]) for r in cur.fetchall() or []]
        conn.commit()
        cur.close()
    finally:
        conn.close()
    return rows


def _legacy_job_keys(job_key: str) -> List[str]:
    conn = common._get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT job_id::text, jobdiva_id FROM monitored_jobs WHERE job_id::text = %s OR jobdiva_id = %s LIMIT 1",
            (job_key, job_key),
        )
        row = cur.fetchone()
        cur.close()
    finally:
        conn.close()
    return _job_key_list((job_key,) + (tuple(row) if row else ()))


def _fail_parked_legacy_rows(keys: Sequence[str], ids: Sequence[str], why: str, block: str = "") -> List[str]:
    if not ids:
        return []
    conn = common._get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(_LEGACY_SETTLE_FAIL_SQL, {
            "keys": list(keys), "ids": list(ids), "why": why, "block": block,
            "now": datetime.now(timezone.utc).isoformat(),
        })
        out = [str(r[0]) for r in cur.fetchall() or []]
        conn.commit()
        cur.close()
    finally:
        conn.close()
    return out


def _parked_age_s(engage_updated_at: str, updated_at: Any) -> Optional[float]:
    ts: Optional[datetime] = None
    try:
        ts = datetime.fromisoformat(engage_updated_at) if engage_updated_at else None
    except ValueError:
        ts = None
    if ts is None and isinstance(updated_at, datetime):
        ts = updated_at
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds()


async def settle_parked_legacy_rows(limit: int = 200) -> Dict[str, int]:
    """One sweep (see above). Never raises: a failure leaves rows parked."""
    out = {"checked": 0, "sent": 0, "blocked": 0, "failed": 0, "still_parked": 0}
    try:
        rows = await asyncio.to_thread(_claim_parked_legacy_rows, limit)
    except Exception as exc:  # noqa: BLE001
        logger.warning("legacy_settle claim failed: %s", exc)
        return out
    by_job: Dict[str, List[Tuple[str, str, Any]]] = {}
    for job_key, cid, eua, ua in rows:
        if job_key and cid:
            by_job.setdefault(job_key, []).append((cid, eua, ua))
    for job_key, items in by_job.items():
        ids = list(dict.fromkeys(c for c, _, _ in items))
        out["checked"] += len(ids)
        try:
            keys = await asyncio.to_thread(_legacy_job_keys, job_key)
            res = await reconcile_with_pairbot(keys, ids)
            sent = [str(c) for c in (res.get("sent") or {})]
            out["sent"] += len(sent)
            twins = [str(c) for c in (res.get("duplicate_of") or {})]
            if twins:
                out["blocked"] += len(await asyncio.to_thread(
                    _fail_parked_legacy_rows, keys, twins, "listed_under_another_id",
                    claims.LAUNCH_BLOCK_DUPLICATE_PERSON_THIS_JOB,
                ))
            age = {c: _parked_age_s(eua, ua) for c, eua, ua in items}
            give_up = [
                str(c) for c in res.get("missing") or []
                if (age.get(str(c)) or 0) >= _LEGACY_SETTLE_GIVE_UP_S
            ]
            if give_up:
                out["failed"] += len(await asyncio.to_thread(
                    _fail_parked_legacy_rows, keys, give_up, "not_created_at_pairbot",
                ))
            settled = set(sent) | set(twins) | set(give_up) | {str(c) for c in (res.get("unrecorded") or {})}
            out["still_parked"] += len([c for c in ids if c not in settled])
            if sent and common.jobdiva_provisioning_enabled():
                try:
                    await provisioning.provision_sent_candidates(keys[-1], sent)
                except Exception as exc:  # noqa: BLE001 — the rows stay launched
                    logger.warning("legacy_settle provisioning failed job=%s: %s", job_key, exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("legacy_settle failed job=%s candidates=%d: %s", job_key, len(ids), exc)
    if out["checked"]:
        logger.info("legacy_settle %s", " ".join(f"{k}={v}" for k, v in out.items()))
    return out


# Imported last: these sibling modules import from this one at load time,
# and the names below are only looked up at call time.
from . import claims  # noqa: E402
from . import provisioning  # noqa: E402
