"""Apollo personal phones via webhook (services/apollo_phone.py, routers/apollo_webhook.py).

Apollo only releases personal / mobile numbers through an async phone job
delivered to a webhook (its poll_only mode fails "Webhook URL is missing" on our
account, probed 2026-09-28). These pin the payload parsing, the token-guarded
webhook, the request / wait / deliver flow, its place in the Launch chain
(after ZoomInfo, before Exa) and the routing (main.py mount + nginx location).
"""
import ast
import asyncio
import json
import os
import re
from pathlib import Path

import psycopg2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import sourcing_config
from routers import apollo_webhook
from routers import candidates as candidates_router
from services import apollo_phone
from services import contact_cache
from services import contact_enrichment as ce

LINKEDIN = "https://www.linkedin.com/in/jane-doe"
REPO = Path(__file__).resolve().parents[3]

# The example payload from Apollo's docs ("Retrieve Mobile Phone Numbers").
DOC_PAYLOAD = {
    "status": "success",
    "total_requested_enrichments": 1,
    "unique_enriched_records": 1,
    "missing_records": 0,
    "credits_consumed": 8,
    "people": [{
        "id": "p1",
        "status": "success",
        "phone_numbers": [{
            "_id": "x", "confidence_cd": "high", "type_cd": "mobile",
            "raw_number": "+1 202-555-0116", "sanitized_number": "+12025550116",
            "status_cd": "valid_number", "dnc_status_cd": "not_found",
        }],
    }],
}


def _num(number, type_cd="mobile", status="valid_number", dnc="not_found", conf="high"):
    return {"sanitized_number": number, "type_cd": type_cd, "status_cd": status,
            "dnc_status_cd": dnc, "confidence_cd": conf}


def _payload(*numbers, person_id="p1"):
    return {"status": "success", "credits_consumed": 8,
            "people": [{"id": person_id, "status": "success", "phone_numbers": list(numbers)}]}


# ---------------------------------------------------------------------------
# pick_phone
# ---------------------------------------------------------------------------

def test_doc_example_payload_yields_the_mobile():
    phone, meta = apollo_phone.pick_phone(DOC_PAYLOAD, "p1")
    assert phone == "+12025550116"
    assert meta["phone_type"] == "mobile" and meta["credits_consumed"] == 8


def test_switchboards_invalid_numbers_and_other_people_are_skipped():
    payload = _payload(
        _num("+12025550100", type_cd="work_hq"),
        _num("+12025550101", status="invalid_number"),
        _num("+12025550102", type_cd="work_direct"),
    )
    # The work direct dial is a work number, so nothing personal is left.
    assert apollo_phone.pick_phone(payload, "p1") == ("", {"credits_consumed": 8, "reason": "no usable number"})
    assert apollo_phone.pick_phone(payload, "someone-else") == ("", {
        "credits_consumed": 8, "reason": "no entry for the requested person"})


def test_mobile_beats_direct_and_confidence_breaks_ties():
    # (a work direct dial is never picked at all, see above)
    payload = _payload(
        _num("+12025550102", type_cd="work_direct"),
        _num("+12025550103", conf="low"),
        _num("+12025550104", conf="high"),
    )
    assert apollo_phone.pick_phone(payload, "p1")[0] == "+12025550104"


def test_do_not_call_numbers_are_skipped_unless_allowed(monkeypatch):
    payload = _payload(_num("+12025550105", dnc="dnc"))
    phone, meta = apollo_phone.pick_phone(payload, "p1")
    assert phone == "" and "do-not-call" in meta["reason"]

    monkeypatch.setenv("APOLLO_PHONE_ALLOW_DNC", "true")
    assert apollo_phone.pick_phone(payload, "p1")[0] == "+12025550105"


def test_failed_job_payload_has_no_phone():
    # Shape Apollo returned in the 2026-09-28 poll_only probe.
    failed = {"status": "success", "total_requested_enrichments": 1, "unique_enriched_records": 0,
              "missing_records": 1, "credits_consumed": 0,
              "people": [{"id": "p1", "status": "failure", "phone_numbers": []}]}
    assert apollo_phone.pick_phone(failed, "p1")[0] == ""


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------

