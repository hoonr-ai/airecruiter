"""save_candidate_feedback's own candidate-row resolution against a real
Postgres — see _CALLBACK_ROW_TIEBREAKER's own docstring in
routers/candidates.py for the regression this guards (a multi-sourced
candidate's duplicate sourced_candidates rows, and why ordering on
updated_at alone isn't enough to keep a reschedule landing on the same row
as the original schedule).

A fake-cursor test (test_candidate_feedback_new_statuses.py) can't catch
this — it never asks a real planner which of two tied rows "LIMIT 1"
actually returns. Modeled on test_upcoming_callbacks_pg.py's own DSN
precedence/_Shared-connection fixture shape.

Skipped when no Postgres is reachable (LAUNCH_PLATFORM_TEST_DSN, then
LAUNCH_REPORT_TEST_DSN, then "dbname=postgres").
"""
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


def _touch_updated_at(conn, row_id):
    """Simulates an unrelated writer (a resync, an enrichment update) bumping
    this row's updated_at — without changing its feedback_type."""
    with conn.cursor() as cur:
        cur.execute("UPDATE sourced_candidates SET updated_at = now() WHERE id = %s", (row_id,))
    conn.commit()


def _scheduled_row(conn):
    """The one row (id, callback_scheduled_at) currently 'Callback
    Scheduled' for C1, or None if there isn't exactly one."""
    with conn.cursor() as cur:
        cur.execute("SELECT id, data FROM sourced_candidates WHERE candidate_id = 'C1' ORDER BY id")
        rows = cur.fetchall()
    scheduled = [r for r in rows if (r[1] or {}).get("feedback_type") == "Callback Scheduled"]
    return scheduled


async def _save_callback(user, callback_at):
    await cand_module.save_candidate_feedback(
        job_id_or_ref="100", candidate_id="C1",
        request=CandidateFeedbackRequest(feedback_type="Callback Scheduled", callback_at=callback_at.isoformat()),
        background_tasks=BackgroundTasks(), user=user,
    )


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

    await _save_callback(user, first)  # the "original schedule"
    scheduled_after_first = _scheduled_row(pg)
    assert len(scheduled_after_first) == 1
    first_row_id = scheduled_after_first[0][0]
    assert first_row_id in (row_a, row_b)

    await _save_callback(user, second)  # the "reschedule"
    scheduled_after_second = _scheduled_row(pg)

    # The bug: both the original row and a duplicate end up "Callback
    # Scheduled" — a phantom second entry in Upcoming Callbacks for the same
    # candidate. The fix: still only one row carries it, and it's the SAME
    # row the first call picked — not just "some" row.
    assert len(scheduled_after_second) == 1
    assert scheduled_after_second[0][0] == first_row_id
    assert scheduled_after_second[0][1]["callback_scheduled_at"] == second.isoformat()


@pytest.mark.anyio
async def test_reschedule_stays_on_the_active_row_even_if_the_sibling_is_touched_meanwhile(pg):
    """PR #854 review: ordering on updated_at alone makes one query
    deterministic but not stable over time — a sync/enrichment write to the
    SIBLING duplicate between the schedule and the reschedule would flip
    which row sorts first on updated_at, and the reschedule would land on
    the wrong one. Preferring the row that already carries "Callback
    Scheduled" must survive that interleaving."""
    row_a = _insert_duplicate(pg, source="jobdiva")
    row_b = _insert_duplicate(pg, source="linkedin")

    user = UserIdentity(email=_RECRUITER, role="recruiter")
    first = datetime.now(timezone.utc) + timedelta(hours=1)
    second = datetime.now(timezone.utc) + timedelta(hours=2)

    await _save_callback(user, first)
    scheduled = _scheduled_row(pg)
    assert len(scheduled) == 1
    active_row_id = scheduled[0][0]
    sibling_row_id = row_b if active_row_id == row_a else row_a

    # An unrelated writer bumps ONLY the sibling's updated_at — the sibling
    # never becomes "Callback Scheduled", but on updated_at alone it would
    # now sort ahead of the active row.
    _touch_updated_at(pg, sibling_row_id)

    await _save_callback(user, second)
    scheduled = _scheduled_row(pg)

    assert len(scheduled) == 1
    assert scheduled[0][0] == active_row_id
    assert scheduled[0][1]["callback_scheduled_at"] == second.isoformat()
