"""Recruiter activity: the action log, active time per surface, the admin read
(services/recruiter_activity.py, routers/recruiter_activity.py)."""
import datetime
import hashlib
import json
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.auth import UserIdentity, get_current_user
from routers import recruiter_activity as router_mod
from services import recruiter_activity as ra

UTC = datetime.timezone.utc


# ---------------------------------------------------------------------------
# Pure rules
# ---------------------------------------------------------------------------
def test_recruiter_hash_is_the_browsers_nr_user_id():
    # apps/web/lib/newrelic.ts nrUserIdFor: sha256(lower(trim(email))), first 6 bytes hex.
    assert ra.recruiter_hash("  Priya.Recruiter@Pyramidci.com ") == \
        hashlib.sha256(b"priya.recruiter@pyramidci.com").hexdigest()[:12]
    # Pinned vector, shared with apps/web/lib/activity.test.ts.
    assert ra.recruiter_hash("recruiter@example.com") == "7a3d6dab7de3"
    assert ra.recruiter_hash("") is None and ra.recruiter_hash(None) is None


def test_attrs_never_carry_pii_or_free_text():
    clean = ra._clean_attrs({
        "seconds": 42, "ok": True, "report": "launch_report", "candidate_name": "Jane",
        "email": "a@b.c", "note": "x" * 80, "who": "jane@x.com", "nested": {"a": 1},
    })
    assert clean == {"seconds": 42, "ok": True, "report": "launch_report"}
    assert ra._clean_attrs(None) is None and ra._clean_attrs({"email": "x"}) is None


def test_unknown_actions_and_identities_are_refused_without_a_db(monkeypatch):
    monkeypatch.setattr(ra, "_connect", lambda: pytest.fail("must not touch the DB"))
    assert ra.record_action("r@x.com", "made_up") is False
    assert ra.record_action("not-a-person", "search_run") is False
    assert ra.SERVER_ACTIONS.isdisjoint(ra.CLIENT_ACTIONS)


def test_active_ms_is_clamped_and_days_are_eastern():
    assert ra.clamp_active_ms(-5) == 0 and ra.clamp_active_ms("x") == 0
    assert ra.clamp_active_ms(10**9) == ra.MAX_ACTIVE_MS_PER_REPORT
    # 02:00 UTC on Oct 13 is still Oct 12 in New York.
    assert ra.eastern_day(datetime.datetime(2026, 10, 13, 2, 0, tzinfo=UTC)) == datetime.date(2026, 10, 12)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
def _client(role="recruiter", email="r@x.com"):
    app = FastAPI()
    app.include_router(router_mod.router)
    app.dependency_overrides[get_current_user] = lambda: UserIdentity(email=email, role=role)
    return TestClient(app)


def test_time_endpoint_records_known_surfaces_and_degrades_to_error(monkeypatch):
    calls = []
    monkeypatch.setattr(router_mod, "_record_time_sync", lambda *a: calls.append(a) or True)
    c = _client()
    assert c.post("/api/v1/activity/time", json={"surface": "rankings", "job_ref": "26-1", "active_ms": 60000}).json() \
        == {"status": "success"}
    assert calls == [("r@x.com", "rankings", "26-1", 60000)]
    assert c.post("/api/v1/activity/time", json={"surface": "nope", "active_ms": 1}).json() == {"status": "ignored"}
    assert c.post("/api/v1/activity/time", json={"surface": "rankings", "active_ms": -1}).status_code == 422
    # A job ref that isn't one is dropped, the time still counts.
    c.post("/api/v1/activity/time", json={"surface": "rankings", "job_ref": "<script>", "active_ms": 5})
    assert calls[-1] == ("r@x.com", "rankings", "", 5)

    def boom(*a):
        raise RuntimeError("pool exhausted")
    monkeypatch.setattr(router_mod, "_record_time_sync", boom)
    assert c.post("/api/v1/activity/time", json={"surface": "rankings", "active_ms": 5}).json() == {"status": "error"}


