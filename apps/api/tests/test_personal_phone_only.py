"""Personal phone numbers only (user 2026-09-28: "we only need personal phone
number, this is returning work info").

The phone PAIR calls must be the candidate's own mobile/home number. Work lines
(direct dials, office numbers, switchboards) are kept as ``workPhone`` for
reference but never become the candidate's phone, never count as "has a phone"
(so the chain keeps looking for a personal one) and are never cached as one.
Kipplo is asked for personal cell numbers only.
"""
import asyncio
import json
import os
from collections import deque

import psycopg2
import pytest

import core.config as core_config
from core import sourcing_config
from routers import candidates as candidates_router
from services import contact_cache
from services import contact_enrichment as ce
from services import kipplo
from services.unified_candidate_search import SearchCriteria, UnifiedCandidateSearch

LINKEDIN = "https://www.linkedin.com/in/jane-doe"
MOBILE = "+14155550100"
WORK = "+14155550199"


def _fields(**kw):
    return {"mobilePhone": "", "workPhone": "", "workEmail": "", "personalEmail": "", "phoneCandidates": [], **kw}


# ---------------------------------------------------------------------------
# Provider parsers
# ---------------------------------------------------------------------------

def test_apollo_work_or_untyped_number_is_never_the_mobile():
    fields = ce.extract_apollo_contact_fields({"person": {
        "email": "jane@acme.com",
        "sanitized_phone": "+1 415-555-0199",
        "phone_numbers": [{"sanitized_number": "+1 415-555-0177"}],   # untyped
    }})

    assert fields["mobilePhone"] == ""
    assert ce._normalise_phone(fields["workPhone"]) == WORK


def test_apollo_mobile_and_home_numbers_are_personal():
    typed = ce.extract_apollo_contact_fields({"person": {"phone_numbers": [
        {"sanitized_number": "+1 415-555-0199", "type": "work"},
        {"sanitized_number": "+1 415-555-0100", "type": "mobile"},
    ]}})
    home = ce.extract_apollo_contact_fields({"person": {"home_phone": "+1 415-555-0100"}})

    n = ce._normalise_phone
    assert n(typed["mobilePhone"]) == MOBILE and n(typed["workPhone"]) == WORK
    assert n(home["mobilePhone"]) == MOBILE


def test_exa_is_asked_for_a_personal_mobile():
    description = ce._EXA_CONTACT_SCHEMA["properties"]["contact"]["properties"]["phone"]["description"].lower()

    assert "personal mobile" in description and "not an office" in description
    assert "personal mobile phone number" in ce._build_exa_contact_query("Jane Doe", "Acme", LINKEDIN, ("phone",))


# ---------------------------------------------------------------------------
# Kipplo: personal cell numbers only, by default
# ---------------------------------------------------------------------------

class _KipploServer:
    def __init__(self, answer):
        self.answer = answer
        self.requests = []

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        self.requests.append(json)
        return self.answer


class _Response:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def _kipplo_hit(**source):
    return {"success": True, "result": {"hits": {"total": {"value": 1}, "hits": [
        {"_source": {"linkedinurl": "linkedin.com/in/jane-doe", **source}}]}}}


@pytest.fixture
def kipplo_on(monkeypatch):
    monkeypatch.setenv("KIPPLO_API_KEY", "test-key")
    monkeypatch.setenv("KIPPLO_BATCH_SIZE", "1")
    monkeypatch.delenv("KIPPLO_CONTACT_FIELDS", raising=False)
    monkeypatch.setattr(kipplo, "_unavailable_until", 0.0)
    monkeypatch.setattr(kipplo, "_rate_blocked_until", 0.0)
    monkeypatch.setattr(kipplo, "_local_calls", deque())

    async def _no_wait(seconds):
        return None

    monkeypatch.setattr(kipplo, "_sleep", _no_wait)


def test_kipplo_is_asked_for_cell_numbers_only(monkeypatch, kipplo_on):
    server = _KipploServer(_Response(_kipplo_hit(cellnumbers=[
        {"phone": "+1 415-555-0100", "phone_cleaned": "14155550100", "type": "cellphone"}])))
    monkeypatch.setattr(kipplo.httpx, "AsyncClient", server)

    phone = asyncio.run(kipplo.enrich_by_linkedin("c1", LINKEDIN, fields=("email", "phone")))
    email_only = asyncio.run(kipplo.enrich_by_linkedin("c1", LINKEDIN, fields=("email",)))

    assert server.requests[0]["params"]["requested_optional_fields"] == ["cell_numbers"]
    assert phone["ok"] and phone["fields"]["mobilePhone"] == MOBILE
    assert email_only == {"ok": False, "message": "no contact fields requested"}
    assert len(server.requests) == 1                     # no call just for an email


