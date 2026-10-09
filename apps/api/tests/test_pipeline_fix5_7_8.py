"""Pipeline optimization Fix 5 (activity markers), Fix 7 (JobAgent cache),
Fix 8 (contact score floor). Redis tests use the test Redis (db 15) with
unique key prefixes and skip when none is reachable."""
import asyncio
import time
import uuid

import pytest

from services import jobdiva_activity as act
from services import jobdiva_jobagent_cache as jac
from services import jobdiva_rate_limit as rl

from tests.test_jobdiva_rate_limit import TEST_REDIS_URL, needs_redis


@pytest.fixture
def redis_env(monkeypatch):
    import sys
    import redis
    # Another test may have stubbed the redis package in sys.modules.
    if not hasattr(redis, "Redis") or not hasattr(sys.modules.get("redis.asyncio"), "from_url"):
        pytest.skip("redis package stubbed by another test")
    tag = uuid.uuid4().hex
    monkeypatch.setattr(rl._cfg, "REDIS_URL", TEST_REDIS_URL, raising=False)
    monkeypatch.setattr(rl, "_redis_client", None)
    monkeypatch.setattr(rl, "_redis_down_until", 0.0)
    monkeypatch.setattr(act, "_PREFIX", f"test:{tag}:active:")
    monkeypatch.setattr(act, "_INDEX_KEY", f"test:{tag}:active:index")
    monkeypatch.setattr(jac, "_PREFIX", f"test:{tag}:ja:")
    yield tag
    import redis
    r = redis.Redis.from_url(TEST_REDIS_URL)
    for key in r.scan_iter(f"test:{tag}:*"):
        r.delete(key)


@pytest.fixture
def no_redis(monkeypatch):
    monkeypatch.setattr(rl._cfg, "REDIS_URL", "", raising=False)
    monkeypatch.setattr(rl, "_redis_client", None)


# ---- Fix 5: activity markers --------------------------------------------------

def test_activity_noop_without_redis(no_redis):
    async def run():
        await act.mark_job_active("j1")
        return await act.is_job_active("j1"), await act.other_job_active("x")

    assert asyncio.run(run()) == (False, False)


@needs_redis
def test_mark_clear_and_ttl(redis_env, monkeypatch):
    monkeypatch.setattr(rl._cfg, "JOBDIVA_ACTIVE_JOB_TTL_S", 30, raising=False)

    async def run():
        await act.mark_job_active("j1")
        ttl = await rl._get_redis().ttl(act._key("j1"))
        a = await act.is_job_active("j1")
        await act.clear_job_active("j1")
        return ttl, a, await act.is_job_active("j1")

    ttl, before, after = asyncio.run(run())
    assert 25 < ttl <= 30 and before is True and after is False


@needs_redis
def test_hydration_ignores_own_job_but_yields_to_others(redis_env):
    async def run():
        await act.mark_job_active("mine")
        # own marker only: hydration (include_self=False) does not pause
        t0 = time.monotonic()
        await act.wait_while_busy("mine", include_self=False, label="t", max_pause_s=1, poll_s=0.05)
        own = time.monotonic() - t0
        # AutoSync (include_self=True) pauses on its own job
        t0 = time.monotonic()
        await act.wait_while_busy("mine", include_self=True, label="t", max_pause_s=0.3, poll_s=0.05)
        autosync = time.monotonic() - t0
        # another job active: hydration pauses until it clears
        await act.mark_job_active("other")

        async def clear_later():
            await asyncio.sleep(0.3)
            await act.clear_job_active("other")

        t0 = time.monotonic()
        await asyncio.gather(
            act.wait_while_busy("mine", include_self=False, label="t", max_pause_s=5, poll_s=0.05),
            clear_later(),
        )
        other = time.monotonic() - t0
        return own, autosync, other

    own, autosync, other = asyncio.run(run())
    assert own < 0.1
    assert autosync >= 0.3
    assert 0.25 <= other < 1.0


@needs_redis
def test_active_job_context_clears_on_exit(redis_env):
    async def run():
        async with act.active_job("j9"):
            inside = await act.is_job_active("j9")
        return inside, await act.is_job_active("j9")

    assert asyncio.run(run()) == (True, False)