def test_actions_endpoint_takes_client_actions_only(monkeypatch):
    seen = []
    monkeypatch.setattr(ra, "record_action", lambda email, action, **kw: seen.append((email, action, kw)) or True)
    out = _client().post("/api/v1/activity/actions", json={"actions": [
        {"action": "recording_played", "job_ref": "26-1", "candidate_id": "c1", "attrs": {"seconds": 30}},
        {"action": "feedback_submit"},       # server-side only: refused from the browser
        {"action": "invented"},
    ]}).json()
    assert out == {"status": "success", "recorded": 1}
    assert seen == [("r@x.com", "recording_played", {"job_key": "26-1", "candidate_id": "c1",
                                                      "attrs": {"seconds": 30}, "source": "client"})]
    too_many = {"actions": [{"action": "candidate_opened"}] * (router_mod.MAX_ACTIONS_PER_REPORT + 1)}
    assert _client().post("/api/v1/activity/actions", json=too_many).status_code == 422


def test_admin_read_is_scoped(monkeypatch):
    monkeypatch.setattr(router_mod, "_compute_sync", lambda *a: {"args": [str(x) for x in a]})
    assert _client().get("/api/v1/admin/recruiter-activity").status_code == 403
    res = _client(role="admin", email="a@x.com").get(
        "/api/v1/admin/recruiter-activity?start_date=2026-10-01&end_date=2026-10-07&team_id=t1")
    assert res.status_code == 200 and res.json()["data"]["args"] == ["t1", "None", "2026-10-01", "2026-10-07"]
    assert _client(role="admin", email="a@x.com").get(
        "/api/v1/admin/recruiter-activity?start_date=2026-10-08&end_date=2026-10-01").status_code == 400


def test_step_time_is_mirrored_as_wizard_surface_time(monkeypatch):
    from routers import job_step_time as jst

    mirrored = []

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *a):
            pass

        def fetchone(self):
            return ("31990001",)

    class _Conn:
        def cursor(self):
            return _Cur()

        def close(self):
            pass

        def rollback(self):
            pass

    monkeypatch.setattr(jst, "get_db_connection", lambda: _Conn())
    monkeypatch.setattr(jst, "record_step_time", lambda *a: None)
    monkeypatch.setattr(ra, "record_active_time", lambda conn, *a: mirrored.append(a) or True)
    assert jst._record_sync("26-1", 5, "r@x.com", 60000) == "31990001"
    assert mirrored == [("r@x.com", "wizard_step_5", "31990001", 60000)]


# ---------------------------------------------------------------------------
# Real Postgres
# ---------------------------------------------------------------------------
psycopg2 = pytest.importorskip("psycopg2")
_DSN = os.getenv("LAUNCH_REPORT_TEST_DSN", "dbname=postgres")


class _Shared:
    def __init__(self, conn):
        self._conn = conn

    def cursor(self, *a, **k):
        return self._conn.cursor(*a, **k)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture()
def pg(monkeypatch):
    try:
        conn = psycopg2.connect(_DSN, connect_timeout=3)
    except psycopg2.OperationalError as exc:
        pytest.skip(f"no Postgres reachable at {_DSN!r}: {exc}")
    try:
        with conn.cursor() as cur:
            cur.execute("SET TIME ZONE 'UTC'")
            for stmt in ra.SCHEMA_STATEMENTS:
                # Tables as TEMP (the indexes then land on them too).
                cur.execute(stmt.replace("CREATE TABLE IF NOT EXISTS", "CREATE TEMP TABLE IF NOT EXISTS"))
            cur.execute("CREATE TEMP TABLE sourced_candidates (id SERIAL, jobdiva_id TEXT, candidate_id TEXT, data JSONB)")
            cur.execute("""CREATE TEMP TABLE launch_sessions (id TEXT, requested_by TEXT, clicked_at TIMESTAMPTZ,
                           first_interview_created_at TIMESTAMPTZ, human_wait_ms BIGINT)""")
        monkeypatch.setattr(ra, "_connect", lambda: _Shared(conn))
        monkeypatch.setattr(ra, "_nr", lambda *a: None)
        yield conn
    finally:
        conn.rollback()
        conn.close()


