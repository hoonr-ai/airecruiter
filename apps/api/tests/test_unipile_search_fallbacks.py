"""Unipile search resilience (PROD 2026-09-09 admin page: all five LinkedIn
accounts "In rotation" yet every search failed):

  - Swati / Sethu  -> 403 errors/feature_not_subscribed  (no Recruiter seat)
  - Somya          -> 401 errors/multiple_sessions        (logged out elsewhere)
  - Akarsh         -> 500 errors/unexpected_error         (Unipile-side)
  - Ronak          -> 504 errors/request_timeout          (LinkedIn upstream too slow)

These tests pin the new behaviour:
  1. a seat-less account falls back to LinkedIn *classic* people search on the
     SAME account (public rows, member ids, cursor pagination, capped) and is
     not benched; the "no seat" fact is remembered so later searches skip the
     Recruiter attempt entirely;
  2. a 5xx/504 on the full page is retried once with a small page before the
     account is benched (transient cooldown);
  3. multiple_sessions / disconnected_account bench for the long (human
     reconnect) window;
  4. a successful search clears the account's stale error / cooldown and
     records which API worked, so the admin page stops showing month-old
     failures on healthy accounts.
"""
import asyncio
import json
import time

import services.unipile as unipile_mod
from services.unipile import (
    UnipileService,
    _COOLDOWN_AUTH_S,
    _COOLDOWN_TRANSIENT_S,
)


# ---------------------------------------------------------------------------
# Fixtures: Unipile error envelopes + row shapes verified live 2026-09-09
# ---------------------------------------------------------------------------

NO_SEAT = {"status": 403, "type": "errors/feature_not_subscribed", "title": "Feature not subscribed",
           "detail": "This feature requires a LinkedIn Recruiter subscription."}
MULTI_SESSIONS = {"status": 401, "type": "errors/multiple_sessions", "title": "Multiple sessions",
                  "detail": "LinkedIn detected multiple simultaneous sessions."}
DISCONNECTED = {"status": 401, "type": "errors/disconnected_account", "title": "Disconnected account",
                "detail": "The account appears to be disconnected from the provider service."}
UNEXPECTED = {"status": 500, "type": "errors/unexpected_error", "title": "Unexpected error"}
TIMEOUT = {"status": 504, "type": "errors/request_timeout", "title": "Request timeout"}


def _recruiter_row(i: int):
    return {
        "id": f"AEMAAhash{i:04d}bcdefghijklmnopqrstuvwxyz",
        "name": f"Rec Person{i}",
        "headline": "Java Developer",
        "location": "Dallas, Texas, United States",
        "profile_url": f"https://www.linkedin.com/talent/search/profile/AEMAAhash{i:04d}",
        "public_profile_url": f"https://www.linkedin.com/in/rec-person-{i}",
        "public_identifier": f"rec-person-{i}",
        "recruiter_candidate_id": str(1000 + i),
        "interests": [],
        "skills": [{"name": "Java", "endorsement_count": 1}],
    }


def _classic_row(i: int):
    return {
        "type": "PEOPLE",
        "id": f"ACoAAmember{i:04d}",
        "name": f"Classic Person{i}",
        "headline": "Software Engineer | Java | Spring Boot",
        "location": "Dallas-Fort Worth Metroplex",
        "profile_url": f"https://www.linkedin.com/in/classic-person-{i}",
        "public_profile_url": f"https://www.linkedin.com/in/classic-person-{i}",
        "public_identifier": f"classic-person-{i}",
        "network_distance": "DISTANCE_2",
        "member_urn": f"urn:li:member:{i}",
        "profile_picture_url": f"https://media.licdn.com/{i}.jpg",
    }


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload) if not isinstance(payload, str) else payload

    def json(self):
        if isinstance(self._payload, str):
            raise ValueError("not json")
        return self._payload


class _Client:
    """Fake httpx.AsyncClient driven by a scripted `search_handler(body, params)`."""

    def __init__(self, calls, search_handler, params_items=None):
        self._calls = calls
        self._search = search_handler
        self._params_items = params_items or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None, headers=None, **kw):
        self._calls.append({"method": "GET", "url": url, "params": params})
        if "/linkedin/search/parameters" in url:
            kind = (params or {}).get("type")
            return _Resp(200, {"items": self._params_items.get(kind, [])})
        return _Resp(404, "not found")

    async def post(self, url, params=None, json=None, headers=None, **kw):
        self._calls.append({"method": "POST", "url": url, "params": dict(params or {}), "json": json})
        return self._search(json, params)


