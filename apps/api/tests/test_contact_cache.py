"""Person-level contact cache (services/contact_cache.py, 2026-09-28).

Every Step-5 re-run and every job that surfaced the same LinkedIn profile used
to buy the same contact again, including lookups that had already come back
empty. The cache reuses provider-found contact and remembers clean misses.
"""
import asyncio
import os

import psycopg2
import pytest

from core import sourcing_config
from routers import candidates as candidates_router
from services import contact_cache
from services import contact_enrichment as ce

LINKEDIN = "https://www.linkedin.com/in/jane-doe"


# ---------------------------------------------------------------------------
# Key normalisation
# ---------------------------------------------------------------------------

def test_slug_is_stable_across_url_forms():
    forms = [
        "https://www.linkedin.com/in/Jane-Doe/",
        "http://in.linkedin.com/in/jane-doe?trk=abc",
        "linkedin.com/in/jane%2Ddoe#about",
        "https://uk.linkedin.com/in/JANE-DOE",
    ]
    assert {contact_cache.linkedin_slug(f) for f in forms} == {"jane-doe"}
    assert contact_cache.linkedin_slug("https://www.linkedin.com/company/acme") == ""
    assert contact_cache.linkedin_slug("") == ""


def test_cache_fails_open_without_a_database():
    # conftest blocks real connections: a read is "nothing known", a write is a no-op.
    assert asyncio.run(contact_cache.get(LINKEDIN)) == contact_cache.empty()
    asyncio.run(contact_cache.record(LINKEDIN, email="a@b.com"))


# ---------------------------------------------------------------------------
# Sourcing chain
# ---------------------------------------------------------------------------

class _Store:
    """In-memory stand-in for contact_cache.get / record."""

    def __init__(self, known=None):
        self.known = {**contact_cache.empty(), **(known or {})}
        self.records = []

    async def get(self, url):
        return dict(self.known)

    async def record(self, url, **kw):
        self.records.append(kw)


def _sourcing(monkeypatch, store, exa=None, apollo=None):
    calls = []

    async def _zi(full_name, company=""):
        calls.append("zoominfo")
        return {}

    async def _apollo(candidate_id, url):
        calls.append("apollo")
        return apollo or {"ok": False}

    async def _exa(candidate_id, url, full_name="", company="", fields=ce.EXA_CONTACT_FIELDS):
        calls.append(("exa", tuple(fields)))
        return exa or {"ok": True, "fields": {}}

    monkeypatch.setattr(contact_cache, "get", store.get)
    monkeypatch.setattr(contact_cache, "record", store.record)
    monkeypatch.setattr(ce, "_zoominfo_enrich_for_sourcing", _zi)
    monkeypatch.setattr(ce, "apollo_enrich_by_linkedin", _apollo)
    monkeypatch.setattr(ce, "exa_enrich_by_linkedin", _exa)
    monkeypatch.setattr(sourcing_config, "EXA_SOURCING_CONTACT_FALLBACK", True)
    ce.reset_job_counter("job-c", include_lifetime=True)
    res = asyncio.run(ce.enrich_contact_for_sourcing(
        LINKEDIN, "job-c", full_name="Jane Doe", include_exa=True, want_phone=False,
    ))
    return res, calls


def test_cached_email_is_reused_without_asking_anyone(monkeypatch):
    store = _Store({"email": "jane@gmail.com", "email_provider": "exa"})
    res, calls = _sourcing(monkeypatch, store)

    assert res["provider_used"] == "cache"
    assert res["personalEmail"] == "jane@gmail.com" and res["workEmail"] == ""
    assert calls == []
    assert store.records == []


def test_recent_miss_is_not_bought_again(monkeypatch):
    store = _Store({"email_missed": True})
    res, calls = _sourcing(monkeypatch, store)

    assert res == {}
    assert calls == []


def test_clean_exa_miss_is_remembered(monkeypatch):
    store = _Store()
    res, calls = _sourcing(monkeypatch, store, exa={"ok": True, "fields": {}})

    assert res == {}
    assert ("exa", ("email",)) in calls
    assert store.records[-1]["missed"] == ("email",)


def test_exa_error_is_not_a_miss(monkeypatch):
    store = _Store()
    _sourcing(monkeypatch, store, exa={"ok": False, "message": "Exa run timed out"})

    assert not store.records or not store.records[-1].get("missed")