def test_reveal_needs_an_explicit_https_base(monkeypatch):
    monkeypatch.setattr(ce, "APOLLO_API_KEY", "k")
    monkeypatch.setattr(ce, "_apollo_no_credits_until", 0.0)
    monkeypatch.setattr(apollo_phone, "_phone_no_credits_until", 0.0)
    monkeypatch.delenv("APOLLO_WEBHOOK_BASE_URL", raising=False)
    monkeypatch.delenv("APP_BASE_URL", raising=False)
    assert not apollo_phone.reveal_available()  # a local run never points Apollo at another env

    monkeypatch.setenv("APP_BASE_URL", "http://localhost:3000")
    assert not apollo_phone.reveal_available()

    monkeypatch.setenv("APP_BASE_URL", "https://pairqa.pyramidci.com/")
    assert apollo_phone.webhook_base_url() == "https://pairqa.pyramidci.com"
    assert apollo_phone.reveal_available()

    monkeypatch.setenv("APOLLO_PHONE_REVEAL_ENABLED", "false")
    assert not apollo_phone.reveal_available()


# ---------------------------------------------------------------------------
# In-memory stand-in for the apollo_phone_requests table
# ---------------------------------------------------------------------------

class _Table:
    def __init__(self):
        self.rows = {}
        self.filled = []

    def run(self, sql, params, *, fetch=False):
        if sql is apollo_phone.INSERT_SQL:
            self.rows[params["token_hash"]] = {**params, "status": "pending", "phone": None, "age_s": 5}
            return 1
        if sql is apollo_phone.GET_SQL:
            row = self.rows.get(params["token_hash"])
            return dict(row) if row else None
        if sql is apollo_phone.SETTLE_SQL:
            row = self.rows.get(params["token_hash"])
            if not row or row["status"] != "pending":
                return None
            row.update(status=params["status"], phone=params["phone"], via=params["via"])
            return {"token_hash": params["token_hash"]}
        if sql is apollo_phone.FILL_ROWS_SQL:
            self.filled.append(params)
            return 1
        if "SET request_id" in sql:
            self.rows[params["token_hash"]].update(request_id=params["request_id"])
            return 1
        raise AssertionError(f"unexpected SQL: {sql[:60]}")


@pytest.fixture
def table(monkeypatch):
    t = _Table()
    monkeypatch.setattr(apollo_phone, "_run", t.run)
    recorded = []

    async def _record(url, **kw):
        recorded.append((url, kw))

    monkeypatch.setattr(contact_cache, "record", _record)
    t.recorded = recorded
    return t


def _pending(table, token="t" * 43, person_id="p1"):
    h = apollo_phone.hash_token(token)
    table.run(apollo_phone.INSERT_SQL, {
        "token_hash": h, "request_id": "123", "apollo_person_id": person_id,
        "linkedin_url": LINKEDIN, "candidate_id": "c1", "jobdiva_id": "26-1",
    })
    return token, h


# ---------------------------------------------------------------------------
# Webhook endpoint
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(apollo_webhook.router, prefix="/api")
    return TestClient(app)


def test_webhook_delivers_once_and_stores_everywhere(client, table):
    token, h = _pending(table)

    r = client.post(f"{apollo_phone.WEBHOOK_PATH}?token={token}", json=DOC_PAYLOAD)
    assert r.status_code == 200 and r.json()["status"] == "delivered"
    assert table.rows[h]["phone"] == "+12025550116" and table.rows[h]["via"] == "webhook"
    assert table.recorded == [(LINKEDIN, {"phone": "+12025550116", "phone_provider": "apollo"})]
    assert table.filled == [{"phone": "+12025550116", "candidate_id": "c1", "jobdiva_id": "26-1"}]

    # Apollo retries are idempotent.
    r = client.post(f"{apollo_phone.WEBHOOK_PATH}?token={token}", json=DOC_PAYLOAD)
    assert r.status_code == 200 and r.json()["status"] == "duplicate"
    assert len(table.recorded) == 1