def _wire(monkeypatch, search_handler, params_items=None):
    calls = []
    monkeypatch.setattr(
        unipile_mod.httpx, "AsyncClient",
        lambda *a, **k: _Client(calls, search_handler, params_items),
    )
    svc = UnipileService()
    benched = []
    succeeded = []

    async def _bench(account_id, error, cooldown_s):
        benched.append((account_id, error, cooldown_s))

    async def _ok(account_id, search_api):
        succeeded.append((account_id, search_api))

    svc.mark_account_failure = _bench
    svc.mark_account_success = _ok
    return svc, calls, benched, succeeded


def _recruiter_posts(calls):
    return [c for c in calls if c["method"] == "POST" and (c["json"] or {}).get("api") == "recruiter"]


def _classic_posts(calls):
    return [c for c in calls if c["method"] == "POST" and (c["json"] or {}).get("api") == "classic"]


SKILLS = [{"value": "Java", "priority": "Must Have"}, {"value": "Spring Boot", "priority": "Preferred"}]


def _once(svc, account="acc-noseat", limit=100, boolean=None, location="Dallas, TX"):
    return asyncio.run(svc._search_candidates_once(account, SKILLS, location, True, limit, boolean))


# ---------------------------------------------------------------------------
# 1. No Recruiter seat -> classic search on the same account
# ---------------------------------------------------------------------------

def test_no_recruiter_seat_signals_classic_and_is_not_benched(monkeypatch):
    svc, calls, benched, _ = _wire(monkeypatch, lambda body, params: _Resp(403, NO_SEAT))

    results, outcome = _once(svc)

    assert (results, outcome) == ([], "classic")
    assert benched == []  # a seat-less account is usable, not faulty
    assert "acc-noseat" in svc._recruiter_unavailable
    assert svc._recruiter_known_unavailable("acc-noseat") is True


def test_classic_search_maps_public_rows_and_member_ids(monkeypatch):
    def handler(body, params):
        assert body["api"] == "classic" and body["category"] == "people"
        return _Resp(200, {"items": [_classic_row(1), _classic_row(2)], "cursor": None})

    svc, calls, benched, _ = _wire(
        monkeypatch, handler,
        params_items={"LOCATION": [{"id": "104194190", "title": "Dallas, Texas"}]},
    )

    results, outcome = asyncio.run(svc._search_classic_once("acc-noseat", SKILLS, "Dallas, TX", 25, None))

    assert outcome == "ok"
    assert [r["provider_id"] for r in results] == ["ACoAAmember0001", "ACoAAmember0002"]
    r0 = results[0]
    assert r0["id"] == "unipile_ACoAAmember0001"
    assert r0["profile_url"] == "https://www.linkedin.com/in/classic-person-1"
    assert r0["recruiter_profile_url"] is None
    assert r0["recruiter_candidate_id"] is None
    assert r0["unipile_search_api"] == "classic"
    assert r0["linkedin_member_id"] == "ACoAAmember0001"
    assert r0["unipile_account_id"] == "acc-noseat"
    assert r0["source"] == "LinkedIn-Unipile"
    assert r0["name"] == "Classic Person1"
    assert r0["title"].startswith("Software Engineer")
    assert r0["image_url"] == "https://media.licdn.com/1.jpg"

    post = _classic_posts(calls)[0]
    # Same geo ids as the Recruiter path; skills ride in the boolean keyword
    # string because classic has no skill ids.
    assert post["json"]["location"] == ["104194190"]
    assert '"Java"' in post["json"]["keywords"] and '"Spring Boot"' in post["json"]["keywords"]
    assert post["params"]["limit"] == 10  # LinkedIn's classic page cap
    assert benched == []


def test_classic_search_uses_sanitized_boolean_when_given(monkeypatch):
    seen = {}

    def handler(body, params):
        seen.update(body)
        return _Resp(200, {"items": [_classic_row(1)], "cursor": None})

    svc, *_ = _wire(monkeypatch, handler)
    asyncio.run(svc._search_classic_once(
        "acc", SKILLS, "", 10,
        '("Java" OR "Kotlin") AND "Microservices" AND "Dallas, TX" within 25 mi AND OVER 5 YRS',
    ))
    kw = seen["keywords"]
    assert '"Java"' in kw and '"Microservices"' in kw
    assert "within" not in kw and "OVER" not in kw and "Dallas" not in kw


