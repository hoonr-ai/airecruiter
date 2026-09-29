"""The contact ladder since 2026-09-29. User: "make it for apollo and exa only",
"if we are not able to get it then we do deep search", "starting with low cost
to high cost but get the contact details at the end":

    contact cache -> Apollo (URL match, then the personal-phone reveal)
    -> Exa Agent normal run (EXA_CONTACT_ENRICH_EFFORT)
    -> Exa deep search (EXA_CONTACT_DEEP_EFFORT, with Fiber.ai attached through
       Exa Connect: EXA_CONTACT_DEEP_DATA_SOURCES) for whatever is still missing.

Kipplo and ZoomInfo stay off unless listed in CONTACT_LOOKUP_PROVIDERS. These
tests run with the production defaults; conftest lists every provider and
turns the deep search off for the older chain tests.
"""
import asyncio

import pytest

import core.config as core_config
from core import sourcing_config
from routers import candidates as candidates_router
from services import apollo_phone, contact_cache
from services import contact_enrichment as ce

LINKEDIN = "https://www.linkedin.com/in/jane-doe"


def _fields(**kw):
    return {"mobilePhone": "", "workPhone": "", "workEmail": "", "personalEmail": "", "phoneCandidates": [], **kw}


@pytest.fixture(autouse=True)
def production_ladder(monkeypatch):
    monkeypatch.setattr(sourcing_config, "CONTACT_LOOKUP_PROVIDERS", ("apollo", "exa"))
    monkeypatch.setattr(ce, "EXA_CONTACT_DEEP_EFFORT", "medium")
    monkeypatch.setattr(core_config, "EXA_CONTACT_ENRICH_ENABLED", True)
    monkeypatch.setattr(sourcing_config, "EXA_ONDEMAND_CONTACT_ONLY_WHEN_NO_CONTACT", False)
    monkeypatch.setattr(apollo_phone, "reveal_available", lambda: False)


class _Chain:
    """Fake providers; every call is recorded in order."""

    def __init__(self, apollo=None, exa=None, deep=None, cached=None):
        self.calls = []
        self.records = []
        self.wait_retry = []
        self._apollo = apollo or {"ok": True, "fields": _fields()}
        self._exa = exa or {"ok": True, "fields": _fields()}
        self._deep = deep or {"ok": True, "fields": _fields()}
        self._cached = cached or {}

    async def kipplo(self, *args, **kwargs):
        self.calls.append("kipplo")
        return {"ok": True, "fields": _fields(mobilePhone="+14155550999")}

    async def zoominfo(self, *args, **kwargs):
        self.calls.append("zoominfo")
        return {"ok": True, "fields": _fields(workEmail="zi@acme.com")}

    async def zoominfo_sourcing(self, *args, **kwargs):
        self.calls.append("zoominfo")
        return _fields(workEmail="zi@acme.com")

    async def apollo(self, candidate_id, linkedin_url):
        self.calls.append("apollo")
        return self._apollo

    async def exa(self, candidate_id, linkedin_url, full_name="", company="",
                  fields=ce.EXA_CONTACT_FIELDS, deep=False, wait_retry=True):
        self.calls.append(("exa_deep" if deep else "exa", tuple(fields)))
        self.wait_retry.append(wait_retry)
        return self._deep if deep else self._exa

    async def cache_get(self, url):
        return {**contact_cache.empty(), **self._cached}

    async def cache_record(self, url, **kw):
        self.records.append(kw)

    def exa_calls(self):
        return [c for c in self.calls if isinstance(c, tuple)]


def _on_demand(monkeypatch, chain, **req):
    def _no_db():
        raise RuntimeError("no db in tests")

    monkeypatch.setattr(candidates_router, "get_db_connection", _no_db)
    monkeypatch.setattr(contact_cache, "get", chain.cache_get)
    monkeypatch.setattr(contact_cache, "record", chain.cache_record)
    monkeypatch.setattr(candidates_router, "_kipplo_enrich_by_linkedin", chain.kipplo)
    monkeypatch.setattr(candidates_router, "_apollo_enrich_by_linkedin", chain.apollo)
    monkeypatch.setattr(candidates_router, "_exa_enrich_by_linkedin", chain.exa)
    monkeypatch.setattr(ce, "zoominfo_enrich_by_email", chain.zoominfo)
    monkeypatch.setattr(ce, "zoominfo_enrich_by_name", chain.zoominfo)
    body = candidates_router.EnrichCandidateContactRequest(
        candidate_id="cand-1", linkedin_url=LINKEDIN, full_name="Jane Doe", **req
    )
    return asyncio.run(candidates_router._enrich_candidate_contact_impl("cand-1", body))


