"""Shared JobDiva BI limiter: pacing, 429 cooldown, budget (in-process fallback)."""
import asyncio
import time

import pytest

from services import jobdiva_rate_limit as rl


@pytest.fixture(autouse=True)
def _local_only(monkeypatch):
    monkeypatch.setattr(rl, "_get_redis", lambda: None)
    monkeypatch.setattr(rl, "MIN_INTERVAL_S", 0.05)
    monkeypatch.setattr(rl, "_local_next", 0.0)
    monkeypatch.setattr(rl, "_local_cooldown_until", 0.0)
    monkeypatch.setattr(rl, "_local_lock", asyncio.Lock())


def test_acquire_spaces_concurrent_callers():
    async def run():
        stamps = []

        async def one():
            assert await rl.acquire(5.0)
            stamps.append(time.monotonic())

        await asyncio.gather(*[one() for _ in range(4)])
        return sorted(stamps)

    stamps = asyncio.run(run())
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert all(g >= 0.04 for g in gaps), gaps


def test_acquire_gives_up_past_budget():
    async def run():
        await rl.note_429("1")
        return await rl.acquire(0.1)

    assert asyncio.run(run()) is False


def test_cooldown_delays_every_caller():
    async def run():
        await rl.note_429("1")
        t0 = time.monotonic()
        assert await rl.acquire(5.0)
        return time.monotonic() - t0

    assert asyncio.run(run()) >= 0.9


def test_retry_after_parsing_and_cap(monkeypatch):
    assert rl._parse_retry_after("7") == 7.0
    assert rl._parse_retry_after("garbage") is None
    monkeypatch.setattr(rl, "COOLDOWN_CAP_S", 3.0)
    assert asyncio.run(rl.note_429("600")) == 3.0


def test_missing_retry_after_uses_jitter_range(monkeypatch):
    monkeypatch.setattr(rl, "COOLDOWN_MIN_S", 2.0)
    monkeypatch.setattr(rl, "COOLDOWN_MAX_S", 4.0)
    secs = asyncio.run(rl.note_429(None))
    assert 2.0 <= secs <= 4.0
