"""Shared JobDiva BI limiter, detail cache and the BI batch-fetch budget.

In-process tests always run. Redis tests run the real Lua scripts against
``JOBDIVA_TEST_REDIS_URL`` (default redis://localhost:6379/15) and are skipped
when no Redis answers there. Every Redis test uses its own random key names,
so nothing is flushed.
"""
import asyncio
import json
import os
import time
import uuid

import pytest

from services import jobdiva_detail_cache as dc
from services import jobdiva_rate_limit as rl

TEST_REDIS_URL = os.getenv("JOBDIVA_TEST_REDIS_URL", "redis://localhost:6379/15")


def _redis_reachable() -> bool:
    try:
        import redis
        redis.Redis.from_url(TEST_REDIS_URL, socket_connect_timeout=0.5).ping()
        return True
    except Exception:
        return False


needs_redis = pytest.mark.skipif(not _redis_reachable(), reason="no test Redis reachable")


@pytest.fixture(autouse=True)
def _fresh_limiter(monkeypatch):
    monkeypatch.setattr(rl._cfg, "REDIS_URL", "", raising=False)
    monkeypatch.setattr(rl, "_redis_client", None)
    monkeypatch.setattr(rl, "_redis_down_until", 0.0)
    monkeypatch.setattr(rl, "MIN_INTERVAL_S", 0.05)
    monkeypatch.setattr(rl, "_local_tokens", None)
    monkeypatch.setattr(rl, "_local_ts", 0.0)
    monkeypatch.setattr(rl, "_prio_stats", {})
    monkeypatch.setattr(rl, "BURST", 1.0)
    monkeypatch.setattr(rl, "BACKGROUND_BURST", 1.0)
    monkeypatch.setattr(rl, "_local_cooldown_until", 0.0)
    monkeypatch.setattr(rl, "_local_fg_waiting_until", 0.0)
    monkeypatch.setattr(rl, "_local_lock", None)
    monkeypatch.setattr(rl, "_stats", {"granted": 0, "timeouts": 0, "http_429": 0})


@pytest.fixture
def redis_keys(monkeypatch):
    """Point the limiter at the test Redis with unique keys; clean them up after."""
    tag = uuid.uuid4().hex
    keys = {
        "_BUCKET_KEY": f"test:{tag}:bucket",
        "_COOLDOWN_KEY": f"test:{tag}:cooldown",
        "_FG_WAITING_KEY": f"test:{tag}:fg",
    }
    for attr, key in keys.items():
        monkeypatch.setattr(rl, attr, key)
    monkeypatch.setattr(rl._cfg, "REDIS_URL", TEST_REDIS_URL, raising=False)
    monkeypatch.setattr(dc, "_PREFIX", f"test:{tag}:cand:")
    yield keys
    import redis
    r = redis.Redis.from_url(TEST_REDIS_URL)
    for key in r.scan_iter(f"test:{tag}:*"):
        r.delete(key)


async def _timed_acquires(n, max_wait_s=5.0, **kw):
    stamps = []

    async def one():
        assert await rl.acquire(max_wait_s, **kw)
        stamps.append(time.monotonic())

    await asyncio.gather(*[one() for _ in range(n)])
    return sorted(stamps)


def _gaps(stamps):
    return [b - a for a, b in zip(stamps, stamps[1:])]


# ---- in-process fallback ---------------------------------------------------

def test_local_acquire_spaces_concurrent_callers():
    stamps = asyncio.run(_timed_acquires(4))
    assert all(g >= 0.045 for g in _gaps(stamps)), _gaps(stamps)


def test_redis_down_paces_each_worker_at_its_share(monkeypatch):
    # Redis configured but unreachable: each worker gets 1/LOCAL_WORKERS of the budget.
    monkeypatch.setattr(rl._cfg, "REDIS_URL", "redis://unreachable", raising=False)
    monkeypatch.setattr(rl, "_redis_down_until", time.monotonic() + 60)
    monkeypatch.setattr(rl, "LOCAL_WORKERS", 4)
    stamps = asyncio.run(_timed_acquires(3))
    assert all(g >= 0.19 for g in _gaps(stamps)), _gaps(stamps)


def test_no_redis_configured_uses_full_budget():
    assert rl._local_interval_s() == rl.MIN_INTERVAL_S


def test_local_acquire_gives_up_past_budget_and_counts_it():
    async def run():
        await rl.note_429("1")
        return await rl.acquire(0.1)

    assert asyncio.run(run()) is False
    assert rl.stats()["timeouts"] == 1
    assert rl.stats()["http_429"] == 1


