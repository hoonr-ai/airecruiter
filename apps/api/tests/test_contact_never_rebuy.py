"""Never pay twice for the same person's contact (2026-10-15, PR 3a).

  - A field the deep search recently found nothing for is a FINAL miss:
    Launch PAIR never re-buys it, and a recruiter's click gets the contact we
    hold plus "Searched <date>, nothing found" unless it confirms a re-buy
    (CONTACT_CLICK_HONOUR_DEEP_MISS=false restores "a click always retries").
  - Every paid step is written to the contact cache as soon as it answers,
    not minutes later at the end of the chain.
  - A sourcing-time Exa run keeps its answer when the Step-5 stream closes.
"""
import asyncio

import pytest

import core.config as core_config
from core import sourcing_config
from routers import candidates as candidates_router
from services import apollo_phone, contact_cache
from services import contact_enrichment as ce

LINKEDIN = "https://www.linkedin.com/in/jane-doe"
MOBILE = "+14155550100"
LAUNCH = {"trigger": "launch", "source": "LinkedIn-Exa", "match_score": 80}
DEEP_MISSED_PHONE = {"phone_missed": True, "phone_deep_missed": True,
                     "phone_deep_missed_at": "2026-10-01T12:00:00+00:00"}


def _fields(**kw):
    return {"mobilePhone": "", "workPhone": "", "workEmail": "", "personalEmail": "", "phoneCandidates": [], **kw}


@pytest.fixture(autouse=True)
def production_ladder(monkeypatch):
    monkeypatch.setattr(sourcing_config, "CONTACT_LOOKUP_PROVIDERS", ("apollo", "exa"))
    monkeypatch.setattr(ce, "EXA_CONTACT_DEEP_EFFORT", "medium")
    monkeypatch.setattr(core_config, "EXA_CONTACT_ENRICH_ENABLED", True)
    monkeypatch.setattr(sourcing_config, "EXA_ONDEMAND_CONTACT_ONLY_WHEN_NO_CONTACT", False)
    monkeypatch.setattr(apollo_phone, "reveal_available", lambda: False)
    monkeypatch.delenv("CONTACT_CLICK_HONOUR_DEEP_MISS", raising=False)


class _Chain:
    """Fake providers, cache and sourced rows. ``events`` interleaves provider
    calls and cache writes in the order they happen."""

    def __init__(self, cached=None, apollo=None, exa=None, deep=None, rows=None):
        self.events = []
        self.records = []
        self.updates = []
        self._cached = cached or {}
        self._apollo = apollo or {"ok": True, "fields": _fields()}
        self._exa = exa or {"ok": True, "fields": _fields()}
        self._deep = deep or {"ok": True, "fields": _fields()}
        self.rows = rows or []

    async def apollo(self, candidate_id, linkedin_url):
        self.events.append("apollo")
        return self._apollo

    async def exa(self, candidate_id, linkedin_url, full_name="", company="",
                  fields=ce.EXA_CONTACT_FIELDS, deep=False, wait_retry=True):
        self.events.append(("exa_deep" if deep else "exa", tuple(fields)))
        return self._deep if deep else self._exa

    async def cache_get(self, url):
        return {**contact_cache.empty(), **self._cached}

    async def cache_record(self, url, **kw):
        self.records.append(kw)
        self.events.append(("record", kw.get("email") or "", kw.get("phone") or ""))

    def calls(self):
        return [e for e in self.events if e == "apollo" or e[0] in {"exa", "exa_deep"}]

    def connection(self):
        chain = self

        class _Cursor:
            rowcount = 0

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql, params=None):
                if sql.lstrip().upper().startswith("UPDATE"):
                    chain.updates.append(params)
                    self.rowcount = 1

            def fetchall(self):
                return [dict(r) for r in chain.rows]

        class _Conn:
            def cursor(self, cursor_factory=None):
                return _Cursor()

            def commit(self):
                pass

            def close(self):
                pass

        return _Conn()


def _on_demand(monkeypatch, chain, **req):
    monkeypatch.setattr(candidates_router, "get_db_connection", chain.connection)
    monkeypatch.setattr(contact_cache, "get", chain.cache_get)
    monkeypatch.setattr(contact_cache, "record", chain.cache_record)
    monkeypatch.setattr(candidates_router, "_apollo_enrich_by_linkedin", chain.apollo)
    monkeypatch.setattr(candidates_router, "_exa_enrich_by_linkedin", chain.exa)
    body = candidates_router.EnrichCandidateContactRequest(
        candidate_id="cand-1", linkedin_url=LINKEDIN, full_name="Jane Doe", **req
    )
    return asyncio.run(candidates_router._enrich_candidate_contact_impl("cand-1", body))


