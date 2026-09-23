"""Launch Report: the two Step 5 ("Source") time columns.

  Step 5 Active Time  step5_active_minutes     services/job_step_time
  Step 5 → Launch     step5_to_launch_minutes  first entry → first successful launch

Both come from ONE fetch_step_metrics call per report, on the report's own
connection, and are optional: a job nobody timed, or a failed read, shows a
dash (None) and never costs the rest of the row.

The Postgres tests reuse test_launch_report's fixture data and skip when no
server is reachable (set LAUNCH_REPORT_TEST_DSN, e.g. to a pgserver instance).
"""
import asyncio
import datetime
import os

import pytest

from routers import launch_report as lr
from services import job_step_time as jst
from tests.test_launch_report import (
    _FIXTURE_SQL,
    _STEP_TIME_SQL,
    _SharedConn,
    _admin_user,
    _job,
    _launched,
    _outreach,
)

UTC = datetime.timezone.utc
DAY = datetime.date(2026, 8, 27)


def _metrics(**fields):
    return {**jst.empty_step_metrics(), **fields}


async def _no_outreach(_interview_ids):
    return {}


def _run_report(start="2026-08-27", end="2026-08-27"):
    response = asyncio.run(lr.get_launch_report(
        date=None, start_date=start, end_date=end, team_id=None, user=_admin_user(),
    ))
    assert response["status"] == "success"
    return {r["job_id"]: r for r in response["data"]["jobs"]}


# ---------------------------------------------------------------------------
# _build_row
# ---------------------------------------------------------------------------
def test_row_carries_the_jobs_step5_metrics():
    m = _metrics(active_ms=45 * 60_000, active_minutes=45.0, to_launch_minutes=95.0)
    row = lr._build_row(_job(0), [], [], {}, step_metrics=m)
    assert row["step5_active_minutes"] == 45.0
    assert row["step5_to_launch_minutes"] == 95.0


def test_an_untimed_job_is_not_tracked_rather_than_zero():
    """Jobs worked before the tracking existed have no rows: the page must
    show a dash, so the row carries None — never 0."""
    for m in (None, {}, jst.empty_step_metrics()):
        row = lr._build_row(_job(0), [], [], {}, step_metrics=m)
        assert (row["step5_active_minutes"], row["step5_to_launch_minutes"]) == (None, None), m
    # Callers that predate the keyword get the same.
    row = lr._build_row(_job(0), [], [], {})
    assert (row["step5_active_minutes"], row["step5_to_launch_minutes"]) == (None, None)


def test_a_tracked_zero_stays_zero():
    """A real 0 is distinct from "not tracked" in both columns: an entry ping
    with no active time yet is 0 minutes, and a launch in the same instant as
    the first entry is a 0-minute span. (A launch that came BEFORE the first
    entry has no span; that is decided by the SQL, so the Postgres test below
    covers it.)"""
    m = _metrics(active_ms=0, active_minutes=0.0, to_launch_minutes=0.0)
    row = lr._build_row(_job(0), [], [], {}, step_metrics=m)
    assert row["step5_active_minutes"] == 0.0
    assert row["step5_to_launch_minutes"] == 0.0


# ---------------------------------------------------------------------------
# _load_report_inputs / the handler (no DB)
# ---------------------------------------------------------------------------
class _Conn:
    def close(self):
        pass


def _patch_reads(monkeypatch, jobs, log, launched=None):
    """Stub every per-job read, recording (name, conn) in call order."""
    conn = _Conn()
    monkeypatch.setattr(lr, "get_db_connection", lambda: conn)
    monkeypatch.setattr(lr, "_fetch_jobs_launched_on", lambda *a, **k: jobs)
    monkeypatch.setattr(
        lr, "fetch_launched_candidates",
        lambda c, keys, **kw: log.append(("launched", c)) or list(launched or []),
    )
    monkeypatch.setattr(lr, "fetch_sourced_candidates", lambda c, keys: log.append(("sourced", c)) or [])
    return conn


