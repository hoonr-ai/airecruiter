"""Regression tests for the dashboard's PAIR feedback metrics.

FEEDBACK COMPLETED read 0 for every job since it shipped. Two independent
defects produced that, and each is pinned below:

  1. `_count_feedback_metrics` (then `_count_feedback_completed`) ran on a
     RealDictCursor but consumed rows positionally. `ref_id, num_id =
     mj_row` unpacks a dict → binds the column NAMES, and `row[0]` on the
     COUNT row raises KeyError(0), which the bare `except` swallowed into
     `return 0`. Guaranteed zero for every job.
  2. The predicate required a non-empty `feedback_reason`. The UI only
     collects a reason on Reject, so every Submit was excluded even had
     the cursor been right.

Also covers PAIR SUBMITS (the new sibling column — what PAIR recorded,
reported next to the JobDiva-verified PAIR EXTERNAL SUBS) and the "don't
overwrite a real number with 0" guard on both JobDiva-derived counters.
"""
import asyncio
from typing import Any, Dict, List, Optional

import pytest

from services.auto_assign_service import auto_assign_service
from services.feedback_metrics import (
    FEEDBACK_COMPLETED_AGG_SQL,
    FEEDBACK_METRICS_SQL,
    PAIR_SUBMITS_AGG_SQL,
    has_recorded_feedback,
    is_pair_submit,
)


# --------------------------------------------------------------------------
# Fake DB plumbing. `as_dict` simulates a cursor handing back dict rows so a
# test can reproduce the original failure mode.
# --------------------------------------------------------------------------
class _FakeCursor:
    """Minimal cursor over an in-memory job + candidate fixture."""

    def __init__(self, job_row, candidates: List[Dict[str, Any]], as_dict: bool = False):
        self._job_row = job_row
        self._candidates = candidates
        self._as_dict = as_dict
        self._result = None

    def execute(self, sql: str, params=None):
        if sql.startswith("SET LOCAL"):
            self._result = None
            return
        if "FROM monitored_jobs" in sql:
            if self._job_row is None:
                self._result = None
            elif self._as_dict:
                self._result = {"jobdiva_id": self._job_row[0], "job_id": self._job_row[1]}
            else:
                self._result = tuple(self._job_row)
            return
        if "FROM sourced_candidates" in sql:
            ref_id, num_id = params
            completed, submits = set(), set()
            for row in self._candidates:
                if row["jobdiva_id"] not in (ref_id, num_id):
                    continue
                data = row.get("data")
                if has_recorded_feedback(data):
                    completed.add(row["candidate_id"])
                if is_pair_submit(data):
                    submits.add(row["candidate_id"])
            if self._as_dict:
                self._result = {"count": len(completed), "count_1": len(submits)}
            else:
                self._result = (len(completed), len(submits))
            return
        raise AssertionError(f"unexpected SQL: {sql}")

    def fetchone(self):
        return self._result

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _FakeConn:
    def __init__(self, cursor: _FakeCursor):
        self._cursor = cursor

    def cursor(self, cursor_factory=None):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


JOB_ROW = ("26-25865", "31920112")  # (jobdiva_id ref, job_id)

CANDIDATES = [
    # Submitted through PAIR — no reason is ever collected for a Submit.
    {"candidate_id": "c1", "jobdiva_id": "26-25865", "data": {"feedback_type": "Submit"}},
    # Rejected with a reason.
    {
        "candidate_id": "c2",
        "jobdiva_id": "26-25865",
        "data": {"feedback_type": "Reject", "feedback_reason": "Communication skills"},
    },
    # Same job, stored under the numeric key variant.
    {"candidate_id": "c3", "jobdiva_id": "31920112", "data": {"feedback_type": "Submit"}},
    # No decision recorded yet.
    {"candidate_id": "c4", "jobdiva_id": "26-25865", "data": {"engage_status": "completed"}},
    # Blank decision — must not count.
    {"candidate_id": "c5", "jobdiva_id": "26-25865", "data": {"feedback_type": "  "}},
    # A different job entirely.
    {"candidate_id": "c6", "jobdiva_id": "26-99999", "data": {"feedback_type": "Submit"}},
]


def _run_counter(monkeypatch, as_dict: bool = False, job_row=JOB_ROW, candidates=None):
    cursor = _FakeCursor(job_row, CANDIDATES if candidates is None else candidates, as_dict=as_dict)
    monkeypatch.setattr(
        auto_assign_service, "_get_db_connection", lambda: _FakeConn(cursor)
    )
    return asyncio.run(auto_assign_service._count_feedback_metrics("26-25865"))