def test_local_cooldown_delays_every_caller():
    async def run():
        await rl.note_429("1")
        t0 = time.monotonic()
        assert await rl.acquire(5.0)
        return time.monotonic() - t0

    assert asyncio.run(run()) >= 0.9


def test_cooldown_mid_wait_does_not_burn_slots():
    """A 429 landing while callers wait must not push later callers back by
    one interval per waiter (the old reserve-ahead scheme did)."""
    async def run():
        waiters = [asyncio.create_task(rl.acquire(10.0)) for _ in range(6)]
        await asyncio.sleep(0.01)
        await rl.note_429("1")
        t0 = time.monotonic()
        assert all(await asyncio.gather(*waiters))
        return time.monotonic() - t0

    # 1s cooldown + ~6 slots at 0.05s + jitter; burned slots would add more.
    assert asyncio.run(run()) < 1.0 + 6 * 0.05 + 6 * 0.0125 + 0.3


def test_background_yields_to_waiting_interactive():
    async def run():
        order = []
        await rl.note_429("1")  # everyone has to wait for the same moment

        async def call(name, priority):
            assert await rl.acquire(5.0, priority=priority)
            order.append(name)

        bg = asyncio.create_task(call("bg", rl.BACKGROUND))
        await asyncio.sleep(0.01)
        fg = asyncio.create_task(call("fg", rl.INTERACTIVE))
        await asyncio.gather(bg, fg)
        return order

    assert asyncio.run(run()) == ["fg", "bg"]


def test_retry_after_parsing_and_cap(monkeypatch):
    assert rl._parse_retry_after("7") == 7.0
    assert rl._parse_retry_after("garbage") is None
    monkeypatch.setattr(rl, "COOLDOWN_CAP_S", 3.0)
    assert asyncio.run(rl.note_429("600")) == 3.0


def test_missing_retry_after_uses_jitter_range(monkeypatch):
    monkeypatch.setattr(rl, "COOLDOWN_MIN_S", 2.0)
    monkeypatch.setattr(rl, "COOLDOWN_MAX_S", 4.0)
    assert 2.0 <= asyncio.run(rl.note_429(None)) <= 4.0


# ---- bi_get ----------------------------------------------------------------

class _Resp:
    def __init__(self, status, headers=None):
        self.status_code = status
        self.headers = headers or {}


class _Client:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0

    async def get(self, url, **kwargs):
        self.calls += 1
        return self.responses.pop(0)


def test_bi_get_429_starts_shared_cooldown_and_returns_response():
    async def run():
        client = _Client(_Resp(429, {"Retry-After": "2"}))
        resp = await rl.bi_get(client, "https://x/apiv2/bi/CandidatesDetail", max_wait_s=1.0)
        return resp, client

    resp, client = asyncio.run(run())
    assert resp.status_code == 429 and client.calls == 1
    assert rl._local_cooldown_until - time.monotonic() > 1.5


def test_bi_get_returns_none_without_calling_when_budget_runs_out():
    async def run():
        await rl.note_429("5")
        client = _Client(_Resp(200))
        return await rl.bi_get(client, "https://x", max_wait_s=0.2), client

    resp, client = asyncio.run(run())
    assert resp is None and client.calls == 0


# ---- Redis (real Lua scripts) ----------------------------------------------

@needs_redis
def test_redis_take_spaces_callers(redis_keys):
    stamps = asyncio.run(_timed_acquires(4))
    assert all(g >= 0.045 for g in _gaps(stamps)), _gaps(stamps)


@needs_redis
def test_redis_cooldown_extends_but_never_shortens(redis_keys):
    async def run():
        r = rl._get_redis()
        await rl.note_429("3")
        first = await r.pttl(redis_keys["_COOLDOWN_KEY"])
        await rl.note_429("1")
        second = await r.pttl(redis_keys["_COOLDOWN_KEY"])
        return first, second

    first, second = asyncio.run(run())
    assert 2500 < first <= 3000
    assert second > 2000  # the shorter Retry-After did not cut it down


@needs_redis
def test_redis_cooldown_is_seen_by_other_workers(redis_keys, monkeypatch):
    async def run():
        await rl.note_429("1")
        # Another worker: no local cooldown, only the shared Redis key.
        monkeypatch.setattr(rl, "_local_cooldown_until", 0.0)
        t0 = time.monotonic()
        assert await rl.acquire(5.0)
        return time.monotonic() - t0

    assert asyncio.run(run()) >= 0.9


@needs_redis
def test_redis_background_yields_to_waiting_interactive(redis_keys):
    async def run():
        order = []
        await rl.note_429("1")

        async def call(name, priority):
            assert await rl.acquire(5.0, priority=priority)
            order.append(name)

        bg = asyncio.create_task(call("bg", rl.BACKGROUND))
        await asyncio.sleep(0.01)
        fg = asyncio.create_task(call("fg", rl.INTERACTIVE))
        await asyncio.gather(bg, fg)
        return order

    assert asyncio.run(run()) == ["fg", "bg"]