def test_pg_activity_read_aggregates_time_actions_latency_and_waits(pg, monkeypatch):
    shared = _Shared(pg)
    today = ra.eastern_day()
    for surface, ms in (("rankings", 600_000), ("rankings", 300_000), ("assess_modal", 120_000)):
        assert ra.record_active_time(shared, "Alice@X.com", surface, "26-1", ms)
    assert ra.record_active_time(shared, "bob@x.com", "jobs_dashboard", "", 60_000)
    assert not ra.record_active_time(shared, "alice@x.com", "made_up_surface", "", 1)
    for action in ("search_run", "search_run", "search_run", "launch_clicked", "feedback_submit", "feedback_reject"):
        assert ra.record_action("alice@x.com", action, job_key="26-1")
    assert ra.record_action("alice@x.com", "recording_played", attrs={"seconds": 40}, source="client")
    now = datetime.datetime.now(UTC)
    with pg.cursor() as cur:
        for cid, passed_min_ago, decided_min_ago in (("c1", 120, 60), ("c2", 50, 40), ("c3", 30, 0)):
            cur.execute("INSERT INTO sourced_candidates (jobdiva_id, candidate_id, data) VALUES ('26-1', %s, %s)", (cid, json.dumps({
                "submitted_by": "alice@x.com", "engage_hard_filter_status": "pass",
                "first_completed_at": (now - datetime.timedelta(minutes=passed_min_ago)).isoformat(),
                "feedback_at": (now - datetime.timedelta(minutes=decided_min_ago)).isoformat(),
            })))
        # Not a pass, or not timed: ignored.
        cur.execute("INSERT INTO sourced_candidates (jobdiva_id, candidate_id, data) VALUES ('26-1', 'c4', %s)",
                    (json.dumps({"submitted_by": "alice@x.com", "engage_hard_filter_status": "fail",
                                 "first_completed_at": now.isoformat(), "feedback_at": now.isoformat()}),))
        cur.execute("INSERT INTO launch_sessions VALUES ('s1', 'Alice@X.com', %s, %s, 60000)",
                    (now - datetime.timedelta(minutes=20), now - datetime.timedelta(minutes=10)))
    data = ra.fetch_activity(pg, start=today - datetime.timedelta(days=1), end=today)
    rec = {r["email"]: r for r in data["recruiters"]}
    alice = rec["alice@x.com"]
    assert alice["active_minutes_total"] == 17.0
    assert alice["active_minutes_by_surface"] == {"rankings": 15.0, "assess_modal": 2.0}
    assert alice["active_minutes_by_day"] == {today.isoformat(): 17.0} and alice["days_active"] == 1
    assert alice["searches"] == 3 and alice["launches"] == 1 and alice["searches_per_launch"] == 3.0
    assert alice["decisions"] == 2 and alice["actions"]["recording_played"] == 1
    assert alice["decision_latency_median_minutes"] == 30.0 and alice["decisions_timed"] == 3   # 60, 10, 30
    assert alice["waiting_on_pair_minutes"] == 9.0                                          # 10 min - 1 min human
    assert alice["recruiter_hash"] == ra.recruiter_hash("alice@x.com")
    assert rec["bob@x.com"]["active_minutes_total"] == 1.0 and rec["bob@x.com"]["waiting_on_pair_minutes"] is None
    assert data["totals"]["recruiters"] == 2 and data["totals"]["searches"] == 3
    # Scoped to one recruiter's team.
    only_bob = ra.fetch_activity(pg, start=today, end=today, emails=["BOB@x.com"])
    assert [r["email"] for r in only_bob["recruiters"]] == ["bob@x.com"]
    # Another day range sees nothing.
    assert ra.fetch_activity(pg, start=today - datetime.timedelta(days=10),
                             end=today - datetime.timedelta(days=9))["recruiters"] == []


def test_browser_lists_match_the_server():
    """apps/web/lib/activity-queue.ts CLIENT_ACTIONS and hooks/use-active-time.ts
    ActivitySurface must stay within what the server accepts (else silently ignored)."""
    import re
    from pathlib import Path

    web = Path(__file__).resolve().parents[2] / "web"
    queue = (web / "lib" / "activity-queue.ts").read_text()
    block = queue.split("CLIENT_ACTIONS = [", 1)[1].split("]", 1)[0]
    assert set(re.findall(r'"([a-z_]+)"', block)) == set(ra.CLIENT_ACTIONS)
    hook = (web / "hooks" / "use-active-time.ts").read_text()
    union = hook.split("export type ActivitySurface =", 1)[1].split(";", 1)[0]
    surfaces = set(re.findall(r'"([a-z_]+)"', union))
    assert surfaces and surfaces <= ra.SURFACES