# ---- Fix 7: JobAgent cache ----------------------------------------------------

@pytest.fixture
def fernet(monkeypatch):
    from cryptography.fernet import Fernet
    from services import jobdiva_detail_cache as dc
    monkeypatch.setattr(dc, "_fernet", Fernet(Fernet.generate_key()))
    monkeypatch.setattr(dc, "_fernet_failed", False)


def test_criteria_hash_is_canonical():
    assert jac.criteria_hash({"a": 1, "b": 2}) == jac.criteria_hash({"b": 2, "a": 1})
    assert jac.cache_key("J1", True) != jac.cache_key("J1", False)


@needs_redis
def test_jobagent_cache_hit_slices_and_invalidates(redis_env, fernet):
    result = {"candidates": [{"id": str(i), "email": "x@y.z"} for i in range(10)],
              "criteria_unconfigured": False}

    async def run():
        assert await jac.get("J1", resume_count=5) is None
        await jac.put("J1", result, resume_count=10)
        hit = await jac.get("J1", resume_count=4)
        too_big = await jac.get("J1", resume_count=20)
        raw = await rl._get_redis().get(jac.cache_key("J1"))
        n = await jac.invalidate("J1")
        gone = await jac.get("J1", resume_count=4)
        return hit, too_big, raw, n, gone

    hit, too_big, raw, n, gone = asyncio.run(run())
    assert hit["cache_hit"] and [c["id"] for c in hit["candidates"]] == ["0", "1", "2", "3"]
    assert too_big is None
    assert "x@y.z" not in raw  # encrypted
    assert n == 1 and gone is None


@needs_redis
def test_jobagent_cache_skips_empty_and_unconfigured(redis_env, fernet):
    async def run():
        await jac.put("J2", {"candidates": []}, resume_count=10)
        await jac.put("J2", {"candidates": [{"id": "1"}], "criteria_unconfigured": True}, resume_count=10)
        return await jac.get("J2", resume_count=1)

    assert asyncio.run(run()) is None


def test_prewarm_disabled_by_flag(monkeypatch):
    monkeypatch.setattr(jac._cfg, "JOB_INTAKE_PREWARM_ENABLED", False, raising=False)
    called = []
    from services import jobdiva as jd

    async def boom(*a, **k):
        called.append(1)

    monkeypatch.setattr(jd.jobdiva_service, "search_via_job_agent", boom)
    asyncio.run(jac.prewarm_job("J3"))
    assert called == []


# ---- Fix 8: contact score floor -------------------------------------------------

def test_contact_score_floor(monkeypatch):
    from core import sourcing_config as sc
    from services import unified_candidate_search as ucs
    monkeypatch.setattr(sc, "CONTACT_ENRICH_MIN_SCORE", 60)
    assert ucs._below_contact_score_floor({"match_score": 59.9}) is True
    assert ucs._below_contact_score_floor({"match_score": 60}) is False
    assert ucs._below_contact_score_floor({"match_score": None}) is False  # caller policy


def test_apply_contact_enrichment_skips_low_score(monkeypatch):
    from services import unified_candidate_search as ucs
    from services import contact_enrichment as ce
    calls = []

    async def fake_enrich(*a, **k):
        calls.append(a)
        return {"workEmail": "a@b.c"}

    monkeypatch.setattr(ce, "enrich_contact_for_sourcing", fake_enrich)
    svc = ucs.UnifiedCandidateSearch.__new__(ucs.UnifiedCandidateSearch)

    class C:
        job_id = "J"

    low = {"profile_url": "https://linkedin.com/in/x", "match_score": 10, "source": "LinkedIn-Exa"}
    high = {"profile_url": "https://linkedin.com/in/y", "match_score": 90, "source": "LinkedIn-Exa"}
    asyncio.run(svc._apply_contact_enrichment(low, C(), overwrite=True))
    asyncio.run(svc._apply_contact_enrichment(high, C(), overwrite=True))
    assert len(calls) == 1 and high["email"] == "a@b.c" and "email" not in low


def test_provider_semaphore_is_eight():
    from services import contact_enrichment as ce
    assert ce._PROVIDER_SEMAPHORE._value == 8
