"""time_to_first_pass (Jobs dashboard / reports) against a real Postgres: from
the job's first INTERVIEW (T2) to the first fixed completion of a passed,
launched person — stable under relaunches and later status write-backs.

Skipped when no Postgres is reachable (LAUNCH_REPORT_TEST_DSN).
"""
import asyncio
import json
import os

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from services.auto_assign_service import auto_assign_service  # noqa: E402

_DSN = os.getenv("LAUNCH_REPORT_TEST_DSN", "dbname=postgres")

_SCHEMA = """
CREATE TEMP TABLE monitored_jobs (job_id TEXT, jobdiva_id TEXT, pair_launched_at TIMESTAMP,
                                  first_launch_at TIMESTAMPTZ);
CREATE TEMP TABLE sourced_candidates (id SERIAL, jobdiva_id TEXT, candidate_id TEXT, data JSONB);
"""


class _Ctx:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, *_exc):
        return False


@pytest.fixture()
def pg(monkeypatch):
    try:
        conn = psycopg2.connect(_DSN, connect_timeout=3)
    except psycopg2.OperationalError as exc:
        pytest.skip(f"no Postgres reachable at {_DSN!r}: {exc}")
    try:
        with conn.cursor() as cur:
            cur.execute("SET TIME ZONE 'UTC'")
            cur.execute(_SCHEMA)
        monkeypatch.setattr(auto_assign_service, "_get_db_connection", lambda: _Ctx(conn))
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _job(conn, *, saved, first_interview):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO monitored_jobs VALUES ('55', '26-1', %s, %s)", (saved, first_interview))


def _cand(conn, cid, data, key="26-1"):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO sourced_candidates (jobdiva_id, candidate_id, data) VALUES (%s, %s, %s)",
                    (key, cid, json.dumps(data)))


def _ttfp():
    return asyncio.run(auto_assign_service._calculate_time_to_first_pass("26-1"))


def test_clock_runs_from_the_first_interview_to_the_first_fixed_pass(pg):
    # The first save (10:00) launched nobody; the first interview came at 12:00.
    _job(pg, saved="2026-10-01 10:00:00", first_interview="2026-10-01 12:00:00+00")
    _cand(pg, "a", {"engage_interview_id": "iv-a", "engage_hard_filter_status": "pass",
                    "first_completed_at": "2026-10-01T12:30:00+00:00",
                    "engage_updated_at": "2026-10-09T08:00:00+00:00"})        # a later write-back
    _cand(pg, "b", {"engage_interview_id": "iv-b", "engage_hard_filter_status": "passed",
                    "engage_completed_at": "2026-10-01 13:00:00+00"})
    # Passed but never launched (an imported status): not PAIR's pass.
    _cand(pg, "c", {"engage_hard_filter_status": "pass", "first_completed_at": "2026-10-01T12:05:00+00:00"})
    _cand(pg, "d", {"engage_interview_id": "iv-d", "engage_hard_filter_status": "pass",
                    "first_completed_at": "not a date"})                       # never fails the query
    assert _ttfp() == 30.0


def test_legacy_jobs_without_t2_fall_back_to_the_first_save(pg):
    _job(pg, saved="2026-10-01 12:00:00", first_interview=None)
    _cand(pg, "a", {"engage_interview_id": "iv-a", "engage_hard_filter_status": "pass",
                    "first_completed_at": "2026-10-01T13:00:00+00:00"})
    assert _ttfp() == 60.0


def test_no_pass_or_no_launch_is_none(pg):
    _job(pg, saved=None, first_interview=None)
    assert _ttfp() is None
