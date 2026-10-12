"""routers/launch_timing: GET /api/v1/jobs/{job_id}/launch-timing and
POST /api/v1/jobs/{job_id}/launch-clicked.

Beyond the auth guard, these pin the "never break the wizard" contract the
step-time endpoint has: a DB failure in the access check or in the
read/stamp answers 200 {"status": "error"}, never a 500 or a 403; only a real
access denial is a 403, and an unknown job a 404 on the read.
"""

import ast
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from core.auth import UserIdentity, get_current_user
from routers import launch_timing as router_mod

ROUTER_PATH = Path(__file__).resolve().parents[1] / "routers" / "launch_timing.py"

RECRUITER = UserIdentity(email="priya.recruiter@example.com", role="recruiter")
ADMIN = UserIdentity(email="admin@example.com", role="admin")


def _client(user=RECRUITER):
    app = FastAPI()
    app.include_router(router_mod.router)
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


@pytest.fixture()
def allow_access():
    with patch.object(router_mod, "_check_access_sync") as check:
        yield check


# ---------------------------------------------------------------------------
# Route inventory and guards (read from source, like the candidates router)
# ---------------------------------------------------------------------------
def _routes():
    src = ROUTER_PATH.read_text(encoding="utf-8")
    found = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for d in node.decorator_list:
            if (
                isinstance(d, ast.Call)
                and isinstance(d.func, ast.Attribute)
                and isinstance(d.func.value, ast.Name)
                and d.func.value.id == "router"
            ):
                body = ast.get_source_segment(src, node) or ""
                found.append({
                    "method": d.func.attr.upper(),
                    "path": d.args[0].value,
                    "authenticated": "get_current_user" in body[: body.find("):")],
                    "job_access": "_access_or_error(" in body,
                })
    return found


def test_every_route_is_authenticated_and_job_scoped():
    routes = _routes()
    assert {(r["method"], r["path"]) for r in routes} == {
        ("GET", "/jobs/{job_id}/launch-timing"),
        ("POST", "/jobs/{job_id}/launch-clicked"),
    }
    for r in routes:
        assert r["authenticated"], r
        assert r["job_access"], r


def test_routes_live_under_api_v1_so_nginx_needs_no_change():
    paths = {getattr(r, "path", "") for r in router_mod.router.routes}
    assert paths == {"/api/v1/jobs/{job_id}/launch-timing", "/api/v1/jobs/{job_id}/launch-clicked"}


def test_unauthenticated_requests_are_rejected():
    app = FastAPI()
    app.include_router(router_mod.router)

    def deny():
        raise HTTPException(status_code=401, detail="Not authenticated")

    app.dependency_overrides[get_current_user] = deny
    client = TestClient(app)
    assert client.get("/api/v1/jobs/1/launch-timing").status_code == 401
    assert client.post("/api/v1/jobs/1/launch-clicked").status_code == 401


# ---------------------------------------------------------------------------
# GET launch-timing
# ---------------------------------------------------------------------------
def test_get_returns_the_job_timing(allow_access):
    timing = {"job_id": "31990001", "click_to_launch_minutes": 9.0, "sessions": []}
    with patch.object(router_mod.launch_timing, "job_timing", return_value=timing) as read:
        res = _client().get("/api/v1/jobs/26-001/launch-timing")
    assert res.status_code == 200
    assert res.json() == {"status": "success", "data": timing}
    read.assert_called_once_with("26-001")
    allow_access.assert_called_once_with("26-001", RECRUITER)


def test_get_unknown_job_is_404(allow_access):
    with patch.object(router_mod.launch_timing, "job_timing", return_value=None):
        assert _client().get("/api/v1/jobs/nope/launch-timing").status_code == 404


def test_get_db_failure_is_a_soft_error(allow_access):
    with patch.object(router_mod.launch_timing, "job_timing", side_effect=RuntimeError("pool exhausted")):
        res = _client().get("/api/v1/jobs/26-001/launch-timing")
    assert res.status_code == 200 and res.json() == {"status": "error", "data": None}


