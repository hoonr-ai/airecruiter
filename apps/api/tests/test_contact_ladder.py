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
import json

import pytest

import core.config as core_config
from core import sourcing_config
from routers import candidates as candidates_router
from services import apollo_phone, contact_cache
from services import contact_enrichment as ce

LINKEDIN = "https://www.linkedin.com/in/jane-doe"     # holdout bucket 40
ALEX = "https://www.linkedin.com/in/alex-smith"       # holdout bucket 93


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


def _on_demand(monkeypatch, chain, linkedin=LINKEDIN, db=None, **req):
    def _no_db():
        raise RuntimeError("no db in tests")

    monkeypatch.setattr(candidates_router, "get_db_connection", db.connect if db else _no_db)
    monkeypatch.setattr(contact_cache, "get", chain.cache_get)
    monkeypatch.setattr(contact_cache, "record", chain.cache_record)
    monkeypatch.setattr(candidates_router, "_kipplo_enrich_by_linkedin", chain.kipplo)
    monkeypatch.setattr(candidates_router, "_apollo_enrich_by_linkedin", chain.apollo)
    monkeypatch.setattr(candidates_router, "_exa_enrich_by_linkedin", chain.exa)
    monkeypatch.setattr(ce, "zoominfo_enrich_by_email", chain.zoominfo)
    monkeypatch.setattr(ce, "zoominfo_enrich_by_name", chain.zoominfo)
    req.setdefault("full_name", "Jane Doe")
    body = candidates_router.EnrichCandidateContactRequest(candidate_id="cand-1", linkedin_url=linkedin, **req)
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


def test_a_recent_deep_miss_is_final_and_a_click_rebuys_it_only_on_confirm(monkeypatch):
    # 2026-10-15 ("never pay twice for the same person"): this used to be
    # Launch-only, and every click re-bought the deep miss (ca1c503b).
    cached = {"email": "jane@acme.com", "phone_missed": True, "phone_deep_missed": True,
              "phone_deep_missed_at": "2026-10-01T12:00:00+00:00"}

    chain = _Chain(cached=cached)
    res = _on_demand(monkeypatch, chain, **LAUNCH)
    assert chain.exa_calls() == []
    assert res["status"] == "success" and res["email"] == "jane@acme.com"
    assert res["final_missed_fields"] == ["phone"] and res["lookup"]["exa_deep"] == "final_miss"

    chain = _Chain(cached=cached)
    res = _on_demand(monkeypatch, chain, fields=["phone"])     # the phone button
    assert chain.calls == ["apollo"]                            # no paid Exa run; Apollo still asked
    assert res["status"] == "skipped" and res["reason"] == "recent_deep_miss"
    assert res["checked_at"] == "2026-10-01T12:00:00+00:00" and 0.15 <= res["est_usd"] <= 0.30

    chain = _Chain(cached=cached)
    _on_demand(monkeypatch, chain, fields=["phone"], confirm=True)   # "Search again"
    assert chain.exa_calls() == [("exa_deep", ("phone",))]


