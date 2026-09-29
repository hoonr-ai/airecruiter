"""Kipplo contact lookup by LinkedIn URL (services/kipplo.py, 2026-09-28).

Kipplo is the first provider in both contact chains (user: "Kipplo can be now
the first source"). It bills per field group it finds, so it is asked only for
the fields a candidate lacks; ZoomInfo -> Apollo -> Exa run only for what it
did not find. Payload shapes below are the ones Kipplo returned when probed,
with made-up values.
"""
import asyncio
import json
import logging
import os
from collections import deque

import httpx
import psycopg2
import pytest

import core.config as core_config
from core import sourcing_config
from routers import candidates as candidates_router
from services import contact_cache
from services import contact_enrichment as ce
from services import kipplo

LINKEDIN = "https://www.linkedin.com/in/jane-doe"


def _hit(slug="jane-doe", business=(), secondary=(), cells=()):
    """A Kipplo answer with one hit, as the API returns it."""
    return {
        "success": True,
        "integration_name": "Enrich Contact Details By linkedin url",
        "result": {"hits": {"total": {"value": 1, "relation": "eq"}, "hits": [{
            "_index": "people_v6",
            "_source": {
                "linkedinurl": f"linkedin.com/in/{slug}",
                "businessemails": [{"email": e, "status": "valid"} for e in business],
                "secondaryemails": [{"email": e} for e in secondary],
                "cellnumbers": [
                    {"phone": p, "phone_cleaned": "".join(c for c in p if c.isdigit()), "type": "cellphone"}
                    for p in cells
                ],
            },
        }]}},
        "error": None,
        "credits_charged": 1.0 * bool(business) + 1.0 * bool(secondary) + 5.0 * bool(cells) or None,
    }


NO_HIT = {"success": True, "result": {"hits": {"total": {"value": 0, "relation": "eq"}, "hits": []}},
          "error": None, "credits_charged": None}
NO_CREDITS = {"success": False, "result": None, "credits_charged": None, "error_code": "INSUFFICIENT_CREDITS",
              "error": "Insufficient credits: Insufficient credits to complete this operation"}


def _rate_limited(*exhausted):
    windows = {"second": 1, "minute": 10, "day": 10000, "month": 100000}
    return {"detail": {"error": True, "error_code": "RATE_LIMIT_EXCEEDED", "rate_limits": {
        w: {"limit": n, "used": n if w in exhausted else 0, "remaining": 0 if w in exhausted else n}
        for w, n in windows.items()
    }}}


