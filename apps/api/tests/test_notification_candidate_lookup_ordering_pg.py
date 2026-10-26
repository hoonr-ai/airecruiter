"""_fetch_job_and_candidate's candidate lookup against a real Postgres.

Regression: sourced_candidates' own unique constraint is (jobdiva_id,
candidate_id, source) — a multi-sourced candidate (JobDiva + LinkedIn +
manual, …) genuinely has more than one row for the same candidate_id +
jobdiva_id. _fetch_job_and_candidate used to run `... LIMIT 1` with no
ORDER BY, so which of those rows came back was up to Postgres, not which one
was actually just updated. A candidate_callback_reminder's own stale-
reminder check reads that row's callback_scheduled_at — landing on a stale
duplicate instead of the just-rescheduled one made it look superseded and
skip firing, even though the reschedule had, in fact, just succeeded.

A fake-cursor test can't catch this (it never asks a real planner which row
"LIMIT 1" without ORDER BY actually returns), hence a real database here —
modeled on test_upcoming_callbacks_pg.py's own DSN precedence/fixture shape.

Skipped when no Postgres is reachable (LAUNCH_PLATFORM_TEST_DSN, then
LAUNCH_REPORT_TEST_DSN, then "dbname=postgres").
"""
import json
import os

import pytest

psycopg2 = pytest.importorskip("psycopg2")
import psycopg2.extras  # noqa: E402

from services.notifications_service import _fetch_job_and_candidate  # noqa: E402

_DSN = os.getenv("LAUNCH_PLATFORM_TEST_DSN") or os.getenv("LAUNCH_REPORT_TEST_DSN", "dbname=postgres")

_FIXTURE_SQL = """
CREATE TEMP TABLE monitored_jobs (
  job_id TEXT PRIMARY KEY, title TEXT, enhanced_title TEXT, customer_name TEXT,
  recruiter_emails TEXT, jobdiva_id TEXT, created_at TIMESTAMPTZ DEFAULT now());
CREATE TEMP TABLE sourced_candidates (
  id SERIAL PRIMARY KEY, candidate_id TEXT, jobdiva_id TEXT, name TEXT, email TEXT,
  data JSONB, source TEXT, updated_at TIMESTAMPTZ DEFAULT now());
INSERT INTO monitored_jobs (job_id, title, customer_name, recruiter_emails, jobdiva_id)
  VALUES ('100', 'Data Engineer', 'Acme', '["recruiter@pyramidci.com"]', '26-100');
"""


@pytest.fixture()
def pg_cursor():
    try:
        conn = psycopg2.connect(_DSN, connect_timeout=3)
    except psycopg2.OperationalError as exc:
        pytest.skip(f"no Postgres reachable at {_DSN!r}: {exc}")
    try:
        with conn.cursor() as cur:
            cur.execute(_FIXTURE_SQL)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield conn, cur
    finally:
        conn.rollback()
        conn.close()


def test_picks_the_most_recently_updated_duplicate_row(pg_cursor):
    conn, cur = pg_cursor
    # Two rows for the SAME candidate_id + jobdiva_id, different `source` —
    # exactly the shape sourced_candidates' own unique constraint allows.
    # The older one simulates a stale duplicate left over from the original
    # schedule/a different sourcing channel; the newer one simulates the row
    # a reschedule actually just touched.
    cur.execute(
        "INSERT INTO sourced_candidates (candidate_id, jobdiva_id, name, data, source, updated_at) "
        "VALUES ('C1', '26-100', 'Jane Doe', %s::jsonb, 'jobdiva', now() - interval '1 hour')",
        (json.dumps({"feedback_type": "Callback Scheduled", "callback_scheduled_at": "2026-01-01T15:00:00+00:00"}),),
    )
    cur.execute(
        "INSERT INTO sourced_candidates (candidate_id, jobdiva_id, name, data, source, updated_at) "
        "VALUES ('C1', '26-100', 'Jane Doe', %s::jsonb, 'linkedin', now())",
        (json.dumps({"feedback_type": "Callback Scheduled", "callback_scheduled_at": "2026-01-01T18:00:00+00:00"}),),
    )
    conn.commit()

    job_row, cand_row = _fetch_job_and_candidate(cur, "100", "C1")

    assert job_row is not None
    assert cand_row is not None
    # The just-updated (rescheduled) row's time, not the older duplicate's.
    assert cand_row["data"]["callback_scheduled_at"] == "2026-01-01T18:00:00+00:00"
