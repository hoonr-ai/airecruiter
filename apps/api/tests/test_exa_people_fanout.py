"""Exa Pass A / Dice fan-out: per-query sizing and the search cache
(ExaService._people_search_fanout, 2026-10-15).

Each title query used to buy all 50 results and the merge kept 50 of 150;
the sample, full, relaxation and resumed runs bought the same searches
again. Now each query buys ceil(limit * F / queries) and is served from the
Redis search cache when it can.

Pins:
  - the per-query num_results table and what the SDK is asked (contents
    spec, category, type), F=0 and one query keep the old size
  - a cache hit skips the SDK, the Exa limiter, the out-of-credits breaker
    and ExaCall; ExaCall fires once per paid call (cache=miss|off)
  - a failed query is not cached while the others are; hit and miss runs
    give the same candidates
  - out of credits: rows from any query (cached or live) are returned, the
    error is raised only when there are none; ExaPeopleFanout on both paths
  - identical in-flight searches in one process are bought once (a bigger
    ask does not join a smaller one); a request already sent still reaches
    the cache when the caller is cancelled, one not yet sent is not bought
  - the cache failing (get or put) never fails the search
  - ExaPeopleFanout attributes survive safe_attributes; starved /
    cf15_same / deepest_rank / dup_rows
  - the round-robin merge cases ported from test_exa_query_builder.py
"""
import asyncio
import math
import time
import types

import pytest

from core import sourcing_config
from core import vendor_limiter as vl
from services import exa_search_cache as cache
from services import exa_service as es
from services import exa_telemetry as tele
from services.exa_errors import ExaOutOfCreditsError
from tests.test_exa_search_cache import redis_on  # noqa: F401 (fixture)

NO_CREDITS = ValueError(
    'Request failed with status code 402: {"error":"out of credits","tag":"NO_MORE_CREDITS"}'
)
COST = {"total": 0.05, "search": 0.04, "contents": {"text": 0.005, "highlights": 0.005}}


@pytest.fixture
def events(monkeypatch):
    sent = []
    monkeypatch.setattr(tele, "_send", lambda event_type, attrs: sent.append((event_type, attrs)))
    return sent


@pytest.fixture(autouse=True)
def _fast_exa_and_shipped_knobs(monkeypatch):
    from core import config as core_config

    monkeypatch.setattr(vl.EXA, "min_interval_s", 0.0)
    # Both knobs come from apps/api/.env when set there: pin the defaults.
    monkeypatch.setattr(sourcing_config, "EXA_PEOPLE_PER_QUERY_FACTOR", 2.0)
    monkeypatch.setattr(core_config, "EXA_SEARCH_CACHE_TTL_S", 43200, raising=False)


def _calls(events, **match):
    return [a for t, a in events if t == "ExaCall" and all(a.get(k) == v for k, v in match.items())]


def _fanouts(events):
    return [a for t, a in events if t == "ExaPeopleFanout"]


def _res(slug, rid=None, title=None):
    return types.SimpleNamespace(
        url=f"https://www.linkedin.com/in/{slug}", id=rid or f"id-{slug}",
        title=title or f"{slug.title()} Person - Engineer", text="Denver, CO", highlights=["python"],
    )


def _people(prefix, count):
    return [_res(f"{prefix}{i}") for i in range(count)]


class _RecordingExa:
    """search_and_contents answers by which role the query mentions and
    records (query, kwargs). An answer may be an exception or a callable."""

    def __init__(self, by_role):
        self.by_role = by_role
        self.calls = []

    def search_and_contents(self, query, **kwargs):
        self.calls.append((query, kwargs))
        for role, answer in self.by_role.items():
            if role in query:
                if callable(answer) and not isinstance(answer, list):
                    answer = answer()
                if isinstance(answer, Exception):
                    raise answer
                return types.SimpleNamespace(results=list(answer)[: kwargs["num_results"]], cost_dollars=COST)
        return types.SimpleNamespace(results=[], cost_dollars=COST)


def _service(by_role):
    svc = es.ExaService()
    svc.exa = _RecordingExa(by_role)
    return svc


def _search(svc, titles, limit=50):
    return asyncio.run(svc.search_candidates(skills=[], location="Denver, CO", limit=limit, titles=titles))