def test_webhook_rejects_unknown_and_malformed(client, table):
    _pending(table)
    assert client.post(apollo_phone.WEBHOOK_PATH, json=DOC_PAYLOAD).status_code == 404
    assert client.post(f"{apollo_phone.WEBHOOK_PATH}?token={'u' * 43}", json=DOC_PAYLOAD).status_code == 404
    token, _ = _pending(table, token="v" * 43)
    r = client.post(f"{apollo_phone.WEBHOOK_PATH}?token={token}", content=b"{not json",
                    headers={"content-type": "application/json"})
    assert r.status_code == 400
    big = b'{"x": "' + b"a" * (apollo_webhook.MAX_BODY_BYTES + 1) + b'"}'
    r = client.post(f"{apollo_phone.WEBHOOK_PATH}?token={token}", content=big,
                    headers={"content-type": "application/json"})
    assert r.status_code == 413
    assert table.recorded == []


def test_webhook_ignores_numbers_for_another_person(client, table):
    token, h = _pending(table, person_id="p1")
    r = client.post(f"{apollo_phone.WEBHOOK_PATH}?token={token}", json=_payload(_num("+12025550199"), person_id="p2"))
    assert r.status_code == 200 and r.json()["status"] == "no_phone"
    assert table.rows[h]["phone"] is None and table.recorded == []


def test_webhook_refuses_expired_requests(client, table):
    token, h = _pending(table)
    table.rows[h]["age_s"] = apollo_phone.REQUEST_TTL_S + 1
    assert client.post(f"{apollo_phone.WEBHOOK_PATH}?token={token}", json=DOC_PAYLOAD).status_code == 410


def test_webhook_asks_apollo_to_retry_when_our_db_is_down(client, monkeypatch):
    def _down(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(apollo_phone, "_run", _down)
    assert client.post(f"{apollo_phone.WEBHOOK_PATH}?token={'t' * 43}", json=DOC_PAYLOAD).status_code == 503


# ---------------------------------------------------------------------------
# request_phone / wait_for_phone
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text or json.dumps(self._payload)

    def json(self):
        return self._payload


def _http(posts, gets=None, calls=None):
    calls = calls if calls is not None else []

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, params=None, json=None):
            calls.append(("POST", url, params, json))
            return posts.pop(0)

        async def get(self, url, headers=None):
            calls.append(("GET", url))
            return (gets or []).pop(0)

    return _Client, calls


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setenv("APP_BASE_URL", "https://pairqa.pyramidci.com")
    monkeypatch.setattr(ce, "APOLLO_API_KEY", "k")
    monkeypatch.setattr(ce, "_apollo_no_credits_until", 0.0)
    monkeypatch.setattr(apollo_phone, "_phone_no_credits_until", 0.0)


def test_request_phone_registers_the_webhook_before_calling_apollo(monkeypatch, table, live):
    client_cls, calls = _http([_Resp(200, {"request_id": "-306885", "person": {"id": "p1"},
                                           "phone_enrichment": {"status": "pending"}})])
    monkeypatch.setattr(apollo_phone.httpx, "AsyncClient", client_cls)

    job = asyncio.run(apollo_phone.request_phone("c1", LINKEDIN, apollo_person_id="p1", jobdiva_id="26-1"))

    assert job["request_id"] == "-306885" and job["apollo_person_id"] == "p1"
    method, url, params, body = calls[0]
    assert url == apollo_phone.APOLLO_MATCH_URL
    assert params["reveal_phone_number"] == "true"
    m = re.fullmatch(r"https://pairqa\.pyramidci\.com/api/webhooks/apollo/phone\?token=([\w-]+)", params["webhook_url"])
    assert m and len(m.group(1)) >= 40
    # Only the hash of the token is stored.
    assert set(table.rows) == {apollo_phone.hash_token(m.group(1))}
    assert m.group(1) not in json.dumps(list(table.rows.values()))
    assert body == {"linkedin_url": LINKEDIN, "id": "p1"}


def test_request_phone_credit_refusal_trips_the_breaker(monkeypatch, table, live):
    client_cls, _ = _http([_Resp(422, text="You have insufficient credits! Upgrade your plan.")])
    monkeypatch.setattr(apollo_phone.httpx, "AsyncClient", client_cls)

    assert asyncio.run(apollo_phone.request_phone("c1", LINKEDIN)) is None
    assert apollo_phone.phone_credits_exhausted()
    assert not apollo_phone.reveal_available()
    assert [r["status"] for r in table.rows.values()] == ["failed"]


