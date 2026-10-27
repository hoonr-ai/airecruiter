"""Unit tests for core/webhook_auth.py — the shared HMAC verification used
by the DNC, Pair-bot evaluation, and recruiter-handoff webhooks alike.

PR #863 review ("Akarsh's Reviewer Agent"): verify_hmac_signature failed
OPEN when ENVIRONMENT was unset or misspelled, get_webhook_secret let
PAIR_API_KEY (the outbound credential) stand in for the inbound webhook
secret even in prod, and there was no replay/timestamp protection at all.

PR #864 review (second pass): the first timestamp check verified freshness
without the timestamp being part of the signed material, so a replayed
request could carry its own fresh, unsigned timestamp and pass anyway;
"dev" allowed unsigned requests unconditionally, the same fail-open shape
the first review flagged for prod-ish values.
"""
import hashlib
import hmac
import time

import pytest

from core import webhook_auth


def _sign(secret: str, body: bytes) -> str:
    """Signs the bare body — what every request looks like until Pair-bot
    starts sending a timestamp header at all."""
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _sign_with_ts(secret: str, body: bytes, timestamp: str) -> str:
    """Signs timestamp + "." + body — the scheme verify_hmac_signature uses
    once a timestamp header IS present (PR #864 review)."""
    payload = f"{timestamp}.".encode() + body
    return "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


# --- fail-closed by default ---------------------------------------------------

