"""Regression guard for _CONFLICT_CANDIDATE_LEVEL against a real Postgres.

PR #805 narrowed uniq_notifications_recipient_candidate_job_type into a
PARTIAL unique index (excluding candidate_internal_review_requested /
candidate_externally_submitted, which need repeat-cycle dedup instead of
"once ever"). _CONFLICT_CANDIDATE_LEVEL — still a bare
`ON CONFLICT (columns) DO NOTHING` with no WHERE predicate — was never
updated to match. Postgres refuses to infer a partial index for a
predicate-less ON CONFLICT target, so every INSERT using this clause
(candidate_passed, candidate_rejected, and the reconciliation backfill)
raised "there is no unique or exclusion constraint matching the ON CONFLICT
specification" — caught by each caller's own top-level try/except, so the
notifications silently stopped being created entirely, with nothing but a
server log line to show for it.

tests/test_notifications.py's FakeCursor mock could never catch this: it
records the SQL text but never asks a real Postgres planner whether an
ON CONFLICT target actually resolves to an index. Only a real database can
raise (or not raise) this specific error, hence this separate test, modeled
on test_launch_platform_sql_pg.py's "unit D1" pattern.

Skipped when no Postgres is reachable: set NOTIFICATIONS_TEST_DSN (falls
back to LAUNCH_REPORT_TEST_DSN, then "dbname=postgres").
"""
import os

import pytest

psycopg2 = pytest.importorskip("psycopg2")
import psycopg2.extras  # noqa: E402

from services.notifications_service import (  # noqa: E402
    _CONFLICT_CANDIDATE_LEVEL,
    _insert_notification,
)

_DSN = os.getenv("NOTIFICATIONS_TEST_DSN") or os.getenv("LAUNCH_REPORT_TEST_DSN", "dbname=postgres")

# Mirrors the real table + the real (narrowed, partial) unique index exactly
# as routers/notifications.py's init_notifications_schema_sync creates them —
# the partiality is the entire point of this test.
_FIXTURE_SQL = """
CREATE TEMP TABLE notifications (
    id BIGSERIAL PRIMARY KEY,
    recipient_email TEXT NOT NULL,
    type TEXT NOT NULL,
    job_id TEXT,
    jobdiva_id TEXT,
    candidate_id TEXT,
    title TEXT NOT NULL,
    body TEXT,
    score NUMERIC,
    read_at TIMESTAMPTZ,
    cleared_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uniq_notifications_recipient_candidate_job_type
    ON notifications (recipient_email, candidate_id, jobdiva_id, type)
    WHERE type NOT IN ('candidate_internal_review_requested', 'candidate_externally_submitted');
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
            yield cur
    finally:
        conn.rollback()
        conn.close()


def _insert(cur, **overrides):
    kwargs = dict(
        notification_type="candidate_passed",
        conflict_clause=_CONFLICT_CANDIDATE_LEVEL,
        recipient_email="a@x.com",
        job_id="J1",
        jobdiva_id="JD1",
        candidate_id="C1",
        title="t",
        body=None,
        score=None,
    )
    kwargs.update(overrides)
    return _insert_notification(cur, **kwargs)


def test_insert_via_conflict_candidate_level_does_not_raise(pg_cursor):
    """The actual regression: this used to raise
    "there is no unique or exclusion constraint matching the ON CONFLICT
    specification" on every call, for every candidate_passed/rejected
    notification, because the index it targets is partial and the clause
    had no matching WHERE predicate."""
    assert _insert(pg_cursor) is True


def test_duplicate_insert_dedupes_silently_not_an_error(pg_cursor):
    assert _insert(pg_cursor) is True
    assert _insert(pg_cursor) is False  # ON CONFLICT DO NOTHING, not an exception


def test_candidate_passed_and_rejected_have_independent_dedup_slots(pg_cursor):
    """`type` is part of the index, so the same (recipient, candidate, job)
    can be Passed and (separately) Rejected without either blocking the
    other — confirms the WHERE predicate fix didn't accidentally change
    which rows count as conflicting."""
    assert _insert(pg_cursor, notification_type="candidate_passed") is True
    assert _insert(pg_cursor, notification_type="candidate_rejected") is True


def test_review_loop_types_still_insert_fine_without_on_conflict(pg_cursor):
    """candidate_internal_review_requested / candidate_externally_submitted
    are excluded from the partial index and use conflict_clause=None (a
    pre-insert existence check instead, done by their own caller) — this
    just confirms the schema itself is self-consistent: a plain INSERT for
    an excluded type isn't blocked by the unique index in some unexpected
    way (e.g. two such rows for the very same tuple would NOT conflict,
    since the index's WHERE excludes them)."""
    assert _insert(pg_cursor, notification_type="candidate_internal_review_requested", conflict_clause=None) is True
    assert _insert(pg_cursor, notification_type="candidate_internal_review_requested", conflict_clause=None) is True
