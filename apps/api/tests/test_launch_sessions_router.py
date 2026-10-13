"""routers/launch_sessions: the auth inventory (read from source), and the
endpoints' contract against an in-memory store (MemoryStore + FakeWorld).

Contract pinned here: create 201 / replay 200 / key reuse with another body
422 / another active session 409 {active_session_id} / outreach stopped 409 /
disabled 503; collecting → upload → start; cancel / confirm / start are single
conditional transitions (409 with the current status otherwise); children
(needs-contact, retry-failed) and "launch remaining" (from_session_id); the
table view never carries résumé text; the failed view is the browser's
LaunchFailedCandidate shape.
"""

from __future__ import annotations

import ast
import datetime
import uuid
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import services.launch_sessions.runner as runner_mod
from core import tasks as core_tasks
from core.auth import UserIdentity, get_current_user
from routers import launch_sessions as router_mod
from services.launch_sessions import control
from services.launch_sessions.effects import Effects
from services.launch_sessions.store import Fence
from tests.launch_sessions_fakes import FakeWorld, MemoryStore, add_job, cand
from tests.launch_sessions_harness import TEST_ENV, Runner

ROUTER_PATH = Path(__file__).resolve().parents[1] / "routers" / "launch_sessions.py"

RECRUITER = UserIdentity(email="r@x.com", role="recruiter")
OTHER = UserIdentity(email="o@x.com", role="recruiter")
ADMIN = UserIdentity(email="admin@x.com", role="admin")


# ── auth inventory (AST) ─────────────────────────────────────────────────────

def _routes() -> List[Dict[str, Any]]:
    src = ROUTER_PATH.read_text(encoding="utf-8")
    found = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for d in node.decorator_list:
            if (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                    and isinstance(d.func.value, ast.Name) and d.func.value.id == "router"):
                body = ast.get_source_segment(src, node) or ""
                found.append({
                    "method": d.func.attr.upper(), "path": d.args[0].value, "func": node.name,
                    "authenticated": "get_current_user" in body[: body.find("):")],
                    "access_checked": "await _job_access(" in body or "await _session_access(" in body,
                })
    return found


def test_route_inventory():
    assert {(r["method"], r["path"]) for r in _routes()} == {
        ("GET", "/launch-sessions/config"),
        ("GET", "/launch-sessions/mine"),
        ("POST", "/jobs/{job_id}/launch-sessions"),
        ("GET", "/jobs/{job_id}/launch-sessions"),
        ("GET", "/launch-sessions/{session_id}"),
        ("GET", "/launch-sessions/{session_id}/events"),
        ("GET", "/launch-sessions/{session_id}/candidates"),
        ("POST", "/launch-sessions/{session_id}/candidates"),
        ("POST", "/launch-sessions/{session_id}/start"),
        ("POST", "/launch-sessions/{session_id}/confirm"),
        ("POST", "/launch-sessions/{session_id}/needs-contact"),
        ("POST", "/launch-sessions/{session_id}/retry-failed"),
        ("POST", "/launch-sessions/{session_id}/cancel"),
    }


def test_every_route_requires_a_user_and_every_scoped_route_checks_access():
    for r in _routes():
        assert r["authenticated"], f"{r['method']} {r['path']} must take Depends(get_current_user)"
        if r["path"] not in ("/launch-sessions/config", "/launch-sessions/mine"):
            assert r["access_checked"], f"{r['method']} {r['path']} must check job / session access"


def test_router_is_mounted_under_api_v1_in_main():
    main_src = (ROUTER_PATH.parents[1] / "main.py").read_text(encoding="utf-8")
    assert 'launch_sessions_router = _safe_import("launch_sessions")' in main_src
    assert '_mount(launch_sessions_router, "launch_sessions")' in main_src
    assert router_mod.router.prefix == "/api/v1"
    assert '"launch_sessions_schema_init"' in main_src
    assert 'background_transaction("scheduler/launch_session_tick")' in main_src
    assert 'background_transaction("scheduler/launch_session_housekeeping")' in main_src


# ── endpoint contract ────────────────────────────────────────────────────────

@pytest.fixture()
def env(monkeypatch):
    for k, v in TEST_ENV.items():
        monkeypatch.setenv(k, v)
    core_tasks.stopping.clear()
    store = MemoryStore()
    store.t = datetime.datetime.now(datetime.timezone.utc)  # views compare leases to the real clock
    world = FakeWorld(store)
    runner = Runner(store, world)
    monkeypatch.setattr(runner_mod, "_runner", runner)
    claims: List[str] = []
    monkeypatch.setattr(control, "_schedule_claim", lambda sid: claims.append(sid))
    jobs = {"1001": add_job(store, "1001", "26-001")}

    def draft(job_ref):
        for j in store.jobs.values():
            if job_ref in (j["job_id"], j["jobdiva_id"]):
                return {"status": "success", "data": {"recruiter_emails": j["recruiter_emails"]}}
        return {"status": "error", "message": "No data found"}

    monkeypatch.setattr(router_mod, "_get_job_draft_sync", draft)
    yield {"store": store, "world": world, "claims": claims, "jobs": jobs, "runner": runner}
    runner_mod._runner = None


