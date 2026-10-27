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

# "local" may always run with no secret configured at all (a bare "warn and
# allow unsigned" convenience for a developer's own machine). "dev"/
# "development" need an explicit opt-in too (PR #864 review: an
# internet-reachable dev deployment with no secret configured would
# otherwise still accept unsigned webhooks just by being named "dev") — set
# ALLOW_UNSIGNED_WEBHOOKS_IN_DEV=true to keep the old convenience there.
# Every other value — including one that's unset, misspelled, or
# unrecognized ("Production ", "live", ...) — REQUIRES a configured secret
# and a valid signature (PR #863 review: the old code only enforced HMAC
# for an exact allowlist match and silently fell back to "allow unsigned"
# for anything else, which fails OPEN exactly when a deployed host's
# ENVIRONMENT is missing or wrong — the one case a security check most
# needs to hold).
_ALWAYS_UNSIGNED_OK_ENVS = frozenset({"local"})
_DEV_UNSIGNED_OK_ENVS = frozenset({"dev", "development"})

# How old a signed request's timestamp may be before it's treated as a
# replay. Only enforced when the caller actually sends one (see
# verify_hmac_signature's docstring) — this constant exists so the window is
# defined in one place once Pair-bot starts sending one.
MAX_WEBHOOK_AGE_SECONDS = 300

# Attacker-controlled; never log a header value in full (PR #864 review).
_LOG_HEADER_MAX_CHARS = 32


def get_webhook_secret(env: Optional[str] = None) -> str:
    """Retrieve the secret used to verify webhook HMAC-SHA256 signatures.

    PAIR_API_KEY is accepted as a fallback when PAIR_WEBHOOK_SECRET isn't
    set — EXCEPT in prod, where it's refused (PR review: PAIR_API_KEY is
    also our OUTBOUND credential to Pair-bot; using it as the INBOUND
    webhook's HMAC key too couples two trust domains that should rotate
    independently — rotating the outbound key would silently rotate, or
    break, inbound verification too).

    staging/qa/dev may still fall back to it for now as a transition —
    TODO(tracking issue needed): drop the fallback there too once every
    staging/QA host has its own dedicated PAIR_WEBHOOK_SECRET configured.
    This is an ops/infra rollout this repo can prompt but not complete
    unilaterally, so no removal date is hardcoded here; file a tracking
    issue when the hosts are confirmed configured.
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

    Fails CLOSED by default: an unsigned request is accepted only in
    "local" (always) or "dev"/"development" (only with
    ALLOW_UNSIGNED_WEBHOOKS_IN_DEV=true). Every other value — including an
    absent or unrecognized one — requires both a configured secret and a
    valid signature.

    `timestamp_header` (replay protection) — PR #864 review: this is NOT
    real protection yet, and won't be until Pair-bot starts sending the
    header. Pair-bot's HMAC today covers only the raw body, with no
    timestamp or nonce, so a captured valid request can be replayed
    indefinitely; idempotency on (interview_id, event) only limits the
    damage for events already seen, not one that was never delivered in
    the first place (e.g. a handoff_missed replayed after an outage).

    When a timestamp header IS present, it is folded into the signed
    material (`f"{timestamp}."` prefixed onto the body, the Stripe/Slack
    scheme) rather than checked as a bare unsigned freshness claim — an
    earlier version of this function checked the header's age without
    including it in the HMAC input at all, which an attacker could defeat
    trivially by replaying a captured (body, signature) pair alongside
    their own fresh, unsigned timestamp (PR #864 review caught this).
    Folding it into what's signed means a replayed request can no longer
    pass the freshness check without also matching the signature. Until
    Pair-bot actually sends this header, none of this runs: the signature
    still covers the bare body exactly as before, and there is NO replay
    protection at all for that case.
    """
    env = os.getenv("ENVIRONMENT", "").strip().lower()
    secret = get_webhook_secret(env)

    dev_opt_in = os.getenv("ALLOW_UNSIGNED_WEBHOOKS_IN_DEV", "").strip().lower() in {"1", "true", "yes", "on"}
    unsigned_ok = env in _ALWAYS_UNSIGNED_OK_ENVS or (env in _DEV_UNSIGNED_OK_ENVS and dev_opt_in)
    if unsigned_ok and not secret:
        logger.warning("Webhook accepted without a secret in %r environment", env or "(unset)")
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

    signed_payload = raw_body
    if timestamp_header:
        try:
            sent_at = int(timestamp_header)
        except (TypeError, ValueError):
            logger.warning(
                "Webhook rejected: unparseable timestamp header %r",
                timestamp_header[:_LOG_HEADER_MAX_CHARS],
            )
            return False
        age = abs(time.time() - sent_at)
        if age > MAX_WEBHOOK_AGE_SECONDS:
            logger.warning(
                "Webhook rejected: timestamp %ss outside the %ss freshness window",
                int(age), MAX_WEBHOOK_AGE_SECONDS,
            )
            return False
        # The timestamp must be part of what's signed — see the docstring
        # above for why a bare (unsigned) freshness check doesn't actually
        # stop a replay.
        signed_payload = f"{timestamp_header}.".encode("utf-8") + raw_body

    expected_digest = signature_header.split("sha256=", 1)[1].strip()
    calculated = hmac.new(
        secret.encode("utf-8"),
        signed_payload,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(calculated, expected_digest)
