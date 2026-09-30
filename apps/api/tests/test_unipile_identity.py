"""Unipile LinkedIn Recruiter identity contract (verified live 2026-08-03).

`POST /linkedin/search` with `api: "recruiter"` returns, per row:
  - `id`                 — recruiter-scoped `AEMAA…` hash (NOT the member id)
  - `profile_url`        — `https://www.linkedin.com/talent/search/profile/<hash>`,
                           a Recruiter deep link that needs an RPS seat
  - `public_profile_url` / `public_identifier` — the real vanity URL / slug
                           (missing on ~2% of rows)
  - `name`               — null on ~2% (out-of-network / privacy-restricted)
  - `skills[{name, endorsement_count}]`, `interests: ["OPEN_TO_WORK", …]`

`GET /users/{id}` needs `linkedin_sections=*`; without it Unipile returns a
~26-key stub with no experience / education / skills / summary.

These tests pin the three "Unipile not working" symptoms that came from
conflating the two identifiers and from the stub fetch:
  1. candidates linked to the Recruiter paywall (RPS link used as profile_url,
     which also disabled every `linkedin.com/in/`-keyed enricher and the
     Apify open-to-work resolver for Unipile rows);
  2. opaque hashes rendered as candidate names;
  3. every candidate scored on an empty profile stub and dropped below the
     external score floor.
"""
import asyncio

import pytest

import services.unipile as unipile_mod
from services.unipile import UnipileService
from services.unified_candidate_search import UnifiedCandidateSearch


HASH_1 = "AEMAAesrdj8Bputbeeugzft99J0Qcie7Kbhun5K"
# Digit-free hash: slips past the old "looks like an id" name heuristic when
# derived from the RPS link, so it is the sharper regression fixture.
HASH_2 = "AEMAAbcdefghijklmnopqrstuvwxyzabcdefgh"
HASH_3 = "AEMAAzyxwvutsrqponmlkjihgfedcbazyxwvut"


def _rps(h: str) -> str:
    return f"https://www.linkedin.com/talent/search/profile/{h}"


ROW_FULL = {
    "id": HASH_1,
    "name": "Jane Doe",
    "headline": "Senior Java Developer at Acme",
    "location": "Dallas, Texas, United States",
    "profile_url": _rps(HASH_1),
    "public_profile_url": "https://www.linkedin.com/in/jane-doe-123",
    "public_identifier": "jane-doe-123",
    "skills": [
        {"name": "Java", "endorsement_count": 12},
        {"name": "Spring Boot", "endorsement_count": 3},
        {"name": "", "endorsement_count": 0},
    ],
    "interests": ["OPEN_TO_WORK"],
    "recruiter_candidate_id": "rc-1",
    "member_urn": None,
}

# No name, no public_profile_url — only the slug.
ROW_IDENT_ONLY = {
    "id": HASH_2,
    "name": None,
    "headline": "Data Engineer",
    "location": "Austin, Texas, United States",
    "profile_url": _rps(HASH_2),
    "public_identifier": "john-smith-42",
    "skills": [],
    "interests": [],
}

# No public identity at all (the ~2% case).
ROW_NO_PUBLIC = {
    "id": HASH_3,
    "name": None,
    "headline": "Cloud Architect",
    "location": "Plano, Texas, United States",
    "profile_url": _rps(HASH_3),
}


class _FakeResponse:
    def __init__(self, status=200, json_data=None, text=""):
        self.status_code = status
        self._json = json_data if json_data is not None else {}
        self.text = text

    def json(self):
        return self._json


class _FakeAsyncClient:
    """Records outbound calls and dispatches a canned response by URL."""

    def __init__(self, calls, rows, profile):
        self._calls = calls
        self._rows = rows
        self._profile = profile

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None, headers=None, **kw):
        self._calls.append({"method": "GET", "url": url, "params": params})
        if "/linkedin/search/parameters" in url:
            return _FakeResponse(200, {"items": []})
        if "/users/" in url:
            return _FakeResponse(200, self._profile)
        return _FakeResponse(404, {}, text="not found")

    async def post(self, url, params=None, json=None, headers=None, **kw):
        self._calls.append({"method": "POST", "url": url, "params": params, "json": json})
        if url.endswith("/linkedin/search"):
            return _FakeResponse(200, {"items": self._rows})
        return _FakeResponse(404, {}, text="not found")