def test_provider_hit_is_recorded(monkeypatch):
    store = _Store()
    res, calls = _sourcing(monkeypatch, store, apollo={"ok": True, "fields": {
        "workEmail": "jane@acme.com", "personalEmail": "", "mobilePhone": "", "workPhone": "",
    }})

    assert res["provider_used"] == "apollo"
    assert store.records[-1]["email"] == "jane@acme.com"
    assert store.records[-1]["email_provider"] == "apollo"


# ---------------------------------------------------------------------------
# On-demand chain (Launch PAIR / buttons)
# ---------------------------------------------------------------------------

def _on_demand(monkeypatch, store, exa_fields_result=None, **req):
    calls = []

    async def _apollo(candidate_id, url):
        calls.append("apollo")
        return {"ok": True, "fields": {"workEmail": "", "personalEmail": "", "mobilePhone": "", "workPhone": ""}}

    async def _exa(candidate_id, url, name="", company="", fields=ce.EXA_CONTACT_FIELDS):
        calls.append(("exa", tuple(fields)))
        return {"ok": True, "fields": exa_fields_result or {}}

    async def _zi(*a, **k):
        return {"ok": False}

    def _no_db():
        raise RuntimeError("no db")

    monkeypatch.setattr(contact_cache, "get", store.get)
    monkeypatch.setattr(contact_cache, "record", store.record)
    monkeypatch.setattr(candidates_router, "_apollo_enrich_by_linkedin", _apollo)
    monkeypatch.setattr(candidates_router, "_exa_enrich_by_linkedin", _exa)
    monkeypatch.setattr(ce, "zoominfo_enrich_by_name", _zi)
    monkeypatch.setattr(ce, "zoominfo_enrich_by_email", _zi)
    monkeypatch.setattr(candidates_router, "get_db_connection", _no_db)
    monkeypatch.setattr(sourcing_config, "EXA_ONDEMAND_CONTACT_ONLY_WHEN_NO_CONTACT", False)
    body = candidates_router.EnrichCandidateContactRequest(
        linkedin_url=LINKEDIN, full_name="Jane Doe", source="LinkedIn-Exa", match_score=80, **req
    )
    return asyncio.run(candidates_router._enrich_candidate_contact_impl("c1", body)), calls


def test_launch_uses_cached_email_and_buys_only_the_phone(monkeypatch):
    store = _Store({"email": "jane@acme.com"})
    res, calls = _on_demand(
        monkeypatch, store, exa_fields_result={"mobilePhone": "+14155550100"}, trigger="launch",
    )

    assert res["email"] == "jane@acme.com"
    assert res["phone"] == "+14155550100"
    assert ("exa", ("phone",)) in calls
    rec = store.records[-1]
    assert rec["phone"] == "+14155550100"
    assert rec["email"] == ""  # the cache's own value is not re-stamped


def test_launch_honours_a_cached_phone_miss_but_a_click_retries(monkeypatch):
    store = _Store({"email": "jane@acme.com", "phone_missed": True})
    _, calls = _on_demand(monkeypatch, store, trigger="launch")
    assert not any(isinstance(c, tuple) and c[0] == "exa" for c in calls)

    _, calls = _on_demand(monkeypatch, _Store({"email": "jane@acme.com", "phone_missed": True}))
    assert ("exa", ("phone",)) in calls


# ---------------------------------------------------------------------------
# SQL (skips without a reachable Postgres; TEMP table only)
# ---------------------------------------------------------------------------

_DSN = os.getenv("LAUNCH_REPORT_TEST_DSN", "dbname=postgres")


@pytest.fixture
def pg(monkeypatch):
    try:
        conn = psycopg2.connect(_DSN, connect_timeout=2)
    except Exception:
        pytest.skip("no Postgres reachable (set LAUNCH_REPORT_TEST_DSN)")

    class _KeepOpen:
        def __getattr__(self, name):
            return getattr(conn, name)

        def close(self):
            pass

    with conn.cursor() as cur:
        cur.execute(contact_cache.CREATE_SQL.replace("CREATE TABLE IF NOT EXISTS", "CREATE TEMP TABLE", 1))
    monkeypatch.setattr(contact_cache, "_connect", lambda: _KeepOpen())
    monkeypatch.setattr(contact_cache, "_table_ready", True)
    yield conn
    conn.rollback()
    conn.close()


