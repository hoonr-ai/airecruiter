"""Exa out-of-credits breaker (2026-10-15).

The PROD key returned 402 NO_MORE_CREDITS from 2026-10-13 19:56 UTC and
every Exa path failed open to an empty answer: Step 5 looked like a thin
market, Launch went ahead without contacts, nobody was told. Now a 402 with
a credit tag trips a breaker shared by every worker (core/vendor_limiter.EXA,
VENDOR_EXA_NO_CREDITS_TTL_S), Exa is skipped while it is open, and Step 5
reports the source as failed "Exa is out of credits".

Pins:
  - the match: 402 + NO_MORE_CREDITS / API_KEY_BUDGET_EXCEEDED /
    TEAM_BUDGET_EXCEEDED only; never X402_*, a 400 or a 429
  - TTL, Redis sharing across workers, ERROR logged once per window,
    acquire() unchanged, TTL 0 = off
  - Pass A fan-out: raises only when no query succeeded; one event per query
  - contact path: breaker open = fatal with no POST; a 402 trips it with the
    answer unchanged; sourcing skips BEFORE the per-job caps, records no miss,
    and refunds them when the breaker opens while the lookup is queued
  - Step 5: source_status "failed" with the out-of-credits reason, JobDiva
    still completes, multi-location keeps the locations that answered
"""
import asyncio
import logging
import time
import types

import pytest

from core import sourcing_config
from core import vendor_limiter as vl
from services import contact_enrichment as ce
from services import exa_service as es
from services import exa_telemetry as tele
from services.exa_errors import (
    ExaOutOfCreditsError,
    credit_tag,
    is_exa_out_of_credits,
    out_of_credits_tag,
)
from services.unified_candidate_search import SearchCriteria, UnifiedCandidateSearch
from tests.test_jobagent_quick_first import _criteria, _drive, _patch_service_for_orchestration

LINKEDIN = "https://www.linkedin.com/in/jane-doe"
NO_CREDITS_BODY = '{"requestId":"r1","error":"You have run out of credits","tag":"NO_MORE_CREDITS"}'


def _sdk_error(status, body):
    """What exa_py raises for a non-2xx answer."""
    return ValueError(f"Request failed with status code {status}: {body}")


@pytest.fixture
def events(monkeypatch):
    sent = []
    monkeypatch.setattr(tele, "_send", lambda event_type, attrs: sent.append((event_type, attrs)))
    return sent


@pytest.fixture(autouse=True)
def _fast_exa(monkeypatch):
    monkeypatch.setattr(vl.EXA, "min_interval_s", 0.0)


# ---------------------------------------------------------------------------
# The match
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tag", ["NO_MORE_CREDITS", "API_KEY_BUDGET_EXCEEDED", "TEAM_BUDGET_EXCEEDED"])
def test_402_with_a_credit_tag_matches(tag):
    assert out_of_credits_tag(_sdk_error(402, '{"tag":"%s"}' % tag)) == tag
    assert credit_tag(402, f'{{"error":"x","tag":"{tag}"}}') == tag


class _HttpErr(Exception):
    def __init__(self, status, text):
        super().__init__("http error")
        self.response = types.SimpleNamespace(status_code=status, text=text)


@pytest.mark.parametrize("exc", [
    _sdk_error(402, '{"tag":"X402_PAYMENT_REQUIRED"}'),
    _sdk_error(402, '{"tag":"X402_NO_MORE_CREDITS"}'),
    _sdk_error(402, "payment required"),  # a bare 402: not proof
    _sdk_error(400, '{"error":"dataSources not available","tag":"NO_MORE_CREDITS"}'),
    _sdk_error(429, "NO_MORE_CREDITS rate limit"),
    _HttpErr(400, NO_CREDITS_BODY),
    RuntimeError("NO_MORE_CREDITS"),  # no status at all
])
def test_other_errors_do_not_match(exc):
    assert not is_exa_out_of_credits(exc)


def test_typed_status_and_response_body_are_read():
    assert out_of_credits_tag(_HttpErr(402, NO_CREDITS_BODY)) == "NO_MORE_CREDITS"
    assert credit_tag(402, "") == "" and credit_tag(None, NO_CREDITS_BODY) == ""


# ---------------------------------------------------------------------------
# The breaker on the shared Exa limiter
# ---------------------------------------------------------------------------

def _limiter(ttl=0.3):
    return vl.VendorLimiter("exatest", no_credits_ttl_s=ttl, label="Exa")


