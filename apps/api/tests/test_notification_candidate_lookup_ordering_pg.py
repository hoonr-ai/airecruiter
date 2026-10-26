"""_fetch_job_and_candidate's candidate lookup against a real Postgres — see
its own docstring in services/notifications_service.py for the regression
this guards. A fake-cursor test can't catch it (it never asks a real planner
which row "LIMIT 1" without ORDER BY actually returns), hence a real
database here — modeled on test_upcoming_callbacks_pg.py's own DSN
precedence/fixture shape.

Skipped when no Postgres is reachable (LAUNCH_PLATFORM_TEST_DSN, then
LAUNCH_REPORT_TEST_DSN, then "dbname=postgres"); CI provides one (see
.github/workflows/deploy.yml's test-api `postgres` service).
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


# Arbitrary, distinct callback_scheduled_at values used only as labels to
# tell the two rows' `data` apart in assertions below — not meaningful dates.
_OLD_CALLBACK_AT = "2026-01-01T15:00:00+00:00"
_NEW_CALLBACK_AT = "2026-01-01T18:00:00+00:00"


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
        (json.dumps({"feedback_type": "Callback Scheduled", "callback_scheduled_at": _OLD_CALLBACK_AT}),),
    )
    cur.execute(
        "INSERT INTO sourced_candidates (candidate_id, jobdiva_id, name, data, source, updated_at) "
        "VALUES ('C1', '26-100', 'Jane Doe', %s::jsonb, 'linkedin', now())",
        (json.dumps({"feedback_type": "Callback Scheduled", "callback_scheduled_at": _NEW_CALLBACK_AT}),),
    )
    conn.commit()

    job_row, cand_row = _fetch_job_and_candidate(cur, "100", "C1")

    assert job_row is not None
    assert cand_row is not None
    # The just-updated (rescheduled) row's time, not the older duplicate's.
    assert cand_row["data"]["callback_scheduled_at"] == _NEW_CALLBACK_AT


def test_a_null_updated_at_loses_to_any_real_timestamp(pg_cursor):
    """Exercises the NULLS LAST branch: a row that has never been touched
    since creation (updated_at never set) must not win over one that has a
    real, even old, timestamp — NULLS LAST, not NULLS FIRST, is what makes
    that true."""
    conn, cur = pg_cursor
    cur.execute(
        "INSERT INTO sourced_candidates (candidate_id, jobdiva_id, name, data, source, updated_at) "
        "VALUES ('C2', '26-100', 'John Roe', %s::jsonb, 'jobdiva', NULL)",
        (json.dumps({"feedback_type": "Callback Scheduled", "callback_scheduled_at": _OLD_CALLBACK_AT}),),
    )
    cur.execute(
        "INSERT INTO sourced_candidates (candidate_id, jobdiva_id, name, data, source, updated_at) "
        "VALUES ('C2', '26-100', 'John Roe', %s::jsonb, 'linkedin', now() - interval '30 days')",
        (json.dumps({"feedback_type": "Callback Scheduled", "callback_scheduled_at": _NEW_CALLBACK_AT}),),
    )
    conn.commit()

    _job_row, cand_row = _fetch_job_and_candidate(cur, "100", "C2")

    assert cand_row is not None
    assert cand_row["data"]["callback_scheduled_at"] == _NEW_CALLBACK_AT


def test_prefer_callback_scheduled_reads_the_active_callback_row_not_a_busier_sibling(pg_cursor):
    """The actual gap this closes: the callback reminder's own stale-check
    calls this with prefer_callback_scheduled=True specifically so a sibling
    duplicate that ISN'T the active callback — but has been touched more
    recently by something unrelated (a resync, an enrichment update) — can't
    outrank the row that actually IS "Callback Scheduled". Without the flag
    (the plain recency order every other notification type still uses), the
    busier sibling would win and the stale-check would read a mismatched
    time, wrongly concluding the reminder was superseded and skipping it —
    even though the real callback row was never touched."""
    conn, cur = pg_cursor
    cur.execute(
        "INSERT INTO sourced_candidates (candidate_id, jobdiva_id, name, data, source, updated_at) "
        "VALUES ('C3', '26-100', 'Sam Lee', %s::jsonb, 'jobdiva', now() - interval '1 hour')",
        (json.dumps({"feedback_type": "Callback Scheduled", "callback_scheduled_at": _OLD_CALLBACK_AT}),),
    )
    cur.execute(
        "INSERT INTO sourced_candidates (candidate_id, jobdiva_id, name, data, source, updated_at) "
        "VALUES ('C3', '26-100', 'Sam Lee', '{}'::jsonb, 'linkedin', now())",
        (),
    )
    conn.commit()

    _job_row, without_flag = _fetch_job_and_candidate(cur, "100", "C3")
    # Demonstrates the actual bug: plain recency picks the busier sibling,
    # which was never the callback at all (no callback_scheduled_at here).
    assert without_flag["data"].get("callback_scheduled_at") is None

    _job_row, with_flag = _fetch_job_and_candidate(cur, "100", "C3", prefer_callback_scheduled=True)
    assert with_flag["data"]["callback_scheduled_at"] == _OLD_CALLBACK_AT