LAUNCH = {"trigger": "launch", "source": "LinkedIn-Exa", "match_score": 80}


# ---------------------------------------------------------------------------
# Launch PAIR / the phone button
# ---------------------------------------------------------------------------

def test_apollo_then_exa_then_the_deep_search_and_nothing_else(monkeypatch):
    chain = _Chain()

    res = _on_demand(monkeypatch, chain)

    assert chain.calls == ["apollo", ("exa", ("email", "phone")), ("exa_deep", ("email", "phone"))]
    assert res["lookup"]["kipplo"] == "-" and res["lookup"]["zoominfo"] == "-"
    assert res["lookup"]["exa"] == "miss" and res["lookup"]["exa_deep"] == "miss"


def test_the_deep_search_is_asked_only_for_what_the_normal_run_missed(monkeypatch):
    chain = _Chain(
        exa={"ok": True, "fields": _fields(workEmail="jane@acme.com")},
        deep={"ok": True, "fields": _fields(mobilePhone="+14155550100")},
    )

    res = _on_demand(monkeypatch, chain)

    assert chain.exa_calls() == [("exa", ("email", "phone")), ("exa_deep", ("phone",))]
    assert res["email"] == "jane@acme.com" and res["email_provider"] == "exa"
    assert res["phone"] == "+14155550100" and res["phone_provider"] == "exa_deep"
    assert res["lookup"]["exa"] == "email" and res["lookup"]["exa_deep"] == "phone"
    assert chain.records[-1]["phone_provider"] == "exa_deep"


def test_cheaper_steps_first_and_no_deep_search_once_found(monkeypatch):
    chain = _Chain(
        apollo={"ok": True, "fields": _fields(workEmail="jane@acme.com")},
        exa={"ok": True, "fields": _fields(mobilePhone="+14155550100")},
    )

    res = _on_demand(monkeypatch, chain)

    assert chain.calls == ["apollo", ("exa", ("phone",))]
    assert res["provider"] == "apollo" and res["phone_provider"] == "exa"


def test_nothing_is_bought_when_the_candidate_has_both(monkeypatch):
    chain = _Chain()

    _on_demand(monkeypatch, chain, email="jane@acme.com", phone="+14155550100")

    assert chain.calls == []


def test_no_deep_search_after_a_failure_it_would_repeat(monkeypatch):
    chain = _Chain(exa={"ok": False, "message": "Exa create error (402)", "fatal": True})

    res = _on_demand(monkeypatch, chain)

    assert chain.exa_calls() == [("exa", ("email", "phone"))]
    assert res["lookup"]["exa"] == "Exa create error (402)" and res["lookup"]["exa_deep"] == "-"


def test_deep_search_after_a_normal_run_that_timed_out(monkeypatch):
    chain = _Chain(
        exa={"ok": False, "message": "Exa run timed out"},
        deep={"ok": True, "fields": _fields(mobilePhone="+14155550100")},
    )

    res = _on_demand(monkeypatch, chain, email="jane@acme.com")

    assert chain.exa_calls() == [("exa", ("phone",)), ("exa_deep", ("phone",))]
    assert res["phone"] == "+14155550100"


def test_each_step_records_its_own_miss(monkeypatch):
    chain = _Chain(exa={"ok": True, "fields": _fields(workEmail="jane@acme.com")})

    _on_demand(monkeypatch, chain)

    assert chain.records[-1]["missed"] == ["phone"]
    assert chain.records[-1]["deep_missed"] == ["phone"]


def test_launch_goes_straight_to_the_deep_search_after_a_recent_normal_miss(monkeypatch):
    chain = _Chain(
        cached={"email": "jane@acme.com", "phone_missed": True},
        deep={"ok": True, "fields": _fields(mobilePhone="+14155550100")},
    )

    res = _on_demand(monkeypatch, chain, **LAUNCH)

    assert chain.exa_calls() == [("exa_deep", ("phone",))]
    assert res["phone"] == "+14155550100"