def client(user=RECRUITER):
    app = FastAPI()
    app.include_router(router_mod.router)
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def _full_body(key="k1", **extra):
    body = {"idempotency_key": key, "mode": "full_search",
            "search_attempts": [{"label": "initial", "payload": {"boolean_string": "A0"}}],
            "launch_count": 25, "options": {"notify_recruiters": True, "is_initial_launch": True},
            "app_base_url": "https://pairqa.test"}
    body.update(extra)
    return body


def test_config(env, monkeypatch):
    out = client().get("/api/v1/launch-sessions/config").json()
    assert out["enabled"] is True and out["poll_ms"] == 3000 and out["hidden_poll_ms"] == 15000
    monkeypatch.setenv("PAIR_SERVER_LAUNCH_ENABLED", "false")
    assert client().get("/api/v1/launch-sessions/config").json()["enabled"] is False


def test_create_replay_conflict_and_mismatch(env):
    c = client()
    r = c.post("/api/v1/jobs/26-001/launch-sessions", json=_full_body())
    assert r.status_code == 201
    s = r.json()["session"]
    assert s["status"] == "queued" and s["job_id"] == "1001" and s["jobdiva_ref"] == "26-001"
    assert s["require_review"] is False and env["claims"] == [s["id"]]
    assert env["world"].clicked and env["world"].clicked[0][0] == "1001"
    again = c.post("/api/v1/jobs/1001/launch-sessions", json=_full_body())
    assert again.status_code == 200 and again.json()["session"]["id"] == s["id"]
    other_body = c.post("/api/v1/jobs/1001/launch-sessions", json=_full_body(launch_count=3))
    assert other_body.status_code == 422 and other_body.json()["detail"]["code"] == "idempotency_mismatch"
    busy = c.post("/api/v1/jobs/1001/launch-sessions", json=_full_body(key="k2"))
    assert busy.status_code == 409
    assert busy.json()["detail"] == {"code": "active_session", "active_session_id": s["id"], "status": "queued"}
    # Someone else replaying the same key is not handed the session.
    stolen = client(ADMIN).post("/api/v1/jobs/1001/launch-sessions", json=_full_body())
    assert stolen.status_code == 409 and stolen.json()["detail"]["code"] == "idempotency_key_in_use"


def test_create_gates(env, monkeypatch):
    c = client()
    assert c.post("/api/v1/jobs/nope/launch-sessions", json=_full_body()).status_code == 404
    env["world"].build_search_request = Effects().build_search_request
    bad = c.post("/api/v1/jobs/1001/launch-sessions", json=_full_body(
        search_attempts=[{"label": "x", "payload": {"max_experience_years": 99}}]))
    assert bad.status_code == 422
    assert c.post("/api/v1/jobs/1001/launch-sessions", json=_full_body(search_attempts=[])).status_code == 422
    env["world"].email_gate_error = HTTPException(status_code=400, detail="no recruiter email")
    assert c.post("/api/v1/jobs/1001/launch-sessions", json=_full_body()).status_code == 400
    env["world"].email_gate_error = None
    env["store"].jobs["1001"]["outreach_stopped_at"] = "2026-10-01"
    r = c.post("/api/v1/jobs/1001/launch-sessions", json=_full_body())
    assert r.status_code == 409 and r.json()["detail"]["code"] == "outreach_stopped"
    env["store"].jobs["1001"]["outreach_stopped_at"] = None
    monkeypatch.setenv("PAIR_SERVER_LAUNCH_ENABLED", "false")
    assert c.post("/api/v1/jobs/1001/launch-sessions", json=_full_body()).status_code == 503


def test_require_review_defaults_to_qa(env, monkeypatch):
    monkeypatch.setenv("PAIR_LAUNCH_REQUIRE_REVIEW_DEFAULT", "true")
    s = client().post("/api/v1/jobs/1001/launch-sessions", json=_full_body()).json()["session"]
    assert s["require_review"] is True
    client().post(f"/api/v1/launch-sessions/{s['id']}/cancel")
    s2 = client().post("/api/v1/jobs/1001/launch-sessions", json=_full_body(key="k9", require_review=False))
    assert s2.json()["session"]["require_review"] is False


def test_job_access_is_enforced(env):
    env["store"].jobs["1001"]["recruiter_emails"] = '["someone@x.com"]'
    assert client(OTHER).post("/api/v1/jobs/1001/launch-sessions", json=_full_body()).status_code == 403
    assert client(OTHER).get("/api/v1/jobs/1001/launch-sessions").status_code == 403
    s = client(ADMIN).post("/api/v1/jobs/1001/launch-sessions", json=_full_body()).json()["session"]
    assert client(OTHER).get(f"/api/v1/launch-sessions/{s['id']}").status_code == 403
    assert client(ADMIN).get(f"/api/v1/launch-sessions/{s['id']}").status_code == 200
    assert client(ADMIN).get(f"/api/v1/launch-sessions/{uuid.uuid4()}").status_code == 404
    assert client(ADMIN).get("/api/v1/launch-sessions/not-a-uuid").status_code == 404


