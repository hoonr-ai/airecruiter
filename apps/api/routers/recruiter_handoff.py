"""Recruiter warm handoff routes.

  POST /api/v1/webhooks/pairbot/handoff     signed handoff_* events from Pair-bot
  GET  /api/v1/handoff/{interview_id}       current state, for the join page
  POST /api/v1/handoff/{interview_id}/claim join: returns {livekit_url, token, room}
  POST /api/v1/handoff/{interview_id}/close the recruiter's End interview
  GET  /api/v1/handoff/{interview_id}/context|transcript|recording
                                            copilot reads, proxied as the recruiter
  GET  /api/v1/handoff/{interview_id}/resume the candidate's resume (Pair's own DB)
  GET  /api/v1/handoff/{interview_id}/review after-the-fact view for anyone with
                                            access to the interview's job

The webhook is authenticated exactly like the creation and DNC webhooks
(HMAC SHA-256 of the raw body in X-Hub-Signature-256). The join routes act as
the logged-in recruiter: their email comes from the verified login, never the
request body, and Pair-bot decides whether that recruiter may join.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from core.auth import UserIdentity, get_current_user
from core.webhook_auth import verify_hmac_signature
from services import recruiter_handoff as handoff

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Recruiter handoff"])


def init_recruiter_handoff_schema_sync() -> None:
    handoff.init_recruiter_handoff_schema_sync()


async def init_recruiter_handoff_schema() -> None:
    await asyncio.to_thread(handoff.init_recruiter_handoff_schema_sync)


@router.post("/api/v1/webhooks/pairbot/handoff")
async def receive_handoff_webhook(
    request: Request,
    x_hub_signature_256: Optional[str] = Header(None, alias="X-Hub-Signature-256"),
    x_pairbot_timestamp: Optional[str] = Header(None, alias="X-Pairbot-Timestamp"),
):
    raw_body = await request.body()
    if not verify_hmac_signature(raw_body, x_hub_signature_256, x_pairbot_timestamp):
        logger.warning("Pairbot handoff webhook signature verification failed")
        raise HTTPException(status_code=401, detail="Invalid signature")
    try:
        data = json.loads(raw_body)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    event = str(data.get("event") or "").strip()
    if event not in handoff.EVENTS:
        return {"status": "ignored", "reason": f"event '{event}' not handled"}
    try:
        data["interview_id"] = int(data.get("interview_id"))
    except (TypeError, ValueError):
        # Malformed for good: a 4xx would only spend Pair-bot's retries.
        logger.warning("Pairbot handoff webhook without a usable interview_id event=%s", event)
        return {"status": "ignored", "reason": "interview_id is required"}

    try:
        outcome = await asyncio.to_thread(handoff.apply_event, data)
    except Exception as exc:  # noqa: BLE001
        logger.error("Pairbot handoff webhook failed interview_id=%s event=%s: %s", data["interview_id"], event, exc)
        # 5xx so Pair-bot's outbox retries it.
        raise HTTPException(status_code=500, detail="update failed")
    return {"status": "ok", "duplicate": outcome == "duplicate"}


class CloseBody(BaseModel):
    notes: Optional[str] = Field(None, max_length=5000)


def _email(user: UserIdentity) -> str:
    email = (user.email or "").strip().lower()
    if not email:
        raise HTTPException(status_code=401, detail="Authentication required")
    return email


# Pair-bot statuses that answer the recruiter's own request (contract §4):
# 403 not your launch, 404 no such interview, 409 not waiting, 410 too late.
_PASS_THROUGH = frozenset({403, 404, 409, 410})


async def _proxy(coro):
    try:
        return await coro
    except handoff.PairbotHandoffError as exc:
        if exc.status in _PASS_THROUGH:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
        # Anything else is Pair's problem with Pair-bot, not the recruiter's.
        # A 401 in particular must never reach the browser: the web client
        # reads it as an expired login and signs the recruiter out.
        logger.error("pairbot handoff call failed status=%s detail=%s", exc.status, exc.detail)
        raise HTTPException(status_code=502, detail="PAIR-bot is unavailable") from exc


@router.get("/api/v1/handoff/{interview_id}")
async def get_handoff(interview_id: int, user: UserIdentity = Depends(get_current_user)):
    email = _email(user)
    state = await _proxy(handoff.state(interview_id))
    owner = (state.get("recruiter_email") or "").strip().lower()
    # None when Pair-bot gave no recruiter (no handoff, or an off-contract
    # reply): unknown, so the page lets claim decide rather than refusing.
    is_mine = (owner == email) if owner else None
    if is_mine is True:
        state["is_your_handoff"] = True
        return state
    if is_mine is False:
        # Confirmed someone else's — apps/web/lib/handoff.ts's joinViewFor
        # checks `is_your_handoff === false` BEFORE it ever reads `state`,
        # so the join page has no legitimate use for it here. Omitting it
        # closes the smaller enumeration leak PR #864 review flagged:
        # interview ids are sequential, so even a bare state value let
        # anyone logged in learn which ids currently have a handoff in
        # flight, without needing to own any of them.
        return {"interview_id": interview_id, "is_your_handoff": False}
    # Ownership unknown (no recruiter_email on Pair-bot's reply at all) —
    # unlike the confirmed case above, joinViewFor DOES read `state` here
    # (is_your_handoff is null, not false, so it reaches the state-based
    # switch instead of short-circuiting), e.g. to show "waiting" so the
    # page can offer Claim. Interview ids are sequential, so returning
    # everything else here (as before this PR's own IDOR fix) let any
    # logged-in recruiter enumerate ids and read a stranger's full handoff
    # state: close_reason, close_notes (free text up to 5000 chars),
    # claimed_at, expires_at — only recruiter_email/claimed_by_email were
    # ever stripped (PR #863 review).
    return {
        "interview_id": interview_id,
        "is_your_handoff": None,
        "state": state.get("state"),
    }


@router.post("/api/v1/handoff/{interview_id}/claim")
async def claim_handoff(interview_id: int, user: UserIdentity = Depends(get_current_user)):
    return await _proxy(handoff.claim(interview_id, _email(user)))


@router.post("/api/v1/handoff/{interview_id}/close")
async def close_handoff(interview_id: int, body: CloseBody, user: UserIdentity = Depends(get_current_user)):
    return await _proxy(handoff.close(interview_id, _email(user), body.notes))


async def _require_own_handoff(interview_id: int, email: str) -> None:
    """Pair-side ownership check for data Pair serves itself: only the
    recruiter whose handoff it is. Unknown ownership is refused here (unlike
    the join view), since nothing downstream would decide it."""
    state = await _proxy(handoff.state(interview_id))
    owner = (state.get("recruiter_email") or "").strip().lower()
    if not owner or owner != email:
        raise HTTPException(status_code=403, detail="Only the recruiter who launched this interview can view it.")


@router.get("/api/v1/handoff/{interview_id}/context")
async def get_handoff_context(interview_id: int, user: UserIdentity = Depends(get_current_user)):
    return await _proxy(handoff.context(interview_id, _email(user)))


@router.get("/api/v1/handoff/{interview_id}/transcript")
async def get_handoff_transcript(interview_id: int, user: UserIdentity = Depends(get_current_user)):
    return await _proxy(handoff.transcript(interview_id, _email(user)))


@router.get("/api/v1/handoff/{interview_id}/recording")
async def get_handoff_recording(interview_id: int, user: UserIdentity = Depends(get_current_user)):
    return await _proxy(handoff.recording(interview_id, _email(user)))


@router.get("/api/v1/handoff/{interview_id}/resume")
async def get_handoff_resume(interview_id: int, user: UserIdentity = Depends(get_current_user)):
    email = _email(user)
    await _require_own_handoff(interview_id, email)
    found = await asyncio.to_thread(handoff.resume_for_interview_sync, interview_id)
    if found is None:
        raise HTTPException(status_code=404, detail="No PAIR launch found for this interview.")
    return {"interview_id": interview_id, **found}


# ---------------------------------------------------------------------------
# Post-handoff review (candidate/interview view)
# ---------------------------------------------------------------------------
#
# The copilot routes above act as the logged-in recruiter, and Pair-bot only
# lets the launch recruiter or the claimer read a handoff. Reviewing a past
# handoff is wider than that: any teammate who can see the interview's job can
# see its AI call, so they can see the handoff too. Pair checks that job
# access itself (the same rule as the call-recordings route), then reads from
# Pair-bot as the launch recruiter. Interviews Pair never launched have no
# audit row and are refused, so ids from another tenant are never proxied.

async def _optional_read(coro):
    """A copilot read as (body, failed): (None, False) when there is none
    (404), (None, True) when Pair-bot failed, so the page can say so."""
    try:
        return await coro, False
    except handoff.PairbotHandoffError as exc:
        if exc.status == 404:
            return None, False
        logger.warning("handoff review read failed status=%s detail=%s", exc.status, exc.detail)
        return None, True


def _review_access_sync(interview_id: int, user: UserIdentity) -> None:
    from routers._helpers import verify_job_access_by_id
    from routers.engagement.recordings import recording_audit_row

    row = recording_audit_row(str(int(interview_id)))
    if not row:
        raise HTTPException(status_code=404, detail="Interview not found")
    jobdiva_id = (row.get("jobdiva_id") or "").strip()
    if jobdiva_id:
        verify_job_access_by_id(jobdiva_id, user)
    elif not user.is_admin:
        raise HTTPException(status_code=403, detail="Access denied. You do not have permission to access this interview.")


@router.get("/api/v1/handoff/{interview_id}/review")
async def get_handoff_review(interview_id: int, user: UserIdentity = Depends(get_current_user)):
    _email(user)
    await asyncio.to_thread(_review_access_sync, interview_id, user)
    st = await _proxy(handoff.state(interview_id))
    out = {
        "interview_id": interview_id,
        "state": st.get("state"),
        "recruiter_email": st.get("recruiter_email"),
        "claimed_by_email": st.get("claimed_by_email"),
        "claimed_at": st.get("claimed_at"),
        "closed_at": st.get("closed_at"),
        "close_reason": st.get("close_reason"),
        "close_notes": st.get("close_notes"),
        "transcript": None,
        "recording": None,
        "transcript_error": False,
        "recording_error": False,
    }
    if not out["state"]:
        return out
    acting = (st.get("recruiter_email") or st.get("claimed_by_email") or "").strip().lower()
    if not acting:
        return out
    (tr, out["transcript_error"]), (rec, out["recording_error"]) = await asyncio.gather(
        _optional_read(handoff.transcript(interview_id, acting)),
        _optional_read(handoff.recording(interview_id, acting)),
    )
    if isinstance(tr, dict):
        out["transcript"] = {
            "lines": tr.get("lines") if isinstance(tr.get("lines"), list) else [],
            "suggestions": tr.get("suggestions") if isinstance(tr.get("suggestions"), list) else [],
        }
    if isinstance(rec, dict) and rec.get("url"):
        out["recording"] = {"url": rec["url"], "expires_at": rec.get("expires_at")}
    return out