def test_trip_opens_for_the_ttl_and_logs_once(caplog):
    lim = _limiter(ttl=0.3)

    async def go():
        assert await lim.no_credits_left() == 0
        with caplog.at_level(logging.ERROR, logger="core.vendor_limiter"):
            assert await lim.trip_no_credits("NO_MORE_CREDITS") is True
            assert await lim.trip_no_credits("NO_MORE_CREDITS") is False  # same window
        assert 0 < await lim.no_credits_left() <= 0.3
        assert await lim.acquire(1) is True  # pacing is untouched
        await asyncio.sleep(0.35)
        return await lim.no_credits_left()

    assert asyncio.run(go()) == 0
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and errors[0].startswith("Exa is OUT OF CREDITS (NO_MORE_CREDITS)")


def test_ttl_zero_switches_the_breaker_off():
    lim = _limiter(ttl=0)

    async def go():
        assert await lim.trip_no_credits("NO_MORE_CREDITS") is False
        return await lim.no_credits_left()

    assert asyncio.run(go()) == 0


def test_env_override_of_the_ttl(monkeypatch):
    monkeypatch.setenv("VENDOR_EXATEST_NO_CREDITS_TTL_S", "0")
    assert _limiter(ttl=300).no_credits_ttl_s == 0
    assert vl.EXA.no_credits_ttl_s == 300.0


class _FakeRedis:
    def __init__(self):
        self.store = {}

    async def pttl(self, key):
        exp = self.store.get(key)
        if exp is None:
            return -2
        left = int((exp - time.monotonic()) * 1000)
        return left if left > 0 else -2

    async def set(self, key, value, nx=False, px=None):
        if nx and await self.pttl(key) > 0:
            return None
        self.store[key] = time.monotonic() + px / 1000.0
        return True


def test_breaker_is_shared_across_workers_through_redis(monkeypatch, caplog):
    fake = _FakeRedis()
    monkeypatch.setattr(vl._cfg, "REDIS_URL", "redis://fake", raising=False)
    monkeypatch.setattr(vl, "_get_redis", lambda: fake)
    worker_a, worker_b = _limiter(ttl=60), _limiter(ttl=60)

    async def go():
        with caplog.at_level(logging.ERROR, logger="core.vendor_limiter"):
            assert await worker_a.trip_no_credits("API_KEY_BUDGET_EXCEEDED") is True
            # Worker B never saw the 402 but skips Exa too ...
            left = await worker_b.no_credits_left()
            # ... and a second trip on B does not log again.
            assert await worker_b.trip_no_credits("API_KEY_BUDGET_EXCEEDED") is False
        return left

    assert 59 < asyncio.run(go()) <= 60
    assert list(fake.store) == ["vendor:exatest:no_credits:local"]
    assert sum(r.levelno == logging.ERROR for r in caplog.records) == 1


# ---------------------------------------------------------------------------
# _exa_paced and the Pass A fan-out
# ---------------------------------------------------------------------------

def _result(slug):
    return types.SimpleNamespace(url=f"https://www.linkedin.com/in/{slug}", id=slug, title=f"{slug} | LinkedIn",
                                 highlights=None, text="")


class _FakeExa:
    """search_and_contents answers by which role the query mentions."""

    def __init__(self, by_role):
        self.by_role = by_role
        self.calls = []

    def search_and_contents(self, query, **kwargs):
        self.calls.append(query)
        for role, answer in self.by_role.items():
            if role in query:
                if isinstance(answer, Exception):
                    raise answer
                return types.SimpleNamespace(results=answer, cost_dollars={"total": 0.177, "contents": {"text": 0.05}})
        return types.SimpleNamespace(results=[])


def _service(by_role):
    svc = es.ExaService()
    svc.exa = _FakeExa(by_role)
    return svc


def _search(svc, titles, **kw):
    return asyncio.run(svc.search_candidates(skills=[], location="Denver, CO", limit=10, titles=titles, **kw))


def test_402_trips_the_breaker_and_the_next_call_is_never_sent(events):
    svc = _service({"Data Engineer": _sdk_error(402, NO_CREDITS_BODY)})

    with pytest.raises(ExaOutOfCreditsError) as first:
        _search(svc, ["Data Engineer"])
    assert first.value.tag == "NO_MORE_CREDITS" and not first.value.skipped

    with pytest.raises(ExaOutOfCreditsError) as second:
        _search(svc, ["Data Engineer"])
    assert second.value.skipped
    assert len(svc.exa.calls) == 1  # the breaker held the second search back
    outcomes = [a["outcome"] for t, a in events if t == "ExaCall"]
    assert outcomes == ["out_of_credits", "breaker_open"]


@pytest.mark.parametrize("error", [
    _sdk_error(429, "too many requests"),
    _sdk_error(400, '{"error":"bad"}'),
    _sdk_error(402, '{"tag":"X402_PAYMENT_REQUIRED"}'),
])
def test_other_failures_do_not_trip_and_still_fail_open(error):
    svc = _service({"Data Engineer": error})

    assert _search(svc, ["Data Engineer"]) == []
    assert asyncio.run(vl.EXA.no_credits_left()) == 0