def test_click_honour_knob_off_restores_a_click_always_retrying(monkeypatch):
    monkeypatch.setenv("CONTACT_CLICK_HONOUR_DEEP_MISS", "false")
    cached = {"email": "jane@acme.com", "phone_missed": True, "phone_deep_missed": True}

    chain = _Chain(cached=cached)
    res = _on_demand(monkeypatch, chain, fields=["phone"])
    assert chain.exa_calls() == [("exa", ("phone",)), ("exa_deep", ("phone",))]
    assert res["status"] == "success" and res["final_missed_fields"] == []

    chain = _Chain(cached=cached)
    _on_demand(monkeypatch, chain, **LAUNCH)                   # Launch still honours it
    assert chain.exa_calls() == []


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
    from services import exa_telemetry

    bodies = []
    sent = []
    monkeypatch.setattr(exa_telemetry, "_send", lambda event_type, attrs: sent.append(attrs))
    refused = _Resp(400, {"error": "dataSources not available"})
    _exa_env(monkeypatch, bodies, [refused, refused, FOUND])

    res = asyncio.run(ce.exa_enrich_by_linkedin("c1", LINKEDIN, "Jane Doe", fields=("phone",), deep=True))

    assert res["ok"] and res["fields"]["mobilePhone"] == "+14155550100"
    # The metadata guard goes first: the same Fiber body without metadata,
    # refused too, so it was not the metadata (no latch) ...
    assert bodies[0]["dataSources"] == [{"provider": "fiber"}] and "metadata" in bodies[0]
    assert bodies[1]["dataSources"] == [{"provider": "fiber"}] and "metadata" not in bodies[1]
    assert exa_telemetry.run_metadata_enabled()
    # ... then the existing fallback: web search alone, still without metadata.
    assert "dataSources" not in bodies[2] and "Fiber" not in bodies[2]["query"]
    assert "search thoroughly" in bodies[2]["query"] and bodies[2]["effort"] == "medium"
    assert "metadata" not in bodies[2]
    guard = [(a["outcome"], a.get("http_status")) for a in sent if a.get("endpoint") == "agent_guard"]
    assert guard == [("sources_refused", 400)]  # not metadata_rejected, not rejected


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

def test_no_deep_search_while_the_normal_run_for_the_same_field_is_still_going(monkeypatch):
    # The normal run gets its second window; still going after it, it is
    # watched in the background and no deep run is bought for its fields
    # (it used to start at once and both were billed for the same field).
    watched = []
    monkeypatch.setattr(ce, "watch_late_exa_run", lambda run_id, url, **kw: watched.append((run_id, kw["provider"])))
    chain = _Chain(
        exa={"ok": False, "message": "Exa run timed out", "run_id": "run-normal"},
        deep={"ok": False, "message": "Exa run timed out", "run_id": "run-deep"},
    )

    res = _on_demand(monkeypatch, chain)

    assert chain.exa_calls() == [("exa", ("email", "phone"))]
    assert chain.wait_retry == [True]
    assert watched == [("run-normal", "exa")]
    assert res["lookup"]["exa_deep"] == "-"
    assert all(not r["missed"] and not r["deep_missed"] for r in chain.records)   # not an answer


def test_a_deep_first_field_is_not_held_back_by_a_slow_normal_run(monkeypatch):
    watched = []
    monkeypatch.setattr(ce, "watch_late_exa_run", lambda run_id, url, **kw: watched.append((run_id, kw["provider"])))
    chain = _Chain(
        cached={"phone_missed": True},
        exa={"ok": False, "message": "Exa run timed out", "run_id": "run-normal"},
        deep={"ok": True, "fields": _fields(mobilePhone="+14155550100")},
    )

    res = _on_demand(monkeypatch, chain, **LAUNCH)

    assert chain.exa_calls() == [("exa", ("email",)), ("exa_deep", ("phone",))]
    assert res["phone"] == "+14155550100"


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
    # Both lookups ran to the end and were kept: each step as it answered
    # (the normal run, the deep search), then the final record.
    assert len(chain.records) == 6


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



# ---------------------------------------------------------------------------
# Launch PAIR's deep email run for someone already holding a personal phone:
# a holdout (EXA_DEEP_SKIP_REACHABLE_PCT, user 2026-10-14). conftest turns it
# off; these tests run it at 50%: jane-doe (bucket 40) skips, alex-smith (93)
# is the control arm.
# ---------------------------------------------------------------------------

@pytest.fixture
def holdout(monkeypatch):
    """The holdout at 50% with its decision log kept in memory; a decision is
    also appended to ``log["calls"]`` when a test points it at a chain's."""
    from services import contact_holdout

    log = {"decisions": [], "outcomes": [], "calls": None}

    async def _decision(url, **kw):
        log["decisions"].append({"slug": contact_cache.linkedin_slug(url), **kw})
        if log["calls"] is not None:
            log["calls"].append(f"decision:{kw['arm']}")
        return True

    async def _outcome(url, outcome):
        log["outcomes"].append((contact_cache.linkedin_slug(url), outcome))

    monkeypatch.setattr(sourcing_config, "EXA_DEEP_SKIP_REACHABLE_PCT", 50)
    monkeypatch.setattr(contact_holdout, "record_decision", _decision)
    monkeypatch.setattr(contact_holdout, "record_deep_outcome", _outcome)
    return log


