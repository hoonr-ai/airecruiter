"""What the endpoints do (routers/launch_sessions.py only authenticates,
checks access and maps to HTTP). Every state change is ONE conditional
statement on the expected status (store.py) — a 0-row answer is a 409 with
the current status, never a lost update.

Create: INSERT … ON CONFLICT DO NOTHING RETURNING covers both unique indexes
(the per-job idempotency key and one-active-session-per-job). Nothing
inserted → the same key, job and requester → 200 with that session (a
different input hash → 422); another active session for the job → 409
{active_session_id}.

Children (needs-contact, retry-failed): a NEW provided-mode session holding
copies of the parent's rows, created in the same transaction that moves the
parent's rows to handed_off — a finished session is never re-opened, its
timings and its once-only email stay its own. A parent whose launch is over
(S.launch_complete: terminal, or only provisioning JobDiva) may have
children. A child of a parent that launched nobody carries the parent's
click time (clicked_at, plus the wait for the recruiter as human_wait_ms):
it is then the launch that answers the job's click, so the job's "Launch
click → Launch" and "Added → Launch click" stay measured from the real click.

Search attempts: each payload's ``job_id`` must be this job (its id or its
JobDiva ref) — the search stream itself does no access check — and is set
to the job's id when absent.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib
import json
import logging
import uuid
from typing import Any, Dict, List, Optional, Sequence, Tuple

from fastapi import HTTPException
from pydantic import ValidationError

from services.launch_sessions import config as cfg
from services.launch_sessions import events as ev
from services.launch_sessions import rules
from services.launch_sessions import schema as S
from services.launch_sessions.models import (
    AddCandidatesRequest,
    ConfirmReviewRequest,
    CreateLaunchSessionRequest,
    NeedsContactRequest,
    RetryFailedRequest,
)
from services.launch_sessions.store import finite_score, input_hash, jsonable
from services.launch_sessions.views import candidate_view, session_view

logger = logging.getLogger(__name__)


def _deps():
    from services.launch_sessions.runner import get_runner

    runner = get_runner()
    return runner.store, runner.fx, runner


def _email(user: Any) -> str:
    return str(getattr(user, "email", "") or "").strip().lower()


def _upload_records(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for i, raw in enumerate(rows):
        if not isinstance(raw, dict):
            raise HTTPException(status_code=422, detail=f"candidates[{i}] must be an object")
        cid = rules.candidate_row_id(raw)
        if not cid:
            raise HTTPException(status_code=422, detail=f"candidates[{i}] has no candidate_id")
        if cid in out:
            continue
        out[cid] = {
            "candidate_id": cid, "source": raw.get("source"), "row": jsonable(raw),
            "match_score": finite_score(raw.get("match_score")), "state": S.ROW_POOLED,
        }
    return list(out.values())


def _schedule_claim(session_id: str) -> None:
    """Start the session in this worker now when it has room (else a tick)."""
    try:
        from core.tasks import spawn

        _, _, runner = _deps()
        spawn(runner.claim_now(session_id), name=f"launch_session_claim:{session_id}", nr_txn=False)
    except Exception as exc:  # noqa: BLE001 — the ticker claims it within seconds
        logger.warning("launch_session_claim_now_schedule_failed session=%s: %s", session_id, exc)


def _waiting_for_worker(row: Dict[str, Any]) -> bool:
    if row.get("status") == S.STATUS_QUEUED:
        return True
    if row.get("status") != S.STATUS_RUNNING:
        return False
    lease = row.get("lease_until")
    if isinstance(lease, _dt.datetime):
        aware = lease if lease.tzinfo else lease.replace(tzinfo=_dt.timezone.utc)
        return aware <= _dt.datetime.now(_dt.timezone.utc)
    return lease is None


async def _view(store: Any, row: Dict[str, Any], *, tail: bool = True) -> Dict[str, Any]:
    events = await asyncio.to_thread(store.events_tail, row["id"], 20) if tail else None
    children = await asyncio.to_thread(store.children, row["id"]) if tail else None
    position = None
    if tail and _waiting_for_worker(row):
        try:
            position = await asyncio.to_thread(store.queue_position, row["id"])
        except Exception as exc:  # noqa: BLE001 — a display hint only
            logger.debug("launch_session queue position failed: %s", exc)
    return session_view(row, events_tail=events, children=children, queue_position=position)


def _bind_search_payloads(body: CreateLaunchSessionRequest, job: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Each attempt's payload with job_id bound to this job: a payload for
    another job is refused (422) — the search stream runs that job's criteria
    lookups, JobAgent search and cross-submission emails without its own
    access check."""
    keys = {str(job["job_id"])} | ({str(job["jobdiva_id"])} if job.get("jobdiva_id") else set())
    out = []
    for i, attempt in enumerate(body.search_attempts):
        dumped = attempt.model_dump()
        payload = dict(dumped.get("payload") or {})
        given = str(payload.get("job_id") or "").strip()
        if given and given not in keys:
            raise HTTPException(status_code=422, detail={
                "code": "job_mismatch", "message": f"search_attempts[{i}].payload.job_id is not this job",
            })
        payload["job_id"] = given or str(job["job_id"])
        dumped["payload"] = payload
        out.append(dumped)
    return out


