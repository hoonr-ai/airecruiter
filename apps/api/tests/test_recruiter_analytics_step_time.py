"""Recruiter Analytics' Step 5 numbers (routers/recruiter_analytics.py, 2026-09-23).

Two numbers with two different attributions, which is what these tests pin:

  * Step 5 Active Time is credited to WHO SPENT IT: a recruiter row gets that
    person's own time (job_step_time.user_email) on any job in the population,
    assigned or not. Rows still only come from assignment, so an unassigned
    helper, an admin or a team outsider adds to the job's total, never a row.
  * Step 5 → Launch is a job property, credited by assignment like the page's
    other job numbers.

Plus: averages skip untimed jobs, untracked is None (never 0), Totals are over
DISTINCT jobs, one step-time read for the whole population, and a failed read
degrades to a warning on the short cache TTL. The Postgres tests skip when no
server is reachable (set LAUNCH_REPORT_TEST_DSN, e.g. to a pgserver instance).
"""

import asyncio
import datetime
import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.auth import UserIdentity
from routers import recruiter_analytics as ra
from services import job_step_time as jst

UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 9, 23, 13, 0, tzinfo=UTC)
MIN = 60_000  # ms


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    ra.clear_recruiter_analytics_cache()
    monkeypatch.delenv("ADMIN_EMAILS", raising=False)
    yield
    ra.clear_recruiter_analytics_cache()


def _row(job_id, emails, jobdiva_id=None):
    rec = {c: None for c in ra._JOB_COLUMNS}
    rec.update({
        "job_id": job_id,
        "jobdiva_id": jobdiva_id,
        "title": f"Job {job_id}",
        "recruiter_emails": json.dumps(emails),
        "is_archived": False,
        "launched_at": T0,
        "version": 1,
    })
    return tuple(rec[c] for c in ra._JOB_COLUMNS)


def _step(by_user=None, *, to_launch=None):
    """One job's fetch_step_metrics entry, built the way the service builds it."""
    by_user = by_user or {}
    ms = sum(by_user.values()) if by_user else None
    entered = T0 if by_user else None
    launched = T0 + datetime.timedelta(minutes=to_launch) if to_launch is not None else None
    return {
        "active_ms": ms,
        "active_minutes": round(ms / 60000.0, 1) if ms is not None else None,
        "first_entered_at": entered,
        "first_launch_at": launched,
        "to_launch_minutes": jst.to_launch_minutes(entered, launched),
        "by_user": dict(by_user),
    }


def _payload(rows, step=None, scope=None, recruiter=None):
    scope_emails = set(scope["emails"]) if scope else None
    jobs = ra._rows_to_jobs(rows, scope_emails, recruiter)
    return ra._build_payload(
        jobs,
        {},
        {},
        scope=scope,
        team_scope_out=None,
        date_range=None,
        recruiter=recruiter,
        warnings=[],
        step_metrics_by_job=step or {},
    )


def _rec(payload):
    return {r["email"]: r for r in payload["recruiters"]}


def _job(payload, job_id):
    return next(j for j in payload["jobs"] if j["job_id"] == job_id)


def _active(block):
    return (block["step5_active_minutes_total"], block["step5_active_minutes_avg"], block["step5_jobs_timed"])


# ---------------------------------------------------------------------------
# Attribution (pure)
# ---------------------------------------------------------------------------


def test_time_spent_by_someone_not_assigned_is_no_row_but_the_launch_lag_is_credited():
    # A did the Step 5 work on j1, but only B is assigned. A gets no row (rows
    # come from assignment); B's own active time is empty; B still gets the
    # job's Step 5 → Launch.
    p = _payload([_row("j1", ["b@x.com"])], {"j1": _step({"a@x.com": 30 * MIN}, to_launch=90)})
    rec = _rec(p)

    assert set(rec) == {"b@x.com"}
    assert _active(rec["b@x.com"]) == (None, None, 0)
    assert rec["b@x.com"]["step5_to_launch_minutes_avg"] == 90.0

    # A's time is still the job's, and in Totals.
    assert _job(p, "j1")["step5_active_minutes"] == 30.0
    assert _job(p, "j1")["step5_to_launch_minutes"] == 90.0
    assert _active(p["totals"]) == (30.0, 30.0, 1)
    assert p["totals"]["step5_to_launch_minutes_avg"] == 90.0
    # The drill-down share only names the job's assigned recruiters.
    assert _job(p, "j1")["step5_active_minutes_by_recruiter"] == {}


