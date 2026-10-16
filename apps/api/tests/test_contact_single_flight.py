"""One paid Exa contact lookup per person at a time across the 8 workers
(2026-10-15, PR 3b; services/contact_enrichment.exa_step).

RUN THESE LOCALLY WITH A POSTGRES BEFORE MERGING ANY CHANGE TO THE LEASE.
CI has no Postgres: without LAUNCH_REPORT_TEST_DSN every test here that takes
the ``pgs`` fixture SKIPS (the lease SQL, every leader / follower / sweep
path), and only the DB-error and pure-function cases run. A broken lease
predicate raises nothing, so a green CI says nothing about it. Locally::

    PGTZ=UTC LAUNCH_REPORT_TEST_DSN='postgresql://...' \\
        python -m pytest -q tests/test_contact_single_flight.py tests/test_contact_cache.py -rs

and check the summary shows no "no Postgres reachable" skips.

The person's contact_enrichment_cache row is a lease: the first lookup leads
(claims it once it holds a slot of its semaphore, for the fields the cache
still lacks, keeps it alive, stamps its run, caches the answer, releases it);
a lookup of the same person meanwhile follows (polls the leader's run for
free, or waits and claims what is still unanswered) or, out of time, joins
the lease and answers ``in_progress`` (never a miss). A run still going goes
to the late watch with the lease; a watch that dies with its worker is
adopted by the 60 s sweep (FOR UPDATE SKIP LOCKED).

The SQL and end-to-end cases need a real Postgres (LAUNCH_REPORT_TEST_DSN):
the table lives in a schema of its own and every call opens its own
connection, as the workers' pool does, so leases are contended for real.
"""
import asyncio
import logging
import os
import time
import uuid
from pathlib import Path

import psycopg2
import pytest

import core.config as core_config
from core import sourcing_config
from routers import candidates as candidates_router
from services import apollo_phone, contact_cache, contact_cache_backfill, exa_telemetry
from services import contact_enrichment as ce

LINKEDIN = "https://www.linkedin.com/in/jane-doe"
SLUG = "jane-doe"
MOBILE = "+14155550100"
_DSN = os.getenv("LAUNCH_REPORT_TEST_DSN", "dbname=postgres")
_H = {"normal_horizon_s": 810, "deep_horizon_s": 870}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def pgs(monkeypatch):
    try:
        admin = psycopg2.connect(_DSN, connect_timeout=2)
    except Exception:
        # CI has no Postgres: run this file locally with the DSN before merging.
        pytest.skip("no Postgres reachable (set LAUNCH_REPORT_TEST_DSN; CI has none, run it locally)")
    admin.autocommit = True
    schema = f"cc_lease_{uuid.uuid4().hex[:10]}"
    with admin.cursor() as cur:
        cur.execute(f"CREATE SCHEMA {schema}")

    def _connect():
        return psycopg2.connect(_DSN, connect_timeout=2, options=f"-c search_path={schema}")

    monkeypatch.setattr(contact_cache, "_connect", _connect)
    monkeypatch.setattr(contact_cache, "_table_ready", False)
    monkeypatch.setattr(contact_cache_backfill, "run_once", lambda conn: None)
    asyncio.run(contact_cache.get(LINKEDIN))   # creates the table (CREATE + MIGRATE)

    def sql(query, params=None):
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(query, params)
                rows = cur.fetchall() if cur.description else None
            conn.commit()
            return rows
        finally:
            conn.close()

    yield sql
    with admin.cursor() as cur:
        cur.execute(f"DROP SCHEMA {schema} CASCADE")
    admin.close()


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = ""
        self.headers = {}

    def json(self):
        return self._payload


class _Exa:
    """The Exa Agent API, shared by every lookup of a test: counts creates;
    a run is "running" until ``finished`` (a run in ``stuck`` for good), then
    answers ``answer``."""

    def __init__(self):
        self.posts = []
        self.finished = False
        self.stuck = set()
        self.answer = {"phone": "+1 415 555 0100"}

    def client(self):
        exa = self

        class _Client:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, headers=None, json=None):
                exa.posts.append(json)
                return _Resp(200, {"id": f"run-{len(exa.posts)}", "status": "running"})

            async def get(self, url, headers=None, params=None):
                run_id = url.rsplit("/", 1)[-1]
                if exa.finished and run_id not in exa.stuck:
                    return _Resp(200, {"id": run_id, "status": "completed",
                                       "output": {"structured": {"contact": exa.answer}}})
                return _Resp(200, {"id": run_id, "status": "running"})

        return _Client