def test_requester_keeps_access_to_their_own_session(env):
    s = client().post("/api/v1/jobs/1001/launch-sessions", json=_full_body()).json()["session"]
    env["store"].jobs["1001"]["recruiter_emails"] = '["someone@x.com"]'  # reassigned since
    assert client().get(f"/api/v1/launch-sessions/{s['id']}").status_code == 200


def test_provided_mode_upload_start_and_conflicts(env):
    c = client()
    rows = [cand(f"p{i}", 80) for i in range(3)]
    r = c.post("/api/v1/jobs/1001/launch-sessions", json={
        "idempotency_key": "p", "mode": "provided", "candidates": rows[:1]})
    s = r.json()["session"]
    assert r.status_code == 201 and s["status"] == "collecting" and env["claims"] == []
    sid = s["id"]
    up = c.post(f"/api/v1/launch-sessions/{sid}/candidates", json={"candidates": rows[1:]})
    assert up.status_code == 200 and up.json() == {"added": 2, "total": 3}
    too_many = c.post(f"/api/v1/launch-sessions/{sid}/candidates", json={"candidates": [cand(f"x{i}", 1) for i in range(51)]})
    assert too_many.status_code == 422
    no_id = c.post(f"/api/v1/launch-sessions/{sid}/candidates", json={"candidates": [{"name": "?"}]})
    assert no_id.status_code == 422
    started = c.post(f"/api/v1/launch-sessions/{sid}/start")
    assert started.status_code == 200 and started.json()["session"]["status"] == "queued"
    assert env["claims"] == [sid]
    assert c.post(f"/api/v1/launch-sessions/{sid}/start").status_code == 409
    late = c.post(f"/api/v1/launch-sessions/{sid}/candidates", json={"candidates": [cand("p9", 80)]})
    assert late.status_code == 409 and late.json()["detail"]["status"] == "queued"


def test_candidates_views_paging_and_events(env):
    store = env["store"]
    s = client().post("/api/v1/jobs/1001/launch-sessions", json={
        "idempotency_key": "v", "mode": "provided", "start": True,
        "candidates": [dict(cand(f"v{i}", 80), resume_text="secret resume", linkedin_profile={"x": 1})
                       for i in range(5)]}).json()["session"]
    sid = s["id"]
    store.claim("T", 1, global_cap=3, lease_s=180)
    store.set_states(Fence(sid, "T", 1), [{"candidate_id": "v1", "state": "failed", "reason": "pairbot_rejected",
                                           "failure_stage": "engage", "batch_index": 3, "error": "rejected"}])
    c = client()
    page = c.get(f"/api/v1/launch-sessions/{sid}/candidates?view=table").json()
    assert page["total"] == 5 and all("resume_text" not in r["row"] and "linkedin_profile" not in r["row"]
                                      for r in page["rows"])
    assert page["rows"][0]["current_email"] == "v0@mail.test"
    full = c.get(f"/api/v1/launch-sessions/{sid}/candidates?view=full&limit=1").json()
    assert full["rows"][0]["row"]["resume_text"] == "secret resume" and full["has_more"] is True
    failed = c.get(f"/api/v1/launch-sessions/{sid}/candidates?view=failed").json()["rows"]
    assert failed == [{
        "candidate_id": "v1", "name": "Person v1", "email": "v1@mail.test", "phone": failed[0]["phone"],
        "source": "LinkedIn-Exa", "headline": "Engineer", "location": "Austin, TX", "experience_years": 0,
        "match_score": 80, "skills": "", "matched_skills": "", "resume_id": "", "profile_url": None,
        "batch_index": 3, "failure_stage": "engage", "error_message": "rejected",
    }]
    seen, cursor = [], {"after_rev": 0, "after_id": ""}
    while True:
        out = c.get(f"/api/v1/launch-sessions/{sid}/candidates", params={**cursor, "limit": 2}).json()
        seen += [r["candidate_id"] for r in out["rows"]]
        cursor = out["next"]
        if not out["has_more"]:
            break
    assert sorted(set(seen)) == [f"v{i}" for i in range(5)]
    assert c.get(f"/api/v1/launch-sessions/{sid}/candidates?state=bogus").status_code == 422
    ev = c.get(f"/api/v1/launch-sessions/{sid}/events?after_seq=0").json()
    assert [e["payload"]["type"] for e in ev["events"]][0] == "created" and ev["last_seq"] >= 1
    view = c.get(f"/api/v1/launch-sessions/{sid}").json()["session"]
    assert view["events_tail"] and view["phase"] == "running" and view["input"]["search_attempts"] == 0