@pytest.fixture
def events(monkeypatch):
    from services import exa_telemetry

    sent = []
    monkeypatch.setattr(exa_telemetry, "_send", lambda event_type, attrs: sent.append((event_type, attrs)))
    return sent


def _summaries(events):
    return [a for t, a in events if t == "ContactLookup" and a["outcome"] == "lookup"]


MOBILE = "+14155550100"
# The commonest deep email run: the email was missed lately (a sourcing-time
# normal run), so Launch sends it straight to the deep search while the
# normal run looks for the phone, and finds it.
REACHABLE = dict(cached={"email_missed": True}, exa={"ok": True, "fields": _fields(mobilePhone=MOBILE)})


def test_holdout_buckets_are_pinned_per_person():
    assert ce.deep_skip_bucket(LINKEDIN) == 40
    assert ce.deep_skip_bucket("https://uk.linkedin.com/in/Alex-Smith/?trk=x") == 93
    assert (ce.deep_skip_arm(LINKEDIN, 50), ce.deep_skip_arm(ALEX, 50)) == ("skip", "control")
    assert (ce.deep_skip_arm(LINKEDIN, 40), ce.deep_skip_arm(ALEX, 100)) == ("control", "skip")
    assert ce.deep_skip_arm(LINKEDIN, 0) == "off"
    assert ce.deep_skip_arm("https://example.com/jane", 50) == "off"


def test_holdout_pct_is_parsed_guarded_and_clamped(monkeypatch):
    for raw, want in (("", 50), ("abc", 50), ("12.5", 50), ("30", 30), (" 70 ", 70), ("150", 100), ("-5", 0)):
        monkeypatch.setenv("EXA_DEEP_SKIP_REACHABLE_PCT", raw)
        assert sourcing_config._env_pct("EXA_DEEP_SKIP_REACHABLE_PCT", 50) == want, raw
    monkeypatch.delenv("EXA_DEEP_SKIP_REACHABLE_PCT")
    assert sourcing_config._env_pct("EXA_DEEP_SKIP_REACHABLE_PCT", 50) == 50
    for value, want in (("garbage", 50), (None, 50), (250, 100), (-1, 0), (25, 25)):
        monkeypatch.setattr(sourcing_config, "EXA_DEEP_SKIP_REACHABLE_PCT", value)
        assert ce.deep_skip_reachable_pct() == want, value


def test_skip_arm_drops_the_deep_email_run_once_a_phone_is_held(monkeypatch, holdout, events):
    chain = _Chain(**REACHABLE)

    res = _on_demand(monkeypatch, chain, **LAUNCH)

    assert chain.exa_calls() == [("exa", ("phone",))]
    assert res["status"] == "success" and res["phone"] == MOBILE and res["email"] is None
    assert res["lookup"]["exa_deep"] == "-" and res["lookup"]["exa_deep_skip"] == "skip"
    # Not a deep miss: the next Launch decides again, and a click still buys it.
    assert all(not r["deep_missed"] for r in chain.records)
    assert holdout["decisions"] == [{
        "slug": "jane-doe", "arm": "skip", "pct": 50, "jobdiva_id": None, "candidate_id": "cand-1",
        "phone_from": "found", "normal_outcome": "phone",
    }]
    assert holdout["outcomes"] == []
    [summary] = _summaries(events)
    assert (summary["deep_skip_eligible"], summary["deep_skip_arm"], summary["deep_skip_pct"]) == (True, "skip", 50)
    assert summary["first_exposure"] is True and summary["deep_asked"] == "-"


