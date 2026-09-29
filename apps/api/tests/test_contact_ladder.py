"""The contact ladder since 2026-09-29. User: "make it for apollo and exa only",
"if we are not able to get it then we do deep search", "starting with low cost
to high cost but get the contact details at the end":

    contact cache -> Apollo (URL match, then the personal-phone reveal)
    -> Exa Agent normal run (EXA_CONTACT_ENRICH_EFFORT)
    -> Exa deep search (EXA_CONTACT_DEEP_EFFORT) for whatever is still missing.

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
                  fields=ce.EXA_CONTACT_FIELDS, deep=False):
        self.calls.append(("exa_deep" if deep else "exa", tuple(fields)))
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


def test_the_deep_search_request(monkeypatch):
    bodies = []

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, json=None):
            bodies.append(json)
            return _Resp(200, {"id": "run-1", "status": "completed",
                               "output": {"structured": {"contact": {"phone": "+1 415 555 0100"}}}})

        async def get(self, url, headers=None):
            raise AssertionError("a completed run is not polled")

    monkeypatch.setattr(ce, "EXA_API_KEY", "test-key")
    monkeypatch.setattr(ce, "EXA_CONTACT_ENRICH_ENABLED", True)
    monkeypatch.setattr(ce, "EXA_CONTACT_ENRICH_EFFORT", "low")
    monkeypatch.setattr(ce.httpx, "AsyncClient", _Client)

    deep = asyncio.run(ce.exa_enrich_by_linkedin("c1", LINKEDIN, "Jane Doe", "Acme", fields=("phone",), deep=True))
    normal = asyncio.run(ce.exa_enrich_by_linkedin("c1", LINKEDIN, "Jane Doe", "Acme", fields=("phone",)))

    assert deep["ok"] and deep["fields"]["mobilePhone"] == "+14155550100"
    assert normal["ok"]
    assert bodies[0]["effort"] == "medium" and bodies[1]["effort"] == "low"
    assert "search thoroughly" in bodies[0]["query"] and "search thoroughly" not in bodies[1]["query"]
    assert "personal mobile phone number" in bodies[0]["query"]
    assert list(bodies[0]["outputSchema"]["properties"]["contact"]["properties"]) == ["phone"]


def test_a_missing_key_is_fatal_so_no_deep_search_follows(monkeypatch):
    monkeypatch.setattr(ce, "EXA_API_KEY", "")
    monkeypatch.setattr(ce, "EXA_CONTACT_ENRICH_ENABLED", True)

    res = asyncio.run(ce.exa_enrich_by_linkedin("c1", LINKEDIN, fields=("phone",)))

    assert res == {"ok": False, "message": "EXA_API_KEY not configured", "fatal": True}