def test_cancel_and_confirm_are_conditional(env):
    store = env["store"]
    c = client()
    s = c.post("/api/v1/jobs/1001/launch-sessions", json=_full_body()).json()["session"]
    sid = s["id"]
    assert c.post(f"/api/v1/launch-sessions/{sid}/confirm", json={"contacts": {}}).status_code == 409
    r = c.post(f"/api/v1/launch-sessions/{sid}/cancel", json={"reason": "changed my mind"})
    assert r.status_code == 200 and r.json()["session"]["status"] == "cancelled"
    assert r.json()["session"]["error"] == "changed my mind"
    assert c.post(f"/api/v1/launch-sessions/{sid}/cancel").status_code == 409

    s = c.post("/api/v1/jobs/1001/launch-sessions", json=_full_body(key="k2")).json()["session"]
    sid = s["id"]
    store.cands[sid] = {}
    store._insert_rows(sid, [{"candidate_id": "a", "row": cand("a"), "state": "selected"},
                             {"candidate_id": "b", "row": cand("b"), "state": "selected"}], 1)
    store.set_column(sid, "status", "awaiting_input")
    store.set_column(sid, "stage", "review")
    r = c.post(f"/api/v1/launch-sessions/{sid}/confirm",
               json={"contacts": {"a": {"phone": " +15550001 ", "email": ""}}})
    assert r.status_code == 200 and r.json()["session"]["status"] == "queued"
    assert store.cands[sid]["a"]["contact"]["final"] == {"phone": "+15550001", "email": None}
    assert store.cands[sid]["b"]["state"] == "skipped"
    assert env["claims"][-1] == sid
    # A running session's cancel is a request the runner finalizes.
    store.set_column(sid, "status", "running")
    r = c.post(f"/api/v1/launch-sessions/{sid}/cancel")
    assert r.status_code == 200 and r.json()["session"]["cancel_requested"] is True


def _finished_parent(env, rows, *, options=None):
    store = env["store"]
    s = client().post("/api/v1/jobs/1001/launch-sessions", json=_full_body(
        options=options or {"notify_recruiters": True, "is_initial_launch": True, "batch_size": 50})).json()["session"]
    sid = s["id"]
    store._insert_rows(sid, rows, 1)
    store.set_column(sid, "status", "completed")
    return sid


def test_needs_contact_child(env):
    store = env["store"]
    sid = _finished_parent(env, [
        {"candidate_id": "n1", "row": cand("n1", email="", phone=""), "state": "needs_contact",
         "contact": {"done": True, "override": {"email": "lookup@x.test"}, "needs": {"needsPhone": True}}},
        {"candidate_id": "n2", "row": cand("n2", email="", phone=""), "state": "needs_contact"},
        {"candidate_id": "n3", "row": cand("n3"), "state": "launched"},
    ])
    c = client()
    r = c.post(f"/api/v1/launch-sessions/{sid}/needs-contact",
               json={"contacts": {"n1": {"phone": "+15125550199"}}, "skip_ids": ["n2", "n3"]})
    assert r.status_code == 201
    child = r.json()["session"]
    assert child["trigger"] == "needs_contact" and child["mode"] == "provided" and child["status"] == "queued"
    assert child["parent_session_id"] == sid and r.json()["skipped"] == 1
    assert child["input"]["options"] == {"notify_recruiters": True, "is_initial_launch": False,
                                         "send_job_posting_email": False, "batch_size": 50}
    contact = store.cands[child["id"]]["n1"]["contact"]
    assert contact["provided"] == {"phone": "+15125550199"} and contact["override"] == {"email": "lookup@x.test"}
    assert "needs" not in contact
    assert store.cands[sid]["n1"]["state"] == "handed_off" and store.cands[sid]["n2"]["state"] == "skipped"
    assert store.cands[sid]["n3"]["state"] == "launched"
    replay = c.post(f"/api/v1/launch-sessions/{sid}/needs-contact",
                    json={"contacts": {"n1": {"phone": "+15125550199"}}, "skip_ids": ["n2", "n3"]})
    assert replay.status_code == 200 and replay.json()["session"]["id"] == child["id"]  # a retried POST
    other = c.post(f"/api/v1/launch-sessions/{sid}/needs-contact", json={"contacts": {"n1": {"phone": "+15125550100"},
                                                                                       "zz": {}}})
    assert other.status_code == 422  # n1 is no longer waiting (handed off)
    assert c.post(f"/api/v1/launch-sessions/{child['id']}/needs-contact", json={}).status_code == 409


def test_needs_contact_skip_only_and_active_parent(env):
    store = env["store"]
    c = client()
    s = c.post("/api/v1/jobs/1001/launch-sessions", json=_full_body()).json()["session"]
    assert c.post(f"/api/v1/launch-sessions/{s['id']}/needs-contact", json={"skip_ids": ["x"]}).json()[
        "detail"]["code"] == "parent_active"
    store.set_column(s["id"], "status", "completed")
    store._insert_rows(s["id"], [{"candidate_id": "x", "row": cand("x"), "state": "needs_contact"}], 2)
    r = c.post(f"/api/v1/launch-sessions/{s['id']}/needs-contact", json={"skip_ids": ["x"]})
    assert r.status_code == 200 and r.json() == {"session": None, "skipped": 1}
    assert store.cands[s["id"]]["x"]["reason"] == "skipped_by_recruiter"