def test_sql_roundtrip(pg):
    run = asyncio.run
    run(contact_cache.record(LINKEDIN, missed=["email", "phone"]))
    got = run(contact_cache.get(LINKEDIN))
    assert got["email_missed"] and got["phone_missed"]

    run(contact_cache.record("https://in.linkedin.com/in/Jane-Doe/", email="Jane@Acme.com", email_provider="exa"))
    got = run(contact_cache.get(LINKEDIN))
    assert got["email"] == "jane@acme.com" and got["email_provider"] == "exa"
    assert got["email_missed"] is False  # a found value beats the older miss
    assert got["phone_missed"] is True

    run(contact_cache.record(LINKEDIN, missed=["email"]))  # a later miss never erases a value
    assert run(contact_cache.get(LINKEDIN))["email"] == "jane@acme.com"


def test_sql_deep_search_misses(pg):
    run = asyncio.run
    run(contact_cache.record(LINKEDIN, missed=["phone"]))
    got = run(contact_cache.get(LINKEDIN))
    assert got["phone_missed"] and not got["phone_deep_missed"]   # next Launch: straight to deep

    run(contact_cache.record(LINKEDIN, deep_missed=["phone"]))
    got = run(contact_cache.get(LINKEDIN))
    assert got["phone_missed"] and got["phone_deep_missed"]       # the whole chain missed

    run(contact_cache.record(LINKEDIN, phone="+14155550100", phone_provider="exa_deep"))
    got = run(contact_cache.get(LINKEDIN))
    assert got["phone"] == "+14155550100" and got["phone_provider"] == "exa_deep"
    assert not got["phone_missed"] and not got["phone_deep_missed"]  # a found value wins