def test_classic_search_paginates_by_cursor_up_to_cap(monkeypatch):
    pages = {
        None: ({"items": [_classic_row(i) for i in range(0, 10)], "cursor": "c1"}),
        "c1": ({"items": [_classic_row(i) for i in range(10, 20)], "cursor": "c2"}),
        "c2": ({"items": [_classic_row(i) for i in range(20, 30)], "cursor": "c3"}),
        "c3": ({"items": [_classic_row(i) for i in range(30, 40)], "cursor": None}),
    }

    def handler(body, params):
        return _Resp(200, pages[body.get("cursor")])

    svc, calls, *_ = _wire(monkeypatch, handler)
    results, outcome = asyncio.run(svc._search_classic_once("acc", SKILLS, "", 25, None))

    assert outcome == "ok"
    assert len(results) == 25  # capped at the requested limit, not the page boundary
    posts = _classic_posts(calls)
    assert [p["json"].get("cursor") for p in posts] == [None, "c1", "c2"]
    assert len({r["provider_id"] for r in results}) == 25


def test_classic_search_never_exceeds_module_cap(monkeypatch):
    def handler(body, params):
        start = int(body.get("cursor") or 0)
        return _Resp(200, {"items": [_classic_row(i) for i in range(start, start + 10)], "cursor": str(start + 10)})

    svc, calls, *_ = _wire(monkeypatch, handler)
    results, _ = asyncio.run(svc._search_classic_once("acc", SKILLS, "", 500, None))
    assert len(results) == 50  # UNIPILE_CLASSIC_SEARCH_LIMIT
    assert len(_classic_posts(calls)) == 5


def test_classic_search_keeps_partial_pages_on_later_failure(monkeypatch):
    def handler(body, params):
        if body.get("cursor"):
            return _Resp(504, TIMEOUT)
        return _Resp(200, {"items": [_classic_row(i) for i in range(10)], "cursor": "c1"})

    svc, calls, benched, _ = _wire(monkeypatch, handler)
    results, outcome = asyncio.run(svc._search_classic_once("acc", SKILLS, "", 30, None))
    assert outcome == "ok" and len(results) == 10
    assert benched == []


def test_classic_search_first_page_auth_failure_benches_and_rotates(monkeypatch):
    svc, calls, benched, _ = _wire(monkeypatch, lambda body, params: _Resp(401, MULTI_SESSIONS))
    results, outcome = asyncio.run(svc._search_classic_once("acc", SKILLS, "", 30, None))
    assert (results, outcome) == ([], "rotate")
    assert benched and benched[0][0] == "acc" and benched[0][2] == _COOLDOWN_AUTH_S
    assert benched[0][1].startswith("classic search 401")


# ---------------------------------------------------------------------------
# 2. Rotation loop: seat-less account served by classic, seat remembered
# ---------------------------------------------------------------------------

def _wire_rotation(monkeypatch, svc, account_ids):
    async def _ids():
        return list(account_ids)

    it = {"i": 0}

    async def _acquire():
        aid = account_ids[it["i"] % len(account_ids)]
        it["i"] += 1
        return aid

    svc.get_rotation_account_ids = _ids
    svc.acquire_account = _acquire


def test_search_candidates_serves_seatless_account_via_classic_and_records_success(monkeypatch):
    def handler(body, params):
        if body["api"] == "recruiter":
            return _Resp(403, NO_SEAT)
        return _Resp(200, {"items": [_classic_row(1)], "cursor": None})

    svc, calls, benched, succeeded = _wire(monkeypatch, handler)
    _wire_rotation(monkeypatch, svc, ["acc-noseat"])

    results = asyncio.run(svc.search_candidates(SKILLS, "Dallas, TX", True, 25, None))

    assert [r["provider_id"] for r in results] == ["ACoAAmember0001"]
    assert len(_recruiter_posts(calls)) == 1 and len(_classic_posts(calls)) == 1
    assert benched == []
    assert succeeded == [("acc-noseat", "classic")]


def test_search_candidates_skips_recruiter_for_known_seatless_account(monkeypatch):
    def handler(body, params):
        assert body["api"] == "classic", "Recruiter search must not be attempted on a known seat-less account"
        return _Resp(200, {"items": [_classic_row(7)], "cursor": None})

    svc, calls, benched, succeeded = _wire(monkeypatch, handler)
    _wire_rotation(monkeypatch, svc, ["acc-noseat"])
    svc._recruiter_unavailable["acc-noseat"] = time.monotonic()

    results = asyncio.run(svc.search_candidates(SKILLS, "Dallas, TX", True, 25, None))

    assert [r["provider_id"] for r in results] == ["ACoAAmember0007"]
    assert _recruiter_posts(calls) == []
    assert succeeded == [("acc-noseat", "classic")]


def test_seatless_memory_expires_after_ttl(monkeypatch):
    svc = UnipileService()
    svc._recruiter_unavailable["acc"] = time.monotonic() - (24 * 3600 + 5)
    assert svc._recruiter_known_unavailable("acc") is False
    assert "acc" not in svc._recruiter_unavailable