def test_access_lookup_failure_is_a_soft_error_not_a_403():
    with patch.object(router_mod, "_check_access_sync", side_effect=RuntimeError("connect timeout")), \
         patch.object(router_mod.launch_timing, "job_timing") as read, \
         patch.object(router_mod.launch_timing, "stamp_launch_clicked") as stamp:
        client = _client()
        assert client.get("/api/v1/jobs/26-001/launch-timing").json() == {"status": "error", "data": None}
        assert client.post("/api/v1/jobs/26-001/launch-clicked").json() == {"status": "error"}
    read.assert_not_called()
    stamp.assert_not_called()


def test_a_real_denial_is_a_403_and_nothing_is_read_or_written():
    denied = HTTPException(status_code=403, detail="Access denied.")
    with patch.object(router_mod, "_check_access_sync", side_effect=denied), \
         patch.object(router_mod.launch_timing, "job_timing") as read, \
         patch.object(router_mod.launch_timing, "stamp_launch_clicked") as stamp:
        client = _client()
        assert client.get("/api/v1/jobs/26-001/launch-timing").status_code == 403
        assert client.post("/api/v1/jobs/26-001/launch-clicked").status_code == 403
    read.assert_not_called()
    stamp.assert_not_called()


# ---------------------------------------------------------------------------
# POST launch-clicked
# ---------------------------------------------------------------------------
def test_post_stamps_the_click(allow_access):
    with patch.object(router_mod.launch_timing, "stamp_launch_clicked", return_value="31990001") as stamp:
        res = _client().post("/api/v1/jobs/26-001/launch-clicked", json={"anything": "ignored"})
    assert res.status_code == 200
    assert res.json() == {"status": "success", "job_id": "31990001"}
    stamp.assert_called_once_with("26-001")  # server clock: no client time accepted


def test_post_unknown_job_or_failed_write_is_ignored(allow_access):
    with patch.object(router_mod.launch_timing, "stamp_launch_clicked", return_value=None):
        assert _client().post("/api/v1/jobs/26-001/launch-clicked").json() == {"status": "ignored"}


# ---------------------------------------------------------------------------
# The access rule itself
# ---------------------------------------------------------------------------
def _draft(recruiter_emails):
    return {"status": "success", "data": {"job_id": "26-001", "recruiter_emails": recruiter_emails}}


def test_check_access_admin_skips_the_lookup():
    with patch.object(router_mod, "_get_job_draft_sync", side_effect=AssertionError("looked up")):
        router_mod._check_access_sync("26-001", ADMIN)


def test_check_access_assigned_recruiter_passes_and_others_get_403():
    with patch.object(router_mod, "_get_job_draft_sync", return_value=_draft(["priya.recruiter@example.com"])):
        router_mod._check_access_sync("26-001", RECRUITER)
    with patch.object(router_mod, "_get_job_draft_sync", return_value=_draft(["someone.else@example.com"])), \
         patch("core.auth.get_user_scope_emails", return_value={"priya.recruiter@example.com"}):
        with pytest.raises(HTTPException) as exc:
            router_mod._check_access_sync("26-001", RECRUITER)
    assert exc.value.status_code == 403


def test_check_access_lets_an_unsaved_job_through_and_raises_on_db_errors():
    not_saved = {"status": "error", "message": "No data found for job 26-001"}
    with patch.object(router_mod, "_get_job_draft_sync", return_value=not_saved):
        router_mod._check_access_sync("26-001", RECRUITER)
    with patch.object(router_mod, "_get_job_draft_sync", side_effect=RuntimeError("db down")):
        with pytest.raises(RuntimeError):
            router_mod._check_access_sync("26-001", RECRUITER)
    with patch.object(router_mod, "_get_job_draft_sync", return_value={"status": "error", "message": "boom"}):
        with pytest.raises(HTTPException) as exc:
            router_mod._check_access_sync("26-001", RECRUITER)
    assert exc.value.status_code == 403