def _patch_http(monkeypatch, rows=None, profile=None):
    calls = []

    def factory(*args, **kwargs):
        return _FakeAsyncClient(calls, rows or [], profile or {})

    monkeypatch.setattr(unipile_mod.httpx, "AsyncClient", factory)
    return calls


def _search(monkeypatch, rows):
    _patch_http(monkeypatch, rows=rows)
    svc = UnipileService()
    results, outcome = asyncio.run(
        svc._search_candidates_once(
            "acc-1",
            [{"value": "Java", "priority": "Must Have"}],
            "Dallas, TX",
            True,
            25,
            None,
        )
    )
    assert outcome == "ok"
    return {r["provider_id"]: r for r in results}


# ---------------------------------------------------------------------------
# 1. Public URL is the identity; the RPS deep link rides separately
# ---------------------------------------------------------------------------

def test_search_row_prefers_public_profile_url_over_recruiter_link(monkeypatch):
    rows = _search(monkeypatch, [ROW_FULL])
    cand = rows[HASH_1]

    assert cand["profile_url"] == "https://www.linkedin.com/in/jane-doe-123"
    assert cand["recruiter_profile_url"] == _rps(HASH_1)
    assert "/talent/" not in cand["profile_url"]
    assert cand["id"] == f"unipile_{HASH_1}"
    assert cand["unipile_account_id"] == "acc-1"
    assert cand["recruiter_candidate_id"] == "rc-1"


def test_search_row_builds_public_url_from_public_identifier(monkeypatch):
    rows = _search(monkeypatch, [ROW_IDENT_ONLY])
    cand = rows[HASH_2]

    assert cand["profile_url"] == "https://www.linkedin.com/in/john-smith-42"
    assert cand["recruiter_profile_url"] == _rps(HASH_2)


def test_search_row_without_public_identity_never_exposes_rps_link_as_profile_url(monkeypatch):
    rows = _search(monkeypatch, [ROW_NO_PUBLIC])
    cand = rows[HASH_3]

    # Downstream keys on `linkedin.com/in/` — an RPS link there paywalls the
    # candidate and silently disables enrichment. Empty is the honest value.
    assert cand["profile_url"] == ""
    assert cand["recruiter_profile_url"] == _rps(HASH_3)


# ---------------------------------------------------------------------------
# 2. Names come from the public slug, never from the recruiter hash
# ---------------------------------------------------------------------------

def test_search_row_name_from_explicit_name(monkeypatch):
    rows = _search(monkeypatch, [ROW_FULL])
    cand = rows[HASH_1]
    assert cand["name"] == "Jane Doe"
    assert (cand["firstName"], cand["lastName"]) == ("Jane", "Doe")


def test_search_row_name_derived_from_public_identifier_when_name_is_null(monkeypatch):
    rows = _search(monkeypatch, [ROW_IDENT_ONLY])
    assert rows[HASH_2]["name"] == "John Smith"


def test_search_row_name_never_the_recruiter_hash(monkeypatch):
    rows = _search(monkeypatch, [ROW_NO_PUBLIC])
    name = rows[HASH_3]["name"]
    assert "AEMAA" not in name
    assert HASH_3.lower() not in name.lower()
    # Falls through to the headline.
    assert name == "Cloud Architect"


def test_resolve_candidate_name_ignores_digit_free_hash_in_rps_link():
    svc = UnipileService()
    item = {"id": HASH_2, "name": None, "profile_url": _rps(HASH_2), "headline": "Data Engineer"}
    assert svc._resolve_candidate_name(item) == "Data Engineer"


def test_public_profile_url_helper_only_returns_public_links():
    svc = UnipileService()
    assert svc._public_profile_url({"profile_url": _rps(HASH_1)}) is None
    assert (
        svc._public_profile_url({"profile_url": "https://www.linkedin.com/in/some-one"})
        == "https://www.linkedin.com/in/some-one"
    )
    assert svc._public_profile_url({"public_identifier": "a b"}) is None
    assert svc._public_profile_url({"public_identifier": "x/y"}) is None
    assert svc._public_profile_url({}) is None
    assert svc._recruiter_profile_url({"profile_url": "https://www.linkedin.com/in/some-one"}) is None
    assert svc._recruiter_profile_url({"profile_url": _rps(HASH_1)}) == _rps(HASH_1)