def test_retry_failed_child(env):
    store = env["store"]
    sid = _finished_parent(env, [
        {"candidate_id": "f1", "row": cand("f1"), "state": "failed", "contact": {"final": {"phone": "+1555", "email": None}}},
        {"candidate_id": "ok", "row": cand("ok"), "state": "launched"},
    ])
    c = client()
    r = c.post(f"/api/v1/launch-sessions/{sid}/retry-failed")
    assert r.status_code == 201
    child = r.json()["session"]
    assert child["trigger"] == "retry"
    assert child["input"]["options"]["notify_recruiters"] is False
    assert child["input"]["options"]["is_initial_launch"] is False
    assert child["input"]["skip_enrichment"] is True and child["require_review"] is False
    assert store.cands[child["id"]]["f1"]["contact"]["final"] == {"phone": "+1555", "email": None}
    assert list(store.cands[child["id"]]) == ["f1"]
    store.set_column(child["id"], "status", "completed")
    again = c.post(f"/api/v1/launch-sessions/{sid}/retry-failed")
    assert again.status_code == 200 and again.json()["session"]["id"] == child["id"]
    assert c.post(f"/api/v1/launch-sessions/{child['id']}/retry-failed").status_code == 422


def test_launch_remaining_copies_rows_from_a_server_session(env):
    store = env["store"]
    src = _finished_parent(env, [
        {"candidate_id": "r1", "row": cand("r1"), "state": "skipped", "contact": {"done": True, "outcome": "x"}},
        {"candidate_id": "r2", "row": cand("r2"), "state": "pooled"},
    ])
    r = client().post("/api/v1/jobs/1001/launch-sessions", json={
        "idempotency_key": "rem", "mode": "provided", "trigger": "launch_remaining",
        "from_session_id": src, "candidate_ids": ["r1", "r2", "zz"]})
    assert r.status_code == 201
    s = r.json()["session"]
    assert s["status"] == "queued" and s["mode"] == "provided"
    rows = store.cands[s["id"]]
    assert sorted(rows) == ["r1", "r2"] and all(c["state"] == "pooled" for c in rows.values())
    assert rows["r1"]["contact"] == {"done": True, "outcome": "x"}
    other = add_job(store, "1002", "26-002")
    bad = client().post("/api/v1/jobs/1002/launch-sessions", json={
        "idempotency_key": "rem2", "mode": "provided", "from_session_id": src, "candidate_ids": ["r1"]})
    assert bad.status_code == 404
    del other


def test_list_job_sessions_for_reattach(env):
    c = client()
    s = c.post("/api/v1/jobs/1001/launch-sessions", json=_full_body()).json()["session"]
    out = c.get("/api/v1/jobs/26-001/launch-sessions?active=true").json()["sessions"]
    assert [x["id"] for x in out] == [s["id"]]
    c.post(f"/api/v1/launch-sessions/{s['id']}/cancel")
    assert c.get("/api/v1/jobs/1001/launch-sessions?active=true").json()["sessions"] == []


def test_my_sessions_lists_only_the_callers_own_launches(env):
    env["store"].jobs["1001"]["title"] = "Java Developer"
    s = client().post("/api/v1/jobs/1001/launch-sessions", json=_full_body()).json()["session"]
    mine = client().get("/api/v1/launch-sessions/mine").json()["sessions"]
    assert [(x["id"], x["job_title"]) for x in mine] == [(s["id"], "Java Developer")]
    assert client(OTHER).get("/api/v1/launch-sessions/mine").json()["sessions"] == []
    assert client(UserIdentity(email="", role="recruiter")).get("/api/v1/launch-sessions/mine").json()["sessions"] == []
    client().post(f"/api/v1/launch-sessions/{s['id']}/cancel")
    # Finished recently: still listed so the tracker can show the result.
    assert [x["id"] for x in client().get("/api/v1/launch-sessions/mine").json()["sessions"]] == [s["id"]]


def test_polls_drop_their_transaction(env, monkeypatch):
    import core.newrelic as nr

    ignored: List[int] = []
    monkeypatch.setattr(nr, "ignore_transaction", lambda: ignored.append(1))
    c = client()
    s = c.post("/api/v1/jobs/1001/launch-sessions", json=_full_body()).json()["session"]
    assert ignored == []  # a create is a real transaction
    c.get(f"/api/v1/launch-sessions/{s['id']}")
    c.get(f"/api/v1/launch-sessions/{s['id']}/events")
    c.get(f"/api/v1/launch-sessions/{s['id']}/candidates")
    assert len(ignored) == 3
    c.get(f"/api/v1/launch-sessions/{uuid.uuid4()}")  # a 404 is kept
    assert len(ignored) == 3