def test_active_time_follows_the_person_and_launch_lag_follows_assignment():
    rows = [_row("j1", ["b@x.com"]), _row("j2", ["a@x.com"])]
    step = {
        "j1": _step({"a@x.com": 30 * MIN, "b@x.com": 10 * MIN}, to_launch=60),
        "j2": _step({"a@x.com": 20 * MIN}),  # timed, not launched yet
    }
    rec = _rec(_payload(rows, step))

    a, b = rec["a@x.com"], rec["b@x.com"]
    # A helped on j1 without being assigned: that time is theirs.
    assert _active(a) == (50.0, 25.0, 2)
    assert a["job_ids"] == ["j2"]  # the drill-down is still by assignment
    # j2 has no launch lag, and j1 is not A's to be credited with.
    assert a["step5_to_launch_minutes_avg"] is None
    assert _active(b) == (10.0, 10.0, 1)
    assert b["step5_to_launch_minutes_avg"] == 60.0


def test_averages_skip_untimed_and_untracked_jobs():
    rows = [_row(j, ["a@x.com"]) for j in ("j1", "j2", "j3", "j4")]
    step = {
        "j1": _step({"a@x.com": 30 * MIN}, to_launch=20),
        # Opened (the entry ping) but no active time was ever reported.
        "j2": _step({"a@x.com": 0}),
        # j3: worked before the tracking existed, nothing recorded.
        "j4": _step({"a@x.com": 60 * MIN}, to_launch=40),
    }
    p = _payload(rows, step)
    a = _rec(p)["a@x.com"]

    assert _active(a) == (90.0, 45.0, 2)  # not 90 / 3 or / 4
    assert a["step5_to_launch_minutes_avg"] == 30.0  # j2 / j3 have none: skipped, not 0
    assert _active(p["totals"]) == (90.0, 45.0, 2)
    assert _job(p, "j3")["step5_active_minutes"] is None  # "—", never 0
    assert _job(p, "j3")["step5_to_launch_minutes"] is None
    assert _job(p, "j3")["step5_first_entered_at"] is None
    assert _job(p, "j3")["step5_first_launch_at"] is None
    assert _job(p, "j2")["step5_active_minutes"] == 0.0  # tracked, just no time
    assert _job(p, "j1")["step5_first_entered_at"] == "2026-09-23T09:00:00-04:00"
    # The hover's launch end is the one the number was computed from.
    assert _job(p, "j1")["step5_first_launch_at"] == "2026-09-23T09:20:00-04:00"


def test_nothing_tracked_is_none_everywhere_not_zero():
    p = _payload([_row("j1", ["a@x.com"]), _row("j2", ["b@x.com"])])
    for block in [*p["recruiters"], p["totals"]]:
        assert _active(block) == (None, None, 0)
        assert block["step5_to_launch_minutes_avg"] is None
    assert all(j["step5_active_minutes"] is None and j["step5_to_launch_minutes"] is None for j in p["jobs"])
    assert p["step_time_available"] is True


def test_totals_count_a_shared_job_once():
    rows = [_row("j1", ["a@x.com", "b@x.com"]), _row("j2", ["a@x.com"])]
    step = {"j1": _step({"a@x.com": 20 * MIN, "b@x.com": 40 * MIN}, to_launch=30)}
    p = _payload(rows, step)
    rec = _rec(p)

    assert _active(rec["a@x.com"]) == (20.0, 20.0, 1)
    assert _active(rec["b@x.com"]) == (40.0, 40.0, 1)
    # Both are credited with j1's launch lag by assignment...
    assert rec["a@x.com"]["step5_to_launch_minutes_avg"] == 30.0
    assert rec["b@x.com"]["step5_to_launch_minutes_avg"] == 30.0
    # ...and Totals see j1 once: 60 minutes on one timed job, one 30-minute lag.
    assert _active(p["totals"]) == (60.0, 60.0, 1)
    assert p["totals"]["step5_to_launch_minutes_avg"] == 30.0
    assert _job(p, "j1")["step5_active_minutes_by_recruiter"] == {"a@x.com": 20.0, "b@x.com": 40.0}