def test_control_arm_logs_the_decision_before_buying_the_deep_email(monkeypatch, holdout):
    from services import exa_telemetry

    chain = _Chain(**REACHABLE, deep={"ok": True, "fields": _fields(workEmail="alex@acme.com")})
    holdout["calls"] = chain.calls
    arms = []
    exa = chain.exa

    async def _exa(*args, deep=False, **kw):
        if deep:
            arms.append(exa_telemetry.current().get("arm"))
        return await exa(*args, deep=deep, **kw)

    chain.exa = _exa

    res = _on_demand(monkeypatch, chain, linkedin=ALEX, **LAUNCH)

    assert chain.exa_calls() == [("exa", ("phone",)), ("exa_deep", ("email",))]
    assert chain.calls.index("decision:control") < chain.calls.index(("exa_deep", ("email",)))
    assert arms == ["control"]                       # ExaCall / run metadata say which arm
    assert res["email"] == "alex@acme.com" and res["lookup"]["exa_deep_skip"] == "control"
    assert [d["arm"] for d in holdout["decisions"]] == ["control"]
    assert holdout["outcomes"] == [("alex-smith", "email")]


def test_control_arm_logs_a_deep_miss_and_a_run_still_going(monkeypatch, holdout):
    _on_demand(monkeypatch, _Chain(**REACHABLE), linkedin=ALEX, **LAUNCH)
    monkeypatch.setattr(ce, "watch_late_exa_run", lambda run_id, url, **kw: True)
    _on_demand(monkeypatch, _Chain(**REACHABLE, deep={"ok": False, "message": "Exa run timed out", "run_id": "r"}),
               linkedin=ALEX, **LAUNCH)
    _on_demand(monkeypatch, _Chain(**REACHABLE, deep={"ok": False, "message": "Exa create error (500)"}),
               linkedin=ALEX, **LAUNCH)

    assert [o for _, o in holdout["outcomes"]] == ["miss", "in_progress", "failed"]


@pytest.mark.parametrize("pct", [0, 100])
def test_the_holdout_share_is_the_knob(monkeypatch, holdout, pct):
    monkeypatch.setattr(sourcing_config, "EXA_DEEP_SKIP_REACHABLE_PCT", pct)
    chain = _Chain(**REACHABLE)

    res = _on_demand(monkeypatch, chain, linkedin=ALEX, **LAUNCH)

    if pct == 0:   # off: everyone keeps the deep email run, nothing is logged
        assert chain.exa_calls() == [("exa", ("phone",)), ("exa_deep", ("email",))]
        assert holdout["decisions"] == [] and "exa_deep_skip" not in res["lookup"]
    else:          # everyone eligible skips it
        assert chain.exa_calls() == [("exa", ("phone",))]
        assert [d["arm"] for d in holdout["decisions"]] == ["skip"]


def test_a_held_seed_phone_is_reachable_without_any_exa_run(monkeypatch, holdout):
    chain = _Chain(cached={"email_missed": True})

    _on_demand(monkeypatch, chain, phone="+1 (415) 555-0100", **LAUNCH)

    assert chain.exa_calls() == []
    assert holdout["decisions"][0]["phone_from"] == "seed"
    assert holdout["decisions"][0]["normal_outcome"] == "-"


def test_without_a_held_phone_there_is_no_holdout(monkeypatch, holdout):
    chain = _Chain()

    res = _on_demand(monkeypatch, chain, **LAUNCH)

    assert chain.exa_calls() == [("exa", ("email", "phone")), ("exa_deep", ("email", "phone"))]
    assert holdout["decisions"] == [] and "exa_deep_skip" not in res["lookup"]


def test_a_final_phone_miss_keeps_the_deep_email_run(monkeypatch, holdout):
    # No phone to fall back on: the email is their only channel.
    chain = _Chain(cached={"phone_missed": True, "phone_deep_missed": True,
                           "phone_deep_missed_at": "2026-10-01T12:00:00+00:00"})

    _on_demand(monkeypatch, chain, **LAUNCH)

    assert chain.exa_calls() == [("exa", ("email",)), ("exa_deep", ("email",))]
    assert holdout["decisions"] == []


