"""Auth guards on the engagement router.

This app has NO global auth middleware and this router is mounted without
router-level dependencies, so every endpoint must carry its own
`Depends(get_current_user)` and be classified: job-scoped
(`_verify_job_access_by_id`), interview-scoped (`_verify_interview_access`),
admin-only (`_require_admin`) or auth-only. Until 2026-09 none of the 19
pre-existing routes had a guard: anyone on the internet could launch PAIR
outreach for any job, write JobDiva applications via re-provision, and read
every interview transcript and evaluation.

The AST half reads the router's own source so it needs no DB; the TestClient
half exercises the guards with the DB and the Pairbot proxies faked.
"""
import ast
from pathlib import Path
from typing import Any, Dict, List, Set

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from core.auth import UserIdentity, get_current_user
import routers.engagement as eng

ROUTER_PATH = Path(__file__).resolve().parents[1] / "routers" / "engagement.py"
SRC = ROUTER_PATH.read_text(encoding="utf-8")

# An entry here is a documented hole, not a parking space.
KNOWN_UNAUTHENTICATED: Set[str] = set()

JOB_SCOPED = {
    "generate_engage_payload", "send_bulk_interview", "launch_bulk_interviews",
    "get_latest_interview", "get_pair_outreach", "get_pair_outreach_jd",
}
INTERVIEW_SCOPED = {
    "get_assessment_data", "get_outreach_status", "get_transcriptions",
    "get_interview_evaluation", "get_interview_score_summary", "get_activity_logs",
    "download_transcriptions",
}
ADMIN_ONLY = {
    "start_scheduler", "trigger_phase2", "re_provision_candidates",
    "jobdiva_profile_audit", "jobdiva_profile_audit_repair",
}
# Aggregate dashboards and the (frontend-unused) Pairbot status proxy: any
# authenticated user, no per-job scope available.
AUTH_ONLY = {"stream_engagement_status", "get_pair_metrics", "get_pair_passed"}