def test_team_scope_keeps_outsiders_and_admins_out_of_rows_and_names():
    scope = {"team_id": "t1", "team_name": "East", "emails": ["a@x.com"], "job_ids": ["j1"], "sc_keys": ["j1"]}
    rows = [_row("j1", ["a@x.com", "outsider@x.com"]), _row("j9", ["outsider@x.com"])]
    step = {
        "j1": _step({"a@x.com": 10 * MIN, "outsider@x.com": 50 * MIN, "admin@pyramid.com": 5 * MIN}, to_launch=15),
        "j9": _step({"a@x.com": 99 * MIN}),  # outside the team's population
    }
    p = _payload(rows, step, scope=scope)

    assert list(_rec(p)) == ["a@x.com"]
    assert _active(_rec(p)["a@x.com"]) == (10.0, 10.0, 1)  # j9 is not in view
    # The job's own total is everyone's; its per-recruiter split names nobody
    # outside the team.
    j1 = _job(p, "j1")
    assert j1["step5_active_minutes"] == 65.0
    assert j1["step5_active_minutes_by_recruiter"] == {"a@x.com": 10.0}
    assert _active(p["totals"]) == (65.0, 65.0, 1)


def test_recruiter_filter_gives_no_row_to_a_co_assigned_recruiters_time():
    rows = [_row("j1", ["a@x.com", "b@x.com"])]
    p = _payload(rows, {"j1": _step({"a@x.com": 5 * MIN, "b@x.com": 25 * MIN}, to_launch=45)}, recruiter="a@x.com")
    assert list(_rec(p)) == ["a@x.com"]
    assert _active(_rec(p)["a@x.com"]) == (5.0, 5.0, 1)
    assert _rec(p)["a@x.com"]["step5_to_launch_minutes_avg"] == 45.0
    assert _active(p["totals"]) == (30.0, 30.0, 1)


def test_by_user_keys_padded_with_whitespace_still_match_the_row():
    # fetch_step_metrics lowercases by_user's keys but does not strip them, so
    # a padded key is the one variant that can reach the payload. Case-only
    # variants collide inside the service and never arrive as two keys.
    p = _payload([_row("j1", ["a@x.com"])], {"j1": _step({" a@x.com ": 2 * MIN, "a@x.com": 1 * MIN})})
    assert _active(_rec(p)["a@x.com"]) == (3.0, 3.0, 1)  # summed, not overwritten


# ---------------------------------------------------------------------------
# Compute with a mocked connection
# ---------------------------------------------------------------------------


def _mock_compute(monkeypatch, step_metrics, rows=None):
    conn = MagicMock()
    monkeypatch.setattr(ra, "get_db_connection", lambda: conn)
    monkeypatch.setattr(ra, "_fetch_job_rows", lambda _c, _s, _r: rows or [
        _row("31990001", ["a@x.com"], jobdiva_id="26-29267"),
        _row("EXT-1", ["b@x.com"]),
    ])
    monkeypatch.setattr(ra, "_load_directory", lambda _c: {"team_by_email": {}, "accounts": set()})
    monkeypatch.setattr(ra, "fetch_job_candidate_metrics", lambda _c, _p: {})
    monkeypatch.setattr(ra, "fetch_step_metrics", step_metrics)
    return conn


