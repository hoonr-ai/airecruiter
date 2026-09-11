"""Every request body PAIR sends to JobDiva must be schema-exact.

JobDiva silently ignores fields its Swagger does not define -- no 400, no
warning. That is how a ``candidateid`` sent to CreateJobApplicationWithResume
was dropped for months (every Launch PAIR of a JobDiva-sourced person minted a
duplicate profile), and how the wrapped TalentSearch payload was answered with an
unfiltered dump. These tests replay the service methods against a recording
HTTP client and check each payload against the checked-in Swagger snapshot
``tests/fixtures/jobdiva_swagger_v2_endpoints.json`` (refresh / drift-check with
``python scripts/jobdiva_swagger_snapshot.py [--check]``).

Deviations that predate this guard are inventoried in KNOWN_UNDEFINED_FIELDS,
exactly (a field that stops being sent must be removed from the list). They are
fields JobDiva has been ignoring all along, so removing them from the payloads
changes nothing on JobDiva's side -- but mapping them onto the fields the schema
DOES define (e.g. ``phones[]``) would, and needs a live check first.
"""
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Set

import pytest

from services.jobdiva import JobDivaService

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "jobdiva_swagger_v2_endpoints.json"
FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
ENDPOINTS = FIXTURE["endpoints"]

# Endpoints services/jobdiva.py calls that JobDiva's Swagger (v1 and v2) does not
# list. Calls to them are best-effort with fallbacks in the code; the inventory is
# exact so a NEW unlisted endpoint cannot slip in unnoticed.
KNOWN_NOT_IN_SWAGGER: Set[str] = {
    "/apiv2/jobdiva/createCandidateStickyNote",  # code falls back to createCandidateNote on failure
    "/apiv2/jobdiva/getCandidateById",
}

# Fields we send that the schema does not define -- JobDiva ignores them today.
# Nested list items are written as `field[].sub`.
KNOWN_UNDEFINED_FIELDS: Dict[str, Set[str]] = {
    # UpdateCandidateProfileDef has `phones: [PhoneType{action, ext, phone, type}]`,
    # not `phone` -- so the post-create phone write has never reached JobDiva.
    "/apiv2/jobdiva/updateCandidateProfile": {"phone"},
    # CreateCandidateProfileDef has cellphone/homephone/workphone and resumeSource.
    # (create_candidate has no callers today.)
    "/apiv2/jobdiva/createCandidate": {"phone", "candidateSource"},
    # QualificationType is {qualificationTypeId, qualificationValue, subValue}: the
    # name, date and recruiter we send per item are dropped; only the numeric
    # qualificationTypeId identifies the qualification.
    "/apiv2/jobdiva/updateCandidateQualifications": {
        "qualifications[].qualification",
        "qualifications[].date",
        "qualifications[].recruiterid",
    },
}

JOB_ID = 31920032


class _Resp:
    def __init__(self, status=200, text="", json_data=None):
        self.status_code, self.text = status, text
        self._json = json_data if json_data is not None else {}

    def json(self):
        return self._json


class _Recorder:
    """httpx.AsyncClient stand-in: records (method, path, json/params), answers 200."""

    def __init__(self, calls):
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None, **kw):
        path = url.split("api.jobdiva.com", 1)[-1] if "api.jobdiva.com" in url else "/" + url.split("/", 3)[-1]
        self._calls.append({"method": "post", "path": path, "json": json})
        if path.endswith("/searchCandidateProfile"):
            return _Resp(200, "[]", [])
        return _Resp(200, "777", {})

    async def get(self, url, params=None, headers=None, **kw):
        path = url.split("api.jobdiva.com", 1)[-1] if "api.jobdiva.com" in url else "/" + url.split("/", 3)[-1]
        self._calls.append({"method": "get", "path": path, "params": params})
        return _Resp(200, "[]", [])


def _service() -> JobDivaService:
    svc = JobDivaService()

    async def _auth(force_refresh=False):
        return "test-token"

    svc.authenticate = _auth  # type: ignore[assignment]
    return svc