# ── review fixes (parity) ────────────────────────────────────────────────────

def test_search_payloads_are_bound_to_the_sessions_job(env):
    """review-parity #1: the search stream does no access check of its own."""
    store = env["store"]
    add_job(store, "1002", "26-999")
    c = client()
    bad = c.post("/api/v1/jobs/1001/launch-sessions", json=_full_body(
        search_attempts=[{"label": "i", "payload": {"boolean_string": "A0", "job_id": "26-999"}}]))
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "job_mismatch"
    ok = c.post("/api/v1/jobs/1001/launch-sessions", json=_full_body(
        search_attempts=[{"label": "i", "payload": {"boolean_string": "A0", "job_id": "26-001"}},
                         {"label": "r", "payload": {"boolean_string": "A1"}}]))
    assert ok.status_code == 201
    attempts = store.sessions[ok.json()["session"]["id"]]["input"]["search_attempts"]
    assert [a["payload"]["job_id"] for a in attempts] == ["26-001", "1001"]
    plan = ok.json()["session"]["sourcing"]["plan"]
    assert plan == [{"label": "i", "boolean_string": "A0"}, {"label": "r", "boolean_string": "A1"}]


def test_a_parent_whose_launch_is_over_takes_children_and_frees_the_job(env):
    """review-parity #4: once S7 is done (only JobDiva provisioning left) the
    needs-contact child and a new Source & Launch are allowed."""
    store = env["store"]
    sid = _finished_parent(env, [
        {"candidate_id": "n1", "row": cand("n1", email="", phone=""), "state": "needs_contact"},
        {"candidate_id": "l1", "row": cand("l1"), "state": "launched"},
    ])
    store.set_column(sid, "status", "running")
    store.set_column(sid, "stage", "launching")
    c = client()
    assert c.post(f"/api/v1/launch-sessions/{sid}/needs-contact",
                  json={"contacts": {"n1": {"phone": "+15125550199"}}}).status_code == 409
    v = c.get(f"/api/v1/launch-sessions/{sid}").json()["session"]
    assert v["launch_complete"] is False and v["terminal"] is False
    store.set_column(sid, "stage", "provisioning")
    v = c.get(f"/api/v1/launch-sessions/{sid}").json()["session"]
    assert v["launch_complete"] is True and v["terminal"] is False
    r = c.post(f"/api/v1/launch-sessions/{sid}/needs-contact", json={"contacts": {"n1": {"phone": "+15125550199"}}})
    assert r.status_code == 201 and r.json()["session"]["parent_session_id"] == sid
    store.set_column(r.json()["session"]["id"], "status", "completed")
    assert c.post("/api/v1/jobs/1001/launch-sessions", json=_full_body(key="again")).status_code == 201


def test_a_child_of_a_parent_that_launched_nobody_answers_the_parents_click(env):
    """review-parity #8: the child carries the parent's click (and the modal
    time as human wait) — the job's click → launch is measured from the
    real click; a parent that launched someone keeps that role."""
    store = env["store"]
    sid = _finished_parent(env, [
        {"candidate_id": "n1", "row": cand("n1", email="", phone=""), "state": "needs_contact"},
        {"candidate_id": "n2", "row": cand("n2", email="", phone=""), "state": "needs_contact"},
    ])
    parent = store.sessions[sid]
    clicked = parent["clicked_at"]
    store.set_column(sid, "completed_at", clicked + datetime.timedelta(seconds=-5))
    r = client().post(f"/api/v1/launch-sessions/{sid}/needs-contact",
                      json={"contacts": {"n1": {"phone": "+15125550199"}}})
    child = store.sessions[r.json()["session"]["id"]]
    assert child["clicked_at"] == clicked and child["human_wait_ms"] >= 5000

    store.set_column(sid, "first_interview_created_at", clicked)  # it launched someone
    r = client().post(f"/api/v1/launch-sessions/{sid}/needs-contact",
                      json={"contacts": {"n2": {"phone": "+15125550198"}}})
    assert r.status_code == 409  # the first child is still active
    store.set_column(child["id"], "status", "completed")
    r = client().post(f"/api/v1/launch-sessions/{sid}/needs-contact",
                      json={"contacts": {"n2": {"phone": "+15125550198"}}})
    child2 = store.sessions[r.json()["session"]["id"]]
    assert child2["clicked_at"] == store.t and child2["human_wait_ms"] == 0


def test_a_waiting_session_shows_its_queue_position(env):
    store = env["store"]
    c = client()
    s = c.post("/api/v1/jobs/1001/launch-sessions", json=_full_body()).json()["session"]
    assert s["queue_position"] == 0 and s["launch_complete"] is False
    add_job(store, "1002", "26-002")
    s2 = c.post("/api/v1/jobs/1002/launch-sessions", json=_full_body(key="k2")).json()["session"]
    assert s2["queue_position"] == 1
    store.claim("W", 1, global_cap=3, lease_s=180, session_id=s["id"])
    assert c.get(f"/api/v1/launch-sessions/{s['id']}").json()["session"]["queue_position"] is None
    assert c.get(f"/api/v1/launch-sessions/{s2['id']}").json()["session"]["queue_position"] == 0