def test_compute_makes_one_step_time_read_for_the_whole_population(monkeypatch):
    calls = []

    def _fetch(_conn, pairs):
        calls.append(list(pairs))
        return {"31990001": _step({"a@x.com": 12 * MIN}, to_launch=33)}

    _mock_compute(monkeypatch, _fetch)
    p = ra._compute_recruiter_analytics_sync(None, None, None)

    assert calls == [[("31990001", "26-29267"), ("EXT-1", None)]]
    assert p["step_time_available"] is True and p["warnings"] == []
    assert _active(_rec(p)["a@x.com"]) == (12.0, 12.0, 1)
    assert _rec(p)["a@x.com"]["step5_to_launch_minutes_avg"] == 33.0
    assert _job(p, "EXT-1")["step5_active_minutes"] is None


def test_step_time_failure_degrades_on_its_own(monkeypatch):
    def _timeout(_conn, _pairs):
        raise RuntimeError('relation "job_step_time" does not exist')

    conn = _mock_compute(monkeypatch, _timeout)
    p = ra._compute_recruiter_analytics_sync(None, None, None)

    conn.rollback.assert_called()  # so the directory read still runs
    assert p["step_time_available"] is False
    assert p["metrics_available"] is True  # the candidate numbers are unaffected
    assert len(p["warnings"]) == 1
    assert "Step 5" in p["warnings"][0] and "job_step_time" not in p["warnings"][0]
    for block in [*p["recruiters"], p["totals"]]:
        assert _active(block) == (None, None, 0)
        assert block["step5_to_launch_minutes_avg"] is None


class _Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def test_step_time_failure_is_cached_on_the_short_ttl(monkeypatch):
    clock = _Clock()
    # Only the router's reference: a global patch would freeze asyncio too.
    monkeypatch.setattr(ra, "time", SimpleNamespace(monotonic=clock))
    calls = []

    def _flaky(_conn, _pairs):
        calls.append(1)
        raise RuntimeError("canceling statement due to statement timeout")

    _mock_compute(monkeypatch, _flaky)
    admin = UserIdentity(email="admin@pyramid.com", role="admin")

    def _call(**kw):
        kwargs = dict(start_date=None, end_date=None, team_id=None, recruiter=None, refresh=False, **kw)
        return asyncio.run(ra.get_recruiter_analytics(user=admin, response=None, **kwargs))["data"]

    first = _call()
    assert first["step_time_available"] is False and first["cached"] is False
    assert _call()["cached"] is True
    assert len(calls) == 1
    clock.now += ra._DEGRADED_CACHE_TTL_SECONDS + 1  # well inside the healthy 60s
    assert _call()["cached"] is False
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# Real Postgres
# ---------------------------------------------------------------------------

psycopg2 = pytest.importorskip("psycopg2")
_TEST_DSN = os.getenv("LAUNCH_REPORT_TEST_DSN", "dbname=postgres")

_SCHEMA_SQL = """
CREATE TEMP TABLE monitored_jobs (
    job_id TEXT PRIMARY KEY, jobdiva_id TEXT, title TEXT, enhanced_title TEXT, customer_name TEXT,
    recruiter_emails TEXT, is_archived BOOLEAN DEFAULT FALSE, created_at TEXT,
    pair_launched_at TIMESTAMP, candidates_sourced INTEGER DEFAULT 0, candidates_launched INTEGER DEFAULT 0,
    pair_external_subs INTEGER DEFAULT 0, jobdiva_total_subs INTEGER DEFAULT 0,
    time_to_first_pass DOUBLE PRECISION, pair_posted_by TEXT, pair_launched_by TEXT,
    parent_job_id TEXT, version INTEGER, posted_date TEXT
) ON COMMIT DROP;
CREATE TEMP TABLE engage_interview_audit (
    id SERIAL PRIMARY KEY, candidate_id VARCHAR(255) NOT NULL, jobdiva_id VARCHAR(255),
    interview_id VARCHAR(255), created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ON COMMIT DROP;
CREATE TEMP TABLE sourced_candidates (
    id SERIAL PRIMARY KEY, jobdiva_id TEXT NOT NULL, candidate_id TEXT NOT NULL, source TEXT NOT NULL,
    data JSONB, status TEXT DEFAULT 'sourced', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(jobdiva_id, candidate_id, source)
) ON COMMIT DROP;
CREATE TEMP TABLE teams (id TEXT PRIMARY KEY, name TEXT NOT NULL) ON COMMIT DROP;
CREATE TEMP TABLE team_members (
    id SERIAL PRIMARY KEY, team_id TEXT NOT NULL, email TEXT NOT NULL, member_role TEXT NOT NULL DEFAULT 'member'
) ON COMMIT DROP;
CREATE TEMP TABLE user_roles (email TEXT PRIMARY KEY, role TEXT) ON COMMIT DROP;
"""


