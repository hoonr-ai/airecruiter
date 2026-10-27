"""Unit tests for core/webhook_auth.py — the shared HMAC verification used
by the DNC, Pair-bot evaluation, and recruiter-handoff webhooks alike.

PR #863 review ("Akarsh's Reviewer Agent"): verify_hmac_signature failed
OPEN when ENVIRONMENT was unset or misspelled, get_webhook_secret let
PAIR_API_KEY (the outbound credential) stand in for the inbound webhook
secret even in prod, and there was no replay/timestamp protection at all.
"""
import hashlib
import hmac
import time

import pytest

from core import webhook_auth


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


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


@pytest.mark.parametrize("env", ["local", "dev", "development", "DEV"])
def test_unsigned_request_is_allowed_only_in_explicit_dev_envs(monkeypatch, env):
    monkeypatch.delenv("PAIR_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("PAIR_API_KEY", raising=False)
    monkeypatch.setenv("ENVIRONMENT", env)
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
    absence must never itself cause a rejection."""
    monkeypatch.setenv("PAIR_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setenv("ENVIRONMENT", "production")
    body = b"{}"
    assert webhook_auth.verify_hmac_signature(body, _sign("s3cret", body), None) is True


def test_a_fresh_timestamp_is_accepted(monkeypatch):
    monkeypatch.setenv("PAIR_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setenv("ENVIRONMENT", "production")
    body = b"{}"
    assert webhook_auth.verify_hmac_signature(body, _sign("s3cret", body), str(int(time.time()))) is True


def test_a_stale_timestamp_is_rejected(monkeypatch):
    monkeypatch.setenv("PAIR_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setenv("ENVIRONMENT", "production")
    body = b"{}"
    stale = str(int(time.time()) - webhook_auth.MAX_WEBHOOK_AGE_SECONDS - 60)
    assert webhook_auth.verify_hmac_signature(body, _sign("s3cret", body), stale) is False


def test_an_unparseable_timestamp_is_rejected(monkeypatch):
    monkeypatch.setenv("PAIR_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setenv("ENVIRONMENT", "production")
    body = b"{}"
    assert webhook_auth.verify_hmac_signature(body, _sign("s3cret", body), "not-a-number") is False
