"""save_candidate_feedback's own candidate-row resolution against a real
Postgres.

Regression: sourced_candidates' real unique constraint is (jobdiva_id,
candidate_id, source) — a multi-sourced candidate (JobDiva + LinkedIn +
manual, …) genuinely has more than one row for the same candidate_id +
jobdiva_id, joining to the exact same monitored_jobs row. The resolution
query's own ORDER BY (job_id numeric-ness, monitored_jobs.created_at) ties
for every such duplicate — it never varies between them — so which one
"LIMIT 1" actually returned was up to Postgres, not which row a PRIOR write
(e.g. the original schedule) had actually touched. A reschedule that landed
on a DIFFERENT duplicate than the original schedule left the old row's
"Callback Scheduled" + stale callback_scheduled_at behind as a second,
phantom entry in Upcoming Callbacks for the same candidate — and broke the
reminder's own stale-check besides (notifications_service._fetch_job_and_
candidate reads whichever row LOOKS most current, which no longer agreed
with whichever row this specific write-call actually landed on).

A fake-cursor test (test_candidate_feedback_new_statuses.py) can't catch
this — it never asks a real planner which of two tied rows "LIMIT 1"
actually returns. Modeled on test_upcoming_callbacks_pg.py's own DSN
precedence/_Shared-connection fixture shape.

Skipped when no Postgres is reachable (LAUNCH_PLATFORM_TEST_DSN, then
LAUNCH_REPORT_TEST_DSN, then "dbname=postgres").
"""
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from fastapi import BackgroundTasks  # noqa: E402

from core.auth import UserIdentity  # noqa: E402
from models import CandidateFeedbackRequest  # noqa: E402
from routers import candidates as cand_module  # noqa: E402

_DSN = os.getenv("LAUNCH_PLATFORM_TEST_DSN") or os.getenv("LAUNCH_REPORT_TEST_DSN", "dbname=postgres")

_RECRUITER = "recruiter@pyramidci.com"


class _Shared:
    """One connection for the whole test, so TEMP tables stay visible across
    the several get_db_connection() calls save_candidate_feedback makes."""

    def __init__(self, conn):
        self._conn = conn

    def cursor(self, *a, **k):
        return self._conn.cursor(*a, **k)

    def commit(self):
        self._conn.commit()

    def close(self):
        pass


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
def pg(monkeypatch):
    try:
        conn = psycopg2.connect(_DSN, connect_timeout=3)
    except psycopg2.OperationalError as exc:
        pytest.skip(f"no Postgres reachable at {_DSN!r}: {exc}")
    try:
        with conn.cursor() as cur:
            cur.execute(_FIXTURE_SQL)
        monkeypatch.setattr(cand_module, "get_db_connection", lambda: _Shared(conn))
        monkeypatch.setattr(cand_module, "_verify_job_access_by_id", lambda *a, **kw: None)
        monkeypatch.setattr(cand_module, "refresh_feedback_metrics_sync", lambda job_ref: {"feedback_completed": 1, "pair_submits": 0})
        monkeypatch.setattr("services.task_outbox.add", lambda *a, **kw: 1)
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _insert_duplicate(conn, *, source):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sourced_candidates (candidate_id, jobdiva_id, name, data, source) "
            "VALUES ('C1', '26-100', 'Jane Doe', '{}'::jsonb, %s) RETURNING id",
            (source,),
        )
        row_id = cur.fetchone()[0]
    conn.commit()
    return row_id


@pytest.mark.anyio
async def test_reschedule_lands_on_the_same_duplicate_row_as_the_original_schedule(pg):
    # Two rows for the SAME candidate_id + jobdiva_id, different `source` —
    # exactly the shape sourced_candidates' own unique constraint allows,
    # and exactly what made the original schedule and a later reschedule
    # land on two different rows.
    row_a = _insert_duplicate(pg, source="jobdiva")
    row_b = _insert_duplicate(pg, source="linkedin")

    user = UserIdentity(email=_RECRUITER, role="recruiter")
    first = datetime.now(timezone.utc) + timedelta(hours=1)
    second = datetime.now(timezone.utc) + timedelta(hours=2)

    # The "original schedule".
    await cand_module.save_candidate_feedback(
        job_id_or_ref="100", candidate_id="C1",
        request=CandidateFeedbackRequest(feedback_type="Callback Scheduled", callback_at=first.isoformat()),
        background_tasks=BackgroundTasks(), user=user,
    )
    # The "reschedule" — a second, independent call for the same candidate+job,
    # exactly like the Upcoming Callbacks page's Reschedule action makes.
    await cand_module.save_candidate_feedback(
        job_id_or_ref="100", candidate_id="C1",
        request=CandidateFeedbackRequest(feedback_type="Callback Scheduled", callback_at=second.isoformat()),
        background_tasks=BackgroundTasks(), user=user,
    )

    with pg.cursor() as cur:
        cur.execute("SELECT id, data FROM sourced_candidates WHERE candidate_id = 'C1' ORDER BY id")
        rows = cur.fetchall()

    scheduled = [r for r in rows if (r[1] or {}).get("feedback_type") == "Callback Scheduled"]
    # The bug: both the original row and a duplicate end up "Callback
    # Scheduled" — a phantom second entry in Upcoming Callbacks for the same
    # candidate. The fix: only ever one row carries it, whichever of row_a/
    # row_b the resolution consistently keeps landing on, with the
    # reschedule's own (second) time.
    assert len(scheduled) == 1
    assert scheduled[0][0] in (row_a, row_b)
    assert scheduled[0][1]["callback_scheduled_at"] == second.isoformat()