def _row(**kw):
    return {"id": 1, "candidate_id": "cand-1", "jobdiva_id": "26-1", "source": "LinkedIn-Exa",
            "name": "Jane Doe", "headline": "", "profile_url": LINKEDIN, "email": "", "phone": "",
            "data": {}, "resume_match_percentage": 80, **kw}


# ---------------------------------------------------------------------------
# Final misses
# ---------------------------------------------------------------------------

def test_a_cached_email_and_a_final_phone_return_and_save_the_email(monkeypatch):
    chain = _Chain(cached={"email": "jane@acme.com", **DEEP_MISSED_PHONE}, rows=[_row()])

    res = _on_demand(monkeypatch, chain, **LAUNCH)

    assert chain.calls() == []                                 # nobody is asked again
    assert res["status"] == "success"                          # Launch: no contact found, never "not eligible"
    assert res["email"] == "jane@acme.com" and res["phone"] is None
    assert res["final_missed_fields"] == ["phone"] and res["lookup"]["exa_deep"] == "final_miss"
    assert chain.updates and chain.updates[0][1] == "jane@acme.com"   # saved on the sourced row


def test_a_click_gets_the_held_contact_and_the_notice_not_an_error(monkeypatch):
    chain = _Chain(cached={"email": "jane@acme.com", **DEEP_MISSED_PHONE})

    res = _on_demand(monkeypatch, chain)                      # Rankings Get Contact: both fields

    assert chain.calls() == []
    assert res["status"] == "success" and res["email"] == "jane@acme.com"
    assert res["final_missed_fields"] == ["phone"]
    assert res["checked_at"] == "2026-10-01T12:00:00+00:00"
    assert res["est_usd"] == ce.rebuy_estimate_usd(("phone",), "deep")


def test_launch_ignores_confirm(monkeypatch):
    chain = _Chain(cached={"email": "jane@acme.com", **DEEP_MISSED_PHONE})

    _on_demand(monkeypatch, chain, confirm=True, **LAUNCH)

    assert chain.calls() == []


def test_confirm_with_the_deep_search_off_runs_a_normal_run(monkeypatch):
    monkeypatch.setattr(ce, "EXA_CONTACT_DEEP_EFFORT", "off")
    chain = _Chain(cached={"email": "jane@acme.com", "phone_missed": True, "phone_missed_in_deep_ttl": True})

    res = _on_demand(monkeypatch, chain, fields=["phone"])
    assert chain.calls() == [] and res["status"] == "skipped"
    assert res["est_usd"] == ce.rebuy_estimate_usd(("phone",), "normal")

    chain = _Chain(cached={"email": "jane@acme.com", "phone_missed": True, "phone_missed_in_deep_ttl": True})
    _on_demand(monkeypatch, chain, fields=["phone"], confirm=True)
    assert chain.calls() == ["apollo", ("exa", ("phone",))]


def test_the_apollo_phone_reveal_skips_a_final_phone_but_reruns_on_confirm(monkeypatch):
    revealed = []

    async def _request(candidate_id, linkedin_url, **kw):
        revealed.append(candidate_id)
        return None

    monkeypatch.setattr(apollo_phone, "reveal_available", lambda: True)
    monkeypatch.setattr(apollo_phone, "request_phone", _request)

    # The email is still wanted, so Apollo is asked, but not for the phone.
    chain = _Chain(cached=DEEP_MISSED_PHONE)
    _on_demand(monkeypatch, chain, **LAUNCH)
    assert "apollo" in chain.calls() and revealed == []
    assert ("exa", ("email",)) in chain.calls()
    assert not any(c[0] == "exa_deep" and "phone" in c[1] for c in chain.calls() if c != "apollo")

    chain = _Chain(cached=DEEP_MISSED_PHONE)
    _on_demand(monkeypatch, chain, fields=["phone"], confirm=True)
    assert revealed == ["cand-1"]
    assert chain.calls() == ["apollo", ("exa_deep", ("phone",))]


def test_a_deep_miss_older_than_its_ttl_is_not_final(monkeypatch):
    # contact_cache.get reports *_deep_missed only within the deep-miss TTL.
    chain = _Chain(cached={"email": "jane@acme.com"})

    res = _on_demand(monkeypatch, chain, **LAUNCH)

    assert chain.calls() == ["apollo", ("exa", ("phone",)), ("exa_deep", ("phone",))]
    assert res["final_missed_fields"] == [] and res["checked_at"] is None and res["est_usd"] is None


