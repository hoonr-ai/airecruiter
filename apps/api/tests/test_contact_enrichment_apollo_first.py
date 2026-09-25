"""Contact lookups ask Apollo first; paid Exa only covers an Apollo miss (2026-09-24).

The Exa Agent bills per contact field it fills (~$0.02 email / $0.07 phone plus
agent compute, ~$0.115 a run); Apollo is an ordinary API call. So every contact
path asks Apollo first and falls back to Exa only for what Apollo could not find:

  - sourcing chain (enrich_contact_for_sourcing): Exa only when Apollo misses
  - on-demand chain (Launch PAIR / Step-5 "Find phone",
    _enrich_candidate_contact_impl): Exa never before Apollo, and asked only
    for the fields still missing afterwards
  - Exa deep search (Pass B): the agent is no longer asked for contacts
    (EXA_AGENT_CONTACT_FIELDS off); shown deep-search rows get the regular
    Apollo-first chain after emit, and rows the gates drop cost nothing
"""
import asyncio

import core.config as core_config
from core import sourcing_config
from routers import candidates as candidates_router
from services import contact_enrichment as ce
from services import exa_service as exa_mod
from services.unified_candidate_search import SearchCriteria, UnifiedCandidateSearch

LINKEDIN = "https://www.linkedin.com/in/jane-doe"
EMPTY_FIELDS = {"mobilePhone": "", "workPhone": "", "workEmail": "", "personalEmail": ""}


def _fields(**kw):
    return {**EMPTY_FIELDS, "phoneCandidates": [], **kw}


class _Providers:
    """Records provider calls (in order) and serves canned results."""

    def __init__(self, apollo=None, exa=None, zi_name=None, zi_email=None):
        self.calls = []
        self.exa_fields = []
        self._apollo = apollo or {"ok": False, "message": "no match"}
        self._exa = exa or {"ok": False, "message": "no match"}
        self._zi_name = zi_name or {"ok": False}
        self._zi_email = zi_email or {"ok": False}

    async def apollo(self, candidate_id, linkedin_url):
        self.calls.append("apollo")
        return self._apollo

    async def exa(self, candidate_id, linkedin_url, full_name="", company="", fields=ce.EXA_CONTACT_FIELDS):
        self.calls.append("exa")
        self.exa_fields.append(tuple(fields))
        return self._exa

    async def zi_name(self, *args, **kwargs):
        self.calls.append("zoominfo_name")
        return self._zi_name

    async def zi_email(self, *args, **kwargs):
        self.calls.append("zoominfo_email")
        return self._zi_email

    async def zi_sourcing(self, full_name):
        self.calls.append("zoominfo_name")
        return {}


# ---------------------------------------------------------------------------
# Sourcing chain — enrich_contact_for_sourcing
# ---------------------------------------------------------------------------

def _patch_sourcing(monkeypatch, providers):
    monkeypatch.setenv("CONTACT_ENRICHMENT_INLINE_ENABLED", "true")
    monkeypatch.setattr(ce, "_zoominfo_enrich_for_sourcing", providers.zi_sourcing)
    monkeypatch.setattr(ce, "apollo_enrich_by_linkedin", providers.apollo)
    monkeypatch.setattr(ce, "zoominfo_enrich_by_email", providers.zi_email)
    monkeypatch.setattr(ce, "exa_enrich_by_linkedin", providers.exa)
    monkeypatch.setattr(sourcing_config, "EXA_SOURCING_CONTACT_FALLBACK", True)
    monkeypatch.setattr(sourcing_config, "EXA_SOURCING_CONTACT_ONLY_WHEN_NO_CONTACT", True)
    ce.reset_job_counter("job-src", include_lifetime=True)


def _source(**kw):
    kw.setdefault("jobdiva_id", "job-src")
    kw.setdefault("full_name", "Jane Doe")
    kw.setdefault("include_exa", True)
    kw.setdefault("want_phone", False)
    return asyncio.run(ce.enrich_contact_for_sourcing(LINKEDIN, **kw))