# --------------------------------------------------------------------------
# The counter itself
# --------------------------------------------------------------------------
def test_counts_submits_and_rejects_across_both_key_variants(monkeypatch):
    """c1 (Submit), c2 (Reject), c3 (Submit under the numeric job key).

    FEEDBACK COMPLETED counts all three; PAIR SUBMITS counts the two
    Submits. Pre-fix both were 0 unconditionally.
    """
    assert _run_counter(monkeypatch) == {"feedback_completed": 3, "pair_submits": 2}


def test_submit_without_reason_still_counts(monkeypatch):
    """The old predicate required feedback_reason; a Submit never carries one."""
    only_submit = [
        {"candidate_id": "c1", "jobdiva_id": "26-25865", "data": {"feedback_type": "Submit"}}
    ]
    assert _run_counter(monkeypatch, candidates=only_submit) == {
        "feedback_completed": 1,
        "pair_submits": 1,
    }


def test_reject_counts_as_feedback_but_not_as_a_submit(monkeypatch):
    """The two columns must not move together — that's the point of showing both."""
    rejects_only = [
        {
            "candidate_id": "c1",
            "jobdiva_id": "26-25865",
            "data": {"feedback_type": "Reject", "feedback_reason": "Overqualified"},
        }
    ]
    assert _run_counter(monkeypatch, candidates=rejects_only) == {
        "feedback_completed": 1,
        "pair_submits": 0,
    }


def test_no_monitored_job_row_is_zero_not_none(monkeypatch):
    """A job that isn't monitored genuinely has no feedback — a real 0."""
    assert _run_counter(monkeypatch, job_row=None) == {
        "feedback_completed": 0,
        "pair_submits": 0,
    }


def test_db_failure_returns_none_so_caller_keeps_last_known(monkeypatch):
    """Indeterminate must not masquerade as zero."""

    def _boom():
        raise RuntimeError("pool exhausted")

    monkeypatch.setattr(auto_assign_service, "_get_db_connection", _boom)
    assert asyncio.run(auto_assign_service._count_feedback_metrics("26-25865")) is None


def test_counter_asks_for_a_plain_tuple_cursor(monkeypatch):
    """Pin the original defect at its source: the cursor factory.

    The counter reads both result rows positionally (`mj_row[0]`,
    `row[0]`). That is only valid on psycopg2's default tuple cursor. The
    original code passed `cursor_factory=RealDictCursor`, so
    `ref_id, num_id = mj_row` unpacked a dict and bound the column NAMES,
    and `row[0]` raised KeyError(0) into the bare `except` — a permanent 0
    in the dashboard's FEEDBACK COMPLETED column.

    Asserting on the requested factory catches the regression directly,
    rather than depending on what a fake happens to return.
    """
    requested: List[Any] = []
    cursor = _FakeCursor(JOB_ROW, CANDIDATES)

    class _FactoryRecordingConn(_FakeConn):
        def cursor(self, cursor_factory=None):
            requested.append(cursor_factory)
            return self._cursor

    monkeypatch.setattr(
        auto_assign_service, "_get_db_connection", lambda: _FactoryRecordingConn(cursor)
    )
    result = asyncio.run(auto_assign_service._count_feedback_metrics("26-25865"))

    assert result == {"feedback_completed": 3, "pair_submits": 2}
    assert requested == [None], (
        "the counter indexes rows positionally, so it must use the default "
        "tuple cursor; a RealDictCursor here returns dict rows and silently "
        "zeroes the metric"
    )


def test_realdict_rows_break_positional_access(monkeypatch):
    """Demonstrate why the factory matters — dict rows kill the count.

    This is the observed pre-fix behaviour reproduced against the fake:
    when the DB hands back dict rows, positional access raises and the
    counter reports 'indeterminate'. Kept so the reason for the assertion
    above is legible.
    """
    assert _run_counter(monkeypatch, as_dict=True) is None


# --------------------------------------------------------------------------
# The shared predicates
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "data,completed,submit",
    [
        ({"feedback_type": "Submit"}, True, True),
        ({"feedback_type": "submit"}, True, True),  # case-insensitive
        ({"feedback_type": " Submit "}, True, True),
        ({"feedback_type": "Reject", "feedback_reason": "Overqualified"}, True, False),
        ({"feedback_type": "Reject"}, True, False),  # a reject with no reason is still a decision
        ({"feedback_type": ""}, False, False),
        ({"feedback_type": "   "}, False, False),
        ({"feedback_type": None}, False, False),
        ({"feedback_reason": "Communication skills"}, False, False),  # reason alone isn't a decision
        ({}, False, False),
        (None, False, False),
    ],
)
def test_predicate_mirrors(data, completed, submit):
    assert has_recorded_feedback(data) is completed
    assert is_pair_submit(data) is submit