# ---------------------------------------------------------------------------
# Each paid step is cached as it answers
# ---------------------------------------------------------------------------

def test_each_paid_step_is_cached_before_the_next_one_runs(monkeypatch):
    chain = _Chain(
        apollo={"ok": True, "fields": _fields(workEmail="jane@acme.com")},
        exa={"ok": True, "fields": _fields()},
        deep={"ok": True, "fields": _fields(mobilePhone=MOBILE)},
    )

    _on_demand(monkeypatch, chain)

    assert chain.events == [
        "apollo",
        ("record", "jane@acme.com", ""),              # Apollo's email, before Exa is asked
        ("exa", ("phone",)),
        ("record", "", ""),                           # the normal run's miss
        ("exa_deep", ("phone",)),
        ("record", "", MOBILE),                       # the deep search's phone
        ("record", "jane@acme.com", MOBILE),          # the final record (idempotent)
    ]
    assert chain.records[1]["missed"] == ["phone"]
    assert chain.records[2]["phone_provider"] == "exa_deep"


# ---------------------------------------------------------------------------
# Sourcing: a stream close does not lose a paid Exa answer
# ---------------------------------------------------------------------------

def _sourcing_env(monkeypatch, exa, records):
    async def _record(url, **kw):
        records.append(kw)

    async def _empty(*args, **kwargs):
        return {"ok": False}

    async def _cache_get(url):
        return contact_cache.empty()

    monkeypatch.setenv("CONTACT_ENRICHMENT_INLINE_ENABLED", "true")
    monkeypatch.setattr(contact_cache, "get", _cache_get)
    monkeypatch.setattr(contact_cache, "record", _record)
    monkeypatch.setattr(ce, "apollo_enrich_by_linkedin", _empty)
    monkeypatch.setattr(ce, "exa_enrich_by_linkedin", exa)
    monkeypatch.setattr(sourcing_config, "EXA_SOURCING_CONTACT_FALLBACK", True)
    monkeypatch.setattr(ce, "_EXA_SEMAPHORE", None)    # a fresh one on this test's loop
    ce.reset_job_counter("job-s", include_lifetime=True)


def _sourcing_call():
    return ce.enrich_contact_for_sourcing(
        LINKEDIN, "job-s", full_name="Jane Doe", include_exa=True, want_phone=False,
    )


def test_a_cancelled_sourcing_lookup_still_keeps_the_paid_exa_answer(monkeypatch):
    records = []
    state = {}

    async def _exa(label, url, full_name="", company="", fields=ce.EXA_CONTACT_FIELDS):
        state["started"].set()
        await state["release"].wait()
        return {"ok": True, "fields": _fields(workEmail="jane@acme.com")}

    _sourcing_env(monkeypatch, _exa, records)

    async def _go():
        state["started"], state["release"] = asyncio.Event(), asyncio.Event()
        outer = asyncio.ensure_future(_sourcing_call())
        await state["started"].wait()
        sem = ce._exa_semaphore()
        held = sem._value
        outer.cancel()                                 # the Step-5 stream closed
        with pytest.raises(asyncio.CancelledError):
            await outer
        assert sem._value == held                      # the run still holds its Exa slot
        assert records == []
        state["release"].set()
        await asyncio.gather(*list(ce._SOURCING_EXA_TASKS))
        return sem._value - held

    freed = asyncio.run(_go())

    assert freed == 1                                  # released once the run ended
    assert records == [{"email": "jane@acme.com", "phone": "", "email_provider": "exa",
                        "phone_provider": "exa", "missed": ("email",)}]
    assert not ce._SOURCING_EXA_TASKS


def test_a_sourcing_run_still_going_is_watched_not_dropped(monkeypatch):
    records, watched = [], []

    async def _exa(label, url, full_name="", company="", fields=ce.EXA_CONTACT_FIELDS):
        return {"ok": False, "message": "Exa run timed out", "run_id": "run-5"}

    _sourcing_env(monkeypatch, _exa, records)
    monkeypatch.setattr(ce, "watch_late_exa_run", lambda run_id, url, **kw: watched.append((run_id, kw)))

    res = asyncio.run(_sourcing_call())

    assert res == {}
    assert watched == [("run-5", {"fields": ("email",), "provider": "exa"})]
    assert all(not r.get("missed") for r in records)   # a run still going is not a miss


