"""Auth guard on GET /interviews/{interview_id}/recordings (engagement.py).

Call recordings are presigned S3 playback links for candidate call audio —
sensitive PII. The route previously required only a signed-in user, with no
check that the caller was allowed to see *that* interview: any authenticated
user who had or guessed an interview_id could pull another recruiter's
candidate recordings.

The AST tests below (mirroring tests/test_candidates_router_auth.py) pin
that the guard calls exist and sit on the right branch, but string-matching
alone would pass even if the call were dead code. The behavioral tests
further down call the handler directly with its DB/HTTP boundaries mocked,
to prove the guard actually runs and actually blocks/allows the right users.
"""
import ast
import asyncio
import hashlib
from pathlib import Path
from typing import Dict
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from core.auth import UserIdentity
from routers import engagement, _helpers

ROUTER_PATH = Path(__file__).resolve().parents[1] / "routers" / "engagement.py"


def _func_body(name: str) -> str:
    src = ROUTER_PATH.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError(f"{name} not found in {ROUTER_PATH.name}")


def _signature(body: str) -> str:
    return body[: body.find("):")]


def test_get_interview_recordings_requires_authentication():
    body = _func_body("get_interview_recordings")
    assert "get_current_user" in _signature(body), (
        "get_interview_recordings must take `user: UserIdentity = "
        "Depends(get_current_user)` — it hands out presigned S3 URLs for "
        "candidate call audio"
    )


def test_get_interview_recordings_verifies_job_access():
    """Login alone is not enough — the caller must be scoped to this
    interview's job, same as the rest of the /interviews/{id}/... routes."""
    body = _func_body("get_interview_recordings")
    assert "_verify_job_access_by_id" in body, (
        "get_interview_recordings must call _verify_job_access_by_id(...) so "
        "a recruiter can't pull call recordings for a candidate on a job "
        "they're not assigned to"
    )


def test_get_interview_recordings_denies_when_jobdiva_id_missing():
    """An interview with no jobdiva_id on its audit row can't be scoped to a
    job at all — the fallback must deny non-admins, not default-allow."""
    body = _func_body("get_interview_recordings")
    assert "elif not user.is_admin:" in body
    assert "status_code=403" in body


# ---------------------------------------------------------------------------
# Behavioral: call the handler directly with its DB/HTTP boundaries mocked.
# ---------------------------------------------------------------------------
class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """Stands in for `httpx.AsyncClient(...)` used as an async context manager."""

    def __init__(self, *_args, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def get(self, *_args, **_kwargs):
        return _FakeResponse({"data": {"sessions": []}})


def _recruiter(email="recruiter@example.com") -> UserIdentity:
    return UserIdentity(email=email, role="recruiter")


def _admin(email="admin@example.com") -> UserIdentity:
    return UserIdentity(email=email, role="admin")


def _audit_row(jobdiva_id):
    return {"jobdiva_id": jobdiva_id, "created_at": None}


def _job_draft(recruiter_emails):
    return {
        "status": "success",
        "data": {"recruiter_emails": recruiter_emails},
    }


def _run_recordings(interview_id, user):
    return asyncio.run(engagement.get_interview_recordings(interview_id, user=user))


@pytest.fixture(autouse=True)
def _enable_recordings():
    with patch("services.call_recordings.is_enabled", return_value=True), \
         patch("services.call_recordings.find_recordings", return_value={"recordings": [], "unavailable": False}), \
         patch("routers.engagement.httpx.AsyncClient", _FakeAsyncClient):
        yield


def test_non_assigned_recruiter_gets_403():
    """A recruiter not on the job's recruiter_emails must be denied, even
    though they're logged in and the interview genuinely exists."""
    with patch("routers.engagement._recording_audit_row", return_value=_audit_row("job-1")), \
         patch("routers._helpers._get_job_draft_sync", return_value=_job_draft(["someone-else@example.com"])):
        with pytest.raises(HTTPException) as exc_info:
            _run_recordings("interview-1", _recruiter("me@example.com"))
    assert exc_info.value.status_code == 403


def test_non_admin_with_no_jobdiva_id_gets_403():
    """No jobdiva_id on the audit row means the interview can't be scoped to
    any job — a non-admin must be denied rather than default-allowed."""
    with patch("routers.engagement._recording_audit_row", return_value=_audit_row(None)):
        with pytest.raises(HTTPException) as exc_info:
            _run_recordings("interview-2", _recruiter("me@example.com"))
    assert exc_info.value.status_code == 403


def test_assigned_recruiter_gets_200():
    """A recruiter on the job's recruiter_emails list can read it."""
    with patch("routers.engagement._recording_audit_row", return_value=_audit_row("job-1")), \
         patch("routers._helpers._get_job_draft_sync", return_value=_job_draft(["me@example.com"])):
        result = _run_recordings("interview-3", _recruiter("me@example.com"))
    assert result["success"] is True
    assert result["enabled"] is True


def test_admin_gets_200_even_with_no_jobdiva_id():
    """Admins bypass job scoping entirely, including the unresolvable case."""
    with patch("routers.engagement._recording_audit_row", return_value=_audit_row(None)):
        result = _run_recordings("interview-4", _admin())
    assert result["success"] is True
    assert result["enabled"] is True


def test_recording_response_hashes_s3_key_and_omits_raw_key():
    raw_key = "recordings/2026-10-05/job-1/0001/1234_session.ogg"
    with patch("routers.engagement._recording_audit_row", return_value=_audit_row(None)), \
         patch(
             "services.call_recordings.find_recordings",
             return_value={
                 "recordings": [{"key": raw_key, "url": "https://example.com/audio.ogg"}],
                 "unavailable": False,
             },
         ):
        result = _run_recordings("interview-5", _admin())

    recording = result["recordings"][0]
    assert recording["id"] == hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:16]
    assert "key" not in recording


def test_denied_access_is_checked_against_the_audit_rows_job_and_short_circuits():
    """A unit-level check on the security boundary itself, not just the
    source text: mocks `_verify_job_access_by_id` to reject, then asserts
    (a) the handler surfaces a 403, (b) the guard was called with the
    audit row's own jobdiva_id and the actual requesting user — not some
    other id/user — and (c) the PAIR detail call and the S3 lookup never
    ran. A guard called with the wrong arguments, or called only after
    the protected work already happened, would be invisible to the
    source-text/AST tests above but fails this one.
    """
    user = _recruiter("me@example.com")
    jobdiva_id = "job-99"

    verify_mock = MagicMock(side_effect=HTTPException(status_code=403, detail="denied"))
    client_instantiations = []

    class _TrackedAsyncClient(_FakeAsyncClient):
        def __init__(self, *args, **kwargs):
            client_instantiations.append((args, kwargs))
            super().__init__(*args, **kwargs)

    with patch("routers.engagement._recording_audit_row", return_value=_audit_row(jobdiva_id)), \
         patch("routers.engagement._verify_job_access_by_id", verify_mock), \
         patch("routers.engagement.httpx.AsyncClient", _TrackedAsyncClient), \
         patch("services.call_recordings.find_recordings") as find_mock:
        with pytest.raises(HTTPException) as exc_info:
            _run_recordings("interview-99", user)

    assert exc_info.value.status_code == 403
    verify_mock.assert_called_once_with(jobdiva_id, user)
    assert client_instantiations == [], (
        "the PAIR /detail call must not run once access is denied"
    )
    find_mock.assert_not_called()