def test_redis_down_falls_back_then_retries(monkeypatch):
    monkeypatch.setattr(rl._cfg, "REDIS_URL", "redis://127.0.0.1:1/0", raising=False)
    assert asyncio.run(rl.acquire(1.0)) is True  # local fallback still paces
    assert rl._redis_down_until > time.monotonic()
    assert rl._get_redis() is None  # skipped while marked down
    monkeypatch.setattr(rl, "_redis_down_until", 0.0)
    assert rl._get_redis() is not None  # tried again once the window passes


# ---- detail cache ----------------------------------------------------------

@pytest.fixture
def fernet(monkeypatch):
    from cryptography.fernet import Fernet
    f = Fernet(Fernet.generate_key())
    monkeypatch.setattr(dc, "_fernet", f)
    monkeypatch.setattr(dc, "_fernet_failed", False)
    monkeypatch.setattr(dc, "TTL_S", 1200)
    return f


@needs_redis
def test_detail_cache_roundtrip_is_encrypted_with_ttl(redis_keys, fernet):
    rec = {"ID": "42", "EMAIL": "jane@example.com", "PHONE": "5551234567"}

    async def run():
        await dc.set_many({"42": rec})
        got = await dc.get_many(["42", "43"])
        r = rl._get_redis()
        raw = await r.get(f"{dc._PREFIX}42")
        ttl = await r.ttl(f"{dc._PREFIX}42")
        return got, raw, ttl

    got, raw, ttl = asyncio.run(run())
    assert got == {"42": rec}
    assert "jane@example.com" not in raw and "5551234567" not in raw
    assert json.loads(fernet.decrypt(raw.encode())) == rec
    assert 1100 < ttl <= 1200


@needs_redis
def test_detail_cache_wrong_key_or_plaintext_is_a_miss(redis_keys, fernet, monkeypatch):
    from cryptography.fernet import Fernet

    async def run():
        await dc.set_many({"1": {"ID": "1"}})
        r = rl._get_redis()
        await r.set(f"{dc._PREFIX}2", json.dumps({"ID": "2"}))  # old plaintext value
        monkeypatch.setattr(dc, "_fernet", Fernet(Fernet.generate_key()))
        return await dc.get_many(["1", "2"])

    assert asyncio.run(run()) == {}


def test_detail_cache_disabled_without_key_or_ttl(monkeypatch):
    monkeypatch.setattr(dc, "_fernet", None)
    monkeypatch.setattr(dc, "_fernet_failed", True)
    assert asyncio.run(dc.get_many(["1"])) == {}
    monkeypatch.setattr(dc, "_fernet_failed", False)
    monkeypatch.setattr(dc, "TTL_S", 0)
    assert asyncio.run(dc.get_many(["1"])) == {}


# ---- CandidatesDetail batch budget -----------------------------------------

def test_details_batch_budget_covers_semaphore_wait(monkeypatch):
    """A call queued behind another caller on the shared per-worker semaphore
    must still give up at CANDIDATES_DETAIL_MAX_TOTAL_S."""
    from core import sourcing_config
    from services import jobdiva as jd

    monkeypatch.setattr(sourcing_config, "CANDIDATES_DETAIL_MAX_TOTAL_S", 0.3, raising=False)
    monkeypatch.setattr(jd, "_BI_WORKER_SEMAPHORE", None)

    async def run():
        sem = jd._bi_worker_semaphore(1)
        await sem.acquire()  # someone else holds the only slot
        try:
            t0 = time.monotonic()
            out = await jd.jobdiva_service._fetch_candidate_details_batch("tok", ["1", "2"])
            return out, time.monotonic() - t0
        finally:
            sem.release()

    out, elapsed = asyncio.run(run())
    assert out == {}
    assert elapsed < 1.0


# ---- background context ------------------------------------------------------

class _FakeClient:
    def __init__(self, status=200):
        self.calls = 0
        self.status = status

    async def get(self, url, **kw):
        self.calls += 1
        import httpx
        return httpx.Response(self.status, request=httpx.Request("GET", url))


def test_background_context_forces_background_priority(monkeypatch):
    seen = []

    async def fake_take(background):
        seen.append(background)
        return 0.0, 0.0

    monkeypatch.setattr(rl, "_take", fake_take)

    async def run():
        await rl.acquire(1.0, priority=rl.INTERACTIVE)
        with rl.background_context():
            await rl.acquire(1.0, priority=rl.INTERACTIVE)

    asyncio.run(run())
    assert seen == [False, True]