@pytest.fixture()
def pg(monkeypatch):
    try:
        conn = psycopg2.connect(_TEST_DSN, connect_timeout=3)
    except psycopg2.OperationalError as exc:
        pytest.skip(f"no Postgres reachable at {_TEST_DSN!r}: {exc}")
    try:
        with conn.cursor() as cur:
            cur.execute("SET TIME ZONE 'UTC'")
            cur.execute(_SCHEMA_SQL)
            # The shipped DDL, as a temp table.
            cur.execute(jst.SCHEMA_STATEMENTS[0].replace("CREATE TABLE", "CREATE TEMP TABLE") + " ON COMMIT DROP")
        _seed(conn)

        class _Borrowed:
            """The router closes its connection; the fixture owns this one."""

            def __getattr__(self, name):
                return getattr(conn, name)

            def close(self):
                pass

        monkeypatch.setattr(ra, "get_db_connection", lambda: _Borrowed())
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _seed(conn):
    t = lambda h, m=0: f"2026-09-23 {h:02d}:{m:02d}:00"  # noqa: E731  UTC

    with conn.cursor() as cur:
        def job(job_id, ref, emails):
            cur.execute(
                "INSERT INTO monitored_jobs (job_id, jobdiva_id, title, recruiter_emails, version) "
                "VALUES (%s, %s, %s, %s, 1)",
                (job_id, ref, f"Job {job_id}", json.dumps(emails)),
            )

        def audit(key, at, interview_id="iv"):
            cur.execute(
                "INSERT INTO engage_interview_audit (candidate_id, jobdiva_id, interview_id, created_at) "
                "VALUES ('c', %s, %s, %s)",
                (key, interview_id, at),
            )

        def timed(job_id, email, minutes, entered, step=jst.STEP_SOURCE):
            cur.execute(
                "INSERT INTO job_step_time (job_id, step, user_email, active_ms, first_entered_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (job_id, step, email, minutes * MIN, entered + "+00"),
            )

        # j1: both assigned recruiters timed it; first entry 13:00; a failed
        # launch at 13:30, the first successful one at 15:00 → 120 minutes.
        job("31990001", "26-29267", ["Alice@Pyramid.com", "bob@pyramid.com"])
        timed("31990001", "alice@pyramid.com", 40, t(13))
        timed("31990001", "bob@pyramid.com", 20, t(14))
        timed("31990001", "alice@pyramid.com", 99, t(12), step=4)  # another step: ignored
        audit("26-29267", t(13, 30), interview_id="")
        audit("26-29267", t(15))
        # j2: Bob's job, but Carol (assigned nowhere) and Alice did the Step 5
        # work; first entry 10:00, launched 10:45 on the numeric key → 45.
        job("31990002", "26-00002", ["bob@pyramid.com"])
        timed("31990002", "carol@pyramid.com", 30, t(10))
        timed("31990002", "alice@pyramid.com", 10, t(10, 15))
        audit("31990002", t(10, 45))
        # j3: launched before the tracking existed; nothing timed → "—".
        job("31990003", "26-00003", ["alice@pyramid.com"])
        audit("26-00003", t(12))
        # j4: timed only after it launched (re-opened) → no Step 5 → Launch.
        job("31990004", "26-00004", ["alice@pyramid.com"])
        audit("26-00004", t(8))
        timed("31990004", "alice@pyramid.com", 5, t(16))