# ── create ────────────────────────────────────────────────────────────────

async def create_session(job_key: str, body: CreateLaunchSessionRequest, user: Any) -> Tuple[int, Dict[str, Any]]:
    store, fx, _ = _deps()
    if not cfg.server_launch_enabled():
        raise HTTPException(status_code=503, detail={"code": "disabled", "message": "Server launch is turned off."})
    if body.trigger not in S.TRIGGERS:
        raise HTTPException(status_code=422, detail=f"unknown trigger {body.trigger!r}")
    job = await asyncio.to_thread(store.resolve_job, job_key)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    job_id = job["job_id"]
    ref = job.get("jobdiva_id") or job_id
    mode = body.mode
    rows: List[Dict[str, Any]] = []
    attempts: List[Dict[str, Any]] = []
    start = body.start
    if body.from_session_id:
        mode, start = S.MODE_PROVIDED, True
        rows = await _rows_from_session(store, body.from_session_id, job_id, body.candidate_ids)
    elif mode == S.MODE_FULL_SEARCH:
        if not body.search_attempts:
            raise HTTPException(status_code=422, detail="search_attempts is required for a full search")
        attempts = _bind_search_payloads(body, job)
        for i, attempt in enumerate(attempts):
            try:
                fx.build_search_request(attempt["payload"])
            except (ValidationError, TypeError, ValueError) as exc:
                raise HTTPException(status_code=422, detail=f"search_attempts[{i}].payload is invalid: {exc}")
    else:
        if len(body.candidates) > cfg.upload_chunk_max():
            raise HTTPException(status_code=422, detail=f"at most {cfg.upload_chunk_max()} candidates per request")
        rows = _upload_records(body.candidates)
        if start and not rows:
            raise HTTPException(status_code=422, detail="no candidates to launch")

    # The launch gates, up front (the save repeats them).
    if job.get("outreach_stopped_at"):
        raise HTTPException(status_code=409, detail={
            "code": "outreach_stopped", "message": "Job activity has been stopped. Cannot launch new candidates.",
        })
    await fx.recruiter_email_gate(ref)

    require_review = body.require_review if body.require_review is not None else cfg.require_review_default()
    snapshot = {
        "mode": mode, "trigger": body.trigger,
        "search_attempts": attempts if mode == S.MODE_FULL_SEARCH else [],
        "min_score": body.min_score, "qualified_target": body.qualified_target,
        "launch_count": body.launch_count, "require_review": bool(require_review),
        "options": body.options.model_dump(), "app_base_url": body.app_base_url,
        "from_session_id": body.from_session_id, "skip_enrichment": False,
    }
    digest = input_hash(body.model_dump(exclude={"idempotency_key"}))
    fields = {
        "id": str(uuid.uuid4()), "job_id": job_id, "jobdiva_ref": ref, "requested_by": _email(user),
        "parent_session_id": None, "trigger": body.trigger, "mode": mode,
        "idempotency_key": body.idempotency_key, "input_hash": digest,
        "status": S.STATUS_QUEUED if (mode == S.MODE_FULL_SEARCH or start) else S.STATUS_COLLECTING,
        "stage": S.STAGE_CREATED, "input": snapshot, "checkpoint": {}, "counts": {},
        "job_added_at": job.get("pair_added_at"),
    }
    created = ev.payload("created", mode=mode, trigger=body.trigger, rows=len(rows))
    row = await asyncio.to_thread(store.create_session, fields, rows, created)
    if row is None:
        existing = await asyncio.to_thread(store.find_by_key, job_id, body.idempotency_key)
        if existing is not None:
            if (existing.get("requested_by") or "") != _email(user):
                raise HTTPException(status_code=409, detail={"code": "idempotency_key_in_use"})
            if existing.get("input_hash") != digest:
                raise HTTPException(status_code=422, detail={
                    "code": "idempotency_mismatch",
                    "message": "This idempotency key was used with a different request.",
                })
            return 200, await _view(store, existing)
        active = await asyncio.to_thread(store.active_session, job_id)
        if active is not None:
            raise HTTPException(status_code=409, detail={
                "code": "active_session", "active_session_id": active["id"], "status": active["status"],
            })
        row = await asyncio.to_thread(store.create_session, fields, rows, created)
        if row is None:
            raise HTTPException(status_code=409, detail={"code": "conflict"})
    try:
        await asyncio.to_thread(fx.stamp_launch_clicked, job_id, row.get("clicked_at"))
    except Exception as exc:  # noqa: BLE001 — a timing stamp never blocks a launch
        logger.warning("launch_session click stamp failed job=%s: %s", job_id, exc)
    if row["status"] == S.STATUS_QUEUED:
        _schedule_claim(row["id"])
    return 201, await _view(store, row)