def test_fanout_raises_when_every_query_failed_and_one_for_credits(monkeypatch):
    calls = {"n": 0}
    real = es._exa_paced

    async def _paced(loop, fn, *args, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise asyncio.TimeoutError()
        return await real(loop, fn, *args, **kw)

    monkeypatch.setattr(es, "_exa_paced", _paced)
    svc = _service({"Data Engineer": _sdk_error(402, NO_CREDITS_BODY), "Data Analyst": _sdk_error(402, NO_CREDITS_BODY)})

    with pytest.raises(ExaOutOfCreditsError):
        _search(svc, ["Data Engineer", "Data Analyst"])


def test_fanout_keeps_the_rows_it_got_when_only_some_queries_ran_out(events):
    svc = _service({"Data Engineer": [_result("alice"), _result("bob")],
                    "Data Analyst": _sdk_error(402, NO_CREDITS_BODY)})

    rows = _search(svc, ["Data Engineer", "Data Analyst"])

    assert {r["id"] for r in rows} == {"exa_linkedin.com/in/alice", "exa_linkedin.com/in/bob"}
    calls = [a for t, a in events if t == "ExaCall"]
    assert len(calls) == 2  # one per query
    ok = next(a for a in calls if a["outcome"] == "ok")
    assert ok["surface"] == "pass_a" and ok["endpoint"] == "search" and ok["num_results"] == 10
    assert ok["n_results"] == 2 and ok["cost_total"] == 0.177 and ok["cost_pages"] == 0.05


def test_dice_raises_too_and_is_its_own_surface(events):
    svc = _service({"Data Engineer": _sdk_error(402, NO_CREDITS_BODY)})

    with pytest.raises(ExaOutOfCreditsError):
        asyncio.run(svc.search_dice_candidates(skills=[], location="Denver, CO", limit=10, titles=["Data Engineer"]))
    assert [a["surface"] for t, a in events if t == "ExaCall"] == ["dice"]


# ---------------------------------------------------------------------------
# Contact path
# ---------------------------------------------------------------------------

class _Client:
    posts = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        _Client.posts.append(json)
        return types.SimpleNamespace(status_code=402, text=NO_CREDITS_BODY, headers={}, json=lambda: {})


@pytest.fixture
def contact_env(monkeypatch):
    _Client.posts = []
    monkeypatch.setattr(ce, "EXA_API_KEY", "test-key")
    monkeypatch.setattr(ce, "EXA_CONTACT_ENRICH_ENABLED", True)
    monkeypatch.setattr(ce, "EXA_CONTACT_ENRICH_RETRY", True)
    monkeypatch.setattr(ce.httpx, "AsyncClient", _Client)


def test_contact_402_trips_the_breaker_with_the_answer_unchanged(contact_env):
    first = asyncio.run(ce.exa_enrich_by_linkedin("c1", LINKEDIN, "Jane Doe", fields=("phone",)))
    second = asyncio.run(ce.exa_enrich_by_linkedin("c2", LINKEDIN, "Jane Doe", fields=("phone",)))

    assert first == {"ok": False, "message": "Exa create error (402)", "fatal": True}
    assert second == {"ok": False, "message": "Exa out of credits", "fatal": True, "skipped": True}
    assert len(_Client.posts) == 1  # fatal: no retry, and nothing sent while open
    assert asyncio.run(vl.EXA.no_credits_left()) > 0


def test_sourcing_skips_exa_before_the_caps_and_records_no_miss(contact_env, monkeypatch):
    monkeypatch.setattr(sourcing_config, "EXA_SOURCING_CONTACT_FALLBACK", True)
    monkeypatch.setattr(sourcing_config, "CONTACT_LOOKUP_PROVIDERS", ("exa",))
    recorded = []

    async def _get(url):
        return {}

    async def _record(url, **kw):
        recorded.append(kw)

    from services import contact_cache

    monkeypatch.setattr(contact_cache, "get", _get)
    monkeypatch.setattr(contact_cache, "record", _record)
    ce.reset_job_counter("job-dry", include_lifetime=True)
    asyncio.run(vl.EXA.trip_no_credits("NO_MORE_CREDITS"))

    res = asyncio.run(ce.enrich_contact_for_sourcing(
        LINKEDIN, "job-dry", full_name="Jane Doe", include_exa=True, want_phone=False,
    ))

    assert res == {}
    assert _Client.posts == []
    assert "job-dry" not in ce._JOB_EXA_COUNTERS and "job-dry" not in ce._JOB_EXA_LIFETIME
    assert recorded and all(not kw.get("missed") for kw in recorded)


def test_sourcing_refunds_the_caps_when_the_breaker_opens_while_queued(contact_env, monkeypatch):
    # Counted, then queued on the Exa semaphore while another lookup's 402
    # trips the breaker: nothing is sent, so both slots come back.
    monkeypatch.setattr(sourcing_config, "EXA_SOURCING_CONTACT_FALLBACK", True)
    monkeypatch.setattr(sourcing_config, "CONTACT_LOOKUP_PROVIDERS", ("exa",))
    recorded = []

    async def _get(url):
        return {}

    async def _record(url, **kw):
        recorded.append(kw)

    from services import contact_cache

    monkeypatch.setattr(contact_cache, "get", _get)
    monkeypatch.setattr(contact_cache, "record", _record)

    class _TripsWhileQueued:
        released = 0

        async def acquire(self):
            await vl.EXA.trip_no_credits("NO_MORE_CREDITS")
            return True

        def release(self):
            _TripsWhileQueued.released += 1

    monkeypatch.setattr(ce, "_exa_semaphore", _TripsWhileQueued)
    ce.reset_job_counter("job-q", include_lifetime=True)
    monkeypatch.setitem(ce._JOB_EXA_LIFETIME, "job-q", 7)

    res = asyncio.run(ce.enrich_contact_for_sourcing(
        LINKEDIN, "job-q", full_name="Jane Doe", include_exa=True, want_phone=False,
    ))

    assert res == {}
    assert _Client.posts == []
    assert ce._JOB_EXA_COUNTERS.get("job-q", 0) == 0 and ce._JOB_EXA_LIFETIME["job-q"] == 7
    assert recorded and all(not kw.get("missed") for kw in recorded)
    assert _TripsWhileQueued.released == 1                     # the slot is given back once
    ce.reset_job_counter("job-q", include_lifetime=True)


# ---------------------------------------------------------------------------
# Step 5
# ---------------------------------------------------------------------------

def _statuses(events_):
    return {ev["data"]["source"]: ev["data"] for ev in events_ if ev.get("type") == "source_status"}


@pytest.mark.parametrize("source, label", [("Exa", "LinkedIn-Exa"), ("Dice", "Dice")])
def test_step5_says_out_of_credits_and_jobdiva_still_completes(monkeypatch, source, label):
    monkeypatch.setattr(sourcing_config, "EXA_AGENT_ENABLED", False)
    svc = UnifiedCandidateSearch()
    _patch_service_for_orchestration(svc, {"calls": [], "full_returned": False})

    async def _out_of_credits(criteria):
        raise ExaOutOfCreditsError("NO_MORE_CREDITS")

    setattr(svc, "_search_exa" if source == "Exa" else "_search_dice", _out_of_credits)

    stream = _drive(svc, _criteria(sources=["JobDiva-JobAgent", source]))

    status = _statuses(stream)
    assert status[label]["status"] == "failed"
    assert status[label]["reason"] == (
        f"Exa is out of credits — {label} results are missing until it is topped up."
    )
    assert status["JobDiva-JobAgent"]["status"] == "ok"
    assert any(ev.get("type") == "summary" for ev in stream)


def test_step5_pass_b_still_starts_when_pass_a_is_out_of_credits(monkeypatch):
    monkeypatch.setattr(sourcing_config, "EXA_AGENT_ENABLED", True)
    svc = UnifiedCandidateSearch()
    _patch_service_for_orchestration(svc, {"calls": [], "full_returned": False})
    started = []

    async def _out_of_credits(criteria):
        raise ExaOutOfCreditsError("NO_MORE_CREDITS")

    async def _deep(**kw):
        started.append(time.monotonic())
        return []

    svc._search_exa = _out_of_credits
    svc.exa_service = types.SimpleNamespace(deep_research_candidates=_deep)

    t0 = time.monotonic()
    _drive(svc, _criteria(sources=["Exa"]))

    assert started and started[0] - t0 < 10  # exa_pass_a_done was set, no 120 s wait


def test_multi_location_keeps_the_locations_that_answered():
    svc = UnifiedCandidateSearch()
    criteria = SearchCriteria(job_id="J", sources=["Exa"], location="Denver, CO",
                              additional_locations=[{"value": "Austin, TX", "within_miles": 25}])
    variants = svc._location_criteria_variants(criteria)
    assert len(variants) == 2

    async def single(variant):
        if variant.location == variants[0].location:
            return {"candidates": [{"id": "a", "profile_url": "https://www.linkedin.com/in/a"}]}
        raise ExaOutOfCreditsError("NO_MORE_CREDITS")

    res = asyncio.run(svc._fan_out_multi_location_search(criteria, single, "LinkedIn-Exa"))
    assert [c["id"] for c in res["candidates"]] == ["a"]

    async def none(variant):
        raise ExaOutOfCreditsError("NO_MORE_CREDITS")

    with pytest.raises(ExaOutOfCreditsError):
        asyncio.run(svc._fan_out_multi_location_search(criteria, none, "LinkedIn-Exa"))