# ── final-review fixes ───────────────────────────────────────────────────────

def test_config_is_disabled_and_create_answers_503_while_the_runner_is_off(env, monkeypatch):
    """rollout #4: /config's enabled needs the runner too; no session is
    created that no worker would claim."""
    c = client()
    assert c.get("/api/v1/launch-sessions/config").json()["enabled"] is True
    monkeypatch.setenv("PAIR_LAUNCH_RUNNER_ENABLED", "false")
    assert c.get("/api/v1/launch-sessions/config").json()["enabled"] is False
    r = c.post("/api/v1/jobs/1001/launch-sessions", json=_full_body())
    assert r.status_code == 503 and r.json()["detail"]["code"] == "disabled"
    assert env["store"].sessions == {} and env["claims"] == []
    monkeypatch.setenv("PAIR_LAUNCH_RUNNER_ENABLED", "true")
    assert c.post("/api/v1/jobs/1001/launch-sessions", json=_full_body()).status_code == 201


def test_a_create_retried_after_a_lost_answer_replays_even_with_the_runner_off(env, monkeypatch):
    """The first create succeeded but its answer was lost; the runner is then
    switched off. The retry (same idempotency key) must get its session back,
    not a 503 that sends the browser to the legacy flow next to it."""
    c = client()
    body = _full_body()
    first = c.post("/api/v1/jobs/1001/launch-sessions", json=body)
    assert first.status_code == 201
    monkeypatch.setenv("PAIR_LAUNCH_RUNNER_ENABLED", "false")
    retry = c.post("/api/v1/jobs/1001/launch-sessions", json=body)
    assert retry.status_code == 200 and retry.json()["session"]["id"] == first.json()["session"]["id"]
    # A NEW click (new key) is still refused while the runner is off.
    fresh = dict(body, idempotency_key="another-click")
    r = c.post("/api/v1/jobs/1001/launch-sessions", json=fresh)
    assert r.status_code == 503 and r.json()["detail"]["code"] == "disabled"


def test_queue_expiry_default_follows_the_runner(monkeypatch):
    from services.launch_sessions import config as cfg_mod
    monkeypatch.delenv("PAIR_LAUNCH_QUEUED_MAX_S", raising=False)
    monkeypatch.setenv("PAIR_LAUNCH_RUNNER_ENABLED", "true")
    assert cfg_mod.queued_max_s() == 21600
    monkeypatch.setenv("PAIR_LAUNCH_RUNNER_ENABLED", "false")
    assert cfg_mod.queued_max_s() == 1800
    monkeypatch.setenv("PAIR_LAUNCH_QUEUED_MAX_S", "0")
    assert cfg_mod.queued_max_s() == 0


def test_children_need_the_runner_but_a_skip_only_answer_or_a_replay_does_not(env, monkeypatch):
    store = env["store"]
    sid = _finished_parent(env, [
        {"candidate_id": "n1", "row": cand("n1", email="", phone=""), "state": "needs_contact"},
        {"candidate_id": "n2", "row": cand("n2", email="", phone=""), "state": "needs_contact"},
        {"candidate_id": "f1", "row": cand("f1"), "state": "failed"},
    ])
    c = client()
    body = {"contacts": {"n1": {"phone": "+15125550199"}}}
    first = c.post(f"/api/v1/launch-sessions/{sid}/needs-contact", json=body)
    assert first.status_code == 201
    monkeypatch.setenv("PAIR_LAUNCH_RUNNER_ENABLED", "false")
    replay = c.post(f"/api/v1/launch-sessions/{sid}/needs-contact", json=body)
    assert replay.status_code == 200 and replay.json()["session"]["id"] == first.json()["session"]["id"]
    store.set_column(first.json()["session"]["id"], "status", "completed")
    off = c.post(f"/api/v1/launch-sessions/{sid}/needs-contact", json={"contacts": {"n2": {"phone": "+15125550198"}}})
    assert off.status_code == 503 and off.json()["detail"]["code"] == "disabled"
    assert store.cands[sid]["n2"]["state"] == "needs_contact"  # still parked, offered again later
    skip = c.post(f"/api/v1/launch-sessions/{sid}/needs-contact", json={"skip_ids": ["n2"]})
    assert skip.status_code == 200 and skip.json() == {"session": None, "skipped": 1}
    retry = c.post(f"/api/v1/launch-sessions/{sid}/retry-failed")
    assert retry.status_code == 503 and retry.json()["detail"]["code"] == "disabled"
    assert store.cands[sid]["f1"]["state"] == "failed"