def test_launch_does_not_rebuy_a_recent_deep_miss_but_a_click_does(monkeypatch):
    cached = {"email": "jane@acme.com", "phone_missed": True, "phone_deep_missed": True}

    chain = _Chain(cached=cached)
    _on_demand(monkeypatch, chain, **LAUNCH)
    assert chain.exa_calls() == []

    chain = _Chain(cached=cached)
    _on_demand(monkeypatch, chain)          # the phone button: a deliberate click
    assert chain.exa_calls() == [("exa", ("phone",)), ("exa_deep", ("phone",))]


def test_the_deep_search_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(ce, "EXA_CONTACT_DEEP_EFFORT", "off")
    chain = _Chain()

    _on_demand(monkeypatch, chain)

    assert chain.calls == ["apollo", ("exa", ("email", "phone"))]


def test_kipplo_and_zoominfo_come_back_by_listing_them(monkeypatch):
    monkeypatch.setattr(sourcing_config, "CONTACT_LOOKUP_PROVIDERS", ("Kipplo", "zoominfo", "apollo", "exa"))
    chain = _Chain()

    _on_demand(monkeypatch, chain)

    assert chain.calls[0] == "kipplo" and "zoominfo" in chain.calls


# ---------------------------------------------------------------------------
# Sourcing (search time): emails only, Apollo then the normal Exa run
# ---------------------------------------------------------------------------

def test_sourcing_asks_apollo_then_exa_for_the_email_and_never_deep(monkeypatch):
    chain = _Chain(exa={"ok": True, "fields": _fields(workEmail="jane@acme.com")})
    monkeypatch.setenv("CONTACT_ENRICHMENT_INLINE_ENABLED", "true")
    monkeypatch.setattr(contact_cache, "get", chain.cache_get)
    monkeypatch.setattr(contact_cache, "record", chain.cache_record)
    monkeypatch.setattr(ce, "kipplo_enrich_by_linkedin", chain.kipplo)
    monkeypatch.setattr(ce, "_zoominfo_enrich_for_sourcing", chain.zoominfo_sourcing)
    monkeypatch.setattr(ce, "apollo_enrich_by_linkedin", chain.apollo)
    monkeypatch.setattr(ce, "zoominfo_enrich_by_email", chain.zoominfo)
    monkeypatch.setattr(ce, "exa_enrich_by_linkedin", chain.exa)
    monkeypatch.setattr(sourcing_config, "EXA_SOURCING_CONTACT_FALLBACK", True)
    ce.reset_job_counter("job-ladder", include_lifetime=True)

    res = asyncio.run(ce.enrich_contact_for_sourcing(
        LINKEDIN, "job-ladder", full_name="Jane Doe", include_exa=True, want_phone=False,
    ))

    assert chain.calls == ["apollo", ("exa", ("email",))]
    assert res["provider_used"] == "exa" and res["workEmail"] == "jane@acme.com"


# ---------------------------------------------------------------------------
# The deep-search request itself
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = ""

    def json(self):
        return self._payload


def _exa_client(bodies, answers):
    """httpx.AsyncClient stand-in: records each create body, answers in order."""
    answers = list(answers)

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, json=None):
            bodies.append(json)
            return answers.pop(0)

        async def get(self, url, headers=None):
            raise AssertionError("a completed run is not polled")

    return _Client


FOUND = _Resp(200, {"id": "run-1", "status": "completed",
                    "output": {"structured": {"contact": {"phone": "+1 415 555 0100"}}}})


def _exa_env(monkeypatch, bodies, answers):
    monkeypatch.setattr(ce, "EXA_API_KEY", "test-key")
    monkeypatch.setattr(ce, "EXA_CONTACT_ENRICH_ENABLED", True)
    monkeypatch.setattr(ce, "EXA_CONTACT_ENRICH_EFFORT", "low")
    monkeypatch.setattr(ce, "EXA_CONTACT_DEEP_DATA_SOURCES", "fiber")
    monkeypatch.setattr(ce.httpx, "AsyncClient", _exa_client(bodies, answers))