def test_kipplo_non_cell_numbers_are_skipped(monkeypatch, kipplo_on):
    server = _KipploServer(_Response(_kipplo_hit(cellnumbers=[
        {"phone": "+1 415-555-0199", "phone_cleaned": "14155550199", "type": "work"},
        {"phone": "+1 415-555-0100", "phone_cleaned": "14155550100", "type": "cellphone"},
    ])))
    monkeypatch.setattr(kipplo.httpx, "AsyncClient", server)

    res = asyncio.run(kipplo.enrich_by_linkedin("c1", LINKEDIN, fields=("phone",)))

    assert res["fields"]["mobilePhone"] == MOBILE and res["fields"]["phoneCandidates"] == [MOBILE]


# ---------------------------------------------------------------------------
# On-demand chain (Launch PAIR / phone button)
# ---------------------------------------------------------------------------

class _Chain:
    def __init__(self, kipplo=None, zoominfo=None, apollo=None, exa=None, rows=()):
        self.calls = []
        self.results = {"kipplo": kipplo, "zoominfo": zoominfo, "apollo": apollo, "exa": exa}
        self.rows = [dict(r) for r in rows]
        self.updates = []
        self.records = []

    def _answer(self, name):
        return self.results[name] or {"ok": True, "fields": _fields()}

    async def kipplo(self, candidate_id, linkedin_url, fields=("phone",)):
        self.calls.append(("kipplo", tuple(fields)))
        return self._answer("kipplo")

    async def zoominfo(self, *args, **kwargs):
        self.calls.append("zoominfo")
        return self._answer("zoominfo")

    async def apollo(self, candidate_id, linkedin_url):
        self.calls.append("apollo")
        return self._answer("apollo")

    async def exa(self, candidate_id, linkedin_url, full_name="", company="", fields=ce.EXA_CONTACT_FIELDS):
        self.calls.append(("exa", tuple(fields)))
        return self._answer("exa")

    async def cache_get(self, url):
        return contact_cache.empty()

    async def cache_record(self, url, **kw):
        self.records.append(kw)

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
                return chain.rows

        class _Conn:
            def cursor(self, cursor_factory=None):
                return _Cursor()

            def commit(self):
                pass

            def close(self):
                pass

        return _Conn()


def _on_demand(monkeypatch, chain, **req):
    monkeypatch.delenv("KIPPLO_CONTACT_FIELDS", raising=False)
    monkeypatch.setattr(candidates_router, "get_db_connection", chain.connection)
    monkeypatch.setattr(core_config, "EXA_CONTACT_ENRICH_ENABLED", True)
    monkeypatch.setattr(sourcing_config, "EXA_ONDEMAND_CONTACT_ONLY_WHEN_NO_CONTACT", False)
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


def test_a_work_number_is_not_the_phone_and_the_chain_keeps_looking(monkeypatch):
    chain = _Chain(
        zoominfo={"ok": True, "fields": _fields(workEmail="jane@acme.com", workPhone=WORK)},
        exa={"ok": True, "fields": _fields(mobilePhone=MOBILE, phoneCandidates=[MOBILE])},
    )

    res = _on_demand(monkeypatch, chain)

    assert chain.calls[0] == ("kipplo", ("phone",))      # Kipplo: personal phone only
    assert ("exa", ("phone",)) in chain.calls            # the work line did not end the search
    assert res["phone"] == MOBILE and res["phone_source"] == "mobilePhone"
    assert res["phoneCandidates"] == [MOBILE]
    assert res["workPhone"] == WORK                      # still reported, never used
    assert chain.records[-1]["phone"] == MOBILE


def test_only_a_work_number_found_means_no_phone(monkeypatch):
    chain = _Chain(apollo={"ok": True, "fields": _fields(workEmail="jane@acme.com", workPhone=WORK,
                                                          phoneCandidates=[WORK])})

    res = _on_demand(monkeypatch, chain)

    assert res["phone"] is None and res["phone_source"] == "none" and res["phoneCandidates"] == []
    assert res["email"] == "jane@acme.com"
    assert not chain.records[-1].get("phone")            # a work line is never cached as the phone