async def _rows_from_session(store: Any, source_id: str, job_id: str, ids: Sequence[str]) -> List[Dict[str, Any]]:
    source = await asyncio.to_thread(store.get_session, source_id)
    if source is None or str(source.get("job_id")) != str(job_id):
        raise HTTPException(status_code=404, detail="from_session_id is not a session of this job")
    if not ids:
        raise HTTPException(status_code=422, detail="candidate_ids is required with from_session_id")
    cands = await asyncio.to_thread(store.candidate_rows, source_id, ids=list(dict.fromkeys(ids)))
    out = []
    for c in cands:
        contact = {k: v for k, v in (c.get("contact") or {}).items() if k != "needs"} or None
        out.append({
            "candidate_id": c["candidate_id"], "ord": c.get("ord"), "source": c.get("source"),
            "row": c.get("row") or {}, "match_score": c.get("match_score"), "state": S.ROW_POOLED,
            "contact": contact,
        })
    if not out:
        raise HTTPException(status_code=422, detail="none of candidate_ids is in that session")
    return out


# ── provided-mode upload / start ──────────────────────────────────────────

async def add_candidates(session: Dict[str, Any], body: AddCandidatesRequest) -> Dict[str, Any]:
    store, _, _ = _deps()
    if len(body.candidates) > cfg.upload_chunk_max():
        raise HTTPException(status_code=422, detail=f"at most {cfg.upload_chunk_max()} candidates per request")
    records = _upload_records(body.candidates)
    res = await asyncio.to_thread(store.add_candidates, session["id"], records, max_total=cfg.max_session_candidates())
    if res.get("too_many"):
        raise HTTPException(status_code=422, detail=f"a session holds at most {cfg.max_session_candidates()} candidates")
    if not res.get("ok"):
        raise HTTPException(status_code=409, detail={"code": "not_collecting", "status": res.get("status")})
    return {"added": len(records), "total": res["total"]}


async def start_session(session: Dict[str, Any]) -> Dict[str, Any]:
    store, _, _ = _deps()
    row, reason = await asyncio.to_thread(store.start_session, session["id"])
    if row is None:
        if reason == "empty":
            raise HTTPException(status_code=422, detail="upload candidates before starting")
        raise HTTPException(status_code=409, detail={"code": "not_collecting", "status": reason})
    await asyncio.to_thread(store.append_event, row["id"], ev.payload("started"))
    _schedule_claim(row["id"])
    return await _view(store, row)


# ── cancel / confirm ──────────────────────────────────────────────────────