THREE = ["Data Engineer", "Data Analyst", "Product Manager"]


def _three(count=40):
    return {"Data Engineer": _people("e", count), "Data Analyst": _people("a", count),
            "Product Manager": _people("m", count)}


# ---------------------------------------------------------------------------
# Sizing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("limit, queries, factor, expected", [
    (50, 3, 2.0, 34),
    (50, 2, 2.0, 50),
    (50, 1, 2.0, 50),
    (50, 3, 0.0, 50),       # 0 = the old size
    (50, 3, 1.5, 25),
    (50, 5, 2.0, 20),
    (50, 12, 2.0, 10),      # never below Exa's 10-result base price
    (10, 3, 2.0, 10),
    (3, 3, 2.0, 3),         # never above the limit
    (50, 3, float("nan"), 50),
    (50, 3, float("inf"), 50),
    (50, 0, 2.0, 50),
])
def test_per_query_num_results(limit, queries, factor, expected):
    assert es._per_query_num_results(limit, queries, factor) == expected


def test_three_titles_buy_34_each_with_the_shared_contents_spec():
    svc = _service(_three())

    rows = _search(svc, THREE)

    assert len(svc.exa.calls) == 3 and len(rows) == 50
    for _query, kwargs in svc.exa.calls:
        assert kwargs == {
            "category": "people", "type": "auto", "num_results": 34,
            "text": {"max_characters": 1000}, "highlights": {"max_characters": 4000},
        }
    assert es._PEOPLE_CONTENTS == {"text": {"max_characters": 1000}, "highlights": {"max_characters": 4000}}


def test_one_title_and_factor_zero_keep_the_old_size(monkeypatch):
    svc = _service(_three())
    _search(svc, ["Data Engineer"])
    assert [k["num_results"] for _q, k in svc.exa.calls] == [50]

    monkeypatch.setattr(sourcing_config, "EXA_PEOPLE_PER_QUERY_FACTOR", 0.0)
    svc = _service(_three())
    _search(svc, THREE)
    assert [k["num_results"] for _q, k in svc.exa.calls] == [50, 50, 50]


def test_dice_is_sized_the_same_and_scoped_to_dice(events):
    svc = _service(_three())
    asyncio.run(svc.search_dice_candidates(skills=[], location="Denver, CO", limit=50, titles=THREE))
    assert {k["num_results"] for _q, k in svc.exa.calls} == {34}
    assert {tuple(k["include_domains"]) for _q, k in svc.exa.calls} == {("dice.com",)}
    assert {a["surface"] for a in _calls(events)} == {"dice"}
    assert _fanouts(events)[0]["surface"] == "dice"


@pytest.mark.parametrize("bad", ["abc", "nan", "-1", "inf"])
def test_a_malformed_factor_env_falls_back_to_the_default(monkeypatch, bad):
    monkeypatch.setenv("EXA_PEOPLE_PER_QUERY_FACTOR", bad)
    assert sourcing_config._env_factor("EXA_PEOPLE_PER_QUERY_FACTOR", 2.0) == 2.0
    monkeypatch.setenv("EXA_PEOPLE_PER_QUERY_FACTOR", "1.5")
    assert sourcing_config._env_factor("EXA_PEOPLE_PER_QUERY_FACTOR", 2.0) == 1.5


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def test_a_hit_skips_the_sdk_the_limiter_the_breaker_and_exacall(redis_on, events, monkeypatch):
    acquired = []
    real_acquire = vl.EXA.acquire

    async def _acquire(*a, **kw):
        acquired.append(1)
        return await real_acquire(*a, **kw)

    monkeypatch.setattr(vl.EXA, "acquire", _acquire)
    svc = _service(_three())

    first = _search(svc, THREE)
    assert len(svc.exa.calls) == 3 and len(acquired) == 3
    assert len(_calls(events, outcome="ok", cache="miss")) == 3
    assert len(redis_on.store) == 3

    second = _search(svc, THREE)
    assert second == first  # same candidates from a hit as from the paid call
    assert len(svc.exa.calls) == 3 and len(acquired) == 3
    assert len(_calls(events)) == 3  # no ExaCall for hits

    asyncio.run(vl.EXA.trip_no_credits("NO_MORE_CREDITS"))
    assert _search(svc, THREE) == first  # served while Exa is out of credits
    assert len(svc.exa.calls) == 3

    first_ev, second_ev = _fanouts(events)[:2]
    assert (first_ev["cache_misses"], first_ev["cache_hits"]) == (3, 0)
    assert first_ev["cost_usd"] == pytest.approx(0.15) and first_ev["cache_state"] == "on"
    assert (second_ev["cache_misses"], second_ev["cache_hits"]) == (0, 3)
    assert second_ev["cost_usd"] == 0 and second_ev["per_query"] == 34