class _Response:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _Client:
    """Stands in for httpx.AsyncClient: serves queued answers, records requests."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.requests = []

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        self.requests.append({"url": url, "headers": headers, "json": json})
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.now += max(0.0, seconds)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setenv("KIPPLO_API_KEY", "test-key")
    # These tests cover mechanics with emails and phones. Production asks
    # Kipplo for personal phones only (tests/test_personal_phone_only.py).
    monkeypatch.setenv("KIPPLO_CONTACT_FIELDS", "email,phone")
    # List lookups (batching) are covered with batching on; production has it
    # off while the integration answers one profile per request
    # (test_by_default_each_profile_is_its_own_request).
    monkeypatch.setenv("KIPPLO_BATCH_SIZE", "10")
    monkeypatch.setattr(kipplo, "_unavailable_until", 0.0)
    monkeypatch.setattr(kipplo, "_unavailable_reason", "")
    monkeypatch.setattr(kipplo, "_rate_blocked_until", 0.0)
    monkeypatch.setattr(kipplo, "_local_calls", deque())
    monkeypatch.setattr(kipplo, "_batch_off_reason", "")
    monkeypatch.setattr(kipplo, "_batch_verified", False)
    monkeypatch.setattr(kipplo, "_batch_cap", 0)
    fake = _Clock()
    monkeypatch.setattr(kipplo, "_clock", fake)
    monkeypatch.setattr(kipplo, "_sleep", fake.sleep)
    return fake


def _serve(monkeypatch, *answers):
    client = _Client(answers)
    monkeypatch.setattr(kipplo.httpx, "AsyncClient", client)
    return client


def _lookup(fields=kipplo.CONTACT_FIELDS, url=LINKEDIN):
    return asyncio.run(kipplo.enrich_by_linkedin("cand-1", url, fields=fields))


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def test_parses_the_answer_into_the_shared_contact_shape():
    payload = _hit(business=["Jane.Doe@Acme.com"], secondary=["jane.d@gmail.com"], cells=["+1 415-555-0100"])

    fields = kipplo.extract_contact_fields(payload, "jane-doe")

    assert fields == {
        "workEmail": "jane.doe@acme.com",
        "personalEmail": "jane.d@gmail.com",
        "mobilePhone": "+14155550100",
        "workPhone": "",
        "phoneCandidates": ["+14155550100"],
    }


def test_a_hit_for_another_profile_is_ignored():
    payload = _hit(slug="someone-else", business=["someone@acme.com"], cells=["+1 415-555-0100"])

    fields = kipplo.extract_contact_fields(payload, "jane-doe")

    assert not any(fields[k] for k in ("workEmail", "personalEmail", "mobilePhone"))


def test_no_hit_and_error_bodies_parse_to_nothing():
    for payload in (NO_HIT, NO_CREDITS, {}, None):
        fields = kipplo.extract_contact_fields(payload, "jane-doe")
        assert fields["phoneCandidates"] == [] and not fields["workEmail"]


def test_invalid_emails_dropped_and_phone_cleaned_keeps_the_country_code():
    payload = _hit()
    source = payload["result"]["hits"]["hits"][0]["_source"]
    source["businessemails"] = [{"email": "old@acme.com", "status": "invalid"}]
    source["cellnumbers"] = [{"phone_cleaned": "919876543210", "type": "cellphone"}]

    fields = kipplo.extract_contact_fields(payload, "jane-doe")

    assert fields["workEmail"] == ""
    assert fields["mobilePhone"] == "+919876543210"


# ---------------------------------------------------------------------------
# The call
# ---------------------------------------------------------------------------

def test_no_key_means_no_call(monkeypatch):
    monkeypatch.delenv("KIPPLO_API_KEY")
    client = _serve(monkeypatch)

    res = _lookup()

    assert res == {"ok": False, "message": "Kipplo not configured"}
    assert client.requests == []


def test_asks_only_for_the_missing_field_groups_by_canonical_url(monkeypatch):
    client = _serve(monkeypatch, _Response(200, _hit(cells=["+1 415-555-0100"])),
                    _Response(200, _hit(business=["jane@acme.com"])))

    phone = _lookup(fields=("phone",), url="https://uk.linkedin.com/in/Jane-Doe/?trk=abc")
    email = _lookup(fields=("email",))

    first, second = client.requests
    assert first["headers"]["X-API-Key"] == "test-key"
    assert first["json"]["params"]["filters"] == [
        {"field": "linkedin_url", "operator": "Equals", "value": "https://www.linkedin.com/in/jane-doe"}
    ]
    assert first["json"]["params"]["requested_optional_fields"] == ["cell_numbers"]
    assert second["json"]["params"]["requested_optional_fields"] == ["business_emails", "secondary_emails"]
    assert phone["ok"] and phone["fields"]["mobilePhone"] == "+14155550100" and phone["credits"] == 5.0
    assert email["ok"] and email["fields"]["workEmail"] == "jane@acme.com"


def test_a_miss_is_an_answer_with_nothing_in_it(monkeypatch):
    _serve(monkeypatch, _Response(200, NO_HIT))

    res = _lookup()

    assert res["ok"] is True
    assert not ce._has_usable_field(res["fields"])


def test_out_of_credits_stops_asking_for_a_while(monkeypatch):
    # Kipplo reports this as HTTP 200 with success=false.
    client = _serve(monkeypatch, _Response(200, NO_CREDITS))

    first = _lookup()
    second = _lookup()

    assert first == {"ok": False, "message": "Kipplo out of credits"}
    assert second["ok"] is False and second["message"].startswith("Kipplo unavailable")
    assert len(client.requests) == 1


def test_rejected_key_stops_asking_for_a_while(monkeypatch):
    client = _serve(monkeypatch, _Response(401, {"detail": {
        "error": True, "error_code": "API_KEY_INVALID", "message": "Invalid or expired API key."}}))

    assert _lookup()["message"] == "Kipplo API key rejected"
    assert _lookup()["message"].startswith("Kipplo unavailable")
    assert len(client.requests) == 1


def test_per_second_429_is_retried(monkeypatch):
    client = _serve(monkeypatch, _Response(429, _rate_limited("second")),
                    _Response(200, _hit(business=["jane@acme.com"])))

    res = _lookup()

    assert res["ok"] and res["fields"]["workEmail"] == "jane@acme.com"
    assert len(client.requests) == 2


def test_per_minute_429_gives_up_within_the_wait_budget(monkeypatch, clock):
    monkeypatch.setenv("KIPPLO_MAX_WAIT_S", "5")
    client = _serve(monkeypatch, _Response(429, _rate_limited("minute")))
    start = clock.now

    res = _lookup()

    assert res["ok"] is False and res.get("rate_limited")
    assert len(client.requests) == 1
    assert clock.now - start <= 5


def test_daily_limit_stops_asking(monkeypatch):
    client = _serve(monkeypatch, _Response(429, _rate_limited("minute", "day")))

    assert _lookup()["ok"] is False
    assert _lookup()["message"].startswith("Kipplo unavailable")
    assert len(client.requests) == 1


def test_network_error_fails_open(monkeypatch):
    _serve(monkeypatch, httpx.ConnectError("boom"))

    res = _lookup()

    assert res["ok"] is False and "request failed" in res["message"]


def test_calls_are_paced_to_the_key_limits(monkeypatch, clock):
    # conftest blocks the DB, so this is the in-process fallback limiter.
    monkeypatch.setenv("KIPPLO_RATE_LIMIT_PER_MINUTE", "2")
    monkeypatch.setenv("KIPPLO_RATE_LIMIT_PER_SECOND", "1")
    claim = kipplo._claim_slot_local

    assert claim() == 0
    assert claim() == pytest.approx(1.0)      # 1/s spacing
    clock.now += 1.0
    assert claim() == 0
    clock.now += 1.0
    assert claim() == pytest.approx(58.0)     # 2/min used; the first frees at +60s


def test_shared_limiter_sql(monkeypatch):
    """The Postgres limiter the 8 workers share (temp table; needs a Postgres)."""
    try:
        conn = psycopg2.connect(os.getenv("LAUNCH_REPORT_TEST_DSN", "dbname=postgres"), connect_timeout=2)
    except Exception:
        pytest.skip("no Postgres reachable (set LAUNCH_REPORT_TEST_DSN)")

    class _KeepOpen:
        def __getattr__(self, name):
            return getattr(conn, name)

        def close(self):
            pass

    def _age_calls(seconds):
        with conn.cursor() as cur:
            cur.execute("UPDATE kipplo_rate_calls SET called_at = called_at - make_interval(secs => %s)", (seconds,))
        conn.commit()

    try:
        with conn.cursor() as cur:
            cur.execute(kipplo.CREATE_SQL.replace("CREATE TABLE IF NOT EXISTS", "CREATE TEMP TABLE", 1))
        conn.commit()
        monkeypatch.setattr(kipplo, "_connect", lambda: _KeepOpen())
        monkeypatch.setattr(kipplo, "_table_ready", True)
        monkeypatch.setenv("KIPPLO_RATE_LIMIT_PER_MINUTE", "2")
        monkeypatch.setenv("KIPPLO_RATE_LIMIT_PER_SECOND", "1")

        assert kipplo._claim_slot_db() == 0
        assert 0 < kipplo._claim_slot_db() <= 1.0      # 1/s spacing; nothing claimed
        _age_calls(2)
        assert kipplo._claim_slot_db() == 0
        _age_calls(2)
        assert 50 < kipplo._claim_slot_db() <= 60      # 2/min used; oldest frees at +56s
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM kipplo_rate_calls")
            assert cur.fetchone()[0] == 2
    finally:
        conn.rollback()
        conn.close()


def test_waits_for_a_slot_then_calls(monkeypatch, clock):
    monkeypatch.setenv("KIPPLO_RATE_LIMIT_PER_SECOND", "1")
    client = _serve(monkeypatch, _Response(200, NO_HIT), _Response(200, NO_HIT))
    start = clock.now

    _lookup()
    _lookup()

    assert len(client.requests) == 2
    assert clock.now - start >= 1.0


# ---------------------------------------------------------------------------
# Chains: Kipplo first, the rest only for what it did not find
# ---------------------------------------------------------------------------

def _fields(**kw):
    return {"mobilePhone": "", "workPhone": "", "workEmail": "", "personalEmail": "", "phoneCandidates": [], **kw}


class _Providers:
    def __init__(self, kipplo=None, apollo=None, exa=None):
        self.calls = []
        self._kipplo = kipplo or {"ok": True, "fields": _fields()}
        self._apollo = apollo or {"ok": False, "message": "no match"}
        self._exa = exa or {"ok": False, "message": "no match"}
        self.records = []

    async def kipplo(self, candidate_id, linkedin_url, fields=kipplo.CONTACT_FIELDS):
        self.calls.append(("kipplo", tuple(fields)))
        return self._kipplo

    async def apollo(self, candidate_id, linkedin_url):
        self.calls.append("apollo")
        return self._apollo

    async def exa(self, candidate_id, linkedin_url, full_name="", company="", fields=ce.EXA_CONTACT_FIELDS):
        self.calls.append(("exa", tuple(fields)))
        return self._exa

    async def zoominfo(self, *args, **kwargs):
        self.calls.append("zoominfo")
        return {"ok": False}

    async def zoominfo_sourcing(self, full_name, company=""):
        self.calls.append("zoominfo")
        return {}

    async def cache_get(self, url):
        return contact_cache.empty()

    async def cache_record(self, url, **kw):
        self.records.append(kw)


def _patch_chain(monkeypatch, providers, kipplo_call=None):
    """The on-demand chain with fake providers. kipplo_call: None = the fake
    provider, "real" = the real client (feed it with _serve), or any callable."""
    def _no_db():
        raise RuntimeError("no db in tests")

    monkeypatch.setattr(candidates_router, "get_db_connection", _no_db)
    monkeypatch.setattr(core_config, "EXA_CONTACT_ENRICH_ENABLED", True)
    monkeypatch.setattr(sourcing_config, "EXA_ONDEMAND_CONTACT_ONLY_WHEN_NO_CONTACT", False)
    monkeypatch.setattr(contact_cache, "get", providers.cache_get)
    monkeypatch.setattr(contact_cache, "record", providers.cache_record)
    if kipplo_call != "real":
        monkeypatch.setattr(candidates_router, "_kipplo_enrich_by_linkedin", kipplo_call or providers.kipplo)
    monkeypatch.setattr(candidates_router, "_apollo_enrich_by_linkedin", providers.apollo)
    monkeypatch.setattr(candidates_router, "_exa_enrich_by_linkedin", providers.exa)
    monkeypatch.setattr(ce, "zoominfo_enrich_by_email", providers.zoominfo)
    monkeypatch.setattr(ce, "zoominfo_enrich_by_name", providers.zoominfo)


def _on_demand(monkeypatch, providers, kipplo_call=None, **req):
    _patch_chain(monkeypatch, providers, kipplo_call)
    body = candidates_router.EnrichCandidateContactRequest(
        candidate_id="cand-1", linkedin_url=LINKEDIN, full_name="Jane Doe", **req
    )
    return asyncio.run(candidates_router._enrich_candidate_contact_impl("cand-1", body))


def test_on_demand_kipplo_is_asked_first_and_a_full_hit_ends_the_chain(monkeypatch):
    providers = _Providers(kipplo={"ok": True, "fields": _fields(
        workEmail="jane@acme.com", mobilePhone="+14155550100", phoneCandidates=["+14155550100"])})

    res = _on_demand(monkeypatch, providers)

    assert providers.calls == [("kipplo", ("email", "phone"))]
    assert res["provider"] == "kipplo"
    assert res["email"] == "jane@acme.com" and res["phone"] == "+14155550100"
    assert providers.records[-1]["email_provider"] == "kipplo"
    assert providers.records[-1]["phone_provider"] == "kipplo"


def test_on_demand_rest_of_chain_only_for_what_kipplo_missed(monkeypatch):
    providers = _Providers(
        kipplo={"ok": True, "fields": _fields(workEmail="jane@acme.com")},
        exa={"ok": True, "fields": _fields(mobilePhone="+14155550100")},
    )

    res = _on_demand(monkeypatch, providers)

    assert providers.calls[0] == ("kipplo", ("email", "phone"))
    assert providers.calls.index("apollo") < providers.calls.index(("exa", ("phone",)))
    assert res["provider"] == "kipplo"
    assert res["email"] == "jane@acme.com" and res["phone"] == "+14155550100"
    assert providers.records[-1]["email_provider"] == "kipplo"
    assert providers.records[-1]["phone_provider"] == "exa"


def test_on_demand_kipplo_not_asked_for_contact_the_candidate_has(monkeypatch):
    providers = _Providers()

    _on_demand(monkeypatch, providers, email="jane@acme.com")

    assert providers.calls[0] == ("kipplo", ("phone",))


def test_on_demand_kipplo_miss_leaves_the_old_chain(monkeypatch):
    providers = _Providers(apollo={"ok": True, "fields": _fields(
        workEmail="jane@acme.com", mobilePhone="+14155550100")})

    res = _on_demand(monkeypatch, providers)

    assert providers.calls[0] == ("kipplo", ("email", "phone"))
    assert "apollo" in providers.calls
    assert res["provider"] == "apollo"


def test_launch_spend_policy_also_covers_kipplo(monkeypatch):
    providers = _Providers()

    res = _on_demand(monkeypatch, providers, trigger="launch", source="Dice", match_score=95)

    assert res["status"] == "skipped"
    assert providers.calls == []


def _sourcing(monkeypatch, providers, kipplo_call=None, **kw):
    monkeypatch.setenv("CONTACT_ENRICHMENT_INLINE_ENABLED", "true")
    monkeypatch.setattr(contact_cache, "get", providers.cache_get)
    monkeypatch.setattr(contact_cache, "record", providers.cache_record)
    if kipplo_call != "real":
        monkeypatch.setattr(ce, "kipplo_enrich_by_linkedin", kipplo_call or providers.kipplo)
    monkeypatch.setattr(ce, "_zoominfo_enrich_for_sourcing", providers.zoominfo_sourcing)
    monkeypatch.setattr(ce, "apollo_enrich_by_linkedin", providers.apollo)
    monkeypatch.setattr(ce, "zoominfo_enrich_by_email", providers.zoominfo)
    monkeypatch.setattr(ce, "exa_enrich_by_linkedin", providers.exa)
    monkeypatch.setattr(sourcing_config, "EXA_SOURCING_CONTACT_FALLBACK", True)
    monkeypatch.setattr(sourcing_config, "EXA_SOURCING_CONTACT_ONLY_WHEN_NO_CONTACT", True)
    ce.reset_job_counter("job-src", include_lifetime=True)
    kw.setdefault("jobdiva_id", "job-src")
    kw.setdefault("full_name", "Jane Doe")
    kw.setdefault("include_exa", True)
    kw.setdefault("want_phone", False)
    return asyncio.run(ce.enrich_contact_for_sourcing(LINKEDIN, **kw))


def test_sourcing_kipplo_first_and_email_only(monkeypatch):
    providers = _Providers(kipplo={"ok": True, "fields": _fields(personalEmail="jane.d@gmail.com")})

    res = _sourcing(monkeypatch, providers)

    # Sourcing never buys phones, so Kipplo is not billed for cell numbers.
    assert providers.calls == [("kipplo", ("email",))]
    assert res["provider_used"] == "kipplo"
    assert res["personalEmail"] == "jane.d@gmail.com"
    assert providers.records[-1]["email_provider"] == "kipplo"


def test_sourcing_kipplo_miss_falls_through_to_the_old_chain(monkeypatch):
    providers = _Providers(apollo={"ok": True, "fields": _fields(workEmail="jane@acme.com")})

    res = _sourcing(monkeypatch, providers)

    assert providers.calls[0] == ("kipplo", ("email",))
    assert "apollo" in providers.calls
    assert res["provider_used"] == "apollo"


# ---------------------------------------------------------------------------
# Whatever goes wrong with Kipplo, the chain still ends at Exa
# ---------------------------------------------------------------------------

KIPPLO_FAILURES = [
    pytest.param(lambda: _Response(429, _rate_limited("minute")), id="rate_limited"),
    pytest.param(lambda: _Response(200, NO_CREDITS), id="out_of_credits"),
    pytest.param(lambda: _Response(401, {"detail": {"error_code": "API_KEY_INVALID"}}), id="key_rejected"),
    pytest.param(lambda: _Response(503, {"detail": "unavailable"}), id="server_error"),
    pytest.param(lambda: httpx.ReadTimeout("slow"), id="timeout"),
]


@pytest.mark.parametrize("failure", KIPPLO_FAILURES)
def test_on_demand_any_kipplo_failure_still_gets_the_contact_from_exa(monkeypatch, failure):
    monkeypatch.setenv("KIPPLO_MAX_WAIT_S", "5")
    client = _serve(monkeypatch, failure())
    providers = _Providers(exa={"ok": True, "fields": _fields(
        workEmail="jane@acme.com", mobilePhone="+14155550100", phoneCandidates=["+14155550100"])})

    res = _on_demand(monkeypatch, providers, kipplo_call="real")

    assert len(client.requests) == 1                     # Kipplo was really asked
    assert "apollo" in providers.calls                   # cheaper steps still ran first
    assert providers.calls[-1] == ("exa", ("email", "phone"))
    assert res["provider"] == "exa"
    assert res["email"] == "jane@acme.com" and res["phone"] == "+14155550100"


@pytest.mark.parametrize("failure", KIPPLO_FAILURES)
def test_sourcing_any_kipplo_failure_still_reaches_exa(monkeypatch, failure):
    monkeypatch.setenv("KIPPLO_MAX_WAIT_S", "5")
    _serve(monkeypatch, failure())
    providers = _Providers(exa={"ok": True, "fields": _fields(workEmail="jane@acme.com")})

    res = _sourcing(monkeypatch, providers, kipplo_call="real")

    assert "apollo" in providers.calls
    assert providers.calls[-1] == ("exa", ("email",))
    assert res["provider_used"] == "exa" and res["workEmail"] == "jane@acme.com"


def test_on_demand_kipplo_crash_still_gets_the_contact_from_exa(monkeypatch):
    async def _crash(*args, **kwargs):
        raise RuntimeError("bug in the Kipplo client")

    providers = _Providers(exa={"ok": True, "fields": _fields(mobilePhone="+14155550100")})

    res = _on_demand(monkeypatch, providers, kipplo_call=_crash, email="jane@acme.com")

    assert providers.calls[-1] == ("exa", ("phone",))
    assert res["status"] == "success" and res["phone"] == "+14155550100"


def test_launch_rate_limited_kipplo_does_not_wait_out_the_minute(monkeypatch, clock):
    """A Launch PAIR burst beyond the key's 10/min must fall through at once,
    not queue each candidate for the rest of the minute."""
    monkeypatch.setenv("KIPPLO_RATE_LIMIT_PER_MINUTE", "1")
    _serve(monkeypatch, _Response(200, NO_HIT))
    assert _lookup()["ok"] is True                      # uses the minute's only slot
    start = clock.now

    res = _lookup()                                     # next one: the slot frees in ~59s

    assert res.get("rate_limited") and clock.now - start < 1


# ---------------------------------------------------------------------------
# Batching: lookups waiting together go out as one request
# ---------------------------------------------------------------------------

JANE = {"businessemails": [{"email": "jane@acme.com", "status": "valid"}],
        "cellnumbers": [{"phone": "+1 415-555-0100", "phone_cleaned": "14155550100", "type": "cellphone"}]}
KNOWN_EMPTY = {"businessemails": [], "secondaryemails": [], "cellnumbers": []}


class _Kipplo:
    """Answers an Equals filter (one URL or a list) from a directory of known
    profiles. ``list_mode`` makes list answers misbehave on purpose."""

    def __init__(self, directory, list_mode="ok"):
        self.directory = directory
        self.list_mode = list_mode
        self.requests = []

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def values(self):
        return [r["params"]["filters"][0]["value"] for r in self.requests]

    async def post(self, url, headers=None, json=None):
        self.requests.append(json)
        value = json["params"]["filters"][0]["value"]
        is_list = isinstance(value, list)
        if is_list and self.list_mode == "reject":
            return _Response(200, {"success": False, "result": None,
                                   "error": "Invalid filters: Filter [0]: value must be a string"})
        urls = (value[:1] if self.list_mode == "first_only" else value) if is_list else [value]
        slugs = [contact_cache.linkedin_slug(u) for u in urls]
        hits = [{"_source": {"linkedinurl": f"linkedin.com/in/{s}", **self.directory[s]}}
                for s in slugs if s in self.directory]
        total = len(hits)
        if is_list and self.list_mode == "cut_off":
            hits = hits[:1]
        if is_list and self.list_mode == "foreign":
            hits.append({"_source": {"linkedinurl": "linkedin.com/in/a-stranger", **JANE}})
            total += 1
        return _Response(200, {"success": True, "credits_charged": 1.0 * len(hits) or None,
                               "result": {"hits": {"total": {"value": total}, "hits": hits}}})


def _gather(*lookups):
    """Run lookups concurrently: [(slug, fields), ...] -> results in order."""
    async def _go():
        return await asyncio.gather(*(
            kipplo.enrich_by_linkedin(f"cand-{slug}", f"https://www.linkedin.com/in/{slug}", fields=fields)
            for slug, fields in lookups
        ))
    return asyncio.run(_go())


EMAIL = ("email",)
BOTH = ("email", "phone")


def test_lookups_waiting_together_share_one_request(monkeypatch):
    server = _Kipplo({"jane": JANE, "john": KNOWN_EMPTY})
    monkeypatch.setattr(kipplo.httpx, "AsyncClient", server)

    jane, john, ghost = _gather(("jane", BOTH), ("john", BOTH), ("ghost", BOTH))

    assert server.values()[0] == [f"https://www.linkedin.com/in/{s}" for s in ("jane", "john", "ghost")]
    assert jane["ok"] and jane["fields"]["workEmail"] == "jane@acme.com" and jane["fields"]["mobilePhone"] == "+14155550100"
    assert john["ok"] and not ce._has_usable_field(john["fields"])
    assert ghost["ok"] and not ce._has_usable_field(ghost["fields"])
    # Not yet trusted: the profile the list came back without is re-asked alone once.
    assert server.values()[1] == "https://www.linkedin.com/in/ghost"
    assert kipplo._batch_verified and not kipplo._batch_off_reason

    _gather(("jane", BOTH), ("ghost", BOTH))
    assert len(server.requests) == 3                     # trusted now: one request, no re-check


def test_lookups_needing_different_fields_are_not_mixed(monkeypatch):
    server = _Kipplo({"jane": JANE, "john": KNOWN_EMPTY})
    monkeypatch.setattr(kipplo.httpx, "AsyncClient", server)

    _gather(("jane", EMAIL), ("john", ("phone",)))

    asked = sorted(tuple(r["params"]["requested_optional_fields"]) for r in server.requests)
    assert asked == [("business_emails", "secondary_emails"), ("cell_numbers",)]


def test_each_request_carries_at_most_the_batch_size(monkeypatch):
    monkeypatch.setenv("KIPPLO_BATCH_SIZE", "2")
    names = ["ann", "bob", "cat", "dan", "eve"]
    server = _Kipplo({n: KNOWN_EMPTY for n in names})
    monkeypatch.setattr(kipplo.httpx, "AsyncClient", server)

    results = _gather(*((n, EMAIL) for n in names))

    assert [len(v) if isinstance(v, list) else 1 for v in server.values()] == [2, 2, 1]
    assert all(r["ok"] for r in results)


def test_the_same_profile_twice_is_asked_once(monkeypatch):
    server = _Kipplo({"jane": JANE})
    monkeypatch.setattr(kipplo.httpx, "AsyncClient", server)

    first, second = _gather(("jane", EMAIL), ("jane", EMAIL))

    assert server.values() == ["https://www.linkedin.com/in/jane"]
    assert first["fields"]["workEmail"] == second["fields"]["workEmail"] == "jane@acme.com"


def test_a_list_that_answers_only_part_switches_batching_off(monkeypatch):
    server = _Kipplo({"ann": JANE, "bob": JANE, "cat": JANE}, list_mode="first_only")
    monkeypatch.setattr(kipplo.httpx, "AsyncClient", server)

    results = _gather(("ann", EMAIL), ("bob", EMAIL), ("cat", EMAIL))

    # bob, re-asked alone, IS known: the list lied, so cat is asked alone too.
    assert all(r["fields"]["workEmail"] == "jane@acme.com" for r in results)
    assert server.values()[1:] == ["https://www.linkedin.com/in/bob", "https://www.linkedin.com/in/cat"]
    assert kipplo._batch_off_reason

    _gather(("ann", EMAIL), ("bob", EMAIL))
    assert all(isinstance(v, str) for v in server.values()[3:])   # one at a time from now on


def test_a_rejected_list_filter_falls_back_to_single_lookups(monkeypatch):
    server = _Kipplo({"ann": JANE, "bob": KNOWN_EMPTY}, list_mode="reject")
    monkeypatch.setattr(kipplo.httpx, "AsyncClient", server)

    ann, bob = _gather(("ann", EMAIL), ("bob", EMAIL))

    assert ann["fields"]["workEmail"] == "jane@acme.com" and bob["ok"]
    assert isinstance(server.values()[0], list) and all(isinstance(v, str) for v in server.values()[1:])
    assert "rejected a list filter" in kipplo._batch_off_reason


def test_a_cut_off_answer_looks_the_rest_up(monkeypatch):
    server = _Kipplo({"ann": JANE, "bob": JANE, "cat": JANE}, list_mode="cut_off")
    monkeypatch.setattr(kipplo.httpx, "AsyncClient", server)

    results = _gather(("ann", EMAIL), ("bob", EMAIL), ("cat", EMAIL))

    assert all(r["fields"]["workEmail"] == "jane@acme.com" for r in results)
    assert kipplo._batch_cap == 1                        # the answer held one hit


def test_hits_for_strangers_switch_batching_off(monkeypatch):
    server = _Kipplo({"ann": JANE, "bob": KNOWN_EMPTY}, list_mode="foreign")
    monkeypatch.setattr(kipplo.httpx, "AsyncClient", server)

    ann, bob = _gather(("ann", EMAIL), ("bob", EMAIL))

    assert ann["fields"]["workEmail"] == "jane@acme.com" and bob["ok"]
    assert "nobody asked for" in kipplo._batch_off_reason


def test_out_of_credits_answers_every_waiter(monkeypatch):
    _serve(monkeypatch, _Response(200, NO_CREDITS))

    results = _gather(("ann", EMAIL), ("bob", EMAIL), ("cat", EMAIL))

    assert all(r == {"ok": False, "message": "Kipplo out of credits"} for r in results)


def test_batch_size_one_turns_batching_off(monkeypatch):
    monkeypatch.setenv("KIPPLO_BATCH_SIZE", "1")
    server = _Kipplo({"ann": JANE, "bob": JANE})
    monkeypatch.setattr(kipplo.httpx, "AsyncClient", server)

    _gather(("ann", EMAIL), ("bob", EMAIL))

    assert server.values() == ["https://www.linkedin.com/in/ann", "https://www.linkedin.com/in/bob"]


def test_a_batch_does_not_warn_about_each_others_profiles(monkeypatch, caplog):
    server = _Kipplo({"ann": JANE, "bob": JANE, "cat": JANE})
    monkeypatch.setattr(kipplo.httpx, "AsyncClient", server)

    with caplog.at_level(logging.WARNING, logger="services.kipplo"):
        results = _gather(("ann", EMAIL), ("bob", EMAIL), ("cat", EMAIL))

    assert len(server.requests) == 1 and all(r["fields"]["workEmail"] for r in results)
    assert not [r for r in caplog.records if "different LinkedIn profile" in r.getMessage()]


# ---------------------------------------------------------------------------
# Launch PAIR's grouped lookup (/candidates/enrich-contacts) and what each
# answer says about the providers
# ---------------------------------------------------------------------------

ANA = {"cellnumbers": [{"phone": "+1 415-555-0101", "phone_cleaned": "14155550101", "type": "cellphone"}]}
BEN = {"cellnumbers": [{"phone": "+1 415-555-0102", "phone_cleaned": "14155550102", "type": "cellphone"}]}


def _person(slug, **kw):
    return {"candidate_id": f"cand-{slug}", "linkedin_url": f"https://www.linkedin.com/in/{slug}",
            "full_name": slug.title(), "source": "LinkedIn-Exa", "match_score": 80, **kw}


def _grouped(monkeypatch, providers, people, kipplo_call="real", **req):
    _patch_chain(monkeypatch, providers, kipplo_call)
    body = candidates_router.EnrichCandidateContactsRequest(
        candidates=[candidates_router.EnrichCandidateContactRequest(**p) for p in people], **req
    )
    return asyncio.run(candidates_router.enrich_candidate_contacts(body, user=None))["results"]


def test_launch_group_asks_kipplo_once_for_everyone(monkeypatch):
    monkeypatch.setenv("KIPPLO_CONTACT_FIELDS", "phone")   # production: personal phones only
    server = _Kipplo({"ana": ANA, "ben": BEN, "cy": KNOWN_EMPTY})
    monkeypatch.setattr(kipplo.httpx, "AsyncClient", server)
    providers = _Providers(exa={"ok": True, "fields": _fields(mobilePhone="+14155550103")})

    results = _grouped(monkeypatch, providers, [
        _person("ana", email="ana@acme.com"), _person("ben", email="ben@acme.com"),
        _person("cy", email="cy@acme.com"),
    ], trigger="launch")

    # One request for the three (the key's limits count requests), cell numbers only.
    assert server.values()[0] == [f"https://www.linkedin.com/in/{s}" for s in ("ana", "ben", "cy")]
    assert server.requests[0]["params"]["requested_optional_fields"] == ["cell_numbers"]
    assert [r["candidate_id"] for r in results] == ["cand-ana", "cand-ben", "cand-cy"]
    ana, ben, cy = results
    assert ana["phone"] == "+14155550101" and ana["phone_provider"] == "kipplo" and ana["lookup"]["kipplo"] == "phone"
    assert ben["phone"] == "+14155550102" and ben["phone_provider"] == "kipplo"
    # Kipplo had nothing for cy: the chain went on and Exa found the phone.
    assert cy["lookup"]["kipplo"] == "miss"
    assert cy["phone"] == "+14155550103" and cy["phone_provider"] == "exa" and cy["lookup"]["exa"] == "phone"
    assert providers.calls.count(("exa", ("phone",))) == 1


def test_launch_group_kipplo_out_of_credits_goes_to_exa_and_says_why(monkeypatch):
    monkeypatch.setenv("KIPPLO_CONTACT_FIELDS", "phone")
    client = _serve(monkeypatch, _Response(200, NO_CREDITS))
    providers = _Providers(exa={"ok": True, "fields": _fields(mobilePhone="+14155550199")})

    results = _grouped(monkeypatch, providers, [
        _person("ana", email="ana@acme.com"), _person("ben", email="ben@acme.com"),
    ], trigger="launch")

    assert len(client.requests) == 1
    for r in results:
        assert "out of credits" in r["lookup"]["kipplo"]
        assert r["phone"] == "+14155550199" and r["phone_provider"] == "exa" and r["lookup"]["exa"] == "phone"


def test_launch_group_applies_the_spend_policy_to_each_candidate(monkeypatch):
    monkeypatch.setenv("KIPPLO_CONTACT_FIELDS", "phone")
    server = _Kipplo({"cy": ANA})
    monkeypatch.setattr(kipplo.httpx, "AsyncClient", server)
    providers = _Providers()

    results = _grouped(monkeypatch, providers, [
        _person("ana", source="Dice"), _person("ben", match_score=45), _person("cy"),
    ], trigger="launch")

    assert [r["status"] for r in results] == ["skipped", "skipped", "success"]
    assert server.values() == ["https://www.linkedin.com/in/cy"]
    assert results[2]["phone"] == "+14155550101"


def test_launch_group_one_failure_does_not_fail_the_others(monkeypatch):
    class _Flaky(_Providers):
        async def apollo(self, candidate_id, linkedin_url):
            if candidate_id == "cand-ben":
                raise RuntimeError("provider bug")
            return await super().apollo(candidate_id, linkedin_url)

    providers = _Flaky(exa={"ok": True, "fields": _fields(workEmail="x@acme.com")})

    results = _grouped(monkeypatch, providers, [_person("ana"), _person("ben")], kipplo_call=None)

    assert results[0]["status"] == "success" and results[0]["email"] == "x@acme.com"
    assert results[1] == {"status": "error", "candidate_id": "cand-ben", "message": "lookup failed: RuntimeError"}


def test_launch_group_size_is_bounded(monkeypatch):
    from fastapi import HTTPException

    for people in ([], [_person(f"p{i}") for i in range(candidates_router.ENRICH_CONTACTS_BATCH_MAX + 1)]):
        body = candidates_router.EnrichCandidateContactsRequest(
            candidates=[candidates_router.EnrichCandidateContactRequest(**p) for p in people]
        )
        with pytest.raises(HTTPException) as err:
            asyncio.run(candidates_router.enrich_candidate_contacts(body, user=None))
        assert err.value.status_code == 400


def test_answer_names_who_found_each_field(monkeypatch):
    monkeypatch.setenv("KIPPLO_CONTACT_FIELDS", "phone")
    providers = _Providers(
        kipplo={"ok": True, "fields": _fields(mobilePhone="+14155550100")},
        exa={"ok": True, "fields": _fields(personalEmail="jane.d@gmail.com")},
    )

    res = _on_demand(monkeypatch, providers)

    assert res["phone_provider"] == "kipplo" and res["email_provider"] == "exa"
    # The fake Apollo answers {"ok": False, "message": "no match"}.
    assert res["lookup"] == {"cache": "-", "kipplo": "phone", "zoominfo": "-", "apollo": "no match",
                             "exa": "email", "exa_deep": "-"}
    assert providers.records[-1]["phone_provider"] == "kipplo"
    assert providers.records[-1]["email_provider"] == "exa"


def test_by_default_each_profile_is_its_own_request(monkeypatch):
    # Probed 2026-09-29: the integration answers at most one hit per request
    # ("size (10) exceeds maximum allowed (1)"), so lists stay off by default.
    monkeypatch.delenv("KIPPLO_BATCH_SIZE")
    server = _Kipplo({"jane": JANE, "john": KNOWN_EMPTY})
    monkeypatch.setattr(kipplo.httpx, "AsyncClient", server)

    jane, john, ghost = _gather(("jane", BOTH), ("john", BOTH), ("ghost", BOTH))

    assert sorted(server.values()) == [f"https://www.linkedin.com/in/{s}" for s in ("ghost", "jane", "john")]
    assert jane["fields"]["mobilePhone"] == "+14155550100"
    assert john["ok"] and ghost["ok"]


def test_a_one_hit_answer_caps_batches_at_one(monkeypatch):
    # What batching on meets today: 3 profiles asked, all known, 1 hit back.
    server = _Kipplo({"ann": JANE, "bob": KNOWN_EMPTY, "cat": KNOWN_EMPTY}, list_mode="cut_off")
    monkeypatch.setattr(kipplo.httpx, "AsyncClient", server)

    ann, bob, cat = _gather(("ann", BOTH), ("bob", BOTH), ("cat", BOTH))

    assert kipplo._batch_cap == 1
    assert len(server.values()) == 3                     # the list, then the two it left out
    assert all(r["ok"] for r in (ann, bob, cat)) and ann["fields"]["workEmail"] == "jane@acme.com"