def test_server_launch_off_still_serves_existing_sessions(env, monkeypatch):
    """rollout #3 (server side): PAIR_SERVER_LAUNCH_ENABLED gates creation
    only — reattach lists, polls and children of an existing session work."""
    c = client()
    s = c.post("/api/v1/jobs/1001/launch-sessions", json=_full_body()).json()["session"]
    monkeypatch.setenv("PAIR_SERVER_LAUNCH_ENABLED", "false")
    assert c.get("/api/v1/launch-sessions/config").json()["enabled"] is False
    assert [x["id"] for x in c.get("/api/v1/jobs/1001/launch-sessions?active=true").json()["sessions"]] == [s["id"]]
    assert c.get(f"/api/v1/launch-sessions/{s['id']}").status_code == 200
    env["store"].set_column(s["id"], "status", "completed")
    env["store"]._insert_rows(s["id"], [{"candidate_id": "n1", "row": cand("n1", email="", phone=""),
                                         "state": "needs_contact"}], 5)
    r = c.post(f"/api/v1/launch-sessions/{s['id']}/needs-contact", json={"contacts": {"n1": {"phone": "+15125550199"}}})
    assert r.status_code == 201


def test_a_malformed_from_session_id_is_a_404_never_a_500(env, monkeypatch):
    """security minor: Postgres raises on `WHERE id = 'abc'::uuid` — the id
    is validated before the lookup."""
    store = env["store"]
    real_get = store.get_session

    def pg_like_get_session(session_id):
        uuid.UUID(str(session_id))  # psycopg2 DataError: invalid input syntax for type uuid
        return real_get(session_id)

    monkeypatch.setattr(store, "get_session", pg_like_get_session)
    c = client()
    for bad in ("abc", "12345678-1234-1234-1234-12345678901z", " "):
        r = c.post("/api/v1/jobs/1001/launch-sessions", json={
            "idempotency_key": f"rem-{bad}", "mode": "provided", "trigger": "launch_remaining",
            "from_session_id": bad, "candidate_ids": ["r1"]})
        assert r.status_code == 404, (bad, r.status_code)
        assert r.json()["detail"] == "from_session_id is not a session of this job"
    unknown = c.post("/api/v1/jobs/1001/launch-sessions", json={
        "idempotency_key": "rem-unknown", "mode": "provided", "from_session_id": str(uuid.uuid4()),
        "candidate_ids": ["r1"]})
    assert unknown.status_code == 404
    # A well-formed id of this job (any spelling) still copies its rows.
    src = _finished_parent(env, [{"candidate_id": "r1", "row": cand("r1"), "state": "pooled"}])
    ok = c.post("/api/v1/jobs/1001/launch-sessions", json={
        "idempotency_key": "rem-ok", "mode": "provided", "from_session_id": src.upper(), "candidate_ids": ["r1"]})
    assert ok.status_code == 201 and list(store.cands[ok.json()["session"]["id"]]) == ["r1"]


def test_candidates_ids_filter_reads_rows_by_id(env):
    """contract #1: the résumé viewer reads one row by id (no keyset position)."""
    s = client().post("/api/v1/jobs/1001/launch-sessions", json={
        "idempotency_key": "ids", "mode": "provided", "start": True,
        "candidates": [dict(cand(f"v{i}", 80), resume_text=f"resume {i}") for i in range(5)]}).json()["session"]
    sid = s["id"]
    c = client()
    one = c.get(f"/api/v1/launch-sessions/{sid}/candidates", params={"view": "full", "ids": "v3", "limit": 1}).json()
    assert [r["candidate_id"] for r in one["rows"]] == ["v3"] and one["rows"][0]["row"]["resume_text"] == "resume 3"
    assert one["total"] == 1 and one["has_more"] is False
    two = c.get(f"/api/v1/launch-sessions/{sid}/candidates", params={"ids": "v4, v1,v4,,zz"}).json()
    assert sorted(r["candidate_id"] for r in two["rows"]) == ["v1", "v4"] and two["total"] == 2
    assert all("resume_text" not in r["row"] for r in two["rows"])  # the table view stays light
    none = c.get(f"/api/v1/launch-sessions/{sid}/candidates", params={"ids": "zz"}).json()
    assert none["rows"] == [] and none["total"] == 0
    assert c.get(f"/api/v1/launch-sessions/{sid}/candidates", params={"ids": ""}).json()["total"] == 5
    assert c.get(f"/api/v1/launch-sessions/{sid}/candidates",
                 params={"ids": ",".join(f"x{i}" for i in range(50))}).status_code == 200
    too_many = c.get(f"/api/v1/launch-sessions/{sid}/candidates", params={"ids": ",".join(f"x{i}" for i in range(51))})
    assert too_many.status_code == 422
    # With the keyset cursor too.
    keyed = c.get(f"/api/v1/launch-sessions/{sid}/candidates",
                  params={"ids": "v2", "after_rev": 0, "after_id": ""}).json()
    assert [r["candidate_id"] for r in keyed["rows"]] == ["v2"] and keyed["has_more"] is False
