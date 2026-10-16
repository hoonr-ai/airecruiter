"""Recruiter warm handoff routes (Pair-bot docs/recruiter-handoff-pair-contract.md).

  POST /api/v1/webhooks/pairbot/handoff     signed handoff_* events from Pair-bot
  GET  /api/v1/handoff/{interview_id}       current state, for the join page
  POST /api/v1/handoff/{interview_id}/claim join: returns {livekit_url, token, room}
  POST /api/v1/handoff/{interview_id}/close the recruiter's End interview

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
from routers.dnc_webhook import _verify_hmac_signature
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
):
    raw_body = await request.body()
    if not _verify_hmac_signature(raw_body, x_hub_signature_256):
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
        raise HTTPException(status_code=422, detail="interview_id is required")

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


async def _proxy(coro):
    try:
        return await coro
    except handoff.PairbotHandoffError as exc:
        # Pair-bot's status is the answer (403 not your launch, 409 not
        # waiting, 410 too late); the join page shows it as is.
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc


@router.get("/api/v1/handoff/{interview_id}")
async def get_handoff(interview_id: int, user: UserIdentity = Depends(get_current_user)):
    state = await _proxy(handoff.state(interview_id))
    state["is_your_handoff"] = (state.get("recruiter_email") or "").lower() == _email(user)
    return state


@router.post("/api/v1/handoff/{interview_id}/claim")
async def claim_handoff(interview_id: int, user: UserIdentity = Depends(get_current_user)):
    return await _proxy(handoff.claim(interview_id, _email(user)))


@router.post("/api/v1/handoff/{interview_id}/close")
async def close_handoff(interview_id: int, body: CloseBody, user: UserIdentity = Depends(get_current_user)):
    return await _proxy(handoff.close(interview_id, _email(user), body.notes))
