"""DNC (Do Not Contact) Webhook endpoint.

Receives candidate suppression and opt-out notifications from PAIR (or external systems)
as specified in DNC_WEBHOOK_INTEGRATION_GUIDE.md.

Validates the HMAC SHA-256 signature in X-Hub-Signature-256 and persists suppression
locally into dnc_list, sourced_candidates.dnc_stopped_at, and outreach_opt_out_audit,
ensuring PAIR's database is not repeatedly hit or queried.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from core.config import DATABASE_URL, SUPABASE_DB_URL
from core.db import get_db_connection
from services.dnc_storage import invalidate_dnc_cache, suppress_contact_locally
from utils.phone import normalize_phone
from utils.pii import mask_email, mask_phone

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/webhooks", tags=["Webhooks"])


def _get_webhook_secret() -> str:
    """Retrieve secret used to verify webhook HMAC SHA-256 signatures."""
    secret = (
        os.getenv("PAIR_WEBHOOK_SECRET", "")
        or os.getenv("PAIR_API_KEY", "")
    ).strip()
    return secret


def _verify_hmac_signature(raw_body: bytes, signature_header: Optional[str]) -> bool:
    """Verify X-Hub-Signature-256: sha256=<hex_digest>."""
    secret = _get_webhook_secret()
    env = os.getenv("ENVIRONMENT", "dev").lower()

    # Enforce HMAC unconditionally across production, staging, and QA
    if env in {"production", "prod", "staging", "qa"}:
        if not secret:
            logger.error("DNC webhook rejected: neither PAIR_WEBHOOK_SECRET nor PAIR_API_KEY is configured in %s environment", env)
            return False
        if not signature_header or not signature_header.startswith("sha256="):
            logger.warning("DNC webhook rejected: Missing or invalid X-Hub-Signature-256 in %s environment", env)
            return False
    elif not secret:
        # In local development without secret configured, log warning and allow
        logger.warning("DNC webhook accepted without secret in local %s environment", env)
        return True

    if not signature_header or not signature_header.startswith("sha256="):
        return False

    expected_digest = signature_header.split("sha256=", 1)[1].strip()
    calculated = hmac.new(
        secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(calculated, expected_digest)


class CandidateInfo(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None


class SuppressionInfo(BaseModel):
    is_suppressed: bool = True
    blocked_channels: List[str] = Field(default_factory=list)
    scope: Optional[str] = None
    trigger: Optional[str] = None
    reasons: Dict[str, Any] = Field(default_factory=dict)


class DncWebhookPayload(BaseModel):
    event: str
    interview_id: Optional[int] = None
    candidate: Optional[CandidateInfo] = None
    source: Optional[str] = None
    udf: Optional[str] = None
    suppression: Optional[SuppressionInfo] = None
    message: Optional[str] = None
    timestamp: Optional[str] = None


@router.post("/dnc")
async def receive_dnc_webhook(
    request: Request,
    x_hub_signature_256: Optional[str] = Header(None, alias="X-Hub-Signature-256"),
):
    """Receive real-time DNC / candidate suppression event and persist locally."""
    raw_body = await request.body()

    if not _verify_hmac_signature(raw_body, x_hub_signature_256):
        logger.warning("DNC webhook signature verification failed")
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        data = json.loads(raw_body)
    except Exception as e:
        logger.warning(f"DNC webhook invalid JSON: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    event_name = data.get("event")
    if event_name != "candidate.outreach_suppressed":
        # Acknowledge non-suppression events idempotently
        return {"status": "ignored", "reason": f"Event '{event_name}' not handled"}

    candidate_data = data.get("candidate") or {}
    suppression_data = data.get("suppression") or {}

    email = (candidate_data.get("email") or "").strip() or None
    phone = (candidate_data.get("phone") or "").strip() or None
    name = candidate_data.get("name")
    interview_id = data.get("interview_id")

    # Guard against payloads missing all candidate identifiers to prevent table-wide scans
    if not email and not phone and not interview_id:
        logger.warning("DNC webhook rejected: missing all candidate identifiers (email, phone, interview_id)")
        raise HTTPException(
            status_code=422,
            detail="Missing candidate identifiers: at least one of email, phone, or interview_id is required",
        )
    trigger = suppression_data.get("trigger") or "webhook"
    blocked_channels = suppression_data.get("blocked_channels") or ["email", "sms", "call"]
    reasons = suppression_data.get("reasons") or {}
    ts = data.get("timestamp")

    reason_text = (
        f"Trigger: {trigger}; Reasons: {json.dumps(reasons)}"
        if reasons
        else f"Trigger: {trigger}"
    )

    logger.info(
        f"DNC webhook received: interview_id={interview_id} "
        f"email={mask_email(email) if email else None} "
        f"phone={mask_phone(phone) if phone else None} trigger={trigger}"
    )

    # 1. Suppress contact locally via dnc_storage (handles dnc_list upsert and audit)
    suppression_res = suppress_contact_locally(
        phone=phone,
        email=email,
        reason=reason_text,
        created_by=f"webhook:{trigger}",
    )

    # 2. Update sourced_candidates JSONB data blob with structured DNC status
    # and mark dnc_stopped_at directly if matched by email, phone, or interview_id
    try:
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                phone_digits = "".join(c for c in (phone or "") if c.isdigit())
                phone_digits_10 = phone_digits[-10:] if len(phone_digits) >= 10 else phone_digits

                dnc_blob = json.dumps({
                    "is_dnc": True,
                    "trigger": trigger,
                    "blocked_channels": blocked_channels,
                    "reasons": reasons,
                    "timestamp": ts,
                    "message": data.get("message") or "Candidate is on DNC / Opt-Out suppression list",
                })

                sql = """
                    UPDATE sourced_candidates
                    SET dnc_stopped_at = COALESCE(dnc_stopped_at, CURRENT_TIMESTAMP),
                        data = jsonb_set(
                            COALESCE(data, '{}'::jsonb),
                            '{dnc}',
                            %s::jsonb
                        )
                    WHERE (
                        (%s IS NOT NULL AND BTRIM(LOWER(email)) = BTRIM(LOWER(%s)))
                        OR (%s != '' AND RIGHT(REGEXP_REPLACE(COALESCE(phone, ''), '\\D', '', 'g'), 10) = %s)
                        OR (%s IS NOT NULL AND (
                            data->>'engage_interview_id' = %s
                            OR candidate_id IN (
                                SELECT candidate_id FROM engage_interview_audit
                                WHERE interview_id = %s
                            )
                        ))
                    )
                """
                iid_str = str(interview_id) if interview_id is not None else None
                cur.execute(
                    sql,
                    (
                        dnc_blob,
                        email,
                        email,
                        phone_digits_10,
                        phone_digits_10,
                        iid_str,
                        iid_str,
                        iid_str,
                    ),
                )
                affected = cur.rowcount
                logger.info(f"DNC webhook updated {affected} sourced_candidates rows with DNC metadata")
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"Error persisting DNC metadata to sourced_candidates: {e}")

    # Invalidate in-memory DNC cache so UI queries reflect updated suppression immediately
    invalidate_dnc_cache()

    return {
        "status": "success",
        "interview_id": interview_id,
        "suppression": suppression_res,
        "persisted_locally": True,
    }
