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


# ---- Review follow-ups ---------------------------------------------------------

@needs_redis
def test_jobagent_cache_aliases_share_one_entry(redis_env, fernet):
    result = {"candidates": [{"id": str(i)} for i in range(5)]}

    async def run():
        await jac.register_aliases("REF-1", ("12345",))
        await jac.put("12345", result, resume_count=5)          # write via numeric
        via_ref = await jac.get("REF-1", resume_count=5)          # read via ref
        via_num = await jac.get("12345", resume_count=5)
        n = await jac.invalidate("12345")                         # edit via numeric
        gone_ref = await jac.get("REF-1", resume_count=5)
        await jac.put("REF-1", result, resume_count=5)
        n2 = await jac.invalidate("REF-1")                        # edit via ref
        gone_num = await jac.get("12345", resume_count=5)
        return via_ref, via_num, n, gone_ref, n2, gone_num

    via_ref, via_num, n, gone_ref, n2, gone_num = asyncio.run(run())
    assert via_ref and via_num and via_ref["cache_hit"]
    assert n == 1 and gone_ref is None and n2 == 1 and gone_num is None


@needs_redis
def test_jobagent_invalidate_uses_key_index_not_scan(redis_env, fernet, monkeypatch):
    async def run():
        await jac.put("J7", {"candidates": [{"id": "1"}]}, resume_count=1)
        client = rl._get_redis()

        def no_scan(*a, **k):
            raise AssertionError("SCAN used")

        monkeypatch.setattr(client, "scan_iter", no_scan)
        return await jac.invalidate("J7"), await jac.get("J7", resume_count=1)

    assert asyncio.run(run()) == (1, None)


def test_yield_budget_caps_total_pause(monkeypatch):
    async def always_busy(_job):
        return True

    monkeypatch.setattr(act, "other_job_active", always_busy)

    async def run():
        with act.yield_budget(total_s=0.3) as b:
            t0 = time.monotonic()
            for _ in range(5):
                await act.wait_while_busy("j", include_self=False, label="t", max_pause_s=0.2, poll_s=0.02)
            return time.monotonic() - t0, b.exhausted_logged

    elapsed, exhausted = asyncio.run(run())
    assert elapsed < 0.6 and exhausted


def test_yield_budget_env_default(monkeypatch):
    monkeypatch.delenv("JOBDIVA_AUTOSYNC_MAX_YIELD_S", raising=False)
    with act.yield_budget() as b:
        assert b.total_s == 300
        with act.yield_budget(total_s=1) as inner:
            assert inner is b  # one cap per run


def test_active_job_refresh_task_tracked_and_cancelled(no_redis):
    from core import tasks

    async def run():
        async with act.active_job("j1") as aj:
            inside = aj._task in tasks._TASKS
            t = aj._task
        return inside, t.done(), aj._task

    inside, done, after = asyncio.run(run())
    assert inside and done and after is None


def test_detail_cache_ttl_default_and_env(monkeypatch):
    import importlib
    from services import jobdiva_detail_cache as dc
    monkeypatch.delenv("JOBDIVA_DETAIL_CACHE_TTL_S", raising=False)
    assert importlib.reload(dc).TTL_S == 2 * 3600
    monkeypatch.setenv("JOBDIVA_DETAIL_CACHE_TTL_S", "600")
    assert importlib.reload(dc).TTL_S == 600
    monkeypatch.delenv("JOBDIVA_DETAIL_CACHE_TTL_S")
    importlib.reload(dc)


def test_contact_min_score_single_source():
    from core import config, sourcing_config
    assert sourcing_config.CONTACT_ENRICH_MIN_SCORE == config.CONTACT_ENRICH_MIN_SCORE


def test_intake_prewarm_single_flight_without_redis(no_redis, monkeypatch):
    from routers import jobs
    from services import jobdiva_jobagent_cache as cache
    monkeypatch.setattr(jobs, "_PREWARM_INFLIGHT", set())
    import core.config as cfg
    monkeypatch.setattr(cfg, "JOB_INTAKE_PREWARM_ENABLED", True, raising=False)
    calls = []

    async def fake_prewarm(job_id, aliases=()):
        calls.append((job_id, aliases))
        await asyncio.sleep(0.05)

    monkeypatch.setattr(cache, "prewarm_job", fake_prewarm)

    async def run():
        jobs._schedule_intake_prewarm(123, "REF-9")
        jobs._schedule_intake_prewarm(123, "REF-9")  # double click
        await asyncio.sleep(0.2)
        jobs._schedule_intake_prewarm(123, "REF-9")  # after completion: runs again
        await asyncio.sleep(0.2)

    asyncio.run(run())
    assert calls == [("REF-9", ("123",)), ("REF-9", ("123",))]
    assert jobs._PREWARM_INFLIGHT == set()


@needs_redis
def test_intake_prewarm_redis_lock(redis_env, monkeypatch):
    from routers import jobs
    monkeypatch.setattr(jobs, "_PREWARM_LOCK_PREFIX", f"test:{redis_env}:prewarm:")

    async def run():
        t1 = await jobs._acquire_prewarm_lock("R1")
        t2 = await jobs._acquire_prewarm_lock("R1")  # other worker
        ttl = await rl._get_redis().ttl(f"test:{redis_env}:prewarm:R1")
        await jobs._release_prewarm_lock("R1", t1)
        t3 = await jobs._acquire_prewarm_lock("R1")
        await jobs._release_prewarm_lock("R1", t3)
        return t1, t2, ttl, t3

    t1, t2, ttl, t3 = asyncio.run(run())
    assert t1 and t2 is None and 0 < ttl <= 600 and t3