def _capture(coro_factory) -> List[Dict[str, Any]]:
    calls: List[Dict[str, Any]] = []
    from unittest.mock import patch

    with patch("services.jobdiva.httpx.AsyncClient", lambda *a, **k: _Recorder(calls)):
        asyncio.run(coro_factory(_service()))
    return calls


def _undefined_fields(path: str, payload: Dict[str, Any]) -> Set[str]:
    """Field names in `payload` (nested list items as `field[].sub`) the schema lacks."""
    spec = ENDPOINTS.get(path)
    assert spec is not None, f"{path} is not in the Swagger snapshot (see KNOWN_NOT_IN_SWAGGER)"
    body = spec["post"]["body"]
    assert body, f"{path} POST declares no body schema"
    props = body["properties"]
    undefined = {k for k in payload if k not in props}
    for key, val in payload.items():
        item_schema = (props.get(key) or {}).get("items")
        if item_schema and isinstance(val, list):
            for item in val:
                if isinstance(item, dict):
                    undefined |= {f"{key}[].{sub}" for sub in item if sub not in item_schema["properties"]}
    return undefined


def _assert_contract(path: str, payload: Dict[str, Any]) -> None:
    body = ENDPOINTS[path]["post"]["body"]
    undefined = _undefined_fields(path, payload)
    known = KNOWN_UNDEFINED_FIELDS.get(path, set())
    new = undefined - known
    assert not new, (
        f"{path}: payload sends field(s) JobDiva's schema does not define -- they will be "
        f"SILENTLY IGNORED: {sorted(new)}. Defined fields: {sorted(body['properties'])}"
    )
    stale = known - undefined
    assert not stale, f"{path}: {sorted(stale)} no longer sent; remove from KNOWN_UNDEFINED_FIELDS"
    missing = set(body["required"]) - set(payload)
    assert not missing, f"{path}: payload lacks required field(s) {sorted(missing)}"


def _only(calls, path):
    hits = [c for c in calls if c["path"].endswith(path)]
    assert len(hits) == 1, f"expected exactly one call to {path}, got {[c['path'] for c in calls]}"
    return hits[0]


# ---------------------------------------------------------------------------
# The snapshot itself documents the contract that was violated
# ---------------------------------------------------------------------------

def test_snapshot_pins_the_two_application_endpoints():
    create = ENDPOINTS["/apiv2/jobdiva/CreateJobApplicationWithResume"]["post"]["body"]
    assert "candidateid" not in create["properties"], (
        "JobDiva now documents candidateid on CreateJobApplicationWithResume -- "
        "re-verify live before changing the link-first logic"
    )
    assert set(create["properties"]) == {
        "filecontent", "filename", "jobid", "recruiterid", "resumeDate", "resumesource", "textfile",
    }
    link = ENDPOINTS["/apiv2/jobdiva/createJobApplication"]["post"]["body"]
    assert set(link["required"]) == {"candidateid", "jobid"}
    assert ENDPOINTS["/apiv2/jobdiva/createJobApplication"]["post"]["response_200"] == {"type": "boolean"}


def test_unlisted_endpoints_inventory_is_exact():
    unlisted = {p for p, spec in ENDPOINTS.items() if spec is None}
    assert unlisted == KNOWN_NOT_IN_SWAGGER, (
        f"new unlisted endpoint(s): {sorted(unlisted - KNOWN_NOT_IN_SWAGGER)}; "
        f"now listed: {sorted(KNOWN_NOT_IN_SWAGGER - unlisted)}"
    )


def test_every_called_endpoint_is_in_the_snapshot():
    """The snapshot must cover every /apiv2/jobdiva/ path services/jobdiva.py hits."""
    import re

    src = (Path(__file__).parents[1] / "services" / "jobdiva.py").read_text(encoding="utf-8")
    called = {"/apiv2/jobdiva/" + m for m in re.findall(r"apiv2/jobdiva/([A-Za-z]+)", src)}
    missing = called - set(ENDPOINTS)
    assert not missing, (
        f"add to ENDPOINTS in scripts/jobdiva_swagger_snapshot.py and refresh the fixture: {sorted(missing)}"
    )