def test_sourcing_apollo_hit_never_reaches_exa(monkeypatch):
    providers = _Providers(apollo={"ok": True, "fields": _fields(workEmail="jane@acme.com")})
    _patch_sourcing(monkeypatch, providers)

    res = _source()

    assert res["provider_used"] == "apollo"
    assert res["workEmail"] == "jane@acme.com"
    assert "exa" not in providers.calls


def test_sourcing_apollo_miss_falls_back_to_exa_after_apollo(monkeypatch):
    providers = _Providers(
        apollo={"ok": False, "message": "Apollo API error (422)"},
        exa={"ok": True, "fields": _fields(workEmail="jane@acme.com")},
    )
    _patch_sourcing(monkeypatch, providers)

    res = _source()

    assert res["provider_used"] == "exa"
    assert providers.calls.index("apollo") < providers.calls.index("exa")
    assert providers.exa_fields == [("email", "phone")]


def test_sourcing_apollo_empty_match_counts_as_a_miss(monkeypatch):
    providers = _Providers(
        apollo={"ok": True, "fields": _fields()},
        exa={"ok": True, "fields": _fields(workEmail="jane@acme.com")},
    )
    _patch_sourcing(monkeypatch, providers)

    assert _source()["provider_used"] == "exa"
    assert providers.calls.count("exa") == 1


def test_sourcing_exa_asked_only_for_fields_the_candidate_lacks(monkeypatch):
    providers = _Providers(exa={"ok": True, "fields": _fields(workEmail="jane@acme.com")})
    _patch_sourcing(monkeypatch, providers)
    # With the no-contact gate off, a candidate holding a phone can reach Exa —
    # which must then only be billed for the email.
    monkeypatch.setattr(sourcing_config, "EXA_SOURCING_CONTACT_ONLY_WHEN_NO_CONTACT", False)

    _source(seed_phone="+1 415 555 0100")

    assert providers.exa_fields == [("email",)]


# ---------------------------------------------------------------------------
# On-demand chain — Launch PAIR / Step-5 "Find phone"
# ---------------------------------------------------------------------------

def _patch_on_demand(monkeypatch, providers):
    def _no_db():
        raise RuntimeError("no db in tests")

    monkeypatch.setattr(candidates_router, "get_db_connection", _no_db)
    monkeypatch.setattr(core_config, "EXA_CONTACT_ENRICH_ENABLED", True)
    monkeypatch.setattr(ce, "zoominfo_enrich_by_email", providers.zi_email)
    monkeypatch.setattr(ce, "zoominfo_enrich_by_name", providers.zi_name)
    monkeypatch.setattr(candidates_router, "_apollo_enrich_by_linkedin", providers.apollo)
    monkeypatch.setattr(candidates_router, "_exa_enrich_by_linkedin", providers.exa)


def _enrich(**kw):
    request = candidates_router.EnrichCandidateContactRequest(
        candidate_id="cand-1", linkedin_url=LINKEDIN, full_name="Jane Doe", **kw
    )
    return asyncio.run(candidates_router._enrich_candidate_contact_impl("cand-1", request))


def test_on_demand_apollo_hit_skips_exa(monkeypatch):
    providers = _Providers(
        apollo={"ok": True, "fields": _fields(workEmail="jane@acme.com", mobilePhone="+14155550100")}
    )
    _patch_on_demand(monkeypatch, providers)

    res = _enrich()

    assert res["provider"] == "apollo"
    assert res["email"] == "jane@acme.com"
    assert res["phone"] == "+14155550100"
    assert "exa" not in providers.calls


def test_on_demand_apollo_miss_falls_back_to_exa(monkeypatch):
    providers = _Providers(
        apollo={"ok": False, "message": "Apollo API error (422)"},
        exa={"ok": True, "fields": _fields(workEmail="jane@acme.com", mobilePhone="+14155550100")},
    )
    _patch_on_demand(monkeypatch, providers)

    res = _enrich()

    assert res["provider"] == "exa"
    assert providers.calls.index("apollo") < providers.calls.index("exa")
    assert providers.exa_fields == [("email", "phone")]