def test_a_sample_size_ask_is_served_from_the_full_size_entry(redis_on, events):
    svc = _service(_three())
    _search(svc, THREE, limit=50)
    rows = _search(svc, ["Data Engineer"], limit=12)
    assert len(rows) == 12 and len(svc.exa.calls) == 3


def test_with_the_cache_off_every_call_is_paid_and_tagged_off(events):
    svc = _service(_three())
    _search(svc, THREE)
    _search(svc, THREE)
    assert len(svc.exa.calls) == 6
    assert len(_calls(events, cache="off")) == 6
    assert {e["cache_state"] for e in _fanouts(events)} == {"off_config"}


def test_a_failed_query_is_not_cached_while_the_others_are(redis_on, events):
    answers = _three()
    answers["Product Manager"] = RuntimeError("boom")
    svc = _service(answers)

    rows = _search(svc, THREE)
    assert len(rows) == 50 and len(redis_on.store) == 2
    assert _fanouts(events)[0]["failed"] == 1

    svc.exa.by_role["Product Manager"] = _people("m", 40)
    _search(svc, THREE)
    assert [q for q, _k in svc.exa.calls[3:]] == ["Product Manager, based in Denver, CO"]


def test_two_hits_and_one_out_of_credits_return_the_rows(redis_on, events):
    answers = _three()
    answers["Product Manager"] = RuntimeError("boom")
    svc = _service(answers)
    _search(svc, THREE)

    svc.exa.by_role["Product Manager"] = NO_CREDITS
    rows = _search(svc, THREE)

    assert len(rows) == 50
    ev = _fanouts(events)[-1]
    assert (ev["cache_hits"], ev["cache_misses"], ev["failed"], ev["ooc_failed"]) == (2, 1, 1, 1)
    assert _calls(events, outcome="out_of_credits")


def test_every_query_out_of_credits_raises_and_still_records_the_fanout(events):
    svc = _service({role: NO_CREDITS for role in THREE})

    with pytest.raises(ExaOutOfCreditsError):
        asyncio.run(svc._people_search_fanout([f"{r}, based in Denver, CO" for r in THREE], 50, surface="pass_a"))

    [ev] = _fanouts(events)
    assert ev["failed"] == 3 and ev["ooc_failed"] == 3 and ev["kept_rows"] == 0
    assert ev["cost_usd"] == 0 and ev["cache_misses"] == 3


def test_no_rows_and_one_query_out_of_credits_raises(events):
    # One query answered with nothing, one ran out: "out of credits", not "no matches".
    svc = _service({"Data Engineer": [], "Data Analyst": NO_CREDITS})
    with pytest.raises(ExaOutOfCreditsError):
        asyncio.run(svc._people_search_fanout(
            ["Data Engineer, based in Denver, CO", "Data Analyst, based in Denver, CO"], 50))


def test_a_cache_get_that_fails_still_calls_exa(redis_on, events):
    redis_on.fail["get"] = RuntimeError("boom")
    svc = _service(_three())
    assert len(_search(svc, THREE)) == 50
    assert len(svc.exa.calls) == 3