# ---------------------------------------------------------------------------
# Payloads the code actually builds
# ---------------------------------------------------------------------------

def test_create_job_application_payload():
    calls = _capture(lambda s: s.link_candidate_to_job("462058065251", str(JOB_ID)))
    call = _only(calls, "/apiv2/jobdiva/createJobApplication")
    _assert_contract("/apiv2/jobdiva/createJobApplication", call["json"])
    assert call["json"] == {"candidateid": 462058065251, "jobid": JOB_ID}


def test_create_job_application_with_resume_payload():
    calls = _capture(lambda s: s.create_job_application_with_resume(
        candidate_id=None, job_id=str(JOB_ID), resume_text="r", filename="x.txt",
    ))
    call = _only(calls, "/apiv2/jobdiva/CreateJobApplicationWithResume")
    _assert_contract("/apiv2/jobdiva/CreateJobApplicationWithResume", call["json"])
    assert "candidateid" not in call["json"]


def test_update_candidate_profile_payload():
    calls = _capture(lambda s: s._update_candidate_name("tok", 777, "Ada", "Lovelace", "ada@example.com", "5551234567"))
    call = _only(calls, "/apiv2/jobdiva/updateCandidateProfile")
    _assert_contract("/apiv2/jobdiva/updateCandidateProfile", call["json"])


def test_search_candidate_profile_payload():
    calls = _capture(lambda s: s.search_candidate_profile("ada@example.com", "Ada", "Lovelace", "5551234567"))
    call = _only(calls, "/apiv2/jobdiva/searchCandidateProfile")
    _assert_contract("/apiv2/jobdiva/searchCandidateProfile", call["json"])


def test_create_candidate_note_payload():
    calls = _capture(lambda s: s.create_candidate_note("777", str(JOB_ID), "PAIR Pass", "note", 42))
    call = _only(calls, "/apiv2/jobdiva/createCandidateNote")
    _assert_contract("/apiv2/jobdiva/createCandidateNote", call["json"])


def test_update_candidate_qualification_payload():
    calls = _capture(lambda s: s.update_candidate_qualification("777", "PAIR Candidates", "PASS", 42, None, 5))
    call = _only(calls, "/apiv2/jobdiva/updateCandidateQualifications")
    _assert_contract("/apiv2/jobdiva/updateCandidateQualifications", call["json"])


def test_create_candidate_payload():
    calls = _capture(lambda s: s.create_candidate("Ada", "Lovelace", "ada@example.com", "5551234567"))
    call = _only(calls, "/apiv2/jobdiva/createCandidate")
    _assert_contract("/apiv2/jobdiva/createCandidate", call["json"])


def test_sticky_note_is_a_known_unlisted_endpoint():
    calls = _capture(lambda s: s.create_candidate_sticky_note("777", "note", 42))
    call = _only(calls, "/apiv2/jobdiva/createCandidateStickyNote")
    assert call["path"] in KNOWN_NOT_IN_SWAGGER


# ---------------------------------------------------------------------------
# Opt-in live drift check (network): JOBDIVA_SWAGGER_LIVE=1 pytest -k live_swagger
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not os.getenv("JOBDIVA_SWAGGER_LIVE"), reason="set JOBDIVA_SWAGGER_LIVE=1 to hit api.jobdiva.com")
def test_live_swagger_matches_snapshot():
    sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
    import jobdiva_swagger_snapshot as snap  # noqa: E402

    live = snap.extract(snap.fetch_live())
    changed = snap.diff_endpoints(ENDPOINTS, live)
    assert not changed, f"JobDiva Swagger drifted for {changed}; run scripts/jobdiva_swagger_snapshot.py"