def test_on_demand_exa_asked_only_for_what_apollo_missed(monkeypatch):
    providers = _Providers(
        apollo={"ok": True, "fields": _fields(workEmail="jane@acme.com")},
        exa={"ok": True, "fields": _fields(mobilePhone="+14155550100")},
    )
    _patch_on_demand(monkeypatch, providers)

    res = _enrich()

    assert providers.exa_fields == [("phone",)]
    assert res["email"] == "jane@acme.com"
    assert res["phone"] == "+14155550100"


def test_on_demand_seed_email_means_exa_is_asked_for_the_phone_only(monkeypatch):
    providers = _Providers()
    _patch_on_demand(monkeypatch, providers)

    _enrich(email="jane@acme.com")

    assert providers.calls[-2:] == ["apollo", "exa"]
    assert providers.exa_fields == [("phone",)]


def test_on_demand_seed_phone_plus_apollo_email_skips_exa(monkeypatch):
    providers = _Providers(apollo={"ok": True, "fields": _fields(workEmail="jane@acme.com")})
    _patch_on_demand(monkeypatch, providers)

    res = _enrich(phone="+1 415 555 0100")

    assert res["email"] == "jane@acme.com"
    assert "exa" not in providers.calls


def test_on_demand_zoominfo_hit_skips_apollo_and_exa(monkeypatch):
    providers = _Providers(
        zi_name={"ok": True, "fields": _fields(workEmail="jane@acme.com", mobilePhone="+14155550100")}
    )
    _patch_on_demand(monkeypatch, providers)

    res = _enrich()

    assert res["provider"] == "zoominfo"
    assert providers.calls == ["zoominfo_name"]


# ---------------------------------------------------------------------------
# Exa helper — only the requested fields go on the wire
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload
        self.text = ""

    def json(self):
        return self._payload


def _fake_exa_client(posted):
    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, json=None):
            posted.append(json)
            return _FakeResponse({
                "id": "run-1",
                "status": "completed",
                "output": {"structured": {"contact": {"phone": "+1 (415) 555-0100"}}},
            })

    return _Client


def test_exa_request_carries_only_the_requested_fields(monkeypatch):
    posted = []
    monkeypatch.setattr(ce, "EXA_CONTACT_ENRICH_ENABLED", True)
    monkeypatch.setattr(ce, "EXA_API_KEY", "test-key")
    monkeypatch.setattr(ce.httpx, "AsyncClient", _fake_exa_client(posted))

    res = asyncio.run(
        ce.exa_enrich_by_linkedin("cand-1", LINKEDIN, "Jane Doe", "Acme", fields=("phone",))
    )

    assert res["ok"] is True
    assert res["fields"]["mobilePhone"] == "+14155550100"
    body = posted[0]
    contact_props = body["outputSchema"]["properties"]["contact"]["properties"]
    assert set(contact_props) == {"phone"}
    # The description is what switches on Exa's contact tool — it must survive.
    assert contact_props["phone"]["description"].strip()
    assert "phone number" in body["query"]
    assert "email" not in body["query"]


def test_exa_with_nothing_requested_makes_no_call(monkeypatch):
    posted = []
    monkeypatch.setattr(ce, "EXA_CONTACT_ENRICH_ENABLED", True)
    monkeypatch.setattr(ce, "EXA_API_KEY", "test-key")
    monkeypatch.setattr(ce.httpx, "AsyncClient", _fake_exa_client(posted))

    res = asyncio.run(ce.exa_enrich_by_linkedin("cand-1", LINKEDIN, fields=()))

    assert res["ok"] is False
    assert posted == []


# ---------------------------------------------------------------------------
# Exa deep search (Pass B)
# ---------------------------------------------------------------------------

def _deep_schema_props():
    return exa_mod.build_deep_research_output_schema()[
        "properties"]["candidates"]["items"]["properties"]


def test_deep_search_schema_omits_contact_fields_by_default(monkeypatch):
    monkeypatch.setattr(exa_mod, "EXA_CONTACT_ENRICH_ENABLED", True)
    monkeypatch.setattr(sourcing_config, "EXA_AGENT_CONTACT_FIELDS", False)

    props = _deep_schema_props()

    assert "email" not in props and "phone" not in props