def test_a_cache_put_that_raises_still_returns_the_rows(redis_on, events, monkeypatch):
    async def _boom(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(cache, "put", _boom)
    svc = _service(_three())
    assert len(_search(svc, THREE)) == 50
    assert len(_calls(events, outcome="ok")) == 3


# ---------------------------------------------------------------------------
# Single flight and cancellation
# ---------------------------------------------------------------------------

def _gated(monkeypatch, send_first=False):
    """_exa_paced that waits on a gate (before or after sending)."""
    gate = {"event": None, "entered": 0}

    async def _paced(loop, fn, *args, **kw):
        if gate["event"] is None:
            gate["event"] = asyncio.Event()
        gate["entered"] += 1
        if send_first:
            resp = fn(*args)
            await gate["event"].wait()
            return resp
        await gate["event"].wait()
        return fn(*args)

    monkeypatch.setattr(es, "_exa_paced", _paced)
    return gate


async def _until(cond, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "condition never held"
        await asyncio.sleep(0.005)


def test_identical_concurrent_searches_are_bought_once(events, monkeypatch):
    gate = _gated(monkeypatch)
    svc = _service(_three())
    q = ["Data Engineer, based in Denver, CO"]

    async def go():
        a = asyncio.create_task(svc._people_search_fanout(q, 50))
        await _until(lambda: gate["entered"] == 1)
        b = asyncio.create_task(svc._people_search_fanout(q, 12))  # smaller ask joins
        c = asyncio.create_task(svc._people_search_fanout(q, 50))
        await asyncio.sleep(0.02)
        gate["event"].set()
        return await asyncio.gather(a, b, c)

    a, b, c = asyncio.run(go())
    assert len(svc.exa.calls) == 1 and len(_calls(events)) == 1
    assert [r.id for r in b] == [r.id for r in a[:12]] and c == a
    assert sorted(e["inflight_joins"] for e in _fanouts(events)) == [0, 1, 1]
    assert not es._inflight


def test_a_bigger_ask_does_not_join_a_smaller_flight(events, monkeypatch):
    gate = _gated(monkeypatch)
    svc = _service(_three())
    q = ["Data Engineer, based in Denver, CO"]

    async def go():
        a = asyncio.create_task(svc._people_search_fanout(q, 12))
        await _until(lambda: gate["entered"] == 1)
        b = asyncio.create_task(svc._people_search_fanout(q, 50))
        await _until(lambda: gate["entered"] == 2)
        gate["event"].set()
        return await asyncio.gather(a, b)

    a, b = asyncio.run(go())
    assert [k["num_results"] for _q, k in svc.exa.calls] == [12, 50]
    assert len(a) == 12 and len(b) == 40


def test_a_request_already_sent_reaches_the_cache_after_the_caller_left(redis_on, events, monkeypatch):
    gate = _gated(monkeypatch, send_first=True)
    svc = _service(_three())
    q = ["Data Engineer, based in Denver, CO"]

    async def go():
        task = asyncio.create_task(svc._people_search_fanout(q, 50))
        await _until(lambda: gate["entered"] == 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert es._inflight  # still buying
        gate["event"].set()
        await _until(lambda: not es._inflight)

    asyncio.run(go())
    assert len(redis_on.store) == 1
    [call] = _calls(events)
    assert call["outcome"] == "ok" and call["cost_total"] == 0.05


def test_a_request_not_yet_sent_is_not_bought_when_the_caller_left(redis_on, events, monkeypatch):
    gate = _gated(monkeypatch)
    svc = _service(_three())

    async def go():
        task = asyncio.create_task(svc._people_search_fanout(["Data Engineer, based in Denver, CO"], 50))
        await _until(lambda: gate["entered"] == 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not es._inflight  # dropped at once: a later caller leads afresh, never joins it
        await asyncio.sleep(0.02)

    asyncio.run(go())
    assert svc.exa.calls == [] and redis_on.store == {}
    assert [c["outcome"] for c in _calls(events)] == ["abandoned"]


def test_a_joiner_keeps_the_flight_alive_when_the_leader_leaves(events, monkeypatch):
    gate = _gated(monkeypatch)
    svc = _service(_three())
    q = ["Data Engineer, based in Denver, CO"]

    async def go():
        leader = asyncio.create_task(svc._people_search_fanout(q, 50))
        await _until(lambda: gate["entered"] == 1)
        joiner = asyncio.create_task(svc._people_search_fanout(q, 50))
        await asyncio.sleep(0.02)
        leader.cancel()
        await asyncio.sleep(0.02)
        gate["event"].set()
        return await joiner

    assert len(asyncio.run(go())) == 40
    assert len(svc.exa.calls) == 1


# ---------------------------------------------------------------------------
# ExaPeopleFanout
# ---------------------------------------------------------------------------

FANOUT_ATTRS = (
    "surface", "queries", "per_query", "limit_total", "raw_rows", "dup_rows", "merged_rows",
    "kept_rows", "deepest_rank", "failed", "ooc_failed", "cache_hits", "cache_misses",
    "inflight_joins", "cache_state", "cost_usd", "starved", "cf15_same",
)


def test_fanout_event_attributes_survive_safe_attributes(events):
    svc = _service(_three())
    _search(svc, THREE)
    [ev] = _fanouts(events)
    assert set(FANOUT_ATTRS) <= set(ev)
    assert (ev["queries"], ev["per_query"], ev["limit_total"]) == (3, 34, 50)
    # 3 x 34 distinct rows: 50 kept reach rank 17 (16 full rounds = 48, then 2).
    assert (ev["raw_rows"], ev["dup_rows"], ev["merged_rows"], ev["kept_rows"]) == (102, 0, 102, 50)
    assert ev["deepest_rank"] == 17
    assert ev["starved"] is False and ev["cf15_same"] is True


def test_starved_when_duplicates_leave_the_merge_short(events):
    same = _people("e", 40)
    svc = _service({"Data Engineer": same, "Data Analyst": same, "Product Manager": []})
    rows = _search(svc, THREE)
    [ev] = _fanouts(events)
    assert len(rows) == 34
    assert (ev["raw_rows"], ev["dup_rows"], ev["merged_rows"]) == (68, 34, 34)
    assert ev["starved"] is True and ev["deepest_rank"] == 34
    assert ev["cf15_same"] is False  # 25 per query would have kept 25


def test_fanout_event_carries_the_job_and_trigger(events):
    svc = _service(_three())
    with tele.scope(trigger="step5", job_id="26-1"):
        _search(svc, THREE)
    [ev] = _fanouts(events)
    assert ev["trigger"] == "step5" and ev["job_id"] == "26-1"


def test_fanout_event_is_off_with_telemetry(events, monkeypatch):
    from core import config as core_config

    monkeypatch.setattr(core_config, "EXA_TELEMETRY_ENABLED", False)
    _search(_service(_three()), THREE)
    assert _fanouts(events) == []


# ---------------------------------------------------------------------------
# Merge (ported from test_exa_query_builder.run_fanout_cases)
# ---------------------------------------------------------------------------

def test_merge_dedupes_urls_interleaves_by_rank_survives_a_failure_and_caps():
    svc = _service({
        "Program Manager": [
            _res("alice", "a1", "Alice Smith - PM"),
            types.SimpleNamespace(url="https://linkedin.com/in/bob", id="b1", title="Bob Jones - PM", highlights=None),
        ],
        "Project Manager": [
            # Same profile as alice at a different URL spelling: deduped.
            types.SimpleNamespace(url="http://linkedin.com/in/alice", id="a2", title="Alice Smith - PjM", highlights=None),
            types.SimpleNamespace(url="https://linkedin.com/in/carol", id="c1", title="Carol Lee - PjM", highlights=None),
        ],
        "Broken Role": RuntimeError("boom"),
    })

    cands = asyncio.run(svc.search_candidates(
        skills=[], location="Denver, CO, United States", limit=3,
        titles=["Program Manager", "Project Manager", "Broken Role"],
    ))

    ids = [c["id"] for c in cands]
    assert len(cands) == 3 and len(set(ids)) == 3
    assert sum(1 for i in ids if "alice" in i) == 1
    assert "alice" in ids[0]  # rank 0 before rank 1
    assert {k["num_results"] for _q, k in svc.exa.calls} == {3}


def test_round_robin_merge_reports_the_deepest_rank_kept():
    a, b = _people("a", 5), _people("b", 5)
    merged, deepest = es._round_robin_merge([a, b], 4)
    assert [r.id for r in merged[:4]] == ["id-a0", "id-b0", "id-a1", "id-b1"]
    assert len(merged) == 10 and deepest == 2
    assert es._round_robin_merge([], 4) == ([], 0)
    assert math.isclose(es._round_robin_merge([a], 50)[1], 5)