def test_wait_returns_as_soon_as_the_webhook_lands(monkeypatch, table):
    token, h = _pending(table)
    job = {"token_hash": h, "request_id": "123"}

    async def _scenario():
        async def _webhook_later():
            await asyncio.sleep(0.05)
            await apollo_phone.deliver(table.rows[h], DOC_PAYLOAD, via="webhook")

        monkeypatch.setattr(apollo_phone.asyncio, "sleep", _fast_sleep)
        task = asyncio.create_task(_webhook_later())
        got = await apollo_phone.wait_for_phone(job, timeout=5)
        await task
        return got

    got = asyncio.run(_scenario())
    assert got == {"phone": "+12025550116", "state": "delivered"}


_real_sleep = asyncio.sleep


async def _fast_sleep(seconds):
    await _real_sleep(min(seconds, 0.01))


def test_wait_falls_back_to_apollos_poll_endpoint(monkeypatch, table, live):
    token, h = _pending(table)
    client_cls, calls = _http([], gets=[_Resp(200, {"webhook_status": "failed", "failure_reason": "timeout",
                                                    "webhook_result": DOC_PAYLOAD})])
    monkeypatch.setattr(apollo_phone.httpx, "AsyncClient", client_cls)

    got = asyncio.run(apollo_phone.wait_for_phone({"token_hash": h, "request_id": "123"}, timeout=0))

    assert got == {"phone": "+12025550116", "state": "delivered"}
    assert table.rows[h]["via"] == "poll"
    assert calls == [("GET", apollo_phone.APOLLO_WEBHOOK_RESULT_URL.format(request_id="123"))]


def test_wait_times_out_when_apollo_is_still_working(monkeypatch, table, live):
    token, h = _pending(table)
    client_cls, _ = _http([], gets=[_Resp(404, {"error": "result_pending", "retry_after_seconds": 10})])
    monkeypatch.setattr(apollo_phone.httpx, "AsyncClient", client_cls)

    got = asyncio.run(apollo_phone.wait_for_phone({"token_hash": h, "request_id": "123"}, timeout=0))

    assert got == {"phone": "", "state": "timeout"}
    assert table.rows[h]["status"] == "pending"  # a late webhook can still settle it


# ---------------------------------------------------------------------------
# Place in the Launch / button chain
# ---------------------------------------------------------------------------

def _chain(monkeypatch, *, phone_outcome=None, reveal=True, cached=None, **req):
    calls = []

    async def _apollo(candidate_id, url):
        calls.append("apollo")
        return {"ok": True, "person_id": "p1", "fields": {
            "workEmail": "jane@acme.com", "personalEmail": "", "mobilePhone": "", "workPhone": ""}}

    async def _zi(*a, **k):
        calls.append("zoominfo")
        return {"ok": False}

    async def _exa(candidate_id, url, name="", company="", fields=ce.EXA_CONTACT_FIELDS):
        calls.append(("exa", tuple(fields)))
        return {"ok": True, "fields": {"mobilePhone": "+14155550100"} if "phone" in fields else {}}

    async def _request(candidate_id, url, apollo_person_id="", jobdiva_id=None):
        calls.append(("apollo_phone", apollo_person_id))
        return {"token_hash": "h", "request_id": "1", "apollo_person_id": apollo_person_id}

    async def _wait(job, timeout=None):
        return phone_outcome or {"phone": "+12025550116", "state": "delivered"}

    async def _cache_get(url):
        return {**contact_cache.empty(), **(cached or {})}

    async def _cache_record(url, **kw):
        pass

    def _no_db():
        raise RuntimeError("no db")

    monkeypatch.setattr(candidates_router, "_apollo_enrich_by_linkedin", _apollo)
    monkeypatch.setattr(candidates_router, "_exa_enrich_by_linkedin", _exa)
    monkeypatch.setattr(ce, "zoominfo_enrich_by_name", _zi)
    monkeypatch.setattr(ce, "zoominfo_enrich_by_email", _zi)
    monkeypatch.setattr(apollo_phone, "reveal_available", lambda: reveal)
    monkeypatch.setattr(apollo_phone, "request_phone", _request)
    monkeypatch.setattr(apollo_phone, "wait_for_phone", _wait)
    monkeypatch.setattr(contact_cache, "get", _cache_get)
    monkeypatch.setattr(contact_cache, "record", _cache_record)
    monkeypatch.setattr(candidates_router, "get_db_connection", _no_db)
    monkeypatch.setattr(sourcing_config, "EXA_ONDEMAND_CONTACT_ONLY_WHEN_NO_CONTACT", False)
    body = candidates_router.EnrichCandidateContactRequest(
        linkedin_url=LINKEDIN, full_name="Jane Doe", source="LinkedIn-Exa", match_score=80, **req
    )
    return asyncio.run(candidates_router._enrich_candidate_contact_impl("c1", body)), calls