def test_a_work_phone_is_not_a_held_phone(monkeypatch, holdout):
    chain = _Chain(cached={"email_missed": True}, exa={"ok": True, "fields": _fields(workPhone="+14155550199")})

    _on_demand(monkeypatch, chain, **LAUNCH)

    assert chain.exa_calls() == [("exa", ("phone",)), ("exa_deep", ("email", "phone"))]
    assert holdout["decisions"] == []


def test_a_junk_seed_phone_does_not_hide_a_found_mobile(monkeypatch, holdout):
    # "N/A" is not a phone; the mobile the normal run finds is.
    chain = _Chain(**REACHABLE)

    res = _on_demand(monkeypatch, chain, phone="N/A", **LAUNCH)

    assert chain.exa_calls() == [("exa", ("phone",))]
    assert res["phone"] == MOBILE and holdout["decisions"][0]["phone_from"] == "found"


def test_a_recruiters_click_is_never_in_the_holdout(monkeypatch, holdout):
    chain = _Chain(**REACHABLE, deep={"ok": True, "fields": _fields(workEmail="jane@acme.com")})

    res = _on_demand(monkeypatch, chain)            # Rankings Get Contact: no trigger

    assert chain.exa_calls() == [("exa", ("phone",)), ("exa_deep", ("email",))]
    assert res["email"] == "jane@acme.com" and holdout["decisions"] == []


def test_each_lookup_sends_one_summary_with_its_seeds_and_no_contact(monkeypatch, events):
    chain = _Chain(exa={"ok": True, "fields": _fields(mobilePhone=MOBILE)})

    _on_demand(monkeypatch, chain, email="jane@gmail.com", company_name="Acme", **LAUNCH)

    [summary] = _summaries(events)
    assert summary["trigger"] == "launch" and summary["path"] == "ondemand"
    assert summary["existing_rows"] == 0 and summary["seed_from"] == "request"
    assert (summary["seeded_email"], summary["has_name"], summary["has_company"]) == (True, True, True)
    assert (summary["normal_asked"], summary["normal_outcome"]) == ("phone", "phone")
    assert (summary["deep_asked"], summary["deep_outcome"]) == ("-", "-")
    assert summary["deep_skip_eligible"] is False and "deep_skip_arm" not in summary
    assert summary["deep_skip_pct"] == 0             # conftest: the holdout is off
    assert not any("@" in str(v) or "linkedin" in str(v).lower() for v in summary.values())


def test_lookup_outcomes_are_enums_never_free_text():
    enum = candidates_router._lookup_outcome_enum
    assert [enum(v) for v in ("email", "phone", "email,phone", "miss", "-", "final_miss", "", None)] == [
        "email", "phone", "email,phone", "miss", "-", "final_miss", "-", "-",
    ]
    assert enum("Exa create error (402): https://x @y") == "failed"
    assert enum("Exa run timed out", still_going=True) == "in_progress"


# ---------------------------------------------------------------------------
# Launch PAIR's seeds (2026-10-15): the JobDiva email the row holds, its name
# and company; never the phone.
# ---------------------------------------------------------------------------

class _Db:
    """sourced_candidates for the chain: the prefetch reads ``rows``; each
    UPDATE's parameters are kept."""

    def __init__(self, rows):
        self.rows = rows
        self.updates = []

    def connect(self):
        db = self

        class _Cur:
            rowcount = 1

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql, params=None):
                if not sql.strip().upper().startswith("SELECT"):
                    db.updates.append(params)

            def fetchall(self):
                return [dict(r) for r in db.rows]

        class _Conn:
            def cursor(self, **kw):
                return _Cur()

            def commit(self):
                pass

            def close(self):
                pass

        return _Conn()