def test_sql_migration_adds_the_deep_miss_columns():
    try:
        conn = psycopg2.connect(_DSN, connect_timeout=2)
    except Exception:
        pytest.skip("no Postgres reachable (set LAUNCH_REPORT_TEST_DSN)")
    try:
        with conn.cursor() as cur:
            cur.execute("""CREATE TEMP TABLE contact_enrichment_cache (
                linkedin_slug TEXT PRIMARY KEY, email TEXT, email_provider TEXT,
                email_found_at TIMESTAMPTZ, email_missed_at TIMESTAMPTZ, phone TEXT,
                phone_provider TEXT, phone_found_at TIMESTAMPTZ, phone_missed_at TIMESTAMPTZ,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
            cur.execute(contact_cache.MIGRATE_SQL)
            cur.execute(contact_cache.MIGRATE_SQL)  # idempotent
            cur.execute("""SELECT column_name FROM information_schema.columns
                           WHERE table_name = 'contact_enrichment_cache'
                             AND table_schema LIKE 'pg_temp%%'""")
            cols = {r[0] for r in cur.fetchall()}
        assert {"phone_personal", "email_deep_missed_at", "phone_deep_missed_at"} <= cols
    finally:
        conn.rollback()
        conn.close()


# ---------------------------------------------------------------------------
# Backfill from past paid lookups (services/contact_cache_backfill.py)
# ---------------------------------------------------------------------------

import datetime as _dt
import json as _json

from services import contact_cache_backfill as bf

_NOW = _dt.datetime(2026, 9, 28, tzinfo=_dt.timezone.utc)


def test_backfill_takes_only_provider_verified_contact():
    rows = [
        {   # on-demand lookup record
            "source": "LinkedIn-Exa", "profile_url": "https://www.linkedin.com/in/amos/",
            "email": "", "phone": "", "updated_at": _NOW,
            "lookup": {"provider": "exa", "enriched_at": "2026-09-20T10:00:00+00:00",
                       "workEmail": "", "personalEmail": "Amos@Gmail.com",
                       "mobilePhone": "", "workPhone": "", "phoneCandidates": ["+1 (415) 555-0100"]},
            "sourcing_provider": None,
        },
        {   # sourcing-chain hit on the row columns
            "source": "LinkedIn-Exa", "profile_url": "https://linkedin.com/in/bea",
            "email": "bea@acme.com", "phone": "", "updated_at": _dt.datetime(2026, 9, 1),
            "lookup": None, "sourcing_provider": "apollo",
        },
        {   # no provider marker: row contact may be LLM text or typed -> ignored
            "source": "LinkedIn-Unipile", "profile_url": "https://linkedin.com/in/cal",
            "email": "cal@acme.com", "phone": "4155550199", "updated_at": _NOW,
            "lookup": None, "sourcing_provider": None,
        },
        {   # placeholders and a cache-sourced value are not facts
            "source": "LinkedIn-Exa", "profile_url": "https://linkedin.com/in/dee",
            "email": "Auto_123@jobdiva.com", "phone": "12", "updated_at": _NOW,
            "lookup": None, "sourcing_provider": "cache",
        },
    ]
    people = bf.plan(rows, now=_NOW)

    assert set(people) == {"amos", "bea"}
    assert people["amos"]["email"] == "amos@gmail.com"
    assert people["amos"]["phone"] == "+14155550100"
    assert people["amos"]["email_provider"] == "exa"
    assert people["bea"]["email"] == "bea@acme.com" and people["bea"]["phone"] is None


def test_backfill_keeps_the_newest_and_drops_stale():
    old = {"source": "LinkedIn-Exa", "profile_url": "https://linkedin.com/in/amos",
           "email": "old@acme.com", "phone": "", "lookup": None, "sourcing_provider": "zoominfo",
           "updated_at": _NOW - _dt.timedelta(days=30)}
    new = {**old, "email": "new@acme.com", "sourcing_provider": "apollo",
           "updated_at": _NOW - _dt.timedelta(days=2)}
    stale = {**old, "profile_url": "https://linkedin.com/in/zed", "email": "zed@acme.com",
             "updated_at": _NOW - _dt.timedelta(days=400)}

    people = bf.plan([new, old, stale], now=_NOW)

    assert people["amos"]["email"] == "new@acme.com"
    assert people["amos"]["email_provider"] == "apollo"
    assert "zed" not in people


def test_backfill_runs_once_against_sql(pg):
    with pg.cursor() as cur:
        cur.execute("""CREATE TEMP TABLE sourced_candidates (
            id SERIAL PRIMARY KEY, source TEXT, profile_url TEXT, email TEXT, phone TEXT,
            data JSONB, updated_at TIMESTAMP DEFAULT NOW())""")
        cur.execute(
            "INSERT INTO sourced_candidates (source, profile_url, email, phone, data) VALUES (%s,%s,%s,%s,%s)",
            ("LinkedIn-Exa", "https://www.linkedin.com/in/amos", "", "",
             _json.dumps({"zoominfo_contact_enrichment": {
                 "provider": "exa", "enriched_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                 "workEmail": "amos@shell.com", "mobilePhone": "+14155550100"}})),
        )
        cur.execute(
            "INSERT INTO sourced_candidates (source, profile_url, email, phone, data) VALUES (%s,%s,%s,%s,%s)",
            ("LinkedIn-Exa", "https://www.linkedin.com/in/bea", "bea@acme.com", "",
             _json.dumps({"enhanced_info": {"contact_enrichment_provider": "apollo"}})),
        )
    # A live value already in the cache is newer than the backfill's: kept.
    asyncio.run(contact_cache.record("https://linkedin.com/in/bea", email="bea@new.com", email_provider="exa"))

    first = bf.run_once(pg)
    second = bf.run_once(pg)

    assert first["people"] == 2 and first["emails"] == 2 and first["phones"] == 1
    assert second is None  # the done-marker stops a re-run
    amos = asyncio.run(contact_cache.get("https://linkedin.com/in/amos"))
    # Backfilled phones came from mobile OR work numbers, so they are stored but
    # never reused as the candidate's personal phone (see phone_personal).
    assert amos["email"] == "amos@shell.com" and amos["phone"] == ""
    with pg.cursor() as cur:
        cur.execute("SELECT phone, phone_personal FROM contact_enrichment_cache WHERE linkedin_slug = 'amos'")
        assert cur.fetchone() == ("+14155550100", None)
    assert asyncio.run(contact_cache.get("https://linkedin.com/in/bea"))["email"] == "bea@new.com"
