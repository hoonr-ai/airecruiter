"""pair-bot do-not-contact client (services/opt_out.py).

The reason this client exists instead of another `_proxy_post` in
routers/engagement.py is that those helpers collapse every upstream failure
into a flat 500. These tests pin the distinction: a 4xx means nothing was
suppressed and the caller must fix the request; anything else means the stop
never reached pair-bot and has to be retried.
"""
import asyncio
import json

import httpx
import pytest

from services import opt_out as mod
from services.dnc_storage import _phone_digit_forms
from services.opt_out import (
    PairBotOptOutError,
    pairbot_opt_in,
    pairbot_opt_out,
    pairbot_opt_out_status,
)


def _run(coro):
    """Drive a coroutine to completion in a sync test.

    Deliberately not @pytest.mark.asyncio: requirements-dev.txt pins pytest and
    nothing else, and this is the only async test module in the suite. Under a
    bare pytest that marker is an unknown mark — the tests do not run async,
    they FAIL, which is how this reached CI green locally and red there.
    """
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _pair_env(monkeypatch):
    monkeypatch.setenv("EXTERNAL_INTERVIEW_API_URL", "https://pairbotqa.hoonr.ai")
    monkeypatch.setenv("PAIR_API_KEY", "test-key")


class _Recorder:
    """Stand-in for httpx.AsyncClient that records the one request made."""

    def __init__(self, responder):
        self.responder = responder
        self.seen = {}

    def __call__(self, *_args, **_kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def request(self, method, url, headers=None, **kwargs):
        self.seen = {"method": method, "url": url, "headers": headers or {}, **kwargs}
        return self.responder(self.seen)


def _install(monkeypatch, responder):
    rec = _Recorder(responder)
    monkeypatch.setattr(mod.httpx, "AsyncClient", rec)
    return rec


def _json_response(status, body):
    return httpx.Response(
        status_code=status,
        content=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
        request=httpx.Request("POST", "https://pairbotqa.hoonr.ai/x"),
    )


# ---------------------------------------------------------------------------
# request shape
# ---------------------------------------------------------------------------
def test_opt_out_uses_the_existing_m2m_key(monkeypatch):
    rec = _install(monkeypatch, lambda _s: _json_response(200, {"success": True}))
    _run(pairbot_opt_out(email="a@b.com"))
    # Same key already used for /api/bulk-interviews — no new credential.
    assert rec.seen["headers"]["Authorization"] == "Bearer test-key"
    assert rec.seen["url"] == "https://pairbotqa.hoonr.ai/api/candidates/opt-out"


def test_opt_out_omits_absent_fields(monkeypatch):
    rec = _install(monkeypatch, lambda _s: _json_response(200, {"success": True}))
    _run(pairbot_opt_out(email="a@b.com"))
    # Only what the caller supplied: pair-bot's own defaults fill the rest.
    assert rec.seen["json"] == {"email": "a@b.com"}


def test_opt_out_truncates_reason_to_500_chars(monkeypatch):
    rec = _install(monkeypatch, lambda _s: _json_response(200, {"success": True}))
    _run(pairbot_opt_out(email="a@b.com", reason="x" * 900))
    assert len(rec.seen["json"]["reason"]) == 500


def test_trailing_slash_on_base_url_does_not_double(monkeypatch):
    monkeypatch.setenv("EXTERNAL_INTERVIEW_API_URL", "https://pairbotqa.hoonr.ai/")
    rec = _install(monkeypatch, lambda _s: _json_response(200, {"success": True}))
    _run(pairbot_opt_out(email="a@b.com"))
    assert rec.seen["url"] == "https://pairbotqa.hoonr.ai/api/candidates/opt-out"


def test_status_sends_identifiers_as_query_params(monkeypatch):
    rec = _install(monkeypatch, lambda _s: _json_response(200, {"success": True}))
    _run(pairbot_opt_out_status(email="a@b.com", phone="5105908688"))
    assert rec.seen["method"] == "GET"
    assert rec.seen["params"] == {"email": "a@b.com", "phone": "5105908688"}


def test_opt_in_never_carries_an_interview_id(monkeypatch):
    rec = _install(monkeypatch, lambda _s: _json_response(200, {"success": True}))
    _run(pairbot_opt_in(email="a@b.com", phone="5105908688"))
    assert set(rec.seen["json"]) == {"email", "phone"}


# ---------------------------------------------------------------------------
# error mapping
# ---------------------------------------------------------------------------
def test_client_error_is_not_retryable_and_keeps_its_status(monkeypatch):
    _install(
        monkeypatch,
        lambda _s: _json_response(422, {"detail": "No identifying field"}),
    )
    with pytest.raises(PairBotOptOutError) as exc:
        _run(pairbot_opt_out(reason="stop"))
    assert exc.value.status_code == 422
    assert exc.value.retryable is False
    # The upstream wording is what tells the recruiter what to fix.
    assert "No identifying field" in exc.value.message


def test_400_no_contact_on_interview_is_surfaced(monkeypatch):
    """pair-bot deliberately errors rather than reporting a silent success —
    reporting success would tell a recruiter the calls had stopped when
    nothing was recorded."""
    _install(
        monkeypatch,
        lambda _s: _json_response(400, {"message": "Interview has no email or phone"}),
    )
    with pytest.raises(PairBotOptOutError) as exc:
        _run(pairbot_opt_out(interview_id=7))
    assert exc.value.status_code == 400
    assert "no email or phone" in exc.value.message


def test_server_error_is_retryable(monkeypatch):
    _install(monkeypatch, lambda _s: _json_response(503, {"detail": "upstream down"}))
    with pytest.raises(PairBotOptOutError) as exc:
        _run(pairbot_opt_out(email="a@b.com"))
    assert exc.value.retryable is True


def test_rate_limited_response_is_retryable(monkeypatch):
    """A 429 means pair-bot is backpressuring us, not that the request is
    malformed — it must fall into the same local-DNC safety net as a 5xx."""
    _install(monkeypatch, lambda _s: _json_response(429, {"detail": "rate limited"}))
    with pytest.raises(PairBotOptOutError) as exc:
        _run(pairbot_opt_out(email="a@b.com"))
    assert exc.value.status_code == 429
    assert exc.value.retryable is True


def test_request_timeout_response_is_retryable(monkeypatch):
    _install(monkeypatch, lambda _s: _json_response(408, {"detail": "request timeout"}))
    with pytest.raises(PairBotOptOutError) as exc:
        _run(pairbot_opt_out(email="a@b.com"))
    assert exc.value.status_code == 408
    assert exc.value.retryable is True


def test_transport_error_is_retryable_with_no_status(monkeypatch):
    def boom(_s):
        raise httpx.ConnectTimeout("timed out")

    _install(monkeypatch, boom)
    with pytest.raises(PairBotOptOutError) as exc:
        _run(pairbot_opt_out(email="a@b.com"))
    assert exc.value.status_code is None
    assert exc.value.retryable is True


def test_non_json_error_body_still_raises_readably(monkeypatch):
    def html(_s):
        return httpx.Response(
            status_code=502,
            content=b"<html>bad gateway</html>",
            request=httpx.Request("POST", "https://pairbotqa.hoonr.ai/x"),
        )

    _install(monkeypatch, html)
    with pytest.raises(PairBotOptOutError) as exc:
        _run(pairbot_opt_out(email="a@b.com"))
    assert "502" in exc.value.message


def test_missing_api_key_does_not_block_the_call(monkeypatch):
    """QA hosts have run unauthenticated. Warn, don't refuse — refusing here
    turns a misconfigured env var into "the calls kept going"."""
    monkeypatch.delenv("PAIR_API_KEY", raising=False)
    rec = _install(monkeypatch, lambda _s: _json_response(200, {"success": True}))
    _run(pairbot_opt_out(email="a@b.com"))
    assert "Authorization" not in rec.seen["headers"]


# ---------------------------------------------------------------------------
# local phone matching
# ---------------------------------------------------------------------------
def test_phone_digit_forms_covers_both_stored_shapes():
    """sourced_candidates.phone holds values like "+1 (510) 590-8688" as well
    as bare 10-digit strings; dnc_list.phone is always the 11-digit form."""
    assert _phone_digit_forms("15105908688") == ["15105908688", "5105908688"]


def test_phone_digit_forms_leaves_non_us_alone():
    assert _phone_digit_forms("445105908688") == ["445105908688"]


# ---------------------------------------------------------------------------
# log masking
# ---------------------------------------------------------------------------
# The do-not-contact flow is the last place a candidate's contact details
# should be written to application logs in cleartext — logs travel further and
# live longer than the rows they describe. utils/pii.py keeps enough to
# correlate a line with a case; dnc_list / outreach_opt_out_audit hold the rest.
def test_mask_email_keeps_only_the_domain_and_first_letter():
    from utils.pii import mask_email

    assert mask_email("ahmay02@gmail.com") == "a***@gmail.com"


def test_mask_email_handles_absent_and_malformed_values():
    from utils.pii import mask_email

    assert mask_email(None) == "-"
    assert mask_email("") == "-"
    # Not an address: reveal its shape, not its content.
    assert "junk" not in mask_email("junk")


def test_mask_phone_keeps_only_the_last_four():
    from utils.pii import mask_phone

    assert mask_phone("+1 (510) 590-8688") == "***8688"
    assert mask_phone("15105908688") == "***8688"
    # The leading digits are what identify a person; they must not survive.
    assert "5105" not in mask_phone("15105908688")


def test_mask_phone_handles_absent_and_digitless_values():
    from utils.pii import mask_phone

    assert mask_phone(None) == "-"
    assert mask_phone("n/a") == "***"


def test_no_raw_contact_values_reach_the_log_calls():
    """Guard against a future log line reintroducing the plaintext values.

    Reads the source rather than capturing output: the risky lines are on
    failure paths that need a live DB to reach, and the property worth pinning
    is "no log call passes a raw contact variable", which is visible statically.
    """
    import ast
    from pathlib import Path

    api_root = Path(__file__).resolve().parents[1]
    raw_names = {"email", "phone", "email_norm", "phone_norm", "contact_value"}
    offenders = []
    for rel in ("services/dnc_storage.py", "routers/outreach_optout.py"):
        tree = ast.parse((api_root / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "logger"
            ):
                continue
            for arg in node.args:
                if isinstance(arg, ast.Name) and arg.id in raw_names:
                    offenders.append(f"{rel}:{node.lineno} logs raw {arg.id}")
    assert not offenders, offenders


def test_dnc_engine_hides_bound_parameters_from_error_strings():
    """A DBAPI error string carries "[parameters: (...)]" unless the engine is
    built with hide_parameters=True — i.e. the candidate's email and phone.
    That string reaches the log line, outreach_opt_out_audit.local_result, and
    the browser via local.error, so masking the log arguments is not enough.
    """
    import inspect

    from services import dnc_storage

    src = inspect.getsource(dnc_storage._get_engine)
    assert "hide_parameters=True" in src