# ---------------------------------------------------------------------------
# 3. Search-row signal carried through: skills + open-to-work
# ---------------------------------------------------------------------------

def test_search_row_carries_skills_and_positive_open_to_work(monkeypatch):
    rows = _search(monkeypatch, [ROW_FULL, ROW_NO_PUBLIC])

    full = rows[HASH_1]
    assert full["skills"] == [{"name": "Java"}, {"name": "Spring Boot"}]
    assert full["open_to_work"] is True

    bare = rows[HASH_3]
    # Absence is NOT a negative signal — leave it unset so the Apify
    # resolver still runs for the row.
    assert "open_to_work" not in bare
    assert "skills" not in bare


# ---------------------------------------------------------------------------
# 4. Profile fetch asks for every section
# ---------------------------------------------------------------------------

def test_get_candidate_profile_requests_all_linkedin_sections(monkeypatch):
    calls = _patch_http(monkeypatch, profile={"provider_id": "ACoAAmember", "work_experience": []})
    svc = UnipileService()

    profile = asyncio.run(svc.get_candidate_profile(HASH_1, account_id="acc-1"))

    assert profile == {"provider_id": "ACoAAmember", "work_experience": []}
    get_calls = [c for c in calls if c["method"] == "GET" and "/users/" in c["url"]]
    assert len(get_calls) == 1
    assert get_calls[0]["url"].endswith(f"/users/{HASH_1}")
    assert get_calls[0]["params"] == {"account_id": "acc-1", "linkedin_sections": "*"}


# ---------------------------------------------------------------------------
# 5. Full-profile sections map onto the candidate row
# ---------------------------------------------------------------------------

FULL_PROFILE = {
    "provider_id": "ACoAAmember",
    "public_identifier": "jane-doe-123",
    "headline": "Senior Java Developer at Acme",
    "location": "Austin, Texas, United States",
    "is_open_to_work": True,
    "summary": "Builds platforms.",
    "work_experience": [
        {"position": "Staff Engineer", "company": "Acme", "start": "2021-03", "end": None},
        {"position": "Engineer", "company": "Globex", "start": "2017-01", "end": "2021-02"},
    ],
    "education": [
        {"degree": "BS Computer Science", "school": "UT Austin", "start": "2011", "end": "2015"},
    ],
    "skills": ["Java", {"name": "Kubernetes"}],
    "certifications": [
        {"name": "CKA", "organization": "CNCF", "start": "2023-01"},
    ],
}


def test_extract_profile_data_maps_unipile_sections():
    svc = UnifiedCandidateSearch()
    extracted = svc._extract_linkedin_profile_data(FULL_PROFILE)

    assert extracted["location"] == "Austin, Texas, United States"
    assert extracted["profile_url"] == "https://www.linkedin.com/in/jane-doe-123"
    assert extracted["open_to_work"] is True
    assert extracted["summary"] == "Builds platforms."

    # Current position becomes the row title (the search row's `title` is
    # the headline, which is what the role-anchor gate used to run against).
    assert extracted["title"] == "Staff Engineer"
    exp = extracted["company_experience"]
    assert [e["company"] for e in exp] == ["Acme", "Globex"]
    assert exp[0]["start_date"] == "2021-03"
    assert exp[1]["end_date"] == "2021-02"

    assert extracted["candidate_education"] == [
        {"degree": "BS Computer Science", "institution": "UT Austin", "year": "2015"},
    ]
    assert extracted["skills"] == [{"name": "Java"}, {"name": "Kubernetes"}]
    assert extracted["candidate_certification"] == [
        {"name": "CKA", "issuer": "CNCF", "year": "2023-01"},
    ]


def test_extract_profile_data_never_writes_rps_link_or_negative_otw():
    svc = UnifiedCandidateSearch()
    extracted = svc._extract_linkedin_profile_data(
        {
            "profile_url": _rps(HASH_1),
            "is_open_to_work": False,
            "work_experience": [],
        }
    )
    assert "profile_url" not in extracted
    assert "open_to_work" not in extracted
    assert "title" not in extracted


def test_extract_profile_data_builds_public_url_from_public_profile_url():
    svc = UnifiedCandidateSearch()
    extracted = svc._extract_linkedin_profile_data(
        {"public_profile_url": "https://www.linkedin.com/in/jane-doe-123"}
    )
    assert extracted["profile_url"] == "https://www.linkedin.com/in/jane-doe-123"