def test_postgres_attribution_end_to_end(pg):
    p = ra._compute_recruiter_analytics_sync(None, None, None)

    assert p["warnings"] == [] and p["step_time_available"] is True
    rec = _rec(p)
    assert set(rec) == {"alice@pyramid.com", "bob@pyramid.com"}  # Carol has no assigned job

    alice, bob = rec["alice@pyramid.com"], rec["bob@pyramid.com"]
    # Her own time on j1, j2 (not hers) and j4; the step-4 row is not Step 5.
    assert _active(alice) == (55.0, 18.3, 3)
    assert alice["step5_to_launch_minutes_avg"] == 120.0  # j1 only: j3 / j4 have none
    assert _active(bob) == (20.0, 20.0, 1)
    assert bob["step5_to_launch_minutes_avg"] == 82.5  # (j1 120 + j2 45) / 2

    # Distinct jobs, everyone's time: j1 60 + j2 40 (Carol's included) + j4 5.
    t = p["totals"]
    assert _active(t) == (105.0, 35.0, 3)
    assert t["step5_to_launch_minutes_avg"] == 82.5

    jobs = {j["job_id"]: j for j in p["jobs"]}
    assert jobs["31990001"]["step5_active_minutes"] == 60.0
    assert jobs["31990001"]["step5_to_launch_minutes"] == 120.0
    assert jobs["31990001"]["step5_first_entered_at"] == "2026-09-23T09:00:00-04:00"
    # Two reads of one first-successful-launch rule: they must agree.
    assert jobs["31990001"]["step5_first_launch_at"] == jobs["31990001"]["launched_at"]
    assert jobs["31990001"]["step5_active_minutes_by_recruiter"] == {"alice@pyramid.com": 40.0, "bob@pyramid.com": 20.0}
    assert jobs["31990002"]["step5_to_launch_minutes"] == 45.0
    assert jobs["31990002"]["step5_active_minutes_by_recruiter"] == {}  # Bob spent none; Alice and Carol aren't assigned
    assert jobs["31990003"]["step5_active_minutes"] is None and jobs["31990003"]["step5_to_launch_minutes"] is None
    assert jobs["31990004"]["step5_active_minutes"] == 5.0 and jobs["31990004"]["step5_to_launch_minutes"] is None


def test_postgres_team_scope_through_the_endpoint(pg, monkeypatch):
    from services import teams_db

    monkeypatch.setattr(teams_db, "get_team", lambda team_id: {
        "id": team_id, "name": "East", "lead_emails": ["alice@pyramid.com"], "member_emails": [],
    })
    lead = UserIdentity(email="alice@pyramid.com", role="team_lead", team_id="t-east", team_name="East")
    data = asyncio.run(ra.get_recruiter_analytics(
        start_date=None, end_date=None, team_id=None, recruiter=None, refresh=False, user=lead, response=None,
    ))["data"]

    assert [r["email"] for r in data["recruiters"]] == ["alice@pyramid.com"]
    assert sorted(j["job_id"] for j in data["jobs"]) == ["31990001", "31990003", "31990004"]
    # j2 is Bob's alone, so it and Alice's 10 minutes on it are outside the team's view.
    assert _active(data["recruiters"][0]) == (45.0, 22.5, 2)
    # Bob's 20 minutes stay in j1's own total, but he is never named.
    j1 = next(j for j in data["jobs"] if j["job_id"] == "31990001")
    assert j1["step5_active_minutes"] == 60.0
    assert j1["step5_active_minutes_by_recruiter"] == {"alice@pyramid.com": 40.0}
    assert _active(data["totals"]) == (65.0, 32.5, 2)
    assert data["totals"]["step5_to_launch_minutes_avg"] == 120.0


def test_postgres_step_time_is_independent_of_session_timezone(pg):
    with pg.cursor() as cur:
        cur.execute("SET TIME ZONE 'Asia/Kolkata'")
    p = ra._compute_recruiter_analytics_sync(None, None, None)
    jobs = {j["job_id"]: j for j in p["jobs"]}
    assert jobs["31990001"]["step5_to_launch_minutes"] == 120.0
    assert jobs["31990001"]["step5_first_entered_at"] == "2026-09-23T09:00:00-04:00"