def _phone(schema):
    return schema["properties"]["contact"]["properties"]["phone"]


def test_the_deep_search_request(monkeypatch):
    bodies = []
    _exa_env(monkeypatch, bodies, [FOUND, FOUND])

    deep = asyncio.run(ce.exa_enrich_by_linkedin("c1", LINKEDIN, "Jane Doe", "Acme", fields=("phone",), deep=True))
    normal = asyncio.run(ce.exa_enrich_by_linkedin("c1", LINKEDIN, "Jane Doe", "Acme", fields=("phone",)))

    assert deep["ok"] and deep["fields"]["mobilePhone"] == "+14155550100"
    assert normal["ok"]
    assert bodies[0]["effort"] == "medium" and bodies[1]["effort"] == "low"
    assert "search thoroughly" in bodies[0]["query"] and "search thoroughly" not in bodies[1]["query"]
    assert "personal mobile phone number" in bodies[0]["query"]
    assert list(bodies[0]["outputSchema"]["properties"]["contact"]["properties"]) == ["phone"]
    # Fiber.ai rides along with the deep search only, and is asked for by name
    # (an attached partner is a tool the agent MAY call).
    assert bodies[0]["dataSources"] == [{"provider": "fiber"}]
    assert "Fiber.ai people database" in bodies[0]["query"]
    assert "Fiber.ai" in _phone(bodies[0]["outputSchema"])["description"]
    assert "dataSources" not in bodies[1] and "Fiber" not in bodies[1]["query"]
    assert "Fiber" not in _phone(bodies[1]["outputSchema"])["description"]
    # The shared schema is not changed by the deep variant.
    assert "Fiber" not in _phone(ce._exa_contact_schema(("phone",)))["description"]


def test_deep_search_goes_web_only_when_exa_refuses_fiber(monkeypatch):
    bodies = []
    _exa_env(monkeypatch, bodies, [_Resp(400, {"error": "dataSources not available"}), FOUND])

    res = asyncio.run(ce.exa_enrich_by_linkedin("c1", LINKEDIN, "Jane Doe", fields=("phone",), deep=True))

    assert res["ok"] and res["fields"]["mobilePhone"] == "+14155550100"
    assert bodies[0]["dataSources"] == [{"provider": "fiber"}]
    assert "dataSources" not in bodies[1] and "Fiber" not in bodies[1]["query"]
    assert "search thoroughly" in bodies[1]["query"] and bodies[1]["effort"] == "medium"


def test_deep_search_without_data_sources_when_switched_off(monkeypatch):
    bodies = []
    _exa_env(monkeypatch, bodies, [FOUND])
    monkeypatch.setattr(ce, "EXA_CONTACT_DEEP_DATA_SOURCES", "")

    asyncio.run(ce.exa_enrich_by_linkedin("c1", LINKEDIN, "Jane Doe", fields=("phone",), deep=True))

    assert "dataSources" not in bodies[0] and "Fiber" not in bodies[0]["query"]


def test_a_missing_key_is_fatal_so_no_deep_search_follows(monkeypatch):
    monkeypatch.setattr(ce, "EXA_API_KEY", "")
    monkeypatch.setattr(ce, "EXA_CONTACT_ENRICH_ENABLED", True)

    res = asyncio.run(ce.exa_enrich_by_linkedin("c1", LINKEDIN, fields=("phone",)))

    assert res == {"ok": False, "message": "EXA_API_KEY not configured", "fatal": True}


# ---------------------------------------------------------------------------
# QA 2026-09-29: contacts found but never shown (slow deep searches, a whole
# group waiting for its slowest candidate, lost create answers)
# ---------------------------------------------------------------------------

def test_a_slow_normal_run_is_not_waited_on_twice_when_the_deep_search_follows(monkeypatch):
    watched = []
    monkeypatch.setattr(ce, "watch_late_exa_run", lambda run_id, url, **kw: watched.append((run_id, kw["provider"])))
    chain = _Chain(
        exa={"ok": False, "message": "Exa run timed out", "run_id": "run-normal"},
        deep={"ok": False, "message": "Exa run timed out", "run_id": "run-deep"},
    )

    _on_demand(monkeypatch, chain)

    assert chain.wait_retry == [False, False]
    assert watched == [("run-normal", "exa"), ("run-deep", "exa_deep")]