def test_a_sourcing_breaker_skip_records_nothing_and_gives_the_slots_back(monkeypatch):
    records = []

    async def _exa(label, url, full_name="", company="", fields=ce.EXA_CONTACT_FIELDS):
        return {"ok": False, "message": "Exa out of credits", "fatal": True, "skipped": True}

    _sourcing_env(monkeypatch, _exa, records)

    assert asyncio.run(_sourcing_call()) == {}
    assert ce._JOB_EXA_LIFETIME.get("job-s", 0) == 0
    assert all(not r.get("missed") for r in records)


def _queued_env(monkeypatch, bought):
    """Sourcing with one Exa slot and a cache that holds what the runs found.
    ``bought`` collects the URL of every run created; each run waits on
    ``gate`` (set it to let runs finish)."""
    store = {}
    gate = asyncio.Event()

    async def _exa(label, url, full_name="", company="", fields=ce.EXA_CONTACT_FIELDS):
        bought.append(url)
        await gate.wait()
        return {"ok": True, "fields": _fields(workEmail=f"{url.rsplit('/', 1)[-1]}@acme.com")}

    _sourcing_env(monkeypatch, _exa, [])

    async def _cache_get(url):
        return {**contact_cache.empty(), **store.get(url, {})}

    async def _record(url, **kw):
        if kw.get("email"):
            store.setdefault(url, {}).update(email=kw["email"], email_provider=kw.get("email_provider", ""))
        for f in kw.get("missed") or ():
            if not kw.get(f):
                store.setdefault(url, {})[f"{f}_missed"] = True

    monkeypatch.setattr(contact_cache, "get", _cache_get)
    monkeypatch.setattr(contact_cache, "record", _record)
    monkeypatch.setattr(ce, "EXA_CONTACT_ENRICH_CONCURRENCY", 1)
    ce.reset_job_counter("job-o", include_lifetime=True)
    return store, gate


async def _until(predicate):
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never met")


def _lookup(url, job="job-s"):
    return ce.enrich_contact_for_sourcing(url, job, full_name="Jane Doe", include_exa=True, want_phone=False)


OTHER = "https://www.linkedin.com/in/someone-else"


def test_a_sourcing_lookup_cancelled_while_queued_for_a_slot_buys_nothing(monkeypatch):
    bought = []
    _store, gate = _queued_env(monkeypatch, bought)

    async def _go():
        other = asyncio.ensure_future(_lookup(OTHER, "job-o"))   # holds the only slot
        await _until(lambda: bought == [OTHER])
        queued = asyncio.ensure_future(_lookup(LINKEDIN))
        await _until(lambda: ce._JOB_EXA_COUNTERS.get("job-s") == 1)
        for _ in range(5):
            await asyncio.sleep(0)                              # parked on the semaphore
        queued.cancel()                                         # the Step-5 stream closed
        with pytest.raises(asyncio.CancelledError):
            await queued
        assert ce._JOB_EXA_COUNTERS.get("job-s", 0) == 0        # cap slots given back
        assert ce._JOB_EXA_LIFETIME.get("job-s", 0) == 0
        gate.set()
        await other
        await asyncio.gather(*list(ce._SOURCING_EXA_TASKS))
        return ce._exa_semaphore()._value

    assert asyncio.run(_go()) == 1                              # no slot leaked
    assert bought == [OTHER]                                    # Jane was never bought


def test_a_re_search_after_stop_does_not_buy_the_same_person_twice(monkeypatch):
    # Search 1's lookup of Jane runs (billed) and the stream closes; the
    # re-search's lookup of Jane misses the cache, queues for the slot, and
    # finds Jane's answer there once the first run gives the slot back.
    bought = []
    _store, gate = _queued_env(monkeypatch, bought)

    async def _go():
        first = asyncio.ensure_future(_lookup(LINKEDIN))
        await _until(lambda: bought == [LINKEDIN])
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        ce.reset_job_counter("job-s")                           # the re-search
        second = asyncio.ensure_future(_lookup(LINKEDIN))
        for _ in range(5):
            await asyncio.sleep(0)
        gate.set()
        return await second

    res = asyncio.run(_go())

    assert bought == [LINKEDIN]                                 # one run, not two
    assert res["provider_used"] == "cache" and res["workEmail"] == "jane-doe@acme.com"
    assert ce._JOB_EXA_COUNTERS.get("job-s", 0) == 0            # the reuse cost no cap slot