def test_apollo_phone_comes_after_zoominfo_and_spares_exa(monkeypatch):
    res, calls = _chain(monkeypatch, trigger="launch")

    assert res["phone"] == "+12025550116" and res["email"] == "jane@acme.com"
    assert calls.index("apollo") < calls.index(("apollo_phone", "p1"))
    # ZoomInfo-by-found-email (cheaper) is asked before Apollo's 8-credit phone job.
    assert [c for c in calls if c == "zoominfo"] and calls.index("zoominfo", calls.index("apollo")) < calls.index(("apollo_phone", "p1"))
    assert not any(isinstance(c, tuple) and c[0] == "exa" for c in calls)


def test_exa_still_covers_a_phone_apollo_does_not_have(monkeypatch):
    res, calls = _chain(monkeypatch, phone_outcome={"phone": "", "state": "no_phone"}, trigger="launch")
    assert res["phone"] == "+14155550100"
    assert ("exa", ("phone",)) in calls


def test_timeout_falls_back_to_exa_unless_switched_off(monkeypatch):
    res, calls = _chain(monkeypatch, phone_outcome={"phone": "", "state": "timeout"}, trigger="launch")
    assert ("exa", ("phone",)) in calls

    monkeypatch.setenv("APOLLO_PHONE_EXA_AFTER_TIMEOUT", "false")
    res, calls = _chain(monkeypatch, phone_outcome={"phone": "", "state": "timeout"}, trigger="launch")
    assert not any(isinstance(c, tuple) and c[0] == "exa" for c in calls)
    assert res["phone"] is None and res["email"] == "jane@acme.com"


def test_no_phone_job_when_unavailable_or_known_missing(monkeypatch):
    _, calls = _chain(monkeypatch, reveal=False, trigger="launch")
    assert not any(isinstance(c, tuple) and c[0] == "apollo_phone" for c in calls)

    # The whole chain (through the Exa deep search) found no phone lately.
    _, calls = _chain(monkeypatch, cached={"phone_missed": True, "phone_deep_missed": True}, trigger="launch")
    assert not any(isinstance(c, tuple) and c[0] == "apollo_phone" for c in calls)

    # Only the normal Exa run missed: Apollo (billed only for a number it finds)
    # is asked again before the deep search.
    _, calls = _chain(monkeypatch, cached={"phone_missed": True}, trigger="launch")
    assert any(isinstance(c, tuple) and c[0] == "apollo_phone" for c in calls)


def test_no_phone_job_when_the_phone_is_already_known(monkeypatch):
    _, calls = _chain(monkeypatch, cached={"phone": "+13125550123"}, trigger="launch")
    assert not any(isinstance(c, tuple) and c[0] == "apollo_phone" for c in calls)


# ---------------------------------------------------------------------------
# Routing and the auth exception
# ---------------------------------------------------------------------------