def test_with_the_deep_search_off_the_normal_run_keeps_its_second_window(monkeypatch):
    monkeypatch.setattr(ce, "EXA_CONTACT_DEEP_EFFORT", "off")
    chain = _Chain()

    _on_demand(monkeypatch, chain)

    assert chain.wait_retry == [True]


class _Req:
    def __init__(self, accept):
        self.headers = {"accept": accept}


def _stream(monkeypatch, chain, people, slow=None, **req):
    """Run the grouped endpoint with Accept: application/x-ndjson; returns the
    parsed lines. ``slow`` = {candidate_id: seconds} delays those lookups."""
    import json as _json

    real_impl = candidates_router._enrich_candidate_contact_impl

    async def _impl(candidate_id, item):
        await asyncio.sleep((slow or {}).get(candidate_id, 0))
        return await real_impl(candidate_id, item)

    monkeypatch.setattr(candidates_router, "_enrich_candidate_contact_impl", _impl)
    monkeypatch.setattr(candidates_router, "ENRICH_STREAM_PING_S", 0.02)

    def _no_db():
        raise RuntimeError("no db in tests")

    monkeypatch.setattr(candidates_router, "get_db_connection", _no_db)
    monkeypatch.setattr(contact_cache, "get", chain.cache_get)
    monkeypatch.setattr(contact_cache, "record", chain.cache_record)
    monkeypatch.setattr(candidates_router, "_apollo_enrich_by_linkedin", chain.apollo)
    monkeypatch.setattr(candidates_router, "_exa_enrich_by_linkedin", chain.exa)
    body = candidates_router.EnrichCandidateContactsRequest(
        candidates=[candidates_router.EnrichCandidateContactRequest(**p) for p in people], **req
    )

    async def _go():
        res = await candidates_router.enrich_candidate_contacts(
            body, http_request=_Req("application/x-ndjson"), user=None
        )
        lines = []
        async for chunk in res.body_iterator:
            lines.append(_json.loads(chunk))
        return res, lines

    return asyncio.run(_go())


def _person(slug, **kw):
    return {"candidate_id": f"cand-{slug}", "linkedin_url": f"https://www.linkedin.com/in/{slug}",
            "full_name": slug.title(), **kw}


def test_grouped_lookups_stream_each_result_as_it_ends(monkeypatch):
    chain = _Chain(exa={"ok": True, "fields": _fields(workEmail="x@acme.com", mobilePhone="+14155550100")})

    res, lines = _stream(monkeypatch, chain, [_person("ann"), _person("bob")], slow={"cand-ann": 0.1})

    assert res.media_type == "application/x-ndjson"
    assert res.headers["x-accel-buffering"] == "no"
    results = [line for line in lines if "index" in line]
    # bob finished first and was not held back by ann's slower lookup.
    assert [r["index"] for r in results] == [1, 0]
    assert all(r["result"]["phone"] == "+14155550100" for r in results)
    assert any(line.get("ping") for line in lines)      # the wait kept the connection alive
    assert lines[-1] == {"done": True}


def test_grouped_lookups_finish_even_if_the_caller_goes_away(monkeypatch):
    chain = _Chain(exa={"ok": True, "fields": _fields(mobilePhone="+14155550100")})
    real_impl = candidates_router._enrich_candidate_contact_impl

    async def _impl(candidate_id, item):
        await asyncio.sleep(0.05 if candidate_id == "cand-ann" else 0)
        return await real_impl(candidate_id, item)

    def _no_db():
        raise RuntimeError("no db in tests")

    monkeypatch.setattr(candidates_router, "_enrich_candidate_contact_impl", _impl)
    monkeypatch.setattr(candidates_router, "get_db_connection", _no_db)
    monkeypatch.setattr(contact_cache, "get", chain.cache_get)
    monkeypatch.setattr(contact_cache, "record", chain.cache_record)
    monkeypatch.setattr(candidates_router, "_apollo_enrich_by_linkedin", chain.apollo)
    monkeypatch.setattr(candidates_router, "_exa_enrich_by_linkedin", chain.exa)
    body = candidates_router.EnrichCandidateContactsRequest(
        candidates=[candidates_router.EnrichCandidateContactRequest(**p) for p in (_person("ann"), _person("bob"))]
    )

    async def _go():
        res = await candidates_router.enrich_candidate_contacts(
            body, http_request=_Req("application/x-ndjson"), user=None
        )
        it = res.body_iterator
        first = await it.__anext__()          # bob's answer
        await it.aclose()                     # the browser tab closes
        await asyncio.sleep(0.2)              # ann's lookup keeps going
        return first

    first = asyncio.run(_go())

    assert b'"index": 1' in first
    assert len(chain.records) == 2            # both lookups ran to the end and were kept


