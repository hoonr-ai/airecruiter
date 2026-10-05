"""Auth guard on GET /interviews/{interview_id}/recordings (engagement.py).

Call recordings are presigned S3 playback links for candidate call audio —
sensitive PII. The route previously required only a signed-in user, with no
check that the caller was allowed to see *that* interview: any authenticated
user who had or guessed an interview_id could pull another recruiter's
candidate recordings. Mirrors the static-AST style of
tests/test_candidates_router_auth.py rather than standing up a DB + TestClient.
"""
import ast
from pathlib import Path
from typing import Dict

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