async def cancel_session(session: Dict[str, Any], reason: Optional[str]) -> Dict[str, Any]:
    store, _, _ = _deps()
    outcome, row = await asyncio.to_thread(store.cancel_session, session["id"], reason or "cancelled by user")
    if outcome == "not_active":
        raise HTTPException(status_code=409, detail={"code": "not_active", "status": (row or {}).get("status")})
    await asyncio.to_thread(
        store.append_event, row["id"], ev.payload("cancelled" if outcome == "cancelled" else "cancel_requested"),
    )
    if outcome == "cancelled":
        ev.nr_session_event(row, stage=str(row.get("stage")), status=S.STATUS_CANCELLED, outcome="user")
    return await _view(store, row)


def _clean(v: Any) -> Optional[str]:
    s = str(v or "").strip()
    return s or None


async def confirm_review(session: Dict[str, Any], body: ConfirmReviewRequest) -> Dict[str, Any]:
    store, _, _ = _deps()
    ids = list(dict.fromkeys(body.candidate_ids if body.candidate_ids is not None else body.contacts.keys()))
    final = {}
    for cid in ids:
        vals = body.contacts.get(cid)
        final[str(cid)] = {"phone": _clean(vals.phone) if vals else None, "email": _clean(vals.email) if vals else None}
    row = await asyncio.to_thread(store.confirm_review, session["id"], final, {"confirmed": True, "count": len(final)})
    if row is None:
        current = await asyncio.to_thread(store.get_session, session["id"])
        raise HTTPException(status_code=409, detail={"code": "not_awaiting_input", "status": (current or {}).get("status")})
    await asyncio.to_thread(store.append_event, row["id"], ev.payload("confirmed", count=len(final)))
    _schedule_claim(row["id"])
    return await _view(store, row)


# ── children ──────────────────────────────────────────────────────────────

def _child_key(trigger: str, parent_id: str, ids: Sequence[str]) -> str:
    digest = hashlib.sha256(json.dumps(sorted(ids)).encode()).hexdigest()[:16]
    return f"{trigger}:{parent_id}:{digest}"


async def _create_child(
    parent: Dict[str, Any], user: Any, *, trigger: str, rows: List[Dict[str, Any]], skipped: Sequence[str],
    options: Dict[str, Any], key: Optional[str],
) -> Tuple[int, Dict[str, Any]]:
    store, _, _ = _deps()
    ids = [r["candidate_id"] for r in rows]
    key = key or _child_key(trigger, parent["id"], ids)
    pinput = parent.get("input") or {}
    clicked_at, human_wait_ms = _inherited_click(parent)
    snapshot = {
        "mode": S.MODE_PROVIDED, "trigger": trigger, "search_attempts": [], "min_score": None,
        "qualified_target": None, "launch_count": len(rows), "require_review": False,
        "options": options, "app_base_url": pinput.get("app_base_url") or "",
        "parent_session_id": parent["id"], "skip_enrichment": True,
    }
    fields = {
        "id": str(uuid.uuid4()), "job_id": parent["job_id"], "jobdiva_ref": parent.get("jobdiva_ref"),
        "requested_by": _email(user), "parent_session_id": parent["id"], "trigger": trigger,
        "mode": S.MODE_PROVIDED, "idempotency_key": key,
        "input_hash": input_hash({"trigger": trigger, "parent": parent["id"], "ids": sorted(ids)}),
        "status": S.STATUS_QUEUED, "stage": S.STAGE_CREATED, "input": snapshot, "checkpoint": {},
        "counts": {}, "job_added_at": parent.get("job_added_at"),
        "clicked_at": clicked_at, "human_wait_ms": human_wait_ms,
    }
    row = await asyncio.to_thread(
        lambda: store.create_child(
            fields, rows, parent_id=parent["id"], handed_off=ids, skipped=list(skipped),
            event=ev.payload("created", mode=S.MODE_PROVIDED, trigger=trigger, rows=len(rows), parent=parent["id"]),
            parent_event=ev.payload("child_created", trigger=trigger, child=fields["id"], rows=len(rows)),
        )
    )
    if row is None:
        existing = await asyncio.to_thread(store.find_by_key, parent["job_id"], key)
        if existing is not None and existing.get("parent_session_id") == parent["id"]:
            return 200, await _view(store, existing)
        active = await asyncio.to_thread(store.active_session, parent["job_id"])
        raise HTTPException(status_code=409, detail={
            "code": "active_session", "active_session_id": (active or {}).get("id"),
            "status": (active or {}).get("status"),
        })
    _schedule_claim(row["id"])
    return 201, await _view(store, row)


