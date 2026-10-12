"""Backend-driven, resumable Source & Launch: the launch-session endpoints.

One recruiter click = one durable launch session the server runs to the end
(services/launch_sessions; design + runbook: docs/launch-sessions.md). The
browser only creates it and watches it.

  GET  /api/v1/launch-sessions/config
  POST /api/v1/jobs/{job_id}/launch-sessions
  GET  /api/v1/jobs/{job_id}/launch-sessions?active=&needs_contact=&limit=
  GET  /api/v1/launch-sessions/{id}
  GET  /api/v1/launch-sessions/{id}/events?after_seq=N
  GET  /api/v1/launch-sessions/{id}/candidates?state=&view=&after_rev=&after_id=&ids=
  POST /api/v1/launch-sessions/{id}/candidates      (provided mode, collecting)
  POST /api/v1/launch-sessions/{id}/start
  POST /api/v1/launch-sessions/{id}/confirm         (QA review)
  POST /api/v1/launch-sessions/{id}/needs-contact   (child session)
  POST /api/v1/launch-sessions/{id}/retry-failed    (child session)
  POST /api/v1/launch-sessions/{id}/cancel

Every route requires a signed-in user (``Depends(get_current_user)``); every
job- or session-scoped route checks access (``_job_access`` /
``_session_access``: admin, the session's requester, or a user with access to
its job) — pinned by tests/test_launch_sessions_router.py. Under /api/v1, so
nginx's /api/ passthrough routes them; the upload endpoint has its own
location under pair_batch_limit (nginx-app-locations.conf). Polls (GET session
/ events / candidates) drop their New Relic transaction on a 2xx.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from core.auth import UserIdentity, get_current_user, verify_job_access
from routers._helpers import _get_job_draft_sync
from services.launch_sessions import config as cfg
from services.launch_sessions import control
from services.launch_sessions import schema as S
from services.launch_sessions.models import (
    AddCandidatesRequest,
    CancelRequest,
    ConfirmReviewRequest,
    CreateLaunchSessionRequest,
    NeedsContactRequest,
    RetryFailedRequest,
)

router = APIRouter(prefix="/api/v1", tags=["Launch Sessions"])
logger = logging.getLogger(__name__)

_VIEWS = ("table", "full", "failed")
_MAX_IDS = 50


def _ignore_poll_transaction() -> None:
    try:
        from core.newrelic import ignore_transaction

        ignore_transaction()
    except Exception:  # noqa: BLE001 — telemetry never breaks a request
        pass


def _check_job_access_sync(job_ref: str, user: UserIdentity) -> None:
    """403 on a real denial, 404 for an unknown job; a failed lookup RAISES
    (the caller answers 503 — a DB blip must read as "try again", never as a
    final denial; routers/job_step_time has the same rule)."""
    if user.is_admin:
        return
    draft = _get_job_draft_sync(job_ref)  # raises on DB errors
    if draft.get("status") == "success" and draft.get("data"):
        verify_job_access(draft["data"], user)
        return
    if draft.get("status") == "error" and "No data found" in str(draft.get("message", "")):
        raise HTTPException(status_code=404, detail="Job not found.")
    raise HTTPException(status_code=403, detail="Access denied. You do not have permission to access this job.")


def _activity(user: UserIdentity, action: str, job_ref: Any, session: Any, **attrs: Any) -> None:
    """The recruiter's action, for the activity log (services/recruiter_activity).
    Fire-and-forget; never affects the response."""
    try:
        from services.recruiter_activity import record_action_soon

        sid = session.get("id") if isinstance(session, dict) else None
        record_action_soon(user.email, action, job_key=job_ref, session_id=sid, attrs=attrs)
    except Exception as exc:  # noqa: BLE001
        logger.debug("launch_sessions: activity %s not recorded: %s", action, exc)


async def _job_access(job_ref: str, user: UserIdentity) -> None:
    ref = (job_ref or "").strip()
    if not ref:
        raise HTTPException(status_code=404, detail="Job not found.")
    try:
        await asyncio.to_thread(_check_job_access_sync, ref, user)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("launch_sessions: job access check failed job=%s: %s", ref, exc)
        raise HTTPException(status_code=503, detail="Could not verify access to this job. Please try again.")


async def _session_access(session_id: str, user: UserIdentity) -> Dict[str, Any]:
    """The session row, when this user may see / act on it."""
    try:
        sid = str(uuid.UUID(str(session_id)))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(status_code=404, detail="Launch session not found.")
    from services.launch_sessions.runner import get_runner

    try:
        row = await asyncio.to_thread(get_runner().store.get_session, sid)
    except Exception as exc:  # noqa: BLE001
        logger.warning("launch_sessions: lookup failed session=%s: %s", sid, exc)
        raise HTTPException(status_code=503, detail="Could not load the launch session. Please try again.")
    if row is None:
        raise HTTPException(status_code=404, detail="Launch session not found.")
    if user.is_admin:
        return row
    if (row.get("requested_by") or "") and row["requested_by"] == (user.email or "").strip().lower():
        return row
    await _job_access(str(row["job_id"]), user)
    return row


# ── config ─────────────────────────────────────────────────────────────────

@router.get("/launch-sessions/config")
async def launch_sessions_config(user: UserIdentity = Depends(get_current_user)):
    """Whether the browser should START new launches as server sessions, and
    how to poll. ``enabled`` is false when sessions are off OR the runner is
    off (nothing would pick a new session up); the browser still reattaches
    to and watches sessions that already exist either way."""
    _ignore_poll_transaction()
    return {
        "enabled": cfg.creation_enabled(),
        "poll_ms": cfg.poll_ms(),
        "hidden_poll_ms": cfg.hidden_poll_ms(),
        "max_upload_chunk": cfg.upload_chunk_max(),
        "max_candidates": cfg.max_session_candidates(),
        "require_review_default": cfg.require_review_default(),
    }


# ── job-scoped ─────────────────────────────────────────────────────────────

@router.post("/jobs/{job_id}/launch-sessions")
async def create_launch_session(
    job_id: str,
    body: CreateLaunchSessionRequest,
    user: UserIdentity = Depends(get_current_user),
):
    """201 {session} (new), 200 {session} (same idempotency key replayed),
    409 {detail: {code: active_session, active_session_id}} / {code:
    outreach_stopped}, 422 (invalid / key reused with another body /
    {code: job_mismatch}: a search payload's job_id is another job),
    400/404/503 from the recruiter-email gate."""
    await _job_access(job_id, user)
    status, view = await control.create_session(job_id, body, user)
    if status == 201:
        _activity(user, "launch_session_created", job_id, view, mode=body.mode, trigger=body.trigger)
    return JSONResponse(status_code=status, content={"session": view})


@router.get("/jobs/{job_id}/launch-sessions")
async def list_job_launch_sessions(
    job_id: str,
    active: bool = Query(False),
    needs_contact: bool = Query(False),
    limit: int = Query(5, ge=1, le=50),
    user: UserIdentity = Depends(get_current_user),
):
    """Reattach on page load: ``active=true`` → the job's active session (if
    any); ``needs_contact=true`` → finished sessions with parked rows."""
    await _job_access(job_id, user)
    sessions = await control.list_for_job(job_id, active=active, needs_contact=needs_contact, limit=limit)
    _ignore_poll_transaction()
    return {"sessions": sessions}


# ── session-scoped ─────────────────────────────────────────────────────────

@router.get("/launch-sessions/{session_id}")
async def get_launch_session(session_id: str, user: UserIdentity = Depends(get_current_user)):
    """The pollable session view (status, stage, counts, timings, events_tail)."""
    row = await _session_access(session_id, user)
    view = await control.get_view(row)
    _ignore_poll_transaction()
    return {"session": view}


@router.get("/launch-sessions/{session_id}/events")
async def get_launch_session_events(
    session_id: str,
    after_seq: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=500),
    user: UserIdentity = Depends(get_current_user),
):
    row = await _session_access(session_id, user)
    out = await control.events(row, after_seq, limit)
    _ignore_poll_transaction()
    return out


@router.get("/launch-sessions/{session_id}/candidates")
async def get_launch_session_candidates(
    session_id: str,
    state: Optional[str] = Query(None, description="comma-separated row states"),
    view: str = Query("table"),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    after_rev: Optional[int] = Query(None, ge=0),
    after_id: Optional[str] = Query(None),
    updated_after: Optional[str] = Query(None),
    ids: Optional[str] = Query(None, description=f"comma-separated candidate ids (at most {_MAX_IDS})"),
    user: UserIdentity = Depends(get_current_user),
):
    """Rows for the table / modals.

    Polling the table: the keyset cursor — ``after_rev=0``, then follow
    ``next`` while ``has_more``, then keep polling from the last ``next``
    (revs are committed in order, so it never misses a write). Do NOT add a
    ``state`` filter to a cursor poll: a row that LEAVES the filtered states
    is never returned again. ``state`` is for one-shot reads (the review /
    needs-contact modals, ``view=failed``). ``updated_after`` is deprecated
    (best-effort: it compares the rows' transaction-start timestamps).
    ``ids`` reads given rows by candidate id (the résumé viewer's
    ``view=full&ids=<id>&limit=1``: a row's keyset position is not needed).
    """
    row = await _session_access(session_id, user)
    if view not in _VIEWS:
        raise HTTPException(status_code=422, detail=f"view must be one of {', '.join(_VIEWS)}")
    id_list: Optional[List[str]] = None
    if ids is not None:
        id_list = list(dict.fromkeys(i.strip() for i in ids.split(",") if i.strip())) or None
        if id_list is not None and len(id_list) > _MAX_IDS:
            raise HTTPException(status_code=422, detail=f"ids takes at most {_MAX_IDS} candidate ids")
    states: Optional[List[str]] = None
    if state:
        states = [s.strip() for s in state.split(",") if s.strip()]
        unknown = [s for s in states if s not in S.ROW_STATES]
        if unknown:
            raise HTTPException(status_code=422, detail=f"unknown state(s): {', '.join(unknown)}")
    after_ts = None
    if updated_after:
        try:
            after_ts = _dt.datetime.fromisoformat(updated_after.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=422, detail="updated_after must be an ISO timestamp")
    out = await control.candidates(
        row, states=states, limit=limit, offset=offset, view=view, after_rev=after_rev,
        after_id=after_id, updated_after=after_ts, ids=id_list,
    )
    _ignore_poll_transaction()
    return out


@router.post("/launch-sessions/{session_id}/candidates")
async def add_launch_session_candidates(
    session_id: str,
    body: AddCandidatesRequest,
    user: UserIdentity = Depends(get_current_user),
):
    """Provided mode: append ≤ 50 rows while the session is collecting."""
    row = await _session_access(session_id, user)
    return await control.add_candidates(row, body)


@router.post("/launch-sessions/{session_id}/start")
async def start_launch_session(session_id: str, user: UserIdentity = Depends(get_current_user)):
    row = await _session_access(session_id, user)
    return {"session": await control.start_session(row)}


@router.post("/launch-sessions/{session_id}/confirm")
async def confirm_launch_session(
    session_id: str,
    body: ConfirmReviewRequest,
    user: UserIdentity = Depends(get_current_user),
):
    row = await _session_access(session_id, user)
    view = await control.confirm_review(row, body)
    _activity(user, "review_confirmed", row.get("job_id"), view, contacts_edited=len(body.contacts or {}))
    return {"session": view}


@router.post("/launch-sessions/{session_id}/needs-contact")
async def launch_session_needs_contact(
    session_id: str,
    body: NeedsContactRequest,
    user: UserIdentity = Depends(get_current_user),
):
    row = await _session_access(session_id, user)
    status, out = await control.needs_contact(row, body, user)
    if status < 300:
        _activity(user, "needs_contact_answered", row.get("job_id"), out.get("session") or row,
                  contacts_given=len(body.contacts or {}), skipped=len(body.skip_ids or []))
    return JSONResponse(status_code=status, content=out)


@router.post("/launch-sessions/{session_id}/retry-failed")
async def launch_session_retry_failed(
    session_id: str,
    body: Optional[RetryFailedRequest] = Body(default=None),
    user: UserIdentity = Depends(get_current_user),
):
    row = await _session_access(session_id, user)
    status, out = await control.retry_failed(row, body or RetryFailedRequest(), user)
    if status < 300:
        _activity(user, "retry_failed", row.get("job_id"), out.get("session") or row)
    return JSONResponse(status_code=status, content=out)


@router.post("/launch-sessions/{session_id}/cancel")
async def cancel_launch_session(
    session_id: str,
    body: Optional[CancelRequest] = Body(default=None),
    user: UserIdentity = Depends(get_current_user),
):
    row = await _session_access(session_id, user)
    view = await control.cancel_session(row, body.reason if body else None)
    _activity(user, "launch_cancelled", row.get("job_id"), view)
    return {"session": view}
