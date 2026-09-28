"""Apollo phone-reveal webhook: POST /api/webhooks/apollo/phone?token=...

The one route in this app that is deliberately NOT behind get_current_user:
Apollo calls it and cannot carry a PAIR login. The capability is the token in
the URL instead. It is random (256 bits), created per request by
services/apollo_phone.request_phone, stored only as a SHA-256 hash, and dies
once the request is settled. A delivery must also match the Apollo person the
request was for. See services/apollo_phone.py for the flow.

Replies: 200 for a known request (including duplicates and "no phone", so Apollo
stops retrying), 404 for an unknown token, 410 for an expired request, 413/400
for oversized/malformed bodies.
"""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, Request

from services import apollo_phone

logger = logging.getLogger(__name__)

router = APIRouter(tags=["webhooks"])

MAX_BODY_BYTES = 512 * 1024


# Mounted with prefix="/api" (main.py) == apollo_phone.WEBHOOK_PATH.
@router.post("/webhooks/apollo/phone")
async def apollo_phone_webhook(request: Request, token: str = ""):
    token = (token or "").strip()
    if len(token) < 20:
        raise HTTPException(status_code=404, detail="unknown request")
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")
    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid JSON")

    try:
        row = await asyncio.to_thread(apollo_phone.get_request_sync, apollo_phone.hash_token(token))
    except Exception as e:
        # Our DB is down: 503 so Apollo retries later; the poll backup also covers it.
        logger.warning("apollo webhook: lookup failed: %s", e)
        raise HTTPException(status_code=503, detail="temporarily unavailable")
    if not row:
        logger.warning("apollo webhook: unknown token")
        raise HTTPException(status_code=404, detail="unknown request")
    if row.get("status") != "pending":
        return {"ok": True, "status": "duplicate"}
    if float(row.get("age_s") or 0) > apollo_phone.REQUEST_TTL_S:
        raise HTTPException(status_code=410, detail="request expired")

    outcome = await apollo_phone.deliver(row, payload, via="webhook")
    return {"ok": True, "status": outcome["status"]}
