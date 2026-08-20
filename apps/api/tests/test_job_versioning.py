"""Tests for JobDiva job versioning (-vN) ref handling.

A versioned ref like "26-06182-v2" is a LOCAL clone identity. The invariants:
  - Every EXTERNAL JobDiva HTTP call must use the ROOT ref "26-06182"
    (the -vN suffix stripped), because JobDiva has no record of "-v2".
  - Local DB identity / candidate + rubric buckets keep the versioned ref.
  - The ref is never digit-mashed (int("".join(filter(str.isdigit, ref)))).

These tests exercise the real service methods with JobDiva's HTTP layer mocked,
asserting on the exact OUTBOUND payloads so we prove the strip happens at the
boundary (not just that a helper exists).

Run standalone:
    cd apps/api && python -m tests.test_job_versioning
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.env_stubs import stub_required_env

stub_required_env()

from unittest.mock import patch  # noqa: E402

from services.jobdiva import (  # noqa: E402
    JobDivaService,
    strip_job_version_suffix,
)

ROOT_NUMERIC = 31920032
ROOT_REF = "26-06182"
V2_REF = "26-06182-v2"

# Minimal JobDiva SearchJob row that get_job_by_id can parse into a result.
ROOT_JOB_ROW = {
    "id": ROOT_NUMERIC,
    "reference #": ROOT_REF,
    "job title": "Senior Platform Engineer",
    "customer": "Acme Corp",
    "job status": "OPEN",
    "job description": "Build things.",
}


class _FakeResponse:
    def __init__(self, status=200, json_data=None, text=""):
        self.status_code = status
        self._json = json_data if json_data is not None else {}
        self.text = text

    def json(self):
        return self._json


def _dispatch(url):
    if "SearchJob" in url:
        return _FakeResponse(200, [ROOT_JOB_ROW])
    if "JobDetail" in url:
        return _FakeResponse(200, {"data": []})
    if "JobsApplicantsDetail" in url:
        return _FakeResponse(200, [])
    if "CreateJobApplicationWithResume" in url:
        return _FakeResponse(200, {}, text="777")
    return _FakeResponse(200, {})


class _FakeAsyncClient:
    """Records outbound calls and dispatches a canned response by URL."""

    def __init__(self, calls, *args, **kwargs):
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None, **kw):
        self._calls.append({"method": "POST", "url": url, "json": json, "params": kw.get("params")})
        return _dispatch(url)

    async def get(self, url, params=None, headers=None, **kw):
        self._calls.append({"method": "GET", "url": url, "json": None, "params": params})
        return _dispatch(url)


def _make_factory(calls):
    def factory(*args, **kwargs):
        return _FakeAsyncClient(calls)

    return factory


def _service():
    svc = JobDivaService()

    async def _auth():
        return "test-token"

    svc.authenticate = _auth  # type: ignore[assignment]
    # get_job_by_id consults the local DB for self-healing — keep it offline.
    svc.get_locally_monitored_job = lambda *a, **k: None  # type: ignore[assignment]
    return svc


def _find(calls, url_substr, method=None):
    for c in calls:
        if url_substr in c["url"] and (method is None or c["method"] == method):
            return c
    return None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_strip_helper():
    assert strip_job_version_suffix(V2_REF) == ROOT_REF
    assert strip_job_version_suffix("26-06182-v10") == ROOT_REF
    assert strip_job_version_suffix(ROOT_REF) == ROOT_REF
    assert strip_job_version_suffix("EXT-5") == "EXT-5"      # not a version suffix
    assert strip_job_version_suffix("31920032") == "31920032"
    assert strip_job_version_suffix(" 26-06182-v2 ") == ROOT_REF
    assert strip_job_version_suffix(None) is None


def test_get_job_by_id_sends_root_ref_to_jobdiva():
    """get_job_by_id("26-06182-v2") must query JobDiva with the ROOT ref."""
    calls = []
    svc = _service()
    with patch("services.jobdiva.httpx.AsyncClient", _make_factory(calls)):
        result = asyncio.run(svc.get_job_by_id(V2_REF))

    assert result is not None, "expected a job (strip lets the strict ref-match pass)"
    assert str(result["id"]) == str(ROOT_NUMERIC)
    assert result["jobdiva_id"] == ROOT_REF

    search = _find(calls, "SearchJob", "POST")
    assert search is not None, "SearchJob was never called"
    # The crux: the outbound payload carries the ROOT ref, never the -v2 suffix.
    assert search["json"] == {"jobdivaref": ROOT_REF, "maxReturned": 1}
    assert "v2" not in str(search["json"])


def test_resolve_jobdiva_job_id_strips_and_resolves():
    calls = []
    svc = _service()
    with patch("services.jobdiva.httpx.AsyncClient", _make_factory(calls)):
        resolved = asyncio.run(svc._resolve_jobdiva_job_id(V2_REF))
    assert resolved == ROOT_NUMERIC
    search = _find(calls, "SearchJob", "POST")
    assert search["json"]["jobdivaref"] == ROOT_REF


def test_get_job_applicants_detail_accepts_versioned_string():
    """B1 contract: a versioned-ref STRING must resolve to the root numeric id
    and never raise (the caller used to int() it and crash)."""
    calls = []
    svc = _service()
    with patch("services.jobdiva.httpx.AsyncClient", _make_factory(calls)):
        applicants = asyncio.run(svc.get_job_applicants_detail(V2_REF))
    assert applicants == []  # canned empty list, but importantly: no exception
    detail = _find(calls, "JobsApplicantsDetail", "GET")
    assert detail is not None, "applicants endpoint was never called"
    assert detail["params"] == {"jobIds": [ROOT_NUMERIC]}


def test_create_job_application_uses_resolved_numeric_not_digit_mash():
    """create_job_application_with_resume("26-06182-v2") must send jobid=31920032,
    not the digit-mashed 26061822."""
    calls = []
    svc = _service()
    with patch("services.jobdiva.httpx.AsyncClient", _make_factory(calls)):
        ok, new_cid = asyncio.run(
            svc.create_job_application_with_resume(
                candidate_id=None,
                job_id=V2_REF,
                resume_text="resume",
                first_name="",  # empty → skip the follow-up name-update call
                last_name="",
                email="",
            )
        )
    assert ok is True
    assert new_cid == 777
    create = _find(calls, "CreateJobApplicationWithResume", "POST")
    assert create is not None, "CreateJobApplicationWithResume was never called"
    assert create["json"]["jobid"] == ROOT_NUMERIC
    assert create["json"]["jobid"] != 26061822  # the old digit-mash bug


# ---------------------------------------------------------------------------
# Standalone runner (mirrors the repo's other test files)
# ---------------------------------------------------------------------------

def _run_all():
    tests = [
        test_strip_helper,
        test_get_job_by_id_sends_root_ref_to_jobdiva,
        test_resolve_jobdiva_job_id_strips_and_resolves,
        test_get_job_applicants_detail_accepts_versioned_string,
        test_create_job_application_uses_resolved_numeric_not_digit_mash,
    ]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS: {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL: {t.__name__}: {e!r}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return failures


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
