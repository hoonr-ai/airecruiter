"""Unit coverage for services.task_outbox.add's `available_at` argument (PR
#841 review: added so a reminder can be queued for a future moment — e.g.
Callback Scheduled's "5 minutes before the recruiter's chosen time" — rather
than firing as soon as the relay next runs). `add` only ever touches the
caller's own cursor, so this needs no real Postgres connection, unlike
test_task_outbox_pg.py's end-to-end coverage of the relay itself.
"""
import json

import core.queue_tasks  # noqa: F401  (registers real tasks, incl. CANDIDATE_CALLBACK_REMINDER)
from core.queue_tasks import CANDIDATE_CALLBACK_REMINDER
from services import task_outbox


class _FakeCursor:
    def __init__(self, fetchone_result=({"id": 1})):
        self.fetchone_result = fetchone_result
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self.fetchone_result


def test_add_passes_available_at_through_to_the_insert():
    cur = _FakeCursor()
    task_outbox.add(cur, CANDIDATE_CALLBACK_REMINDER, "J1", "C1", "a@x.com", "2026-01-01T15:00:00+00:00",
                     available_at="2026-01-01T14:55:00+00:00", dedupe_key="k1")

    sql, params = cur.executed[0]
    assert "available_at" in sql
    task_name, args_json, dedupe_key, max_attempts, available_at = params
    assert task_name == CANDIDATE_CALLBACK_REMINDER
    assert json.loads(args_json) == ["J1", "C1", "a@x.com", "2026-01-01T15:00:00+00:00"]
    assert dedupe_key == "k1"
    assert available_at == "2026-01-01T14:55:00+00:00"


def test_add_omits_available_at_by_default():
    """The SQL's COALESCE(%s, now()) means a caller that doesn't pass
    available_at gets a row due immediately — same as every task queued
    before this argument existed."""
    cur = _FakeCursor()
    task_outbox.add(cur, CANDIDATE_CALLBACK_REMINDER, "J1", "C1", "a@x.com", "2026-01-01T15:00:00+00:00")

    _sql, params = cur.executed[0]
    assert params[-1] is None


def test_add_returns_none_without_inserting_a_row_on_dedupe_conflict():
    cur = _FakeCursor(fetchone_result=None)
    result = task_outbox.add(cur, CANDIDATE_CALLBACK_REMINDER, "J1", dedupe_key="k1")
    assert result is None
