"""HMAC verification for webhooks Pair-bot signs with the shared secret.

Pair-bot sends HMAC-SHA256 of the raw body as `X-Hub-Signature-256:
sha256=<hex>` (DNC, evaluation and recruiter handoff webhooks alike).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Environment values that may run with no webhook secret configured at all
# (a bare "warn and allow unsigned" convenience for a developer's own
# machine). Every other value — including one that's unset, misspelled, or
# unrecognized ("Production ", "live", ...) — now REQUIRES a configured
# secret and a valid signature (PR review: the old code only enforced HMAC
# for an exact allowlist match and silently fell back to "allow unsigned" for
# anything else, which fails OPEN exactly when a deployed host's ENVIRONMENT
# is missing or wrong — the one case a security check most needs to hold).
_UNSIGNED_OK_ENVS = frozenset({"local", "dev", "development"})

# How old a signed request's timestamp may be before it's treated as a
# replay. Only enforced when the caller actually sends a timestamp (see
# verify_hmac_signature's docstring) — this constant exists so the window is
# defined in one place once Pair-bot starts sending one.
MAX_WEBHOOK_AGE_SECONDS = 300


def get_webhook_secret(env: Optional[str] = None) -> str:
    """Retrieve the secret used to verify webhook HMAC-SHA256 signatures.

    PAIR_API_KEY is accepted as a fallback when PAIR_WEBHOOK_SECRET isn't
    set — EXCEPT in prod, where it's refused (PR review: PAIR_API_KEY is
    also our OUTBOUND credential to Pair-bot; using it as the INBOUND
    webhook's HMAC key too couples two trust domains that should rotate
    independently — rotating the outbound key would silently rotate, or
    break, inbound verification too). staging/qa/dev may still fall back to
    it for now; tighten further once every host has its own dedicated
    secret configured.
    """
    if env is None:
        env = os.getenv("ENVIRONMENT", "").strip().lower()
    dedicated = os.getenv("PAIR_WEBHOOK_SECRET", "").strip()
    if dedicated:
        return dedicated
    if env in {"production", "prod"}:
        return ""
    return os.getenv("PAIR_API_KEY", "").strip()


def verify_hmac_signature(
    raw_body: bytes,
    signature_header: Optional[str],
    timestamp_header: Optional[str] = None,
) -> bool:
    """Verify X-Hub-Signature-256: sha256=<hex_digest>.

    Fails CLOSED by default: an unsigned request is accepted only when
    ENVIRONMENT is explicitly one of _UNSIGNED_OK_ENVS. Every other value —
    including an absent or unrecognized one — requires both a configured
    secret and a valid signature.

    `timestamp_header` (PR review — replay protection): Pair-bot's HMAC
    today covers only the raw body, with no timestamp or nonce, so a
    captured valid request can be replayed indefinitely; idempotency on
    (interview_id, event) only limits the damage for events already seen,
    not one that was never delivered in the first place (e.g. a
    handoff_missed replayed after an outage). Closing that fully needs
    Pair-bot to start signing a timestamp alongside the body — a contract
    change on their side, not something this repo can enforce unilaterally.
    This function is ready for it: when `timestamp_header` is passed and
    parses as a Unix timestamp, a request older than
    MAX_WEBHOOK_AGE_SECONDS (or more than that far in the future, which
    would only mean clock skew gone wrong) is rejected. Until the caller
    starts passing one, this check is simply skipped — it does not make an
    unsigned-timestamp request fail.
    """
    env = os.getenv("ENVIRONMENT", "").strip().lower()
    secret = get_webhook_secret(env)

    if env in _UNSIGNED_OK_ENVS and not secret:
        logger.warning("Webhook accepted without a secret in local %r environment", env or "(unset)")
        return True

    if not secret:
        logger.error(
            "Webhook rejected: no webhook secret configured (ENVIRONMENT=%r) — "
            "failing closed rather than assuming this is a local/dev host",
            env or "(unset)",
        )
        return False

    if not signature_header or not signature_header.startswith("sha256="):
        logger.warning("Webhook rejected: missing or invalid X-Hub-Signature-256 (ENVIRONMENT=%r)", env or "(unset)")
        return False

    if timestamp_header:
        try:
            sent_at = int(timestamp_header)
        except (TypeError, ValueError):
            logger.warning("Webhook rejected: unparseable timestamp header %r", timestamp_header)
            return False
        age = abs(time.time() - sent_at)
        if age > MAX_WEBHOOK_AGE_SECONDS:
            logger.warning(
                "Webhook rejected: timestamp %ss outside the %ss freshness window",
                int(age), MAX_WEBHOOK_AGE_SECONDS,
            )
            return False

    expected_digest = signature_header.split("sha256=", 1)[1].strip()
    calculated = hmac.new(
        secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(calculated, expected_digest)