def test_sql_definitions_agree_with_the_mirrors():
    """Every SQL form must match the Python mirrors' definition."""
    for sql in (FEEDBACK_COMPLETED_AGG_SQL, PAIR_SUBMITS_AGG_SQL, FEEDBACK_METRICS_SQL):
        assert "feedback_type" in sql
        assert "feedback_reason" not in sql, "a Submit carries no reason"
        assert "DISTINCT" in sql, "a candidate under both job-key variants counts once"
    assert "'submit'" in PAIR_SUBMITS_AGG_SQL and "LOWER(" in PAIR_SUBMITS_AGG_SQL
    # The single-job query returns both counts in one round trip.
    assert FEEDBACK_METRICS_SQL.count("COUNT(DISTINCT") == 2


# --------------------------------------------------------------------------
# "Indeterminate is not zero" on the write path
# --------------------------------------------------------------------------
class _RecordingCursor:
    def __init__(self, job_row):
        self._job_row = job_row
        self.updates: List[str] = []
        self._result = None

    def execute(self, sql, params=None):
        if sql.startswith("SET LOCAL"):
            return
        if sql.startswith("SELECT job_id"):
            self._result = self._job_row
            return
        if sql.startswith("UPDATE monitored_jobs"):
            self.updates.append(sql)
            return
        raise AssertionError(f"unexpected SQL: {sql}")

    def fetchone(self):
        return self._result

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _RecordingConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = False

    def cursor(self, cursor_factory=None):
        return self._cursor

    def commit(self):
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _refresh_with(
    monkeypatch,
    ext_subs: Optional[int],
    feedback: Optional[Dict[str, int]],
    resolve_numeric: bool = True,
):
    cursor = _RecordingCursor(("31920112", "26-25865"))
    monkeypatch.setattr(
        auto_assign_service, "_get_db_connection", lambda: _RecordingConn(cursor)
    )

    async def _ext(*_a, **_k):
        return ext_subs

    async def _fb(*_a, **_k):
        return feedback

    async def _ttp(*_a, **_k):
        return 49.4

    monkeypatch.setattr(auto_assign_service, "_count_external_curate_submittals", _ext)
    monkeypatch.setattr(auto_assign_service, "_count_feedback_metrics", _fb)
    monkeypatch.setattr(auto_assign_service, "_calculate_time_to_first_pass", _ttp)
    monkeypatch.setattr(
        auto_assign_service,
        "_compute_candidate_counters",
        lambda _job: {
            "candidates_sourced": 5,
            "candidates_launched": 4,
            "complete_submissions": 3,
            "pass_submissions": 2,
        },
    )

    class _FakeJobDiva:
        async def _resolve_jobdiva_job_id(self, _job_id):
            return "31920112" if resolve_numeric else None

        async def get_job_submittals(self, _job_id, none_on_error=False):
            return None  # simulate a failed JobDiva fetch

    import services.jobdiva as jd_module

    monkeypatch.setattr(jd_module, "jobdiva_service", _FakeJobDiva())

    asyncio.run(auto_assign_service.refresh_job_performance_metrics("26-25865"))
    return cursor.updates


def test_determined_counters_are_written(monkeypatch):
    updates = _refresh_with(
        monkeypatch, ext_subs=2, feedback={"feedback_completed": 7, "pair_submits": 4}
    )
    assert len(updates) == 1
    assert "pair_external_subs = %s" in updates[0]
    assert "feedback_completed = %s" in updates[0]
    assert "pair_submits = %s" in updates[0]


def test_indeterminate_counters_are_not_written_as_zero(monkeypatch):
    """A JobDiva outage / DB error must leave the last-known values alone."""
    updates = _refresh_with(monkeypatch, ext_subs=None, feedback=None)
    assert len(updates) == 1
    assert "pair_external_subs" not in updates[0]
    assert "feedback_completed" not in updates[0]
    assert "pair_submits" not in updates[0]
    # The purely local metrics still get refreshed.
    assert "time_to_first_pass = %s" in updates[0]
    assert "pass_submissions = %s" in updates[0]


def test_unresolvable_jobdiva_id_still_writes_local_metrics(monkeypatch):
    """Pre-fix this returned early and wrote nothing at all.

    A job whose numeric JobDiva id can't be resolved (JobDiva down, or a
    locally-created job) still has valid local metrics — feedback, PAIR
    submits, time-to-first-pass and the candidate counters.
    """
    updates = _refresh_with(
        monkeypatch,
        ext_subs=None,
        feedback={"feedback_completed": 7, "pair_submits": 4},
        resolve_numeric=False,
    )
    assert len(updates) == 1
    assert "feedback_completed = %s" in updates[0]
    assert "pair_submits = %s" in updates[0]
    assert "time_to_first_pass = %s" in updates[0]
    # No numeric id → nothing JobDiva-derived to write.
    assert "pair_external_subs" not in updates[0]