def test_the_phone_button_only_looks_for_a_phone(monkeypatch):
    chain = _Chain(exa={"ok": True, "fields": _fields(mobilePhone=MOBILE)})

    res = _on_demand(monkeypatch, chain, fields=["phone"])

    assert res["phone"] == MOBILE
    assert ("exa", ("phone",)) in chain.calls and ("exa", ("email", "phone")) not in chain.calls


def test_a_saved_work_number_does_not_count_as_having_a_phone(monkeypatch):
    row = {
        "id": 7, "candidate_id": "cand-1", "jobdiva_id": "26-1", "source": "LinkedIn-Exa",
        "name": "Jane Doe", "headline": "", "profile_url": LINKEDIN, "email": "jane@acme.com",
        "phone": WORK, "resume_match_percentage": 80,
        "data": json.dumps({"zoominfo_contact_enrichment": {"workPhone": "+1 (415) 555-0199", "mobilePhone": ""}}),
    }
    chain = _Chain(kipplo={"ok": True, "fields": _fields(mobilePhone=MOBILE, phoneCandidates=[MOBILE])}, rows=[row])

    res = _on_demand(monkeypatch, chain)

    assert chain.calls[0] == ("kipplo", ("phone",))
    assert res["phone"] == MOBILE
    assert chain.updates and chain.updates[0][0] == MOBILE  # the personal number replaces the work one


def test_a_personal_saved_phone_is_kept(monkeypatch):
    row = {
        "id": 7, "candidate_id": "cand-1", "jobdiva_id": "26-1", "source": "JobDiva-Applicants",
        "name": "Jane Doe", "headline": "", "profile_url": LINKEDIN, "email": "jane@acme.com",
        "phone": MOBILE, "resume_match_percentage": 80, "data": json.dumps({}),
    }
    chain = _Chain(rows=[row])

    _on_demand(monkeypatch, chain)

    assert chain.calls == []                             # has email and a personal phone already


# ---------------------------------------------------------------------------
# Sourcing: a work number never becomes the row's phone
# ---------------------------------------------------------------------------

def test_sourcing_never_puts_a_work_number_on_the_row(monkeypatch):
    async def _chain(*args, **kwargs):
        return {"workEmail": "jane@acme.com", "workPhone": WORK, "mobilePhone": "", "provider_used": "zoominfo"}

    monkeypatch.setattr(ce, "enrich_contact_for_sourcing", _chain)
    cand = {"id": "c1", "name": "Jane Doe", "profile_url": LINKEDIN, "source": "LinkedIn-Exa"}

    asyncio.run(UnifiedCandidateSearch()._apply_contact_enrichment(
        cand, SearchCriteria(job_id="26-1"), overwrite=True))

    assert cand["email"] == "jane@acme.com" and not cand.get("phone")


# ---------------------------------------------------------------------------
# Contact cache: only phones stored as personal are reused
# ---------------------------------------------------------------------------

def test_cache_reuses_only_phones_stored_as_personal(monkeypatch):
    try:
        conn = psycopg2.connect(os.getenv("LAUNCH_REPORT_TEST_DSN", "dbname=postgres"), connect_timeout=2)
    except Exception:
        pytest.skip("no Postgres reachable (set LAUNCH_REPORT_TEST_DSN)")

    class _KeepOpen:
        def __getattr__(self, name):
            return getattr(conn, name)

        def close(self):
            pass

    try:
        with conn.cursor() as cur:
            cur.execute(contact_cache.CREATE_SQL.replace("CREATE TABLE IF NOT EXISTS", "CREATE TEMP TABLE", 1))
            # A row from before the marker (or the backfill): mobile-or-work, unknown which.
            cur.execute("INSERT INTO contact_enrichment_cache (linkedin_slug, phone, phone_provider, phone_found_at) "
                        "VALUES ('jane-doe', %s, 'apollo', NOW())", (WORK,))
        conn.commit()
        monkeypatch.setattr(contact_cache, "_connect", lambda: _KeepOpen())
        monkeypatch.setattr(contact_cache, "_table_ready", True)

        assert asyncio.run(contact_cache.get(LINKEDIN))["phone"] == ""

        asyncio.run(contact_cache.record(LINKEDIN, phone=MOBILE, phone_provider="kipplo"))
        got = asyncio.run(contact_cache.get(LINKEDIN))
        assert got["phone"] == MOBILE and got["phone_provider"] == "kipplo"
    finally:
        conn.rollback()
        conn.close()