def test_search_candidates_recruiter_success_records_recruiter_api(monkeypatch):
    svc, calls, benched, succeeded = _wire(
        monkeypatch, lambda body, params: _Resp(200, {"items": [_recruiter_row(1)]})
    )
    _wire_rotation(monkeypatch, svc, ["acc-seat"])

    results = asyncio.run(svc.search_candidates(SKILLS, "Dallas, TX", True, 25, None))

    assert results[0]["unipile_search_api"] == "recruiter"
    assert results[0]["profile_url"] == "https://www.linkedin.com/in/rec-person-1"
    assert succeeded == [("acc-seat", "recruiter")]
    assert benched == []


# ---------------------------------------------------------------------------
# 3. 5xx / 504 on the full page -> one small-page retry, then bench
# ---------------------------------------------------------------------------

def test_timeout_on_full_page_retries_once_with_small_page(monkeypatch):
    def handler(body, params):
        if int(params["limit"]) > 25:
            return _Resp(504, TIMEOUT)
        return _Resp(200, {"items": [_recruiter_row(1), _recruiter_row(2)]})

    svc, calls, benched, _ = _wire(monkeypatch, handler)
    results, outcome = _once(svc, account="acc-seat", limit=100)

    assert outcome == "ok" and len(results) == 2
    limits = [c["params"]["limit"] for c in _recruiter_posts(calls)]
    assert limits == [100, 25]
    assert benched == []


def test_unexpected_error_twice_benches_transiently_and_rotates(monkeypatch):
    svc, calls, benched, _ = _wire(monkeypatch, lambda body, params: _Resp(500, UNEXPECTED))
    results, outcome = _once(svc, account="acc-seat", limit=100)

    assert (results, outcome) == ([], "rotate")
    assert [c["params"]["limit"] for c in _recruiter_posts(calls)] == [100, 25]
    assert benched == [("acc-seat", benched[0][1], _COOLDOWN_TRANSIENT_S)]
    assert benched[0][1].startswith("search 500")


def test_small_page_request_is_not_retried_on_5xx(monkeypatch):
    svc, calls, benched, _ = _wire(monkeypatch, lambda body, params: _Resp(504, TIMEOUT))
    results, outcome = _once(svc, account="acc-seat", limit=20)

    assert (results, outcome) == ([], "rotate")
    assert [c["params"]["limit"] for c in _recruiter_posts(calls)] == [20]
    assert benched[0][2] == _COOLDOWN_TRANSIENT_S


# ---------------------------------------------------------------------------
# 4. Auth-class failures bench for the human-reconnect window
# ---------------------------------------------------------------------------

def test_multiple_sessions_benches_for_auth_cooldown_and_rotates(monkeypatch):
    svc, calls, benched, _ = _wire(monkeypatch, lambda body, params: _Resp(401, MULTI_SESSIONS))
    results, outcome = _once(svc, account="acc-somya")

    assert (results, outcome) == ([], "rotate")
    assert benched == [("acc-somya", benched[0][1], _COOLDOWN_AUTH_S)]
    assert len(_recruiter_posts(calls)) == 1  # no 5xx ladder for auth errors
    assert _classic_posts(calls) == []        # and no classic fallback either


def test_classify_account_failure_knows_unipile_error_types():
    f = UnipileService.classify_account_failure
    assert f(401, json.dumps(MULTI_SESSIONS)) == _COOLDOWN_AUTH_S
    assert f(401, json.dumps(DISCONNECTED)) == _COOLDOWN_AUTH_S
    assert f(200, json.dumps({"type": "errors/multiple_sessions"})) == _COOLDOWN_AUTH_S
    assert f(504, json.dumps(TIMEOUT)) == _COOLDOWN_TRANSIENT_S
    assert f(500, json.dumps(UNEXPECTED)) == _COOLDOWN_TRANSIENT_S
    assert f(400, json.dumps({"type": "errors/invalid_parameters"})) is None


def test_no_recruiter_detection_is_403_specific():
    svc = UnipileService()
    assert svc._is_no_recruiter_error(403, json.dumps(NO_SEAT)) is True
    assert svc._is_no_recruiter_error(403, "feature_not_subscribed plain text") is True
    assert svc._is_no_recruiter_error(401, json.dumps(NO_SEAT)) is False
    assert svc._is_no_recruiter_error(403, json.dumps({"type": "errors/insufficient_credentials"})) is False
    assert svc._error_type("not json") == ""
    assert svc._error_type(json.dumps(TIMEOUT)) == "errors/request_timeout"
