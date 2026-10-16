"""HMAC verification for webhooks Pair-bot signs with the shared secret.

Pair-bot sends HMAC-SHA256 of the raw body as `X-Hub-Signature-256:
sha256=<hex>` (DNC, evaluation and recruiter handoff webhooks alike).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


def get_webhook_secret() -> str:
    """Retrieve secret used to verify webhook HMAC SHA-256 signatures."""
    secret = (
        os.getenv("PAIR_WEBHOOK_SECRET", "")
        or os.getenv("PAIR_API_KEY", "")
    ).strip()
    return secret


def verify_hmac_signature(raw_body: bytes, signature_header: Optional[str]) -> bool:
    """Verify X-Hub-Signature-256: sha256=<hex_digest>."""
    secret = get_webhook_secret()
    env = os.getenv("ENVIRONMENT", "dev").lower()

    # Enforce HMAC unconditionally across production, staging, and QA
    if env in {"production", "prod", "staging", "qa"}:
        if not secret:
            logger.error("Webhook rejected: neither PAIR_WEBHOOK_SECRET nor PAIR_API_KEY is configured in %s environment", env)
            return False
        if not signature_header or not signature_header.startswith("sha256="):
            logger.warning("Webhook rejected: Missing or invalid X-Hub-Signature-256 in %s environment", env)
            return False
    elif not secret:
        # In local development without secret configured, log warning and allow
        logger.warning("Webhook accepted without secret in local %s environment", env)
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