def test_a_seeded_email_buys_only_the_phone_and_is_never_overwritten(monkeypatch):
    db = _Db([{
        "id": 7, "candidate_id": "cand-1", "jobdiva_id": "26-1", "source": "JobDiva-TalentSearch", "name": "Jane Doe",
        "headline": "", "profile_url": LINKEDIN, "email": "jane@gmail.com", "phone": "", "data": "{}",
        "resume_match_percentage": 80,
    }])
    chain = _Chain(
        apollo={"ok": True, "fields": _fields(workEmail="jane.doe@acme.com")},
        exa={"ok": True, "fields": _fields(mobilePhone=MOBILE)},
    )
    launch = {**LAUNCH, "source": "JobDiva-TalentSearch"}

    res = _on_demand(monkeypatch, chain, db=db, email="jane@gmail.com", **launch)

    assert chain.exa_calls() == [("exa", ("phone",))]
    # EXA_ONDEMAND_CONTACT_ONLY_WHEN_NO_CONTACT stays off: a seeded email never
    # stops the phone lookup.
    assert sourcing_config.EXA_ONDEMAND_CONTACT_ONLY_WHEN_NO_CONTACT is False
    [(phone, email, blob, row_id)] = db.updates
    assert (phone, email, row_id) == (MOBILE, None, 7)     # the row keeps its email
    assert json.loads(blob)["zoominfo_contact_enrichment"]["workEmail"] == "jane.doe@acme.com"
    assert res["phone"] == MOBILE and res["updated_rows"] == 1


def test_a_clicks_request_email_keeps_the_old_save(monkeypatch):
    # Rankings Get Contact sends the row's email too and shows the answer's.
    db = _Db([{
        "id": 7, "candidate_id": "cand-1", "jobdiva_id": "26-1", "source": "JobDiva-TalentSearch", "name": "Jane Doe",
        "headline": "", "profile_url": LINKEDIN, "email": "jane@gmail.com", "phone": "", "data": "{}",
        "resume_match_percentage": 80,
    }])
    chain = _Chain(apollo={"ok": True, "fields": _fields(workEmail="jane.doe@acme.com")},
                   exa={"ok": True, "fields": _fields(mobilePhone=MOBILE)})

    _on_demand(monkeypatch, chain, db=db, email="jane@gmail.com")

    [(_phone, email, _blob, _id)] = db.updates
    assert email == "jane.doe@acme.com"


def test_a_found_email_still_fills_a_row_whose_email_came_from_the_row(monkeypatch):
    db = _Db([{
        "id": 7, "candidate_id": "cand-1", "jobdiva_id": "26-1", "source": "LinkedIn-Exa", "name": "Jane Doe",
        "headline": "", "profile_url": LINKEDIN, "email": "", "phone": "", "data": "{}",
        "resume_match_percentage": 80,
    }])
    chain = _Chain(exa={"ok": True, "fields": _fields(workEmail="jane@acme.com", mobilePhone=MOBILE)})

    _on_demand(monkeypatch, chain, db=db, **LAUNCH)

    [(phone, email, _blob, _id)] = db.updates
    assert (phone, email) == (MOBILE, "jane@acme.com")


def test_a_placeholder_seed_email_is_ignored(monkeypatch):
    chain = _Chain(exa={"ok": True, "fields": _fields(workEmail="jane@acme.com", mobilePhone=MOBILE)})

    res = _on_demand(monkeypatch, chain, email="Auto_123@jobdiva.com", **LAUNCH)

    assert chain.exa_calls() == [("exa", ("email", "phone"))]
    assert res["email"] == "jane@acme.com"


def test_after_a_deferred_search_launch_asks_one_normal_run_for_both(monkeypatch):
    # A deferred sourcing lookup asked no Exa, so it cached no miss: Launch's
    # normal run asks for the email and the phone together, deep only after.
    chain = _Chain(exa={"ok": True, "fields": _fields(mobilePhone=MOBILE)})

    _on_demand(monkeypatch, chain, **LAUNCH)

    assert chain.exa_calls() == [("exa", ("email", "phone")), ("exa_deep", ("email",))]
