"""Circuit breaker for JobDiva endpoints that fail persistently (e.g. 404)."""
import asyncio

import pytest

from services import jobdiva_endpoint_breaker as eb
from services import jobdiva_rate_limit as rl


class FakeRedis:
    def __init__(self):
        self.store = {}

    async def pttl(self, key):
        return self.store.get(key, (None, -2))[1]

    async def set(self, key, value, px=None):
        self.store[key] = (value, px)


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    monkeypatch.setattr(rl, "_get_redis", lambda: None)
    eb._breakers.clear()


def test_opens_after_threshold_consecutive_failures():
    b = eb.EndpointBreaker("t", threshold=3, open_s=60)

    async def go():
        for _ in range(2):
            await b.record_failure(404)
        assert not await b.is_open()
        await b.record_failure(404)
        assert await b.is_open()

    asyncio.run(go())


def test_success_resets_failure_count():
    b = eb.EndpointBreaker("t", threshold=2, open_s=60)

    async def go():
        await b.record_failure(404)
        b.record_success()
        await b.record_failure(404)
        assert not await b.is_open()

    asyncio.run(go())


def test_open_state_shared_via_redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(rl, "_get_redis", lambda: fake)
    worker_a = eb.EndpointBreaker("shared", threshold=1, open_s=60)
    worker_b = eb.EndpointBreaker("shared", threshold=1, open_s=60)

    async def go():
        await worker_a.record_failure(404)
        assert fake.store["jobdiva:breaker:shared"][1] == 60_000
        assert await worker_b.is_open()

    asyncio.run(go())


def test_get_job_submittals_skips_call_when_open(monkeypatch):
    from services.jobdiva import JobDivaService

    svc = JobDivaService.__new__(JobDivaService)
    called = {"auth": 0}

    async def fake_auth():
        called["auth"] += 1
        return None

    svc.authenticate = fake_auth
    b = eb.get("JobSubmittalsDetail")
    b._open_until = float("inf")

    assert asyncio.run(svc.get_job_submittals("123", none_on_error=True)) is None
    assert asyncio.run(svc.get_job_submittals("123")) == []
    assert called["auth"] == 0