@pytest.fixture
def exa(monkeypatch):
    fake = _Exa()
    monkeypatch.setattr(ce.httpx, "AsyncClient", fake.client())
    monkeypatch.setattr(ce, "EXA_API_KEY", "test-key")
    monkeypatch.setattr(ce, "EXA_CONTACT_ENRICH_ENABLED", True)
    monkeypatch.setattr(ce, "_EXA_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(ce, "_EXA_LATE_POLL_S", 0.01)
    monkeypatch.setattr(ce, "FOLLOW_POLL_S", 0.01)
    monkeypatch.setattr(ce, "LEASE_HEARTBEAT_S", 0.05)
    monkeypatch.setattr(ce, "EXA_CONTACT_ENRICH_TIMEOUT_S", 5)
    monkeypatch.setattr(ce, "EXA_LATE_RESULT_WAIT_S", 5)
    monkeypatch.delenv("CONTACT_LOOKUP_SINGLE_FLIGHT", raising=False)
    return fake


@pytest.fixture
def events(monkeypatch):
    sent = []
    monkeypatch.setattr(exa_telemetry, "_send", lambda event_type, attrs: sent.append((event_type, attrs)))
    return sent


def _lookups(sent):
    return [a for t, a in sent if t == exa_telemetry.LOOKUP_EVENT]


def _following(monkeypatch):
    """An Event set once a lookup starts polling another lookup's run."""
    followed = asyncio.Event()
    real = ce._follow_run

    async def _spy(run_id, deadline):
        followed.set()
        return await real(run_id, deadline)

    monkeypatch.setattr(ce, "_follow_run", _spy)
    return followed


async def _until(predicate, timeout=5.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not predicate():
        if loop.time() > end:
            raise AssertionError("condition never met")
        await asyncio.sleep(0.01)


async def _held():
    return (await contact_cache.holder(SLUG, **_H))["state"] == "held"


# ---------------------------------------------------------------------------
# The lease (SQL)
# ---------------------------------------------------------------------------

def test_sql_one_of_many_concurrent_claims_wins(pgs):
    async def _go():
        return await asyncio.gather(*(contact_cache.claim(SLUG, ("phone",), "normal", **_H) for _ in range(8)))

    results = asyncio.run(_go())

    assert [s for s, _ in results].count("won") == 1
    assert [s for s, _ in results].count("held") == 7


def test_sql_only_the_holder_renews_stamps_or_releases(pgs):
    run = asyncio.run
    _, token = run(contact_cache.claim(SLUG, ("email", "phone"), "deep", **_H))

    assert run(contact_cache.heartbeat(SLUG, "not-the-holder", run_id="run-x")) == 0
    assert run(contact_cache.release(SLUG, "not-the-holder")) is None
    assert run(contact_cache.heartbeat(SLUG, token, run_id="run-1")) == 1
    assert run(contact_cache.holder(SLUG, **_H)) == {
        "state": "held", "fields": ("email", "phone"), "tier": "deep", "run_id": "run-1",
    }
    assert run(contact_cache.release(SLUG, token)) == []
    assert run(contact_cache.holder(SLUG, **_H)) == {"state": "free"}


def test_sql_a_lapsed_lease_is_free_unless_its_run_can_still_answer(pgs):
    run = asyncio.run
    _, token = run(contact_cache.claim(SLUG, ("phone",), "normal", **_H))
    pgs("UPDATE contact_enrichment_cache SET pending_until = NOW() - interval '1 second'")
    assert run(contact_cache.claim(SLUG, ("phone",), "normal", **_H))[0] == "won"   # no run: free

    _, token = run(contact_cache.claim("bo-b", ("phone",), "normal", **_H))
    run(contact_cache.heartbeat("bo-b", token, run_id="run-1"))
    pgs("UPDATE contact_enrichment_cache SET pending_until = NOW() - interval '1 second' "
        "WHERE linkedin_slug = 'bo-b'")
    assert run(contact_cache.claim("bo-b", ("phone",), "normal", **_H))[0] == "held"  # an orphan
    assert run(contact_cache.holder("bo-b", **_H))["run_id"] == "run-1"

    pgs("UPDATE contact_enrichment_cache SET pending_run_at = NOW() - interval '811 seconds' "
        "WHERE linkedin_slug = 'bo-b'")
    assert run(contact_cache.claim("bo-b", ("phone",), "normal", **_H))[0] == "won"   # past its horizon


def test_sql_contexts_join_once_and_come_back_on_release(pgs):
    run = asyncio.run
    assert run(contact_cache.join(SLUG, {"candidate_id": "c2"})) is False             # nobody holds it
    _, token = run(contact_cache.claim(SLUG, ("phone",), "normal", ctx={"candidate_id": "c1"}, **_H))
    assert run(contact_cache.join(SLUG, {"candidate_id": "c2", "jobdiva_id": "26-1"})) is True
    assert run(contact_cache.join(SLUG, {"candidate_id": "c2", "jobdiva_id": "26-1"})) is False

    assert run(contact_cache.release(SLUG, token)) == [
        {"candidate_id": "c1"}, {"candidate_id": "c2", "jobdiva_id": "26-1"},
    ]
    assert pgs("SELECT pending_meta FROM contact_enrichment_cache") == [(None,)]


def test_sql_the_lease_never_touches_the_contact(pgs):
    run = asyncio.run
    run(contact_cache.record(LINKEDIN, email="jane@acme.com", email_provider="exa", missed=["phone"]))
    _, token = run(contact_cache.claim(SLUG, ("phone",), "deep", **_H))
    run(contact_cache.heartbeat(SLUG, token, run_id="run-1"))
    run(contact_cache.release(SLUG, token))

    got = run(contact_cache.get(LINKEDIN))
    assert got["email"] == "jane@acme.com" and got["phone_missed"]


def _orphan(pgs, slug=SLUG, ctx=None):
    """A lease whose worker died after its run started."""
    _, token = asyncio.run(contact_cache.claim(slug, ("phone",), "deep", ctx=ctx, **_H))
    asyncio.run(contact_cache.heartbeat(slug, token, run_id="run-1"))
    pgs("UPDATE contact_enrichment_cache SET pending_until = NOW() - interval '1 second' "
        "WHERE linkedin_slug = %s", (slug,))
    return token


def test_sql_the_sweep_adopts_an_orphan_once_and_skips_one_being_adopted(pgs):
    old = _orphan(pgs)

    locker = contact_cache._connect()
    try:
        with locker.cursor() as cur:   # another worker's sweep holds the row
            cur.execute("SELECT 1 FROM contact_enrichment_cache WHERE linkedin_slug = %s FOR UPDATE", (SLUG,))
        assert asyncio.run(contact_cache.adopt_orphans(**_H)) == []   # skipped, not waited on
    finally:
        locker.rollback()
        locker.close()

    async def _two_workers():
        return await asyncio.gather(contact_cache.adopt_orphans(**_H), contact_cache.adopt_orphans(**_H))

    first, second = asyncio.run(_two_workers())
    adopted = first + second
    assert len(adopted) == 1
    row = adopted[0]
    assert row["slug"] == SLUG and row["run_id"] == "run-1" and row["tier"] == "deep"
    assert row["fields"] == ("phone",) and row["token"] != old
    assert 860 < row["remaining_s"] <= 870
    assert asyncio.run(contact_cache.adopt_orphans(**_H)) == []      # its new lease is live
    assert asyncio.run(contact_cache.heartbeat(SLUG, old)) == 0        # the dead worker's token is void


# ---------------------------------------------------------------------------
# exa_step: leader, follower, passthrough
# ---------------------------------------------------------------------------

def test_two_lookups_of_one_person_buy_one_run(pgs, exa, events, monkeypatch):
    async def _go():
        followed = _following(monkeypatch)
        a = asyncio.ensure_future(ce.exa_step("c1", LINKEDIN, "Jane Doe", fields=("phone",)))
        b = asyncio.ensure_future(ce.exa_step("c2", LINKEDIN, "Jane Doe", fields=("phone",)))
        await asyncio.wait_for(followed.wait(), 5)
        exa.finished = True
        return await asyncio.gather(a, b)

    a, b = asyncio.run(_go())

    assert len(exa.posts) == 1                                     # one paid run
    assert sorted([a["role"], b["role"]]) == ["follower", "leader"]
    assert a["ok"] and b["ok"] and a["fields"]["mobilePhone"] == b["fields"]["mobilePhone"] == MOBILE
    assert asyncio.run(contact_cache.get(LINKEDIN))["phone"] == MOBILE
    assert asyncio.run(contact_cache.holder(SLUG, **_H)) == {"state": "free"}
    # Only the run's owner reports it; the follower's polls send no ExaCall.
    runs = [x for t, x in events if t == exa_telemetry.EVENT and x.get("endpoint") == "agent_run"]
    assert [(x["outcome"], x.get("role")) for x in runs] == [("completed", "leader")]
    assert sorted(x["outcome"] for x in _lookups(events)) == ["follower", "leader"]
    assert all(x.get("exa_run_id") == "run-1" for x in _lookups(events))


def test_a_field_the_holders_run_dropped_is_not_recorded_as_a_miss(pgs, exa, monkeypatch):
    # The holder claimed email+phone, then the cache answered the phone while
    # it queued, so its run asks the email only. A follower after the phone
    # takes it from the cache, not as the run's miss.
    _, token = asyncio.run(contact_cache.claim(SLUG, ("email", "phone"), "normal", **_H))
    asyncio.run(contact_cache.heartbeat(SLUG, token, run_id="run-1"))
    exa.answer = {"email": "jane@acme.com"}

    async def _go():
        followed = _following(monkeypatch)
        follower = asyncio.ensure_future(ce.exa_step("c2", LINKEDIN, "Jane Doe", fields=("phone",)))
        await asyncio.wait_for(followed.wait(), 5)
        await contact_cache.record(LINKEDIN, phone=MOBILE, phone_provider="apollo")
        exa.finished = True
        return await follower

    res = asyncio.run(_go())

    assert res["role"] == "follower" and res["fields"]["mobilePhone"] == MOBILE
    assert exa.posts == []
    assert pgs("SELECT phone_missed_at FROM contact_enrichment_cache") == [(None,)]


def test_a_lookup_of_fields_the_holder_is_not_after_runs_at_once(pgs, exa, events):
    _, token = asyncio.run(contact_cache.claim(SLUG, ("phone",), "normal", **_H))
    exa.finished = True
    exa.answer = {"email": "jane@acme.com"}

    res = asyncio.run(ce.exa_step("c1", LINKEDIN, "Jane Doe", fields=("email",)))

    assert res["role"] == "passthrough" and res["fields"]["workEmail"] == "jane@acme.com"
    assert len(exa.posts) == 1
    assert [(x["outcome"], x.get("reason")) for x in _lookups(events)] == [("passthrough", "no_overlap")]
    assert asyncio.run(contact_cache.heartbeat(SLUG, token)) == 1   # the holder's lease is untouched


def test_out_of_time_a_follower_joins_the_lease_and_is_not_a_miss(pgs, exa, events, monkeypatch):
    monkeypatch.setattr(ce, "EXA_CONTACT_ENRICH_TIMEOUT_S", 0.3)
    _, token = asyncio.run(contact_cache.claim(SLUG, ("email", "phone"), "normal", **_H))
    unused = []

    res = asyncio.run(ce.exa_step(
        "c2", LINKEDIN, "Jane Doe", fields=("phone",), wait_retry=False,
        late={"candidate_id": "c2", "jobdiva_id": "26-1"}, on_unused=lambda: unused.append(1),
    ))

    assert res["ok"] is False and res["in_progress"] is True and res["role"] == "follower"
    assert exa.posts == [] and unused == [1]
    assert [x["outcome"] for x in _lookups(events)] == ["in_progress"]
    got = asyncio.run(contact_cache.get(LINKEDIN))
    assert not got["phone_missed"] and not got["phone_deep_missed"]   # never a miss
    assert asyncio.run(contact_cache.release(SLUG, token)) == [{"candidate_id": "c2", "jobdiva_id": "26-1"}]


def test_the_lease_does_not_lapse_while_the_leaders_run_goes(pgs, exa, monkeypatch):
    monkeypatch.setattr(contact_cache, "LEASE_S", 1)
    monkeypatch.setattr(ce, "LEASE_HEARTBEAT_S", 0.2)

    async def _go():
        leader = asyncio.ensure_future(ce.exa_step("c1", LINKEDIN, "Jane Doe", fields=("phone",)))
        held = []
        for _ in range(3):
            await asyncio.sleep(0.6)                                 # 1.8 s > the 1 s lease
            held.append(await _held())
        rival = await contact_cache.claim(SLUG, ("phone",), "normal", **_H)
        exa.finished = True
        return held, rival, await leader

    held, rival, res = asyncio.run(_go())

    assert held == [True, True, True] and rival[0] == "held"
    assert res["role"] == "leader" and res["fields"]["mobilePhone"] == MOBILE
    assert len(exa.posts) == 1


def test_a_lookup_queued_for_a_slot_holds_nobody_up(pgs, exa, monkeypatch):
    # Review fix (3b): the lease is claimed only with a slot in hand. A
    # sourcing lookup queued behind the 3 sourcing slots used to hold the
    # person, so a click waited its whole window and bought nothing.
    exa.answer = {"email": "jane@acme.com", "phone": "+1 415 555 0100"}

    async def _go():
        followed = _following(monkeypatch)
        sem = asyncio.Semaphore(1)
        await sem.acquire()                                          # every sourcing slot taken
        queued = asyncio.ensure_future(ce.exa_step(
            "Jane Doe", LINKEDIN, "Jane Doe", fields=("email",), semaphore=sem, tasks=set(),
        ))
        await asyncio.sleep(0.1)
        held_while_queued = await _held()
        click = asyncio.ensure_future(ce.exa_step("c1", LINKEDIN, "Jane Doe", fields=("email", "phone")))
        await _until(lambda: len(exa.posts) == 1)                    # the click bought at once
        sem.release()                                                # the queued lookup gets its slot...
        await asyncio.wait_for(followed.wait(), 5)                   # ...and follows the click's run
        exa.finished = True
        return held_while_queued, await click, await queued

    held_while_queued, click, queued = asyncio.run(_go())

    assert held_while_queued is False
    assert click["role"] == "leader" and click["fields"]["mobilePhone"] == MOBILE
    assert queued["role"] == "follower" and queued["fields"]["workEmail"] == "jane@acme.com"
    assert len(exa.posts) == 1                                       # still one paid run


def test_a_leader_whose_lease_was_taken_before_its_create_follows_instead(pgs, exa, monkeypatch):
    unused = []
    real_before_create = ce._StepRun.before_create
    taken = []

    async def _taken_first(self):
        if not taken:
            # A DB hiccup let the lease lapse and another worker took the
            # person and started its run.
            taken.append(1)
            pgs("UPDATE contact_enrichment_cache SET pending_token = 'other-worker', "
                "pending_until = NOW() + interval '60 seconds', pending_run_id = 'run-other', pending_run_at = NOW()")
        return await real_before_create(self)

    monkeypatch.setattr(ce._StepRun, "before_create", _taken_first)
    exa.finished = True

    res = asyncio.run(ce.exa_step(
        "c1", LINKEDIN, "Jane Doe", fields=("phone",), semaphore=asyncio.Semaphore(1),
        on_unused=lambda: unused.append(1),
    ))

    assert exa.posts == []                                           # no second paid run
    assert res["role"] == "follower" and res["fields"]["mobilePhone"] == MOBILE
    assert unused == [1]


def test_the_kill_switch_buys_one_run_per_lookup_without_a_lease(pgs, exa, monkeypatch):
    monkeypatch.setenv("CONTACT_LOOKUP_SINGLE_FLIGHT", "false")
    exa.finished = True

    async def _go():
        return await asyncio.gather(
            ce.exa_step("c1", LINKEDIN, "Jane Doe", fields=("phone",)),
            ce.exa_step("c2", LINKEDIN, "Jane Doe", fields=("phone",)),
        )

    a, b = asyncio.run(_go())

    assert len(exa.posts) == 2 and a["role"] == b["role"] == "passthrough"
    assert pgs("SELECT COUNT(*) FROM contact_enrichment_cache WHERE pending_token IS NOT NULL") == [(0,)]


def test_a_db_error_runs_the_lookup_without_a_lease(exa, events):
    # No Postgres here: conftest blocks every connection.
    exa.finished = True

    res = asyncio.run(ce.exa_step("c1", LINKEDIN, "Jane Doe", fields=("phone",)))

    assert res["ok"] and res["role"] == "passthrough" and len(exa.posts) == 1
    assert [(x["outcome"], x.get("reason")) for x in _lookups(events)] == [("lease_error", "lease_error")]


def test_a_breaker_skip_releases_the_lease(pgs, exa):
    from core.vendor_limiter import EXA

    asyncio.run(EXA.trip_no_credits("NO_MORE_CREDITS"))
    unused = []

    res = asyncio.run(ce.exa_step("c1", LINKEDIN, "Jane Doe", fields=("phone",), on_unused=lambda: unused.append(1)))

    assert res.get("skipped") and exa.posts == [] and unused == [1]
    asyncio.run(asyncio.sleep(0))
    assert asyncio.run(contact_cache.holder(SLUG, **_H)) == {"state": "free"}


# ---------------------------------------------------------------------------
# Late watches that survive restarts
# ---------------------------------------------------------------------------

def test_a_run_still_going_goes_to_the_late_watch_with_its_lease(pgs, exa, events, monkeypatch):
    monkeypatch.setattr(ce, "EXA_CONTACT_ENRICH_TIMEOUT_S", 0.2)
    fills = []
    monkeypatch.setattr(ce, "_fill_sourced_rows_sync", fills.append)

    async def _go():
        first = await ce.exa_step("c1", LINKEDIN, "Jane Doe", fields=("phone",), wait_retry=False,
                                  late={"candidate_id": "c1", "jobdiva_id": "26-1"})
        held = await contact_cache.holder(SLUG, **_H)
        # A second lookup meanwhile follows the same run, runs out of time
        # and joins the lease.
        second = await ce.exa_step("c2", LINKEDIN, "Jane Doe", fields=("phone",), wait_retry=False,
                                   late={"candidate_id": "c2", "jobdiva_id": "26-2"})
        exa.finished = True
        await asyncio.gather(*list(ce._LATE_WATCHES))
        return first, held, second

    first, held, second = asyncio.run(_go())

    assert first["run_id"] == "run-1" and first["role"] == "leader"
    assert held == {"state": "held", "fields": ("phone",), "tier": "normal", "run_id": "run-1"}
    assert second["in_progress"] and len(exa.posts) == 1
    assert asyncio.run(contact_cache.get(LINKEDIN))["phone"] == MOBILE
    assert asyncio.run(contact_cache.holder(SLUG, **_H)) == {"state": "free"}
    assert sorted((f["candidate_id"], f["jobdiva_id"], f["phone"]) for f in fills) == [
        ("c1", "26-1", MOBILE), ("c2", "26-2", MOBILE),
    ]


def test_a_cancelled_late_watch_leaves_its_lease_for_the_sweep(pgs, exa):
    _, token = asyncio.run(contact_cache.claim(SLUG, ("phone",), "normal", **_H))
    asyncio.run(contact_cache.heartbeat(SLUG, token, run_id="run-1"))

    async def _go():
        watch = asyncio.ensure_future(ce.keep_late_exa_result(
            "run-1", LINKEDIN, fields=("phone",), token=token, slug=SLUG,
        ))
        await asyncio.sleep(0.05)
        watch.cancel()                                              # the worker is going away
        with pytest.raises(asyncio.CancelledError):
            await watch

    asyncio.run(_go())

    assert asyncio.run(contact_cache.heartbeat(SLUG, token)) == 1  # not released


def test_the_sweep_adopts_an_orphaned_run_and_fills_the_rows(pgs, exa, events, monkeypatch, caplog):
    _orphan(pgs, ctx={"candidate_id": "c1", "jobdiva_id": "26-1"})
    fills = []
    monkeypatch.setattr(ce, "_fill_sourced_rows_sync", fills.append)
    exa.finished = True

    async def _go():
        first = await ce.resume_orphaned_late_watches()
        again = await ce.resume_orphaned_late_watches()
        await asyncio.gather(*list(ce._LATE_WATCHES))
        return first, again

    with caplog.at_level(logging.WARNING, logger=ce.logger.name):
        first, again = asyncio.run(_go())

    assert (first, again) == (1, 0)
    assert exa.posts == []
    got = asyncio.run(contact_cache.get(LINKEDIN))
    assert got["phone"] == MOBILE and got["phone_provider"] == "exa_deep"
    assert [(f["candidate_id"], f["phone"]) for f in fills] == [("c1", MOBILE)]
    assert asyncio.run(contact_cache.holder(SLUG, **_H)) == {"state": "free"}
    assert [(x["outcome"], x.get("exa_run_id")) for x in _lookups(events)] == [("adopted", "run-1")]
    late = [x for t, x in events if t == exa_telemetry.EVENT and x.get("late")]
    assert [(x["outcome"], x.get("role")) for x in late] == [("completed", "adopted")]
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("run-1" in m and "adopted" in m for m in warned)
    assert not any("jane" in m.lower() for m in warned)              # never the profile


def test_the_sweep_does_nothing_with_the_kill_switch_off(pgs, exa, monkeypatch):
    _orphan(pgs)
    monkeypatch.setenv("CONTACT_LOOKUP_SINGLE_FLIGHT", "false")

    assert asyncio.run(ce.resume_orphaned_late_watches()) == 0


def test_main_schedules_the_sweep_every_minute():
    main_src = (Path(__file__).resolve().parents[1] / "main.py").read_text()

    assert "resume_orphaned_late_watches" in main_src
    assert 'id="exa_late_watch_sweep"' in main_src


# ---------------------------------------------------------------------------
# The ladders
# ---------------------------------------------------------------------------

def test_two_tabs_on_one_person_make_one_run(pgs, exa, monkeypatch):
    monkeypatch.setattr(sourcing_config, "CONTACT_LOOKUP_PROVIDERS", ("apollo", "exa"))
    monkeypatch.setattr(core_config, "EXA_CONTACT_ENRICH_ENABLED", True)
    monkeypatch.setattr(sourcing_config, "EXA_ONDEMAND_CONTACT_ONLY_WHEN_NO_CONTACT", False)
    monkeypatch.setattr(apollo_phone, "reveal_available", lambda: False)

    async def _apollo(candidate_id, linkedin_url):
        return {"ok": True, "fields": {}}

    def _no_db():
        raise RuntimeError("no db in tests")

    monkeypatch.setattr(candidates_router, "get_db_connection", _no_db)
    monkeypatch.setattr(candidates_router, "_apollo_enrich_by_linkedin", _apollo)

    def _click(candidate_id):
        body = candidates_router.EnrichCandidateContactRequest(
            candidate_id=candidate_id, linkedin_url=LINKEDIN, full_name="Jane Doe", fields=["phone"],
        )
        return candidates_router._enrich_candidate_contact_impl(candidate_id, body)

    async def _go():
        followed = _following(monkeypatch)
        a = asyncio.ensure_future(_click("cand-1"))
        b = asyncio.ensure_future(_click("cand-1"))
        await asyncio.wait_for(followed.wait(), 5)
        exa.finished = True
        return await asyncio.gather(a, b)

    a, b = asyncio.run(_go())

    assert len(exa.posts) == 1
    assert a["status"] == b["status"] == "success"
    assert a["phone"] == b["phone"] == MOBILE


def test_a_sourcing_follower_gives_back_its_cap_slot(pgs, exa, monkeypatch):
    monkeypatch.setattr(sourcing_config, "CONTACT_LOOKUP_PROVIDERS", ("apollo", "exa"))
    monkeypatch.setattr(sourcing_config, "EXA_SOURCING_CONTACT_FALLBACK", True)
    monkeypatch.setenv("CONTACT_ENRICHMENT_INLINE_ENABLED", "true")
    monkeypatch.setattr(ce, "_EXA_SEMAPHORE", None)
    exa.answer = {"email": "jane@acme.com"}

    async def _apollo(candidate_id, linkedin_url):
        return {"ok": False}

    monkeypatch.setattr(ce, "apollo_enrich_by_linkedin", _apollo)
    ce.reset_job_counter("job-sf", include_lifetime=True)

    def _lookup():
        return ce.enrich_contact_for_sourcing(LINKEDIN, "job-sf", full_name="Jane Doe", include_exa=True,
                                              want_phone=False)

    async def _go():
        followed = _following(monkeypatch)
        a = asyncio.ensure_future(_lookup())
        b = asyncio.ensure_future(_lookup())
        await asyncio.wait_for(followed.wait(), 5)
        exa.finished = True
        return await asyncio.gather(a, b)

    a, b = asyncio.run(_go())

    assert len(exa.posts) == 1
    assert a["workEmail"] == b["workEmail"] == "jane@acme.com"
    assert ce._JOB_EXA_LIFETIME.get("job-sf") == 1 and ce._JOB_EXA_COUNTERS.get("job-sf") == 1
    ce.reset_job_counter("job-sf", include_lifetime=True)


def test_a_re_search_after_stop_follows_the_run_still_going(pgs, exa, monkeypatch):
    # Search 1's lookup of Jane runs (billed) and its stream closes (Stop);
    # the re-search's lookup of Jane finds an Exa slot free (3 of them) and
    # the person held by that run: it follows the run instead of buying one.
    monkeypatch.setattr(sourcing_config, "CONTACT_LOOKUP_PROVIDERS", ("apollo", "exa"))
    monkeypatch.setattr(sourcing_config, "EXA_SOURCING_CONTACT_FALLBACK", True)
    monkeypatch.setenv("CONTACT_ENRICHMENT_INLINE_ENABLED", "true")
    monkeypatch.setattr(ce, "_EXA_SEMAPHORE", None)
    monkeypatch.setattr(ce, "EXA_CONTACT_ENRICH_CONCURRENCY", 3)
    exa.answer = {"email": "jane@acme.com"}

    async def _apollo(candidate_id, linkedin_url):
        return {"ok": False}

    monkeypatch.setattr(ce, "apollo_enrich_by_linkedin", _apollo)
    ce.reset_job_counter("job-rs", include_lifetime=True)

    def _lookup():
        return ce.enrich_contact_for_sourcing(LINKEDIN, "job-rs", full_name="Jane Doe", include_exa=True,
                                              want_phone=False)

    async def _go():
        followed = _following(monkeypatch)
        first = asyncio.ensure_future(_lookup())
        await _until(lambda: len(exa.posts) == 1)
        first.cancel()                                               # Stop: the stream closes
        with pytest.raises(asyncio.CancelledError):
            await first
        ce.reset_job_counter("job-rs")                               # the re-search
        second = asyncio.ensure_future(_lookup())
        await asyncio.wait_for(followed.wait(), 5)
        assert ce._exa_semaphore()._value == 2                       # a slot was free all along
        exa.finished = True
        res = await second
        await asyncio.gather(*list(ce._SOURCING_EXA_TASKS))
        return res

    res = asyncio.run(_go())

    assert len(exa.posts) == 1                                       # one run, not two
    assert res["workEmail"] == "jane@acme.com"
    assert ce._JOB_EXA_LIFETIME.get("job-rs") == 1                   # the follower gave its slot back
    assert asyncio.run(contact_cache.get(LINKEDIN))["email"] == "jane@acme.com"
    ce.reset_job_counter("job-rs", include_lifetime=True)


# ---------------------------------------------------------------------------
# What counts as answered meanwhile; the on-demand chain's events
# ---------------------------------------------------------------------------

def test_a_miss_the_cache_already_held_does_not_answer_a_confirmed_rebuy():
    old = {"phone_missed": True, "phone_deep_missed": True, "phone_deep_missed_at": "2026-10-01T12:00:00+00:00"}
    new = {**old, "phone_deep_missed_at": "2026-10-15T09:00:00+00:00"}

    assert ce._answered_since(old, old, ("phone",), deep=True) == {}
    assert ce._answered_since(old, new, ("phone",), deep=True) == {"phone": ""}
    assert ce._answered_since({}, {"phone_missed": True}, ("phone",), deep=True) == {}   # a normal miss
    assert ce._answered_since({}, {"phone_missed": True}, ("phone",), deep=False) == {"phone": ""}
    assert ce._answered_since({}, {"phone": MOBILE}, ("phone",), deep=True) == {"phone": MOBILE}


def test_the_chain_reports_final_misses_and_confirmed_rebuys(events, monkeypatch):
    monkeypatch.setattr(sourcing_config, "CONTACT_LOOKUP_PROVIDERS", ("apollo", "exa"))
    monkeypatch.setattr(ce, "EXA_CONTACT_DEEP_EFFORT", "medium")
    monkeypatch.setattr(core_config, "EXA_CONTACT_ENRICH_ENABLED", True)
    monkeypatch.setattr(apollo_phone, "reveal_available", lambda: False)
    monkeypatch.delenv("CONTACT_CLICK_HONOUR_DEEP_MISS", raising=False)
    cached = {**contact_cache.empty(), "email": "jane@acme.com", "phone_missed": True, "phone_deep_missed": True,
              "phone_deep_missed_at": "2026-10-01T12:00:00+00:00"}

    async def _get(url):
        return cached

    async def _nothing(*args, **kwargs):
        return {"ok": True, "fields": {}}

    async def _record(url, **kw):
        pass

    def _no_db():
        raise RuntimeError("no db in tests")

    monkeypatch.setattr(candidates_router, "get_db_connection", _no_db)
    monkeypatch.setattr(contact_cache, "get", _get)
    monkeypatch.setattr(contact_cache, "record", _record)
    monkeypatch.setattr(candidates_router, "_apollo_enrich_by_linkedin", _nothing)
    monkeypatch.setattr(candidates_router, "_exa_enrich_by_linkedin", _nothing)

    def _ask(**req):
        body = candidates_router.EnrichCandidateContactRequest(
            candidate_id="cand-1", linkedin_url=LINKEDIN, full_name="Jane Doe", fields=["phone"], **req,
        )
        return asyncio.run(candidates_router._enrich_candidate_contact_impl("cand-1", body))

    _ask()
    _ask(confirm=True)

    # (Each lookup also ends with its "lookup" summary.)
    steps = [x for x in _lookups(events) if x["outcome"] != "lookup"]
    outcomes = [(x["outcome"], x.get("fields_final"), x.get("trigger")) for x in steps]
    assert outcomes[0] == ("final_miss", "phone", "click_phone")
    assert outcomes[1] == ("confirm_rebuy", "phone", "click_phone")
    assert [x["outcome"] for x in steps][2:] == ["passthrough"]   # the deep run (no key here)
    assert [x["deep_asked"] for x in _lookups(events) if x["outcome"] == "lookup"] == ["-", "phone"]


# ---------------------------------------------------------------------------
# Review fixes (PR 3b)
# ---------------------------------------------------------------------------

def _hold(fields, run_id="", tier="normal"):
    """Another lookup holds the person (its run, if any, stamped)."""
    _, token = asyncio.run(contact_cache.claim(SLUG, fields, tier, **_H))
    if run_id:
        asyncio.run(contact_cache.heartbeat(SLUG, token, run_id=run_id))
    return token


def _counting(monkeypatch, name):
    calls = []
    real = getattr(contact_cache, name)

    async def _spy(*args, **kwargs):
        calls.append(1)
        return await real(*args, **kwargs)

    monkeypatch.setattr(contact_cache, name, _spy)
    return calls


def _asked(post):
    """The contact fields an Exa create asked for."""
    return tuple(post["outputSchema"]["properties"]["contact"]["properties"])


@pytest.mark.parametrize("failure", ["unreachable", "not_a_dict"])
def test_a_follower_whose_polls_fail_waits_instead_of_spinning(pgs, exa, monkeypatch, failure):
    # The holder's run could not be read: _follow_run returned at once and the
    # follower went straight round again (hundreds of lease reads and GETs a
    # second, for its whole window, while Exa was unreachable).
    _hold(("phone",), run_id="run-other")
    gets = []

    class _Failing:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None, params=None):
            gets.append(url)
            if failure == "unreachable":
                raise ce.httpx.ConnectError("unreachable")
            return _Resp(200, ["not", "a", "run"])

        async def post(self, url, headers=None, json=None):
            raise AssertionError("a follower never creates")

    monkeypatch.setattr(ce.httpx, "AsyncClient", _Failing)
    monkeypatch.setattr(ce, "FOLLOW_POLL_S", 0.25)
    monkeypatch.setattr(ce, "_EXA_POLL_INTERVAL_S", 0.25)
    monkeypatch.setattr(ce, "EXA_CONTACT_ENRICH_TIMEOUT_S", 1)
    holder_reads = _counting(monkeypatch, "holder")

    res = asyncio.run(ce.exa_step("c1", LINKEDIN, "Jane Doe", fields=("phone",), wait_retry=False,
                                  cached=contact_cache.empty()))

    assert res["in_progress"] is True and res["in_progress_fields"] == ["phone"]
    assert len(holder_reads) <= 6 and len(gets) <= 6                 # ~1 s window / 0.25 s


def test_a_lookup_buys_at_once_what_the_holder_is_not_after(pgs, exa, events, monkeypatch):
    # Launch PAIR (email + phone) behind a sourcing lookup of the email whose
    # run is still going: the phone is bought now, the email comes from the
    # sourcing lookup's run (followed for free).
    _hold(("email",), run_id="run-email")
    exa.answer = {"email": "jane@acme.com", "phone": "+1 415 555 0100"}
    exa.finished = True
    exa.stuck = {"run-email"}

    async def _go():
        followed = _following(monkeypatch)
        launch = asyncio.ensure_future(ce.exa_step(
            "c9", LINKEDIN, "Jane Doe", fields=("email", "phone"), late={"candidate_id": "c9"},
        ))
        await asyncio.wait_for(followed.wait(), 5)                   # the phone is bought by now
        exa.stuck.clear()                                            # the sourcing run answers
        return await launch

    res = asyncio.run(_go())

    assert [p["outputSchema"] for p in exa.posts] == [ce._exa_contact_schema(("phone",))]
    assert res["ok"] is True and not res.get("in_progress") and res["in_progress_fields"] == []
    assert res["fields"]["mobilePhone"] == MOBILE and res["fields"]["workEmail"] == "jane@acme.com"
    assert [(x["outcome"], x.get("reason")) for x in _lookups(events)] == [
        ("passthrough", "partial_overlap"), ("follower", None),
    ]


def test_out_of_time_only_the_holders_fields_are_in_progress(pgs, exa, monkeypatch):
    token = _hold(("email",), run_id="run-email")
    exa.finished = True
    exa.stuck = {"run-email"}                                        # still going after our window
    monkeypatch.setattr(ce, "EXA_CONTACT_ENRICH_TIMEOUT_S", 0.5)

    res = asyncio.run(ce.exa_step(
        "c9", LINKEDIN, "Jane Doe", fields=("email", "phone"), wait_retry=False,
        late={"candidate_id": "c9"}, cached=contact_cache.empty(),
    ))

    assert len(exa.posts) == 1                                       # the phone, bought
    assert res["ok"] is True and res["fields"]["mobilePhone"] == MOBILE
    assert res["in_progress"] is True and res["in_progress_fields"] == ["email"]
    got = asyncio.run(contact_cache.get(LINKEDIN))
    assert got["phone"] == MOBILE and not got["email_missed"]       # the email is no miss
    assert asyncio.run(contact_cache.release(SLUG, token)) == [{"candidate_id": "c9"}]


def test_a_part_that_failed_is_never_taken_for_a_miss():
    answered = {"ok": True, "fields": {"workEmail": "jane@acme.com", "mobilePhone": ""}}
    failed = {"ok": False, "message": "Exa create error (500)"}

    joined = ce._with_part(answered, failed)
    assert joined["ok"] is False and joined["fields"]["workEmail"] == "jane@acme.com"
    assert joined["message"] == "Exa create error (500)"

    still_going = {"ok": False, "in_progress": True, "message": "Another lookup ..."}
    joined = ce._with_part(still_going, {"ok": True, "fields": {"mobilePhone": MOBILE}, "run_id": ""})
    assert joined["ok"] is True and joined["in_progress"] and joined["fields"]["mobilePhone"] == MOBILE

    assert "from_cache" not in ce._with_part({"ok": True, "from_cache": True}, {"ok": True})


def test_a_follower_that_misses_the_release_still_gets_the_answer(pgs, exa, monkeypatch):
    # The holder recorded its answer and released between the follower's last
    # cache read and its join: the follower used to answer in_progress.
    token = _hold(("phone",))
    monkeypatch.setattr(ce, "EXA_CONTACT_ENRICH_TIMEOUT_S", 0.3)
    real_join = contact_cache.join

    async def _join(slug, ctx):
        await contact_cache.record(LINKEDIN, phone=MOBILE, phone_provider="exa")
        await contact_cache.release(SLUG, token)
        return await real_join(slug, ctx)

    monkeypatch.setattr(contact_cache, "join", _join)

    res = asyncio.run(ce.exa_step(
        "c2", LINKEDIN, "Jane Doe", fields=("phone",), wait_retry=False,
        late={"candidate_id": "c2"}, cached=contact_cache.empty(),
    ))

    assert not res.get("in_progress") and res["in_progress_fields"] == []
    assert res["ok"] and res["from_cache"] and res["fields"]["mobilePhone"] == MOBILE
    assert exa.posts == []


def test_a_lookup_the_cache_answers_once_it_has_its_slot_claims_nothing(pgs, exa, events, monkeypatch):
    # Second-round fix (3b): the cache is looked at again with the slot in
    # hand BEFORE the claim (it used to be after, with the lease already
    # naming the fields), so a lookup the cache answered never holds the
    # person, not even for a moment.
    real_get = contact_cache.get
    reads = []

    async def _get(url):
        reads.append(1)
        await contact_cache.record(LINKEDIN, phone=MOBILE, phone_provider="apollo")
        return await real_get(url)

    monkeypatch.setattr(contact_cache, "get", _get)
    claims = _counting(monkeypatch, "claim")

    res = asyncio.run(ce.exa_step(
        "c1", LINKEDIN, "Jane Doe", fields=("phone",), semaphore=asyncio.Semaphore(1),
        late={"candidate_id": "c1", "jobdiva_id": "26-1"}, cached=contact_cache.empty(),
    ))

    assert res["ok"] and res["from_cache"] and res["fields"]["mobilePhone"] == MOBILE
    assert res["role"] == "follower" and res["in_progress_fields"] == []
    assert claims == [] and reads == [1] and exa.posts == []
    assert pgs("SELECT COUNT(*) FROM contact_enrichment_cache WHERE pending_token IS NOT NULL") == [(0,)]
    assert [(x["outcome"], x.get("from_cache")) for x in _lookups(events)] == [("follower", True)]


def test_the_lease_names_only_the_fields_its_run_asks(pgs, exa, events, monkeypatch):
    # Second-round fix (3b): the cache answered the phone (a normal miss)
    # after the caller read it, so the run asks the email only. The lease
    # used to keep naming the phone: a deep search for the phone then waited
    # its whole window behind a run nobody was asking the phone of, and
    # answered in_progress for it.
    monkeypatch.setattr(ce, "EXA_CONTACT_ENRICH_TIMEOUT_S", 0.2)
    monkeypatch.setattr(ce, "EXA_CONTACT_DEEP_TIMEOUT_S", 1)
    exa.answer = {"email": "jane@acme.com", "phone": "+1 415 555 0100"}
    exa.finished = True
    exa.stuck = {"run-1"}                                            # the email run outlives its window
    asyncio.run(contact_cache.record(LINKEDIN, missed=["phone"]))     # another lookup, meanwhile
    sem = asyncio.Semaphore(3)
    kw = {"semaphore": sem, "late": {"candidate_id": "c1"}, "cached": contact_cache.empty(), "wait_retry": False}

    async def _go():
        normal = await ce.exa_step("c1", LINKEDIN, "Jane Doe", fields=("email", "phone"), **kw)
        held = await contact_cache.holder(SLUG, **_H)
        loop = asyncio.get_running_loop()
        started = loop.time()
        deep = await ce.exa_step("c1", LINKEDIN, "Jane Doe", fields=("phone",), deep=True, **kw)
        took = loop.time() - started
        exa.stuck.clear()
        await asyncio.gather(*list(ce._LATE_WATCHES))
        return normal, held, deep, took

    normal, held, deep, took = asyncio.run(_go())

    assert normal["run_id"] == "run-1" and normal["in_progress_fields"] == ["email"]
    assert held["fields"] == ("email",)                             # not ("email", "phone")
    assert [_asked(p) for p in exa.posts] == [("email",), ("phone",)]
    assert deep["role"] == "passthrough" and deep["ok"] and deep["fields"]["mobilePhone"] == MOBILE
    assert deep["in_progress_fields"] == [] and took < 0.9           # never waited on the email run
    assert [(x["outcome"], x.get("reason")) for x in _lookups(events)] == [
        ("leader", None), ("passthrough", "no_overlap"),
    ]
    got = asyncio.run(contact_cache.get(LINKEDIN))
    assert got["email"] == "jane@acme.com" and got["phone"] == MOBILE


def test_a_click_whose_phone_the_cache_missed_meanwhile_still_buys_its_deep_search(pgs, exa, monkeypatch):
    # The router's own chain: Apollo's turn is the gap in which another
    # lookup records a normal phone miss. The normal run asks the email only
    # and outlives its windows (late-watched, holding the lease); the deep
    # search for the phone is bought at once instead of waiting behind it.
    monkeypatch.setattr(sourcing_config, "CONTACT_LOOKUP_PROVIDERS", ("apollo", "exa"))
    monkeypatch.setattr(core_config, "EXA_CONTACT_ENRICH_ENABLED", True)
    monkeypatch.setattr(sourcing_config, "EXA_ONDEMAND_CONTACT_ONLY_WHEN_NO_CONTACT", False)
    monkeypatch.setattr(apollo_phone, "reveal_available", lambda: False)
    monkeypatch.setattr(ce, "EXA_CONTACT_DEEP_EFFORT", "medium")
    monkeypatch.setattr(ce, "EXA_CONTACT_ENRICH_TIMEOUT_S", 0.2)
    monkeypatch.setattr(ce, "EXA_CONTACT_DEEP_TIMEOUT_S", 1)
    exa.finished = True                                              # (each run answers the phone only)
    exa.stuck = {"run-1"}
    steps = []
    real_step = ce.exa_step

    async def _step(*args, **kwargs):
        steps.append("deep" if kwargs.get("deep") else "normal")
        return await real_step(*args, **kwargs)

    async def _apollo(candidate_id, linkedin_url):
        await contact_cache.record(LINKEDIN, missed=["phone"])      # another lookup's miss, meanwhile
        return {"ok": True, "fields": {}}

    def _no_db():
        raise RuntimeError("no db in tests")

    monkeypatch.setattr(ce, "exa_step", _step)
    monkeypatch.setattr(candidates_router, "get_db_connection", _no_db)
    monkeypatch.setattr(candidates_router, "_apollo_enrich_by_linkedin", _apollo)
    body = candidates_router.EnrichCandidateContactRequest(
        candidate_id="cand-1", linkedin_url=LINKEDIN, full_name="Jane Doe", fields=["email", "phone"],
    )

    async def _go():
        res = await candidates_router._enrich_candidate_contact_impl("cand-1", body)
        exa.stuck.clear()
        await asyncio.gather(*list(ce._LATE_WATCHES))
        return res

    res = asyncio.run(_go())

    assert steps == ["normal", "deep"]
    assert [_asked(p) for p in exa.posts] == [("email",), ("phone",)]
    assert res["phone"] == MOBILE and res["in_progress_fields"] == ["email"]


def test_a_lookup_cancelled_while_it_claims_leaves_no_lease(pgs, exa, monkeypatch):
    # Second-round fix (3b): a lookup cancelled (a closed Step-5 stream)
    # while its claim ran in its thread: the claim still committed, and the
    # lease it took was held and renewed by nobody, so every lookup of the
    # person waited up to LEASE_S for it to lapse.
    real_lease_sync = contact_cache._lease_sync
    claimed = []

    def _slow_claim(sql, params, fetch=""):
        if sql is contact_cache.CLAIM_SQL:
            time.sleep(0.4)
            out = real_lease_sync(sql, params, fetch)
            claimed.append(out)
            return out
        return real_lease_sync(sql, params, fetch)

    monkeypatch.setattr(contact_cache, "_lease_sync", _slow_claim)
    unused = []

    async def _go():
        step = asyncio.ensure_future(ce.exa_step(
            "c1", LINKEDIN, "Jane Doe", fields=("phone",), semaphore=asyncio.Semaphore(1),
            cached=contact_cache.empty(), on_unused=lambda: unused.append(1),
        ))
        await asyncio.sleep(0.15)                                    # inside the claim
        step.cancel()
        with pytest.raises(asyncio.CancelledError):
            await step
        await _until(lambda: bool(claimed))                         # the claim committed...
        for _ in range(100):                                         # ...and its lease is given back
            held = await contact_cache.holder(SLUG, **_H)
            if held["state"] == "free":
                break
            await asyncio.sleep(0.02)
        return held

    held = asyncio.run(_go())

    assert claimed and claimed[0] is not None                        # it did win the lease
    assert held == {"state": "free"}
    assert pgs("SELECT COUNT(*) FROM contact_enrichment_cache WHERE pending_token IS NOT NULL") == [(0,)]
    assert exa.posts == [] and unused == [1]
    # Whoever looks the person up next leads at once.
    exa.finished = True
    res = asyncio.run(ce.exa_step("c2", LINKEDIN, "Jane Doe", fields=("phone",)))
    assert res["role"] == "leader" and len(exa.posts) == 1


def test_a_click_behind_another_lookups_run_buys_no_deep_search(pgs, exa, monkeypatch):
    # Router: a normal step that ran out of time behind another lookup's run
    # (in_progress) never buys the deep search for that field.
    monkeypatch.setattr(sourcing_config, "CONTACT_LOOKUP_PROVIDERS", ("apollo", "exa"))
    monkeypatch.setattr(core_config, "EXA_CONTACT_ENRICH_ENABLED", True)
    monkeypatch.setattr(sourcing_config, "EXA_ONDEMAND_CONTACT_ONLY_WHEN_NO_CONTACT", False)
    monkeypatch.setattr(apollo_phone, "reveal_available", lambda: False)
    monkeypatch.setattr(ce, "EXA_CONTACT_DEEP_EFFORT", "medium")
    monkeypatch.setattr(ce, "EXA_CONTACT_ENRICH_TIMEOUT_S", 0.3)
    _hold(("phone",), run_id="run-other")
    exa.finished = True
    exa.stuck = {"run-other"}
    steps = []
    real_step = ce.exa_step

    async def _step(*args, **kwargs):
        steps.append("deep" if kwargs.get("deep") else "normal")
        return await real_step(*args, **kwargs)

    async def _apollo(candidate_id, linkedin_url):
        return {"ok": True, "fields": {}}

    def _no_db():
        raise RuntimeError("no db in tests")

    monkeypatch.setattr(ce, "exa_step", _step)
    monkeypatch.setattr(candidates_router, "get_db_connection", _no_db)
    monkeypatch.setattr(candidates_router, "_apollo_enrich_by_linkedin", _apollo)
    body = candidates_router.EnrichCandidateContactRequest(
        candidate_id="cand-1", linkedin_url=LINKEDIN, full_name="Jane Doe", fields=["phone"],
    )

    res = asyncio.run(candidates_router._enrich_candidate_contact_impl("cand-1", body))

    assert steps == ["normal"]                                       # no deep search behind it
    assert exa.posts == []
    assert res["status"] == "in_progress" and res["in_progress_fields"] == ["phone"]