def test_a_queued_lookup_does_not_rebuy_a_miss_recorded_while_it_waited(monkeypatch):
    bought = []
    store, gate = _queued_env(monkeypatch, bought)

    async def _go():
        other = asyncio.ensure_future(_lookup(OTHER, "job-o"))
        await _until(lambda: bought == [OTHER])
        queued = asyncio.ensure_future(_lookup(LINKEDIN))
        for _ in range(5):
            await asyncio.sleep(0)
        store[LINKEDIN] = {"email_missed": True}                # another worker's run found none
        gate.set()
        await other
        return await queued

    assert asyncio.run(_go()) == {}
    assert bought == [OTHER]


# ---------------------------------------------------------------------------
# Review fixes (PR 3a)
# ---------------------------------------------------------------------------

def test_only_when_no_contact_still_buys_the_email_of_someone_with_no_contact(monkeypatch):
    # No email and no phone, the phone a final miss: not reachable, so the
    # cost switch still lets Exa look for the email.
    monkeypatch.setattr(sourcing_config, "EXA_ONDEMAND_CONTACT_ONLY_WHEN_NO_CONTACT", True)
    chain = _Chain(cached=DEEP_MISSED_PHONE, exa={"ok": True, "fields": _fields(workEmail="jane@acme.com")})

    res = _on_demand(monkeypatch, chain, **LAUNCH)

    assert chain.calls() == ["apollo", ("exa", ("email",))]
    assert res["email"] == "jane@acme.com"


def test_only_when_no_contact_still_skips_a_reachable_candidate(monkeypatch):
    monkeypatch.setattr(sourcing_config, "EXA_ONDEMAND_CONTACT_ONLY_WHEN_NO_CONTACT", True)
    chain = _Chain(cached={"email": "jane@acme.com"})

    _on_demand(monkeypatch, chain, **LAUNCH)

    assert chain.calls() == ["apollo"]


def test_a_click_whose_run_is_still_going_says_so(monkeypatch):
    monkeypatch.setattr(ce, "watch_late_exa_run", lambda *a, **kw: None)
    still_going = {"ok": False, "message": "Exa run timed out", "run_id": "run-normal"}

    chain = _Chain(exa=still_going)
    res = _on_demand(monkeypatch, chain, fields=["phone"])     # the phone button
    assert chain.calls() == ["apollo", ("exa", ("phone",))]    # no deep run while it may answer
    assert res["status"] == "in_progress" and res["in_progress_fields"] == ["phone"]

    # A deep run still going counts too.
    chain = _Chain(cached={"phone_missed": True},
                   deep={"ok": False, "message": "Exa run timed out", "run_id": "run-deep"})
    res = _on_demand(monkeypatch, chain, fields=["phone"])
    assert chain.calls() == ["apollo", ("exa_deep", ("phone",))]
    assert res["status"] == "in_progress" and res["in_progress_fields"] == ["phone"]

    # Launch PAIR always gets "success".
    chain = _Chain(exa=still_going)
    res = _on_demand(monkeypatch, chain, **LAUNCH)
    assert res["status"] == "success" and res["in_progress_fields"] == ["email", "phone"]

    # A click that got what it asked for is a success.
    chain = _Chain(apollo={"ok": True, "fields": _fields(workEmail="jane@acme.com")}, exa=still_going)
    res = _on_demand(monkeypatch, chain)
    assert res["status"] == "success" and res["email"] == "jane@acme.com"
    assert res["in_progress_fields"] == ["phone"]


def test_a_late_sourcing_result_never_logs_the_linkedin_url(monkeypatch, caplog):
    import logging

    class _Resp:
        status_code = 200

        def json(self):
            return {"id": "run-7", "status": "completed",
                    "output": {"structured": {"contact": {"email": "jane@acme.com"}}}}

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None, params=None):
            return _Resp()

    async def _record(url, **kw):
        pass

    monkeypatch.setattr(ce.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(ce, "_EXA_LATE_POLL_S", 0)
    monkeypatch.setattr(ce, "EXA_LATE_RESULT_WAIT_S", 60)
    monkeypatch.setattr(contact_cache, "record", _record)

    with caplog.at_level(logging.DEBUG, logger=ce.logger.name):
        got = asyncio.run(ce.keep_late_exa_result("run-7", LINKEDIN, fields=("email",), provider="exa"))

    assert got["email"] == "jane@acme.com"
    assert any("run-7" in r.getMessage() for r in caplog.records)
    assert not any("linkedin" in r.getMessage().lower() for r in caplog.records)
