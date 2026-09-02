"""pair-bot do-not-contact client (services/opt_out.py).

The reason this client exists instead of another `_proxy_post` in
routers/engagement.py is that those helpers collapse every upstream failure
into a flat 500. These tests pin the distinction: a 4xx means nothing was
suppressed and the caller must fix the request; anything else means the stop
never reached pair-bot and has to be retried.
"""
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
@pytest.mark.asyncio
async def test_opt_out_uses_the_existing_m2m_key(monkeypatch):
    rec = _install(monkeypatch, lambda _s: _json_response(200, {"success": True}))
    await pairbot_opt_out(email="a@b.com")
    # Same key already used for /api/bulk-interviews — no new credential.
    assert rec.seen["headers"]["Authorization"] == "Bearer test-key"
    assert rec.seen["url"] == "https://pairbotqa.hoonr.ai/api/candidates/opt-out"


@pytest.mark.asyncio
async def test_opt_out_omits_absent_fields(monkeypatch):
    rec = _install(monkeypatch, lambda _s: _json_response(200, {"success": True}))
    await pairbot_opt_out(email="a@b.com")
    # Only what the caller supplied: pair-bot's own defaults fill the rest.
    assert rec.seen["json"] == {"email": "a@b.com"}


@pytest.mark.asyncio
async def test_opt_out_truncates_reason_to_500_chars(monkeypatch):
    rec = _install(monkeypatch, lambda _s: _json_response(200, {"success": True}))
    await pairbot_opt_out(email="a@b.com", reason="x" * 900)
    assert len(rec.seen["json"]["reason"]) == 500


@pytest.mark.asyncio
async def test_trailing_slash_on_base_url_does_not_double(monkeypatch):
    monkeypatch.setenv("EXTERNAL_INTERVIEW_API_URL", "https://pairbotqa.hoonr.ai/")
    rec = _install(monkeypatch, lambda _s: _json_response(200, {"success": True}))
    await pairbot_opt_out(email="a@b.com")
    assert rec.seen["url"] == "https://pairbotqa.hoonr.ai/api/candidates/opt-out"


@pytest.mark.asyncio
async def test_status_sends_identifiers_as_query_params(monkeypatch):
    rec = _install(monkeypatch, lambda _s: _json_response(200, {"success": True}))
    await pairbot_opt_out_status(email="a@b.com", phone="5105908688")
    assert rec.seen["method"] == "GET"
    assert rec.seen["params"] == {"email": "a@b.com", "phone": "5105908688"}


@pytest.mark.asyncio
async def test_opt_in_never_carries_an_interview_id(monkeypatch):
    rec = _install(monkeypatch, lambda _s: _json_response(200, {"success": True}))
    await pairbot_opt_in(email="a@b.com", phone="5105908688")
    assert set(rec.seen["json"]) == {"email", "phone"}


# ---------------------------------------------------------------------------
# error mapping
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_client_error_is_not_retryable_and_keeps_its_status(monkeypatch):
    _install(
        monkeypatch,
        lambda _s: _json_response(422, {"detail": "No identifying field"}),
    )
    with pytest.raises(PairBotOptOutError) as exc:
        await pairbot_opt_out(reason="stop")
    assert exc.value.status_code == 422
    assert exc.value.retryable is False
    # The upstream wording is what tells the recruiter what to fix.
    assert "No identifying field" in exc.value.message


@pytest.mark.asyncio
async def test_400_no_contact_on_interview_is_surfaced(monkeypatch):
    """pair-bot deliberately errors rather than reporting a silent success —
    reporting success would tell a recruiter the calls had stopped when
    nothing was recorded."""
    _install(
        monkeypatch,
        lambda _s: _json_response(400, {"message": "Interview has no email or phone"}),
    )
    with pytest.raises(PairBotOptOutError) as exc:
        await pairbot_opt_out(interview_id=7)
    assert exc.value.status_code == 400
    assert "no email or phone" in exc.value.message


@pytest.mark.asyncio
async def test_server_error_is_retryable(monkeypatch):
    _install(monkeypatch, lambda _s: _json_response(503, {"detail": "upstream down"}))
    with pytest.raises(PairBotOptOutError) as exc:
        await pairbot_opt_out(email="a@b.com")
    assert exc.value.retryable is True


@pytest.mark.asyncio
async def test_transport_error_is_retryable_with_no_status(monkeypatch):
    def boom(_s):
        raise httpx.ConnectTimeout("timed out")

    _install(monkeypatch, boom)
    with pytest.raises(PairBotOptOutError) as exc:
        await pairbot_opt_out(email="a@b.com")
    assert exc.value.status_code is None
    assert exc.value.retryable is True


@pytest.mark.asyncio
async def test_non_json_error_body_still_raises_readably(monkeypatch):
    def html(_s):
        return httpx.Response(
            status_code=502,
            content=b"<html>bad gateway</html>",
            request=httpx.Request("POST", "https://pairbotqa.hoonr.ai/x"),
        )

    _install(monkeypatch, html)
    with pytest.raises(PairBotOptOutError) as exc:
        await pairbot_opt_out(email="a@b.com")
    assert "502" in exc.value.message


@pytest.mark.asyncio
async def test_missing_api_key_does_not_block_the_call(monkeypatch):
    """QA hosts have run unauthenticated. Warn, don't refuse — refusing here
    turns a misconfigured env var into "the calls kept going"."""
    monkeypatch.delenv("PAIR_API_KEY", raising=False)
    rec = _install(monkeypatch, lambda _s: _json_response(200, {"success": True}))
    await pairbot_opt_out(email="a@b.com")
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