def _inherited_click(parent: Dict[str, Any]) -> Tuple[Any, Optional[int]]:
    """(clicked_at, human_wait_ms) for a child: the parent's click when the
    parent launched nobody (the child then answers that click; the time the
    recruiter spent on the modal counts as human wait), else (None, None) —
    the INSERT defaults (now(), 0)."""
    if parent.get("first_interview_created_at") or not isinstance(parent.get("clicked_at"), _dt.datetime):
        return None, None
    now = _dt.datetime.now(_dt.timezone.utc)
    ended = next((v for v in (parent.get("completed_at"), parent.get("last_batch_done_at"), parent.get("updated_at"))
                  if isinstance(v, _dt.datetime)), now)
    if ended.tzinfo is None:
        ended = ended.replace(tzinfo=_dt.timezone.utc)
    waited = max(0, int((now - ended).total_seconds() * 1000))
    return parent["clicked_at"], int(parent.get("human_wait_ms") or 0) + waited


def _require_finished(parent: Dict[str, Any]) -> None:
    """Children need the parent's launch to be over (terminal, or only
    provisioning JobDiva — S.launch_complete)."""
    if not S.launch_complete(parent.get("status"), parent.get("stage")):
        raise HTTPException(status_code=409, detail={"code": "parent_active", "status": parent.get("status")})


async def _existing_child(parent: Dict[str, Any], key: str) -> Optional[Dict[str, Any]]:
    """A replayed request (same key): the child it already created."""
    store, _, _ = _deps()
    existing = await asyncio.to_thread(store.find_by_key, parent["job_id"], key)
    if existing is not None and existing.get("parent_session_id") == parent["id"]:
        return existing
    return None


async def needs_contact(parent: Dict[str, Any], body: NeedsContactRequest, user: Any) -> Tuple[int, Dict[str, Any]]:
    """Launch the parked needs-contact rows with the recruiter's contacts in
    a child session (recruiter email only); ``skip_ids`` are skipped."""
    store, _, _ = _deps()
    _require_finished(parent)
    wanted = list(dict.fromkeys(body.candidate_ids or list(body.contacts.keys())))
    key = body.idempotency_key or _child_key("needs_contact", parent["id"], wanted)
    if wanted:
        existing = await _existing_child(parent, key)
        if existing is not None:
            return 200, {"session": await _view(store, existing), "skipped": 0}
    parked = {c["candidate_id"]: c for c in await asyncio.to_thread(
        store.candidate_rows, parent["id"], states=[S.ROW_NEEDS_CONTACT])}
    ids = [cid for cid in wanted if cid in parked]
    skip = [cid for cid in dict.fromkeys(body.skip_ids) if cid in parked and cid not in ids]
    if not ids:
        if skip:
            await asyncio.to_thread(
                lambda: store.set_states(
                    None, [{"candidate_id": c, "state": S.ROW_SKIPPED, "reason": "skipped_by_recruiter"} for c in skip],
                    session_id=parent["id"], from_states=[S.ROW_NEEDS_CONTACT],
                )
            )
            return 200, {"session": None, "skipped": len(skip)}
        raise HTTPException(status_code=422, detail="none of candidate_ids is waiting for contact details")
    rows = []
    for cid in ids:
        c = parked[cid]
        contact = {k: v for k, v in (c.get("contact") or {}).items() if k != "needs"}
        vals = body.contacts.get(cid)
        phone, email = (_clean(vals.phone), _clean(vals.email)) if vals else (None, None)
        if isinstance(contact.get("final"), dict):
            # Reviewed contacts are final: the recruiter's answer replaces them.
            contact["final"] = {"phone": phone, "email": email}
        else:
            contact["provided"] = {k: v for k, v in (("phone", phone), ("email", email)) if v}
        rows.append({"candidate_id": cid, "ord": c.get("ord"), "source": c.get("source"), "row": c.get("row") or {},
                     "match_score": c.get("match_score"), "state": S.ROW_POOLED, "contact": contact})
    popts = (parent.get("input") or {}).get("options") or {}
    options = {
        # Recruiter email only (never a second applicant sync / job posting).
        "notify_recruiters": bool(popts.get("notify_recruiters") or popts.get("is_initial_launch")),
        "is_initial_launch": False, "send_job_posting_email": False, "batch_size": popts.get("batch_size"),
    }
    status, view = await _create_child(parent, user, trigger="needs_contact", rows=rows, skipped=skip,
                                       options=options, key=key)
    return status, {"session": view, "skipped": len(skip)}


