"""Pairbot bulk-creation webhook (pipeline optimization Fix 3).

POST /api/v1/webhooks/pairbot/creation
  {"bulk_id": "...", "status": "creation_completed" | "creation_failed",
   "data": [...], "message": "..."}

Authenticated exactly like the DNC webhook: HMAC SHA-256 of the raw body in
X-Hub-Signature-256 with PAIR_WEBHOOK_SECRET (fallback PAIR_API_KEY).
Updates launch_batches; the launch's batch waiter polls that row and still
falls back to Pairbot's SSE stream if the webhook never arrives.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request

from routers.dnc_webhook import _verify_hmac_signature

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/webhooks", tags=["Webhooks"])

_HANDLED = {"creation_completed", "creation_failed"}


def _apply(bulk_id: str, status: str, event: dict, error: Optional[str]) -> int:
    from routers.engagement import _launch_db

    rows = _launch_db(
        """
        UPDATE launch_batches
           SET status = %s, creation_event = %s::jsonb, error = %s, updated_at = now()
         WHERE pairbot_bulk_id = %s
        RETURNING id
        """,
        (status, json.dumps(event), error, bulk_id),
        True,
    )
    return len(rows or [])


@router.post("/pairbot/creation")
async def receive_pairbot_creation_webhook(
    request: Request,
    x_hub_signature_256: Optional[str] = Header(None, alias="X-Hub-Signature-256"),
):
    raw_body = await request.body()
    if not _verify_hmac_signature(raw_body, x_hub_signature_256):
        logger.warning("Pairbot creation webhook signature verification failed")
        raise HTTPException(status_code=401, detail="Invalid signature")
    try:
        data = json.loads(raw_body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    bulk_id = str(data.get("bulk_id") or "").strip()
    status = str(data.get("status") or data.get("event") or "").strip()
    if not bulk_id:
        raise HTTPException(status_code=422, detail="bulk_id is required")
    if status not in _HANDLED:
        return {"status": "ignored", "reason": f"status '{status}' not handled"}

    error = None
    if status == "creation_failed":
        error = str(data.get("message") or f"Pairbot creation_failed for bulk_id={bulk_id}")
    try:
        updated = await asyncio.to_thread(_apply, bulk_id, status, data, error)
    except Exception as exc:  # noqa: BLE001
        logger.error("Pairbot creation webhook DB update failed bulk_id=%s: %s", bulk_id, exc)
        raise HTTPException(status_code=500, detail="update failed")
    if not updated:
        # Not an async-launch batch (legacy flow) — acknowledge idempotently.
        return {"status": "ignored", "reason": "unknown bulk_id"}
    return {"status": "ok", "updated": updated}