@pytest.mark.parametrize("env", [None, "", "Production", "production ", "live", "prd"])
def test_unsigned_request_is_rejected_for_any_non_dev_environment(monkeypatch, env):
    """The core regression this PR fixes: an unset, blank, misspelled, or
    simply unrecognized ENVIRONMENT value used to fall through to "allow
    unsigned" whenever no secret was configured — exactly the host
    misconfiguration a security check most needs to catch. Every one of
    these must now fail closed instead."""
    monkeypatch.delenv("PAIR_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("PAIR_API_KEY", raising=False)
    if env is None:
        monkeypatch.delenv("ENVIRONMENT", raising=False)
    else:
        monkeypatch.setenv("ENVIRONMENT", env)
    assert webhook_auth.verify_hmac_signature(b"{}", None) is False


def test_unsigned_request_is_always_allowed_in_local(monkeypatch):
    monkeypatch.delenv("PAIR_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("PAIR_API_KEY", raising=False)
    monkeypatch.delenv("ALLOW_UNSIGNED_WEBHOOKS_IN_DEV", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "local")
    assert webhook_auth.verify_hmac_signature(b"{}", None) is True


@pytest.mark.parametrize("env", ["dev", "development", "DEV"])
def test_unsigned_request_in_dev_needs_an_explicit_opt_in(monkeypatch, env):
    """PR #864 review: "dev" used to allow unsigned requests unconditionally
    — the same fail-open shape as the prod-ish case above, just for a value
    that's easy to leave on an internet-reachable deployment. Now requires
    ALLOW_UNSIGNED_WEBHOOKS_IN_DEV=true as well."""
    monkeypatch.delenv("PAIR_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("PAIR_API_KEY", raising=False)
    monkeypatch.setenv("ENVIRONMENT", env)
    monkeypatch.delenv("ALLOW_UNSIGNED_WEBHOOKS_IN_DEV", raising=False)
    assert webhook_auth.verify_hmac_signature(b"{}", None) is False
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS_IN_DEV", "true")
    assert webhook_auth.verify_hmac_signature(b"{}", None) is True


@pytest.mark.parametrize("env", ["production", "prod", "staging", "qa", "dev", "local"])
def test_a_configured_secret_still_requires_a_valid_signature_everywhere(monkeypatch, env):
    """Having a secret configured must never relax verification, even in a
    dev-ish environment — a wrong/missing signature still fails."""
    monkeypatch.setenv("PAIR_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setenv("ENVIRONMENT", env)
    assert webhook_auth.verify_hmac_signature(b"{}", None) is False
    assert webhook_auth.verify_hmac_signature(b"{}", "sha256=" + "0" * 64) is False
    assert webhook_auth.verify_hmac_signature(b"{}", _sign("s3cret", b"{}")) is True


# --- get_webhook_secret: PAIR_API_KEY must not back prod ----------------------

@pytest.mark.parametrize("env", ["production", "prod"])
def test_prod_refuses_the_pair_api_key_fallback(monkeypatch, env):
    monkeypatch.delenv("PAIR_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("PAIR_API_KEY", "outbound-key")
    assert webhook_auth.get_webhook_secret(env) == ""


@pytest.mark.parametrize("env", ["staging", "qa", "dev", "", None])
def test_non_prod_still_falls_back_to_pair_api_key(monkeypatch, env):
    monkeypatch.delenv("PAIR_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("PAIR_API_KEY", "outbound-key")
    assert webhook_auth.get_webhook_secret(env) == "outbound-key"


def test_dedicated_secret_always_wins_over_the_fallback(monkeypatch):
    monkeypatch.setenv("PAIR_WEBHOOK_SECRET", "dedicated")
    monkeypatch.setenv("PAIR_API_KEY", "outbound-key")
    assert webhook_auth.get_webhook_secret("production") == "dedicated"


# --- replay/timestamp protection (opt-in until Pair-bot signs one) -----------

def test_missing_timestamp_header_does_not_fail_a_request(monkeypatch):
    """Backward compatible: Pair-bot doesn't send this header yet, so its
    absence must never itself cause a rejection — the signature still
    covers the bare body, exactly as before this existed."""
    monkeypatch.setenv("PAIR_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setenv("ENVIRONMENT", "production")
    body = b"{}"
    assert webhook_auth.verify_hmac_signature(body, _sign("s3cret", body), None) is True


def test_a_fresh_correctly_signed_timestamp_is_accepted(monkeypatch):
    monkeypatch.setenv("PAIR_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setenv("ENVIRONMENT", "production")
    body = b"{}"
    ts = str(int(time.time()))
    assert webhook_auth.verify_hmac_signature(body, _sign_with_ts("s3cret", body, ts), ts) is True


def test_a_stale_timestamp_is_rejected(monkeypatch):
    monkeypatch.setenv("PAIR_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setenv("ENVIRONMENT", "production")
    body = b"{}"
    stale = str(int(time.time()) - webhook_auth.MAX_WEBHOOK_AGE_SECONDS - 60)
    assert webhook_auth.verify_hmac_signature(body, _sign_with_ts("s3cret", body, stale), stale) is False


def test_an_unparseable_timestamp_is_rejected(monkeypatch):
    monkeypatch.setenv("PAIR_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setenv("ENVIRONMENT", "production")
    body = b"{}"
    assert webhook_auth.verify_hmac_signature(body, _sign("s3cret", body), "not-a-number") is False


def test_a_replayed_body_cannot_be_revived_with_a_forged_fresh_timestamp(monkeypatch):
    """The exact bypass PR #864 review caught: a captured, validly-signed
    (body, signature) pair — signed over the bare body, as every real
    request looks like today — must NOT be revivable by an attacker simply
    attaching their own current, UNSIGNED timestamp to slip past the
    freshness check. Folding the timestamp into the signed material means
    this now fails instead of silently passing."""
    monkeypatch.setenv("PAIR_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setenv("ENVIRONMENT", "production")
    body = b'{"event": "handoff_requested", "interview_id": 4242}'
    captured_signature = _sign("s3cret", body)  # signed over the bare body only
    forged_fresh_timestamp = str(int(time.time()))
    assert webhook_auth.verify_hmac_signature(body, captured_signature, forged_fresh_timestamp) is False


def test_timestamp_signed_correctly_but_body_tampered_is_rejected(monkeypatch):
    monkeypatch.setenv("PAIR_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setenv("ENVIRONMENT", "production")
    ts = str(int(time.time()))
    sig = _sign_with_ts("s3cret", b'{"event": "handoff_requested"}', ts)
    tampered = b'{"event": "handoff_closed"}'
    assert webhook_auth.verify_hmac_signature(tampered, sig, ts) is False