async def retry_failed(parent: Dict[str, Any], body: RetryFailedRequest, user: Any) -> Tuple[int, Dict[str, Any]]:
    """Relaunch the parent's failed rows in a child session (no side effects)."""
    store, _, _ = _deps()
    _require_finished(parent)
    requested = sorted(set(body.candidate_ids)) if body.candidate_ids is not None else ["*"]
    key = body.idempotency_key or _child_key("retry", parent["id"], requested)
    existing = await _existing_child(parent, key)
    if existing is not None:
        return 200, {"session": await _view(store, existing)}
    failed = await asyncio.to_thread(store.candidate_rows, parent["id"], states=[S.ROW_FAILED])
    if body.candidate_ids is not None:
        wanted = set(body.candidate_ids)
        failed = [c for c in failed if c["candidate_id"] in wanted]
    if not failed:
        raise HTTPException(status_code=422, detail="no failed candidates to retry")
    rows = [{
        "candidate_id": c["candidate_id"], "ord": c.get("ord"), "source": c.get("source"), "row": c.get("row") or {},
        "match_score": c.get("match_score"), "state": S.ROW_POOLED,
        "contact": {k: v for k, v in (c.get("contact") or {}).items() if k != "needs"} or None,
    } for c in failed]
    popts = (parent.get("input") or {}).get("options") or {}
    options = {"notify_recruiters": False, "is_initial_launch": False, "send_job_posting_email": False,
               "batch_size": popts.get("batch_size")}
    status, view = await _create_child(parent, user, trigger="retry", rows=rows, skipped=(), options=options, key=key)
    return status, {"session": view}


# ── reads ─────────────────────────────────────────────────────────────────

async def get_view(session: Dict[str, Any]) -> Dict[str, Any]:
    store, _, _ = _deps()
    return await _view(store, session)


async def list_for_job(job_key: str, *, active: bool, needs_contact: bool, limit: int) -> List[Dict[str, Any]]:
    store, _, _ = _deps()
    job = await asyncio.to_thread(store.resolve_job, job_key)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    rows = await asyncio.to_thread(
        lambda: store.list_sessions(job["job_id"], active_only=active, needs_contact_only=needs_contact, limit=limit)
    )
    return [session_view(r) for r in rows]


async def events(session: Dict[str, Any], after_seq: int, limit: int) -> Dict[str, Any]:
    store, _, _ = _deps()
    rows = await asyncio.to_thread(store.events_after, session["id"], after_seq, limit)
    return jsonable({
        "events": [{"seq": r["seq"], "payload": r["payload"], "created_at": r.get("created_at")} for r in rows],
        "last_seq": rows[-1]["seq"] if rows else int(after_seq),
        "status": session.get("status"),
    })


async def candidates(
    session: Dict[str, Any], *, states: Optional[List[str]], limit: int, offset: int, view: str,
    after_rev: Optional[int], after_id: Optional[str], updated_after: Any,
) -> Dict[str, Any]:
    """Rows for the table / modals. Incremental (``after_rev``): keyset pages
    — call again with ``next`` until ``has_more`` is false, then keep polling
    with the last ``next``."""
    store, _, _ = _deps()
    if view == "failed":
        states = [S.ROW_FAILED]
    rows, total = await asyncio.to_thread(
        lambda: store.candidates_page(
            session["id"], states=states, limit=limit, offset=offset, after_rev=after_rev,
            after_id=after_id, updated_after=updated_after, light=view != "full",
        )
    )
    out: Dict[str, Any] = {
        "rows": [candidate_view(r, view=view) for r in rows],
        "total": total,
        "cand_rev": int(session.get("cand_rev") or 0),
    }
    if after_rev is not None:
        last = rows[-1] if rows else None
        out["next"] = (
            {"after_rev": int(last["rev"]), "after_id": str(last["candidate_id"])} if last
            else {"after_rev": int(after_rev), "after_id": str(after_id or "")}
        )
        out["has_more"] = len(rows) < total
    else:
        out["has_more"] = offset + len(rows) < total
    return out