def test_deep_search_contact_fields_need_the_opt_in_and_the_master_switch(monkeypatch):
    monkeypatch.setattr(sourcing_config, "EXA_AGENT_CONTACT_FIELDS", True)
    monkeypatch.setattr(exa_mod, "EXA_CONTACT_ENRICH_ENABLED", True)
    assert {"email", "phone"} <= set(_deep_schema_props())

    monkeypatch.setattr(exa_mod, "EXA_CONTACT_ENRICH_ENABLED", False)
    assert "email" not in _deep_schema_props()


class _FakeExaAgent:
    def __init__(self, entries):
        self.entries = entries
        self.calls = 0

    async def deep_research_candidates(self, **kwargs):
        self.calls += 1
        return self.entries


def _deep_entry(name, slug):
    return {
        "linkedin_url": f"https://www.linkedin.com/in/{slug}",
        "name": name,
        "current_title": "Data Engineer",
        "location": "",
        "recent_companies": [],
        "fit_rationale": "Builds data pipelines.",
    }


def test_deep_search_rows_get_apollo_first_lookup_only_once_shown(monkeypatch):
    monkeypatch.setattr(sourcing_config, "EXA_AGENT_ENABLED", True)
    monkeypatch.setattr(sourcing_config, "EXTERNAL_SOURCE_MIN_SCORE", 60)

    lookups = []

    async def _fake_sourcing_chain(linkedin_url, jobdiva_id=None, **kwargs):
        lookups.append((linkedin_url, kwargs))
        return {"workEmail": "shown@acme.com", "provider_used": "apollo"}

    monkeypatch.setattr(ce, "enrich_contact_for_sourcing", _fake_sourcing_chain)

    svc = UnifiedCandidateSearch()
    agent = _FakeExaAgent([
        _deep_entry("Shown Person", "shown"),
        _deep_entry("Weak Match", "weak"),
        _deep_entry("Blocked Employer", "blocked"),
    ])
    svc.exa_service = agent

    async def _empty_pass_a(criteria):
        return {"candidates": [], "source_type": "LinkedIn-Exa"}

    def _policy(cand, criteria):
        if cand["name"] == "Weak Match":
            cand["match_score"] = 10  # below the external score floor -> dropped
        elif cand["name"] == "Blocked Employer":
            cand["no_contact"] = True  # shown greyed out, never contacted
            cand["match_score"] = None
        else:
            cand["match_score"] = 90
        return cand

    svc._search_exa = _empty_pass_a
    svc.apply_scoring_policy = _policy
    svc._filter_assessment = lambda cand, criteria, enforce_years=False: {
        "passes": True, "matched": [], "missing": [], "excluded": [], "score": 0,
    }

    async def _run():
        return [ev async for ev in svc.search_candidates(SearchCriteria(job_id="job-deep", sources=["Exa"]))]

    events = asyncio.run(_run())

    assert agent.calls == 1
    shown = {ev["data"]["name"]: ev["data"] for ev in events if ev.get("type") == "candidate"}
    assert set(shown) == {"Shown Person", "Blocked Employer"}

    # One paid-chain lookup: the shown row. The dropped row and the
    # no-contact row cost nothing.
    assert [url for url, _ in lookups] == ["https://www.linkedin.com/in/shown"]
    assert lookups[0][1]["include_exa"] is True

    shown_id = shown["Shown Person"]["candidate_id"]
    patches = [
        ev for ev in events
        if ev.get("type") == "candidate_detail" and ev.get("stage") == "contact_enrichment"
    ]
    assert patches == [{
        "type": "candidate_detail",
        "candidate_id": shown_id,
        "stage": "contact_enrichment",
        "patch": {"email": "shown@acme.com"},
    }]
    candidate_idx = next(
        i for i, ev in enumerate(events)
        if ev.get("type") == "candidate" and ev["data"]["candidate_id"] == shown_id
    )
    assert candidate_idx < events.index(patches[0])