def _routes() -> List[Dict[str, Any]]:
    tree = ast.parse(SRC)
    found: List[Dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        decorators = [
            (d.func.attr.upper(), d.args[0].value)
            for d in node.decorator_list
            if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
            and isinstance(d.func.value, ast.Name) and d.func.value.id == "router"
            and d.args and isinstance(d.args[0], ast.Constant)
        ]
        if not decorators:
            continue
        body = ast.get_source_segment(SRC, node) or ""
        signature = body[: body.find("):")]
        for method, path in decorators:
            found.append({
                "method": method, "path": path, "func": node.name, "line": node.lineno,
                "authenticated": "get_current_user" in signature,
                "job_scoped": "_verify_job_access_by_id(" in body,
                "interview_scoped": "_verify_interview_access(" in body,
                "admin_only": "_require_admin(" in body,
            })
    return found


# ---------------------------------------------------------------------------
# Static: every route is authenticated and classified
# ---------------------------------------------------------------------------

def test_every_route_declares_get_current_user():
    unguarded = [
        f"{r['method']} {r['path']} ({r['func']}:{r['line']})"
        for r in _routes() if not r["authenticated"] and r["func"] not in KNOWN_UNAUTHENTICATED
    ]
    assert not unguarded, (
        "Routes missing `user: UserIdentity = Depends(get_current_user)` — this router "
        "has no router-level auth, so these are PUBLIC in PROD:\n  " + "\n  ".join(unguarded)
    )
    stale = KNOWN_UNAUTHENTICATED - {r["func"] for r in _routes() if not r["authenticated"]}
    assert not stale, f"now guarded, remove from KNOWN_UNAUTHENTICATED: {sorted(stale)}"


def test_every_route_is_classified_exactly_once():
    funcs = {r["func"] for r in _routes()}
    classified = JOB_SCOPED | INTERVIEW_SCOPED | ADMIN_ONLY | AUTH_ONLY
    assert funcs == classified, (
        f"unclassified routes (add to one of the sets above and give it the matching guard): "
        f"{sorted(funcs - classified)}; listed but gone: {sorted(classified - funcs)}"
    )
    groups = [JOB_SCOPED, INTERVIEW_SCOPED, ADMIN_ONLY, AUTH_ONLY]
    for i, a in enumerate(groups):
        for b in groups[i + 1:]:
            assert not (a & b), f"route in two groups: {sorted(a & b)}"


def test_each_route_carries_the_guard_its_class_requires():
    by_func = {r["func"]: r for r in _routes()}
    for f in JOB_SCOPED:
        assert by_func[f]["job_scoped"], f"{f} must call _verify_job_access_by_id(...)"
    for f in INTERVIEW_SCOPED:
        assert by_func[f]["interview_scoped"], f"{f} must call _verify_interview_access(...)"
    for f in ADMIN_ONLY:
        assert by_func[f]["admin_only"], f"{f} must call _require_admin(user)"


def test_handlers_are_never_called_as_plain_functions():
    """A handler with a `Depends(...)` default called directly binds the Depends
    object as `user`. Internal callers (the applicant auto-launch) must use the
    core functions the thin wrappers delegate to."""
    for handler in ("generate_engage_payload", "send_bulk_interview", "launch_bulk_interviews"):
        assert f"await {handler}(" not in SRC, f"{handler} is called as a function; call its _core/_for twin"
    assert "await _generate_payload_for(" in SRC and "await _send_bulk_interview_core(" in SRC


# ---------------------------------------------------------------------------
# Behavioural: guards fire through the HTTP layer
# ---------------------------------------------------------------------------

class _Cursor:
    def __init__(self, row):
        self._row = row

    def execute(self, sql, params=None):
        self.sql, self.params = sql, params

    def fetchone(self):
        return self._row

    def close(self):
        pass


class _Conn:
    def __init__(self, row):
        self._row = row

    def cursor(self, **_kw):
        return _Cursor(self._row)

    def close(self):
        pass


def _client(role: str) -> TestClient:
    app = FastAPI()
    app.include_router(eng.router)
    app.dependency_overrides[get_current_user] = lambda: UserIdentity(email="who@pyramidci.com", role=role)
    return TestClient(app)


@pytest.fixture
def fakes(monkeypatch):
    calls: Dict[str, List[Any]] = {"proxy_get": [], "proxy_post": [], "job_access": [], "core": []}

    async def _proxy_get(path, params=None):
        calls["proxy_get"].append(path)
        return {"ok": True, "path": path}

    async def _proxy_post(path, json_data=None):
        calls["proxy_post"].append(path)
        return {"ok": True, "path": path}

    def _job_access(job_id, user, allow_not_found=False):
        calls["job_access"].append((job_id, user.role, allow_not_found))
        if user.role != "admin" and job_id.startswith("forbidden"):
            raise HTTPException(status_code=403, detail="Access denied")

    async def _generate(request):
        calls["core"].append(("generate", request.job_id))
        return {"success": True, "payload": "{}"}

    monkeypatch.setattr(eng, "_proxy_get", _proxy_get)
    monkeypatch.setattr(eng, "_proxy_post", _proxy_post)
    monkeypatch.setattr(eng, "_verify_job_access_by_id", _job_access)
    monkeypatch.setattr(eng, "_generate_payload_for", _generate)
    monkeypatch.setattr(eng, "_get_db_connection", lambda: _Conn(("26-1",)))
    return calls


def test_ops_endpoints_are_admin_only(fakes):
    rec = _client("recruiter")
    assert rec.post("/outreach/start-scheduler").status_code == 403
    assert rec.post("/interviews/int-1/trigger-phase2").status_code == 403
    assert rec.post("/engage/re-provision", json={"job_id": "26-1"}).status_code == 403
    assert fakes["proxy_post"] == []

    adm = _client("admin")
    assert adm.post("/outreach/start-scheduler").status_code == 200
    assert fakes["proxy_post"] == ["/api/outreach/start-scheduler"]


def test_generate_payload_is_job_scoped(fakes):
    rec = _client("recruiter")
    res = rec.post("/engage/generate-payload", json={"candidate_ids": ["1"], "job_id": "forbidden-26-9"})
    assert res.status_code == 403
    assert fakes["core"] == []  # refused before any work

    res = rec.post("/engage/generate-payload", json={"candidate_ids": ["1"], "job_id": "26-1"})
    assert res.status_code == 200
    assert fakes["job_access"][-1] == ("26-1", "recruiter", True)
    assert fakes["core"] == [("generate", "26-1")]


def test_send_bulk_interview_scopes_on_the_payloads_job(fakes, monkeypatch):
    async def _core(request):
        return {"success": True}
    monkeypatch.setattr(eng, "_send_bulk_interview_core", _core)
    rec = _client("recruiter")
    payload = '{"jd": {"jobdiva_id": "forbidden-26-9"}, "resumes": []}'
    res = rec.post("/engage/send-bulk-interview", json={"payload": payload, "real_candidate_ids": ["1"]})
    assert res.status_code == 403
    assert fakes["job_access"][-1][0] == "forbidden-26-9"


def test_send_bulk_interview_without_a_job_is_refused_not_waved_through(fakes, monkeypatch):
    """An optional scope key must not be a skippable guard (the candidates-router
    evaluation-report lesson): no job in the payload -> 400, core never runs."""
    called = []

    async def _core(request):
        called.append(request)
        return {"success": True}
    monkeypatch.setattr(eng, "_send_bulk_interview_core", _core)
    rec = _client("recruiter")
    for payload in ('{"resumes": []}', '{"jd": {}, "resumes": []}', 'not json'):
        res = rec.post("/engage/send-bulk-interview", json={"payload": payload, "real_candidate_ids": ["1"]})
        assert res.status_code == 400, payload
    assert called == [] and fakes["job_access"] == []


def test_launch_refuses_before_streaming(fakes):
    res = _client("recruiter").post("/engage/launch", json={"candidate_ids": ["1"], "job_id": "forbidden-26-9"})
    assert res.status_code == 403


def test_interview_routes_resolve_the_job_through_the_audit(fakes):
    rec = _client("recruiter")
    for path in ("/interviews/int-1/evaluation", "/interviews/int-1/transcriptions",
                 "/interviews/int-1/score-summary", "/interviews/int-1/activity-logs",
                 "/interviews/int-1/outreach-status"):
        assert rec.get(path).status_code == 200, path
    # every call verified access to the job the audit row named
    assert fakes["job_access"] and all(j == ("26-1", "recruiter", True) for j in fakes["job_access"])
    assert len(fakes["proxy_get"]) == 5


def test_interview_routes_refuse_a_foreign_job(fakes, monkeypatch):
    monkeypatch.setattr(eng, "_get_db_connection", lambda: _Conn(("forbidden-26-9",)))
    assert _client("recruiter").get("/interviews/int-1/evaluation").status_code == 403
    assert fakes["proxy_get"] == []


def test_unknown_interview_stays_readable_to_authenticated_users(fakes, monkeypatch):
    monkeypatch.setattr(eng, "_get_db_connection", lambda: _Conn(None))
    assert _client("recruiter").get("/interviews/never-audited/evaluation").status_code == 200
    assert fakes["job_access"] == []  # nothing to scope against


def test_interview_lookup_failure_fails_closed(fakes, monkeypatch):
    def _boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(eng, "_get_db_connection", _boom)
    assert _client("recruiter").get("/interviews/int-1/evaluation").status_code == 403
    assert _client("admin").get("/interviews/int-1/evaluation").status_code == 200  # admins skip the lookup


def test_dashboard_by_job_is_job_scoped(fakes):
    rec = _client("recruiter")
    assert rec.get("/dashboard/pair-outreach/forbidden-26-9").status_code == 403
    assert rec.get("/dashboard/pair-outreach", params={"jd_id": "forbidden-26-9"}).status_code == 403
    assert rec.get("/dashboard/pair-outreach").status_code == 200
    assert rec.get("/dashboard/pair-metrics").status_code == 200


def test_job_id_from_engage_payload():
    assert eng._job_id_from_engage_payload('{"jd": {"job_id": "26-1"}}') == "26-1"
    assert eng._job_id_from_engage_payload('{"jd": {"jobdiva_id": " 31920032 "}}') == "31920032"
    assert eng._job_id_from_engage_payload('{"jd": {}}') is None
    assert eng._job_id_from_engage_payload('{"jd": "not-a-dict"}') is None
    assert eng._job_id_from_engage_payload("not json") is None
    assert eng._job_id_from_engage_payload("") is None