def test_webhook_is_mounted_under_api_and_has_its_own_nginx_location():
    main_src = (REPO / "apps/api/main.py").read_text(encoding="utf-8")
    assert '_mount(apollo_webhook_router, "apollo_webhook", prefix="/api")' in main_src
    paths = [r.path for r in apollo_webhook.router.routes]
    assert ["/api" + p for p in paths] == [apollo_phone.WEBHOOK_PATH]

    conf = (REPO / "nginx-app-locations.conf").read_text(encoding="utf-8")
    m = re.search(r"location = /api/webhooks/apollo/phone \{(.*?)\n    \}", conf, re.S)
    assert m, "exact nginx location for the Apollo webhook is missing"
    assert "zone=enrich_limit" in m.group(1) and "zone=api_limit" not in m.group(1)
    assert "proxy_pass http://airecruiter_api$uri$is_args$args;" in m.group(1)


def test_the_only_unauthenticated_route_checks_its_token():
    """This router is the deliberate exception to "every route has
    Depends(get_current_user)". Keep it to one route, and that route must look
    its token up before doing anything else with the request."""
    src = (REPO / "apps/api/routers/apollo_webhook.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    handlers = [
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(isinstance(d, ast.Call) and getattr(d.func, "attr", "") in {"get", "post", "put", "patch", "delete"}
                for d in n.decorator_list)
    ]
    assert [h.name for h in handlers] == ["apollo_phone_webhook"]
    body = ast.get_source_segment(src, handlers[0])
    assert "apollo_phone.hash_token(token)" in body and "get_request_sync" in body
    assert body.index("get_request_sync") < body.index("apollo_phone.deliver")


# ---------------------------------------------------------------------------
# SQL (skips without a reachable Postgres; TEMP tables only)
# ---------------------------------------------------------------------------

_DSN = os.getenv("LAUNCH_REPORT_TEST_DSN", "dbname=postgres")


def test_sql_request_settle_and_fill(monkeypatch):
    try:
        conn = psycopg2.connect(_DSN, connect_timeout=2)
    except Exception:
        pytest.skip("no Postgres reachable (set LAUNCH_REPORT_TEST_DSN)")

    class _KeepOpen:
        def __getattr__(self, name):
            return getattr(conn, name)

        def close(self):
            pass

    try:
        with conn.cursor() as cur:
            cur.execute(apollo_phone.CREATE_SQL.replace("CREATE TABLE IF NOT EXISTS", "CREATE TEMP TABLE", 1))
            cur.execute("CREATE TEMP TABLE sourced_candidates (candidate_id TEXT, jobdiva_id TEXT, phone TEXT, updated_at TIMESTAMP)")
            cur.execute("INSERT INTO sourced_candidates VALUES ('c1','26-1',NULL,NOW()), ('c1','26-2','',NOW()), ('c1','26-1','+13125550123',NOW())")
        monkeypatch.setattr(apollo_phone, "_connect", lambda: _KeepOpen())
        monkeypatch.setattr(apollo_phone, "_table_ready", True)
        recorded = []

        async def _record(url, **kw):
            recorded.append(kw)

        monkeypatch.setattr(contact_cache, "record", _record)

        h = apollo_phone.hash_token("t" * 43)
        apollo_phone._run(apollo_phone.INSERT_SQL, {
            "token_hash": h, "request_id": "123", "apollo_person_id": "p1",
            "linkedin_url": LINKEDIN, "candidate_id": "c1", "jobdiva_id": "26-1",
        })
        row = apollo_phone.get_request_sync(h)
        assert row["status"] == "pending" and float(row["age_s"]) < 60

        first = asyncio.run(apollo_phone.deliver(row, DOC_PAYLOAD, via="webhook"))
        second = asyncio.run(apollo_phone.deliver(row, DOC_PAYLOAD, via="poll"))
        assert first == {"settled": True, "phone": "+12025550116", "status": "delivered"}
        assert second["status"] == "duplicate"
        assert apollo_phone.get_request_sync(h)["phone"] == "+12025550116"
        with conn.cursor() as cur:
            cur.execute("SELECT jobdiva_id, phone FROM sourced_candidates ORDER BY jobdiva_id, phone")
            # Only the job's row that had no phone is filled; a phone already there is kept.
            assert cur.fetchall() == [("26-1", "+12025550116"), ("26-1", "+13125550123"), ("26-2", "")]
        assert len(recorded) == 1
    finally:
        conn.rollback()
        conn.close()