def test_step_metrics_are_fetched_once_for_every_report_job_on_the_same_connection(monkeypatch):
    jobs = [
        {"job_id": 55, "jobdiva_id": "26-01234"},
        {"job_id": "60", "jobdiva_id": ""},        # no JobDiva ref
        {"job_id": "61", "jobdiva_id": None},
    ]
    log = []
    conn = _patch_reads(monkeypatch, jobs, log)
    metrics = {"55": _metrics(active_minutes=12.0)}
    captured = []

    def _fetch(c, pairs, step=jst.STEP_SOURCE):
        log.append(("step", c))
        captured.append((list(pairs), step))
        return metrics

    monkeypatch.setattr(lr, "fetch_step_metrics", _fetch)
    jobs_out, _launched_by_job, _sourced_by_job, step_by_job = lr._load_report_inputs(DAY, DAY, None)

    assert jobs_out is jobs and step_by_job is metrics
    # One statement for the whole report, with every job's (job_id, jobdiva_id).
    assert captured == [([(55, "26-01234"), ("60", ""), ("61", None)], jst.STEP_SOURCE)]
    # Same connection as every other read, and the last statement on it: a
    # failure aborts the transaction, which must not reach the required reads.
    assert all(c is conn for _, c in log)
    assert log[-1][0] == "step" and [n for n, _ in log].count("step") == 1


def test_a_failed_step_time_read_blanks_only_the_two_columns(monkeypatch, caplog):
    job = {**_job(1), "job_id": "55"}
    _patch_reads(monkeypatch, [job], [], launched=[_launched("1", "c1")])

    def _boom(*_a, **_k):
        raise RuntimeError('relation "job_step_time" does not exist')

    async def _outreach_for(_ids):
        return {"1": _outreach("completed")}

    monkeypatch.setattr(lr, "fetch_step_metrics", _boom)
    monkeypatch.setattr(lr, "_fetch_all_outreach", _outreach_for)
    with caplog.at_level("WARNING"):
        rows = _run_report()

    row = rows["55"]
    assert (row["step5_active_minutes"], row["step5_to_launch_minutes"]) == (None, None)
    assert row["total_candidates_launched"] == 1 and row["completed"] == 1
    assert "Step 5 time unavailable" in caplog.text


def test_each_row_gets_its_own_jobs_step_metrics(monkeypatch):
    jobs = [{**_job(0), "job_id": "55"}, {**_job(0), "job_id": "56", "jobdiva_id": "26-05678"}]
    metrics = {"55": _metrics(active_ms=82 * 60_000, active_minutes=82.0, to_launch_minutes=1500.0)}
    monkeypatch.setattr(lr, "_load_report_inputs", lambda *_a: (jobs, {}, {}, metrics))
    monkeypatch.setattr(lr, "_fetch_all_outreach", _no_outreach)

    rows = _run_report()
    assert (rows["55"]["step5_active_minutes"], rows["55"]["step5_to_launch_minutes"]) == (82.0, 1500.0)
    assert (rows["56"]["step5_active_minutes"], rows["56"]["step5_to_launch_minutes"]) == (None, None)


# ---------------------------------------------------------------------------
# Real Postgres: the whole endpoint over the shipped SQL
# ---------------------------------------------------------------------------
psycopg2 = pytest.importorskip("psycopg2")
_TEST_DSN = os.getenv("LAUNCH_REPORT_TEST_DSN", "dbname=postgres")


@pytest.fixture()
def pg_conn():
    try:
        conn = psycopg2.connect(_TEST_DSN, connect_timeout=3)
    except psycopg2.OperationalError as exc:
        pytest.skip(f"no Postgres reachable at {_TEST_DSN!r}: {exc}")
    try:
        with conn.cursor() as cur:
            cur.execute("SET TIME ZONE 'UTC'")
            cur.execute(_FIXTURE_SQL)
            cur.execute(_STEP_TIME_SQL)
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _step_time(conn, job_id, email, *, minutes, entered, step=jst.STEP_SOURCE):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO job_step_time (job_id, step, user_email, active_ms, first_entered_at)"
            " VALUES (%s, %s, %s, %s, %s)",
            (job_id, step, email, int(minutes * 60_000), entered),
        )


