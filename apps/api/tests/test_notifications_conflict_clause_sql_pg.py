"""Regression guard for _CONFLICT_CANDIDATE_LEVEL and
_CONFLICT_CALLBACK_REMINDER against a real Postgres.

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

A PR #855 follow-up found the same family of bug from the other direction:
candidate_callback_reminder used a bare `conflict_clause=None` INSERT while
still falling under that same "once ever" index's scope. Confirmed live in
QA: the first callback reminder a (recipient, candidate, job) combo ever got
permanently UniqueViolation'd every later one — including a reschedule's —
killing that task_outbox row for good with no notification ever created.
Fixed by excluding candidate_callback_reminder from the "once ever" index
too (ONCE_EVER_EXCLUDED_TYPES) and giving it its own narrower, per-occurrence
dedup (_CONFLICT_CALLBACK_REMINDER / uniq_notifications_callback_reminder_
occurrence, scoped by callback_at) instead of no dedup at all.

tests/test_notifications.py's FakeCursor mock could never catch either bug:
it records the SQL text but never asks a real Postgres planner whether an
ON CONFLICT target actually resolves to an index, or whether an un-indexed
bare INSERT collides with one. Only a real database can raise (or not raise)
these specific errors, hence this separate test, modeled on
test_launch_platform_sql_pg.py's "unit D1" pattern.

Skipped unless NOTIFICATIONS_TEST_DSN is explicitly set — no fallback to a
shared/dev DSN. (Safe either way: everything here runs in a TEMP table
inside a transaction that's always rolled back, but an explicit opt-in costs
nothing and rules out ever touching a database nobody meant this to touch.)
"""
import os

import pytest

psycopg2 = pytest.importorskip("psycopg2")
import psycopg2.extras  # noqa: E402

from services.notifications_service import (  # noqa: E402
    CALLBACK_REMINDER_TYPE,
    ONCE_EVER_EXCLUDED_TYPES,
    REVIEW_LOOP_TYPES,
    _CONFLICT_CALLBACK_REMINDER,
    _CONFLICT_CANDIDATE_LEVEL,
    _insert_notification,
)

_DSN = os.getenv("NOTIFICATIONS_TEST_DSN")

# Mirrors the real table + the real (narrowed, partial) unique index(es)
# exactly as routers/notifications.py's init_notifications_schema_sync
# creates them — the partiality is the entire point of this test. Built from
# ONCE_EVER_EXCLUDED_TYPES (the same source _CONFLICT_CANDIDATE_LEVEL itself
# uses) rather than its own hardcoded copy, so this fixture can't drift from
# the real predicate either.
_REVIEW_LOOP_TYPES_SQL = ", ".join(f"'{t}'" for t in REVIEW_LOOP_TYPES)
_ONCE_EVER_EXCLUDED_TYPES_SQL = ", ".join(f"'{t}'" for t in ONCE_EVER_EXCLUDED_TYPES)
_FIXTURE_SQL = f"""
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
    metadata JSONB DEFAULT '{{}}'::jsonb,
    read_at TIMESTAMPTZ,
    cleared_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uniq_notifications_recipient_candidate_job_type
    ON notifications (recipient_email, candidate_id, jobdiva_id, type)
    WHERE type NOT IN ({_ONCE_EVER_EXCLUDED_TYPES_SQL});
CREATE UNIQUE INDEX uniq_notifications_callback_reminder_occurrence
    ON notifications (recipient_email, candidate_id, jobdiva_id, (metadata->>'callback_at'))
    WHERE type = '{CALLBACK_REMINDER_TYPE}';
"""


@pytest.fixture()
def pg_cursor():
    if not _DSN:
        pytest.skip("NOTIFICATIONS_TEST_DSN not set")
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
    review_type = REVIEW_LOOP_TYPES[0]
    assert _insert(pg_cursor, notification_type=review_type, conflict_clause=None) is True
    assert _insert(pg_cursor, notification_type=review_type, conflict_clause=None) is True


# --- candidate_callback_reminder (PR #855 follow-up) -------------------------
#
# Confirmed live in QA: candidate_callback_reminder used to fall under
# uniq_notifications_recipient_candidate_job_type's "once ever" scope with a
# bare conflict_clause=None insert. The very FIRST callback reminder a
# (recipient, candidate, job) combo ever got permanently blocked every later
# one — including a reschedule's — with a raw UniqueViolation that killed the
# task_outbox row for good (candidate_callback_reminder_skipped_stale never
# even runs for this; the stale-check passed, the INSERT itself raised).


def _insert_reminder(cur, **overrides):
    kwargs = dict(
        notification_type=CALLBACK_REMINDER_TYPE,
        conflict_clause=_CONFLICT_CALLBACK_REMINDER,
        recipient_email="a@x.com",
        job_id="J1",
        jobdiva_id="JD1",
        candidate_id="C1",
        title="t",
        body=None,
        score=None,
        metadata={"callback_at": "2026-01-01T15:00:00+00:00"},
    )
    kwargs.update(overrides)
    return _insert_notification(cur, **kwargs)


def test_callback_reminder_for_a_rescheduled_time_still_notifies(pg_cursor):
    """The fix: a second callback reminder for the same (recipient,
    candidate, job) but a DIFFERENT callback_at — exactly what a reschedule
    produces — now inserts its own row instead of raising."""
    assert _insert_reminder(pg_cursor, metadata={"callback_at": "2026-01-01T15:00:00+00:00"}) is True
    assert _insert_reminder(pg_cursor, metadata={"callback_at": "2026-01-01T18:00:00+00:00"}) is True


def test_callback_reminder_exact_duplicate_still_dedupes_silently(pg_cursor):
    """A retried run of the exact same task_outbox row (same callback_at)
    must not double-notify — ON CONFLICT DO NOTHING, not an exception, same
    idempotency guarantee task_outbox's own dedupe_key gives the enqueue
    side."""
    same_metadata = {"callback_at": "2026-01-01T15:00:00+00:00"}
    assert _insert_reminder(pg_cursor, metadata=dict(same_metadata)) is True
    assert _insert_reminder(pg_cursor, metadata=dict(same_metadata)) is False


def test_callback_reminder_and_candidate_passed_have_independent_dedup_slots(pg_cursor):
    """`type` still isolates candidate_callback_reminder from the once-ever
    types sharing the same (recipient, candidate, job) — confirms widening
    the exclusion list didn't accidentally merge their dedup scopes."""
    assert _insert(pg_cursor, notification_type="candidate_passed") is True
    assert _insert_reminder(pg_cursor) is True