def test_a_lost_create_answer_is_picked_up_not_bought_again(monkeypatch):
    import httpx as _httpx

    query = ce._build_exa_contact_query("Jane Doe", "", LINKEDIN, ("phone",))
    posts = []

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, json=None):
            posts.append(json)
            raise _httpx.ReadTimeout("create answered late")

        async def get(self, url, headers=None, params=None):
            if url.endswith("/agent/runs"):
                return _Resp(200, {"data": [
                    {"id": "run-old", "status": "completed", "request": {"query": "another person"}},
                    {"id": "run-7", "status": "completed", "request": {"query": query},
                     "output": {"structured": {"contact": {"phone": "+1 415 555 0100"}}}},
                ]})
            raise AssertionError(f"unexpected poll {url}")

    monkeypatch.setattr(ce, "EXA_API_KEY", "test-key")
    monkeypatch.setattr(ce, "EXA_CONTACT_ENRICH_ENABLED", True)
    monkeypatch.setattr(ce.httpx, "AsyncClient", _Client)

    res = asyncio.run(ce.exa_enrich_by_linkedin("c1", LINKEDIN, "Jane Doe", fields=("phone",)))

    assert res["ok"] and res["fields"]["mobilePhone"] == "+14155550100"
    assert len(posts) == 1                    # no second paid run


def test_wait_retry_false_hands_back_the_run_still_going(monkeypatch):
    async def _attempt(client, headers, body, candidate_id, run_id="", timeout_s=None):
        return {"message": "Exa run timed out", "retry": "wait", "run_id": "run-9"}

    monkeypatch.setattr(ce, "EXA_API_KEY", "test-key")
    monkeypatch.setattr(ce, "EXA_CONTACT_ENRICH_ENABLED", True)
    monkeypatch.setattr(ce, "_exa_contact_attempt", _attempt)

    res = asyncio.run(ce.exa_enrich_by_linkedin("c1", LINKEDIN, fields=("phone",), deep=True, wait_retry=False))

    assert res == {"ok": False, "message": "Exa run timed out", "run_id": "run-9"}


def test_a_late_deep_result_is_kept_for_the_next_lookup(monkeypatch):
    polls = [
        _Resp(200, {"id": "run-9", "status": "running"}),
        _Resp(200, {"id": "run-9", "status": "completed",
                    "output": {"structured": {"contact": {"phone": "+1 415 555 0100"}}}}),
    ]

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None, params=None):
            return polls.pop(0)

    records, fills = [], []

    async def _record(url, **kw):
        records.append(kw)

    monkeypatch.setattr(ce.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(ce, "_EXA_LATE_POLL_S", 0)
    monkeypatch.setattr(ce, "EXA_LATE_RESULT_WAIT_S", 60)
    monkeypatch.setattr(contact_cache, "record", _record)
    monkeypatch.setattr(ce, "_fill_sourced_rows_sync", fills.append)

    got = asyncio.run(ce.keep_late_exa_result(
        "run-9", LINKEDIN, candidate_id="cand-1", jobdiva_id="26-1", fields=("phone",), provider="exa_deep",
    ))

    assert got == {"email": "", "phone": "+14155550100"}
    assert records == [{"email": "", "phone": "+14155550100", "email_provider": "exa_deep",
                        "phone_provider": "exa_deep", "deep_missed": []}]
    assert fills == [{"email": None, "phone": "+14155550100", "candidate_id": "cand-1", "jobdiva_id": "26-1"}]