def _serve_from(pg_conn, monkeypatch):
    monkeypatch.setattr(lr, "get_db_connection", lambda: _SharedConn(pg_conn))
    monkeypatch.setattr(lr, "_fetch_all_outreach", _no_outreach)


def _t(day, hour, minute=0):
    return datetime.datetime(2026, 8, day, hour, minute, tzinfo=UTC)


def test_pg_rows_show_step5_time_per_job(pg_conn, monkeypatch):
    # 26-06182: first launched 2026-08-28 01:00 UTC (21:00 EDT), audited on
    # its ref. Two recruiters; the earliest Step 5 entry is 19:30 EDT.
    _step_time(pg_conn, "26-06182", "priya@x.com", minutes=30, entered=_t(27, 23, 30))
    _step_time(pg_conn, "26-06182", "sam@x.com", minutes=15, entered=_t(28, 0, 10))
    _step_time(pg_conn, "26-06182", "priya@x.com", minutes=99, entered=_t(27, 20), step=4)  # not Step 5
    # Ghost A ('60', no JobDiva ref, launched 02:10 UTC under its job_id):
    # Step 5 opened only after the launch, and no active time reported yet.
    _step_time(pg_conn, "60", "late@x.com", minutes=0, entered=_t(28, 2, 30))
    _serve_from(pg_conn, monkeypatch)

    rows = _run_report()

    a = rows["26-06182"]
    assert a["step5_active_minutes"] == 45.0
    assert a["pair_launch_at"] == "2026-08-27T21:00:00-04:00"
    assert a["step5_to_launch_minutes"] == 90.0          # 19:30 EDT → the PAIR Launch shown
    ghost = rows["60"]
    assert ghost["step5_active_minutes"] == 0.0          # tracked: a real zero
    assert ghost["step5_to_launch_minutes"] is None      # launched before Step 5 was opened
    v2 = rows["26-06182-v2"]                             # never timed: no data, not 0
    assert (v2["step5_active_minutes"], v2["step5_to_launch_minutes"]) == (None, None)


def test_pg_step5_to_launch_ends_at_the_first_successful_launch(pg_conn, monkeypatch):
    """26-07100's first attempt failed on 08-28 01:40 UTC; it first launched
    successfully on 08-30 01:40 UTC. The span ends where PAIR Launch does."""
    _step_time(pg_conn, "26-07100", "r@x.com", minutes=20, entered=_t(28, 1))
    _serve_from(pg_conn, monkeypatch)

    row = _run_report("2026-08-29", "2026-08-29")["26-07100"]
    assert row["pair_launch_at"] == "2026-08-29T21:40:00-04:00"
    assert row["step5_to_launch_minutes"] == 2 * 24 * 60 + 40.0   # not 40, the failed attempt


def test_pg_a_failed_step_time_statement_leaves_the_report_intact(pg_conn, monkeypatch, caplog):
    """A real failed statement aborts the transaction. Because the Step 5
    read is the last one on the connection, every required column is
    already loaded and the report still comes back."""

    def _failing_fetch(conn, pairs, step=jst.STEP_SOURCE):
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM job_step_time_missing_for_this_test")

    _step_time(pg_conn, "26-06182", "priya@x.com", minutes=30, entered=_t(27, 23, 30))
    _serve_from(pg_conn, monkeypatch)
    monkeypatch.setattr(lr, "fetch_step_metrics", _failing_fetch)
    with caplog.at_level("WARNING"):
        rows = _run_report()

    assert {"26-06182", "26-06182-v2", "60"} <= set(rows)
    assert rows["26-06182"]["total_candidates_launched"] == 2
    assert rows["26-06182"]["pair_launch_at"] == "2026-08-27T21:00:00-04:00"
    for row in rows.values():
        assert (row["step5_active_minutes"], row["step5_to_launch_minutes"]) == (None, None)
    assert "Step 5 time unavailable" in caplog.text
