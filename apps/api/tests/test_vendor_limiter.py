"""Shared vendor limiter (OpenAI, Exa): pacing, 429 cooldown, fail-closed fallback."""
import asyncio
import time

import httpx
import pytest

from core import vendor_limiter as vl


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    monkeypatch.setattr(vl._cfg, "REDIS_URL", "", raising=False)
    monkeypatch.setattr(vl, "_redis_client", None)
    monkeypatch.setattr(vl, "_redis_down_until", 0.0)


def _limiter(**kw):
    return vl.VendorLimiter("test", **kw)


def test_spaces_requests_by_min_interval():
    lim = _limiter(min_interval_s=0.05)

    async def run():
        stamps = []
        for _ in range(3):
            assert await lim.acquire(5)
            stamps.append(time.monotonic())
        return stamps

    s = asyncio.run(run())
    assert all(b - a >= 0.045 for a, b in zip(s, s[1:]))


def test_zero_interval_does_not_pace():
    lim = _limiter(min_interval_s=0.0)

    async def run():
        t0 = time.monotonic()
        for _ in range(20):
            assert await lim.acquire(1)
        return time.monotonic() - t0

    assert asyncio.run(run()) < 0.1


def test_429_cooldown_blocks_until_it_expires():
    lim = _limiter(cooldown_cap_s=1.0)

    async def run():
        await lim.note_429("1")
        assert await lim.acquire(0.2) is False
        t0 = time.monotonic()
        assert await lim.acquire(5)
        return time.monotonic() - t0

    assert asyncio.run(run()) >= 0.6
    assert lim.stats["http_429"] == 1 and lim.stats["timeouts"] == 1


def test_redis_down_paces_each_worker_at_its_share(monkeypatch):
    monkeypatch.setattr(vl._cfg, "REDIS_URL", "redis://unreachable", raising=False)
    monkeypatch.setattr(vl, "_redis_down_until", time.monotonic() + 60)
    lim = _limiter(min_interval_s=0.05)
    lim.local_workers = 4
    assert lim._local_interval_s() == pytest.approx(0.2)


def test_retry_after_parsing():
    assert vl.parse_retry_after("3") == 3.0
    assert vl.parse_retry_after(None) is None
    assert vl.parse_retry_after("garbage") is None


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("VENDOR_TEST_MIN_INTERVAL_S", "0.7")
    assert _limiter(min_interval_s=0.1).min_interval_s == 0.7


def test_openai_client_hooks_share_429_cooldown(monkeypatch):
    from core import llm_client

    monkeypatch.setattr(vl.OPENAI, "_local_cooldown_until", 0.0)
    resp = httpx.Response(429, headers={"retry-after": "2"}, request=httpx.Request("POST", "https://x"))
    asyncio.run(llm_client._note_429(resp))
    assert vl.OPENAI._local_cooldown_until - time.monotonic() > 1.5
    ok = httpx.Response(200, request=httpx.Request("POST", "https://x"))
    before = vl.OPENAI.stats["http_429"]
    asyncio.run(llm_client._note_429(ok))
    assert vl.OPENAI.stats["http_429"] == before