def test_bg_get_interactive_bypasses_limiter(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("interactive bg_get must not wait for a slot")

    monkeypatch.setattr(rl, "acquire", boom)
    client = _FakeClient()
    resp = asyncio.run(rl.bg_get(client, "https://x/y", label="t"))
    assert resp.status_code == 200 and client.calls == 1


def test_bg_get_background_raises_when_no_slot(monkeypatch):
    async def no_slot(*a, **k):
        return False

    monkeypatch.setattr(rl, "acquire", no_slot)
    client = _FakeClient()

    async def run():
        with rl.background_context():
            await rl.bg_get(client, "https://x/y", label="t", max_wait_s=0.1)

    with pytest.raises(rl.SlotTimeout):
        asyncio.run(run())
    assert client.calls == 0


def test_bg_get_interactive_429_starts_shared_cooldown():
    client = _FakeClient(status=429)
    asyncio.run(rl.bg_get(client, "https://x/y", label="t"))
    assert rl.stats()["http_429"] == 1
    assert rl._local_cooldown_until > time.monotonic()


# ---- token bucket (Fix 5) ----------------------------------------------------

def test_bucket_step_math(monkeypatch):
    monkeypatch.setattr(rl, "BURST", 3.0)
    monkeypatch.setattr(rl, "BACKGROUND_BURST", 1.0)
    # full bucket: interactive takes, leaving 2
    wait, tok, ts = rl._bucket_step(3.0, 0.0, 0.0, 1.5, False, False, 0.0)
    assert wait == 0 and tok == 2.0
    # background must leave the 2-token interactive reserve: needs 3
    wait, tok, _ = rl._bucket_step(2.0, 0.0, 0.0, 1.5, True, False, 0.0)
    assert wait == pytest.approx(1.5)
    # refill: 0 tokens + 3s at 1.5s/token = 2 tokens
    wait, tok, ts = rl._bucket_step(0.0, 0.0, 3.0, 1.5, False, False, 0.0)
    assert wait == 0 and tok == pytest.approx(1.0) and ts == 3.0
    # refill caps at burst
    _, tok, _ = rl._bucket_step(0.0, 0.0, 100.0, 1.5, False, False, 0.0)
    assert tok == pytest.approx(2.0)
    # empty: wait the time to one token
    wait, _, _ = rl._bucket_step(0.25, 0.0, 0.0, 1.5, False, False, 0.0)
    assert wait == pytest.approx(1.125)
    # cooldown wins over available tokens
    wait, tok, _ = rl._bucket_step(3.0, 0.0, 0.0, 1.5, False, False, 4.0)
    assert wait == 4.0 and tok == 3.0
    # background yields to waiting interactive even with tokens
    wait, _, _ = rl._bucket_step(3.0, 0.0, 0.0, 1.5, True, True, 0.0)
    assert wait == 1.5


def test_default_throughput_matches_old_limiter():
    # burst 1: steady 1 call per MIN_INTERVAL_S, first call immediate
    assert rl.BURST == 1.0
    stamps = asyncio.run(_timed_acquires(4))
    assert all(g >= 0.045 for g in _gaps(stamps)), _gaps(stamps)


def test_burst_allows_immediate_calls_then_paces(monkeypatch):
    monkeypatch.setattr(rl, "BURST", 3.0)
    monkeypatch.setattr(rl, "MIN_INTERVAL_S", 0.2)

    async def run():
        t0 = time.monotonic()
        stamps = await _timed_acquires(4)
        return [s - t0 for s in stamps]

    stamps = asyncio.run(run())
    assert stamps[2] < 0.1 and stamps[3] >= 0.18


def test_priority_counters_track_wait_and_429():
    async def run():
        await rl.note_429("1", priority=rl.BACKGROUND)
        assert await rl.acquire(5.0, priority=rl.BACKGROUND)

    asyncio.run(run())
    bp = rl.stats()["by_priority"][rl.BACKGROUND]
    assert bp["bi_429_total"] == 1
    assert bp["bi_wait_ms"] >= 900 and bp["bi_cooldown_ms"] >= 900


@needs_redis
def test_redis_bucket_burst(redis_keys, monkeypatch):
    monkeypatch.setattr(rl, "BURST", 3.0)
    monkeypatch.setattr(rl, "MIN_INTERVAL_S", 0.2)

    async def run():
        t0 = time.monotonic()
        stamps = await _timed_acquires(4)
        return [s - t0 for s in stamps]

    stamps = asyncio.run(run())
    assert stamps[2] < 0.1 and stamps[3] >= 0.15
