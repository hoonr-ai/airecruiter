"""services/launch_timing: T0 (added) / T1 (first Launch click) / T2 (first
interview created), the A and B durations, the stamps, the New Relic event and
the one-time backfill.

The unit tests use fakes. The Postgres tests skip unless a server is reachable
(set LAUNCH_REPORT_TEST_DSN, e.g. to a pgserver instance); they run the real
DDL, backfill SQL, reads and stamps against temp tables, including the two
created_at shapes (" IST" India wall clock and NOW() UTC) and garbage values.
"""

import datetime
import os
from typing import Any, List, Optional
from unittest.mock import MagicMock

import psycopg2
import psycopg2.extensions
import pytest

from services import launch_timing as lt

UTC = datetime.timezone.utc
D = datetime.datetime


def _utc(*args) -> datetime.datetime:
    return D(*args, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Schema statements
# ---------------------------------------------------------------------------
def test_schema_statements_are_ddl_only():
    """The backfill must never ride in the startup schema step: a long UPDATE
    there holds the 10 s step open and queues the next worker's ALTERs."""
    assert len(lt.SCHEMA_STATEMENTS) == 1
    (stmt,) = lt.SCHEMA_STATEMENTS
    upper = stmt.upper()
    assert "UPDATE" not in upper
    assert "INSERT" not in upper and "DELETE" not in upper
    # Later boots take no lock: the catalog is checked before any ALTER, and
    # the lock wait is capped — once, for all three columns (one block, one
    # transaction), so the wait never exceeds 5 s of the 10 s startup step.
    assert "PG_ATTRIBUTE" in upper
    assert upper.count("LOCK_TIMEOUT") == 1 and "'5000MS'" in upper
    assert upper.count("DO $$") == 1
    for col in lt.TIMING_COLUMNS:
        assert f"ADD COLUMN IF NOT EXISTS {col} TIMESTAMPTZ NULL" in stmt
    # pair_added_at gets its default in a later statement of the same block,
    # so existing rows stay NULL; the other two have none.
    assert stmt.count("SET DEFAULT") == 1
    assert "ALTER COLUMN pair_added_at SET DEFAULT now()" in stmt
    assert stmt.index("ADD COLUMN IF NOT EXISTS pair_added_at") < stmt.index("SET DEFAULT now()")


def test_schema_statements_are_registered_with_monitored_jobs_bootstrap():
    import inspect

    from routers import jobs

    src = inspect.getsource(jobs._ensure_monitored_jobs_schema)
    assert "*LAUNCH_TIMING_SCHEMA_STATEMENTS" in src
    assert jobs.LAUNCH_TIMING_SCHEMA_STATEMENTS is lt.SCHEMA_STATEMENTS


def test_first_launch_sql_is_the_launch_report_rule():
    sql = " ".join(lt.first_launch_sql("scoped").split())
    assert "%" not in sql
    assert "FROM scoped mj" in sql
    # Both job keys, as equality lookups, never one OR join.
    assert "a.jobdiva_id = NULLIF(mj.jobdiva_id, '')" in sql
    assert "a.jobdiva_id = mj.job_id::text" in sql
    assert " OR " not in sql
    assert "UNION ALL" in sql
    assert sql.count("NULLIF(a.jobdiva_id, '') IS NOT NULL") == 2
    assert sql.count("COALESCE(NULLIF(a.interview_id, ''), '') <> ''") == 2
    assert sql.count("COALESCE(NULLIF(a.candidate_id, ''), '') <> ''") == 2
    assert "MIN(a.created_at) AS first_launch_at" in sql and "GROUP BY mj.job_id" in sql


def test_launch_report_uses_the_shared_rule():
    from routers import launch_report as lr

    assert lr.first_launch_sql is lt.first_launch_sql


# ---------------------------------------------------------------------------
# Pure timing math
# ---------------------------------------------------------------------------
def _session(**overrides):
    row = {
        "id": "s-1",
        "job_id": "31990001",
        "trigger": "button",
        "mode": "full_search",
        "status": "completed",
        "attempt": 1,
        "requested_by": "priya.recruiter@example.com",
        "clicked_at": _utc(2026, 10, 1, 10, 0),
        "sourcing_started_at": _utc(2026, 10, 1, 10, 0, 5),
        "sourcing_done_at": _utc(2026, 10, 1, 10, 4, 5),
        "selection_done_at": _utc(2026, 10, 1, 10, 4, 6),
        "enrich_done_at": _utc(2026, 10, 1, 10, 5, 6),
        "save_started_at": _utc(2026, 10, 1, 10, 6),
        "save_done_at": _utc(2026, 10, 1, 10, 7),
        "launch_started_at": _utc(2026, 10, 1, 10, 7),
        "first_interview_created_at": _utc(2026, 10, 1, 10, 9),
        "last_batch_done_at": _utc(2026, 10, 1, 10, 12),
        "human_wait_ms": 30_000,
        "counts": {"pool": 120, "selected": 50, "sent": 48, "skipped": {"no_contact": 2}},
    }
    row.update(overrides)
    return row


def test_session_timing_breaks_b_into_stages():
    t = lt.session_timing(_session())
    assert t["click_to_launch_seconds"] == 540.0
    assert t["machine_seconds"] == 510.0  # minus the 30 s QA review
    assert (t["sourcing_s"], t["selection_s"], t["enrichment_s"]) == (240.0, 1.0, 60.0)
    assert (t["human_wait_s"], t["saving_s"], t["pairbot_s"], t["launch_total_s"]) == (30.0, 60.0, 120.0, 300.0)
    assert t["session_id"] == "s-1" and t["trigger"] == "button" and t["mode"] == "full_search"


def test_session_timing_reads_to_jsonb_strings_and_degrades_to_none():
    """launch_sessions rows read through to_jsonb carry ISO strings; a missing
    or backwards stage is None, never negative."""
    t = lt.session_timing({
        "id": "s-2",
        "clicked_at": "2026-10-01T10:00:00+05:30",
        "first_interview_created_at": "2026-10-01T04:40:00+00:00",
        "save_started_at": "2026-10-01T04:39:00Z",
        "save_done_at": "2026-10-01T04:38:00Z",  # backwards
        "review_started_at": "2026-10-01T04:31:00+00:00",
        "review_done_at": "2026-10-01T04:33:00+00:00",
    })
    assert t["click_to_launch_seconds"] == 600.0
    assert t["saving_s"] is None
    assert t["sourcing_s"] is None and t["pairbot_s"] is None
    assert t["human_wait_s"] == 120.0  # no human_wait_ms: review span instead
    assert t["machine_seconds"] == 480.0


def test_provided_mode_selection_starts_at_the_click():
    t = lt.session_timing(_session(mode="provided", sourcing_started_at=None, sourcing_done_at=None))
    assert t["sourcing_s"] is None
    assert t["selection_s"] == 246.0  # click → selection_done_at


def test_job_timing_b_comes_from_the_first_session_that_launched():
    """A failed first click must not be charged to B: B starts at the click
    that produced the first interview. A still ends at the first click."""
    failed = lt.session_timing(_session(id="s-fail", clicked_at=_utc(2026, 10, 1, 9, 0),
                                        first_interview_created_at=None))
    ok = lt.session_timing(_session(id="s-ok"))
    t = lt._job_timing(
        "31990001", "26-001",
        added=_utc(2026, 10, 1, 8, 0),
        clicked=_utc(2026, 10, 1, 9, 0),
        stamped_launch=None,
        audit_launch=_utc(2026, 10, 1, 10, 9),
        sessions=[failed, ok],
    )
    assert t["added_to_click_seconds"] == 3600.0 and t["added_to_click_minutes"] == 60.0
    assert t["click_to_launch_seconds"] == 540.0 and t["click_to_launch_minutes"] == 9.0
    assert t["click_to_launch_source"] == "session"
    assert t["first_launch_session"]["session_id"] == "s-ok"


def test_job_timing_falls_back_to_the_job_level_span_for_legacy_launches():
    t = lt._job_timing("1", "", _utc(2026, 10, 1, 8), _utc(2026, 10, 1, 9), None, _utc(2026, 10, 1, 9, 30), [])
    assert (t["click_to_launch_minutes"], t["click_to_launch_source"]) == (30.0, "job")
    assert t["jobdiva_id"] is None


def test_job_timing_prefers_the_audit_over_the_stamped_column():
    t = lt._job_timing("1", "r", None, _utc(2026, 10, 1, 9), _utc(2026, 10, 1, 9, 31), _utc(2026, 10, 1, 9, 30), [])
    assert t["first_launch_at"] == _utc(2026, 10, 1, 9, 30)
    t = lt._job_timing("1", "r", None, _utc(2026, 10, 1, 9), _utc(2026, 10, 1, 9, 31), None, [])
    assert t["first_launch_at"] == _utc(2026, 10, 1, 9, 31)


def test_job_timing_negative_spans_are_not_durations():
    """A job launched before the click tracking existed and clicked again
    later: no negative or zero B, and no A without both ends."""
    t = lt._job_timing("1", "r", None, _utc(2026, 10, 2), None, _utc(2026, 10, 1), [])
    assert t["click_to_launch_seconds"] is None and t["click_to_launch_source"] is None
    assert t["added_to_click_seconds"] is None
    assert t["first_launch_clicked_at"] is None and t["click_recorded_after_launch"] is True


def test_a_click_recorded_after_the_first_launch_is_not_the_first_click():
    """Review finding: a job added 08-01 and launched 08-02 (before the click
    tracking; T2 backfilled) is relaunched on 10-11. That click is not its
    first: no 71-day Added → Click, no Launch Clicked after PAIR Launch, and
    the relaunch session's 20 minutes are not the job's Click → Launch."""
    relaunch = lt.session_timing(_session(
        id="s-relaunch", clicked_at=_utc(2026, 10, 11, 10), launch_started_at=_utc(2026, 10, 11, 10, 15),
        first_interview_created_at=_utc(2026, 10, 11, 10, 20),
    ))
    t = lt._job_timing(
        "100", "26-00100",
        added=_utc(2026, 8, 1, 10),
        clicked=_utc(2026, 10, 11, 10),
        stamped_launch=_utc(2026, 8, 2, 10),
        audit_launch=_utc(2026, 8, 2, 10),
        sessions=[relaunch],
    )
    assert t["first_launch_at"] == _utc(2026, 8, 2, 10)
    assert t["first_launch_clicked_at"] is None and t["click_recorded_after_launch"] is True
    assert t["added_to_click_seconds"] is None and t["added_to_click_minutes"] is None
    assert t["click_to_launch_seconds"] is None and t["click_to_launch_source"] is None
    assert t["first_launch_session"] is None
    # No click recorded at all: the same answer, without the flag.
    t = lt._job_timing("100", "26-00100", _utc(2026, 8, 1, 10), None, None, _utc(2026, 8, 2, 10), [relaunch])
    assert t["click_to_launch_seconds"] is None and t["click_recorded_after_launch"] is False


def test_the_session_that_launched_the_job_stands_in_for_a_lost_click_stamp():
    """Every session is a click: when the launching session's T1 stamp was
    lost and only a later click is on record, the session's click is T1."""
    ok = lt.session_timing(_session(id="s-ok"))  # clicked 10:00, interview 10:09
    t = lt._job_timing(
        "31990001", "26-001",
        added=_utc(2026, 10, 1, 8),
        clicked=_utc(2026, 10, 1, 10, 30),  # a later click; after T2
        stamped_launch=None,
        audit_launch=_utc(2026, 10, 1, 10, 9),
        sessions=[ok],
    )
    assert t["first_launch_clicked_at"] == _utc(2026, 10, 1, 10)
    assert t["click_recorded_after_launch"] is False
    assert t["added_to_click_minutes"] == 120.0
    assert (t["click_to_launch_minutes"], t["click_to_launch_source"]) == (9.0, "session")
    # A recorded click no later than the session's is kept.
    t = lt._job_timing("31990001", "26-001", _utc(2026, 10, 1, 8), _utc(2026, 10, 1, 9), None,
                       _utc(2026, 10, 1, 10, 9), [ok])
    assert t["first_launch_clicked_at"] == _utc(2026, 10, 1, 9) and t["added_to_click_minutes"] == 60.0


def test_sessions_sql_reads_only_the_listed_columns():
    sql = lt._sessions_sql({"job_id", "id", "clicked_at", "first_interview_created_at", "input", "error"},
                           launched_only=True)
    assert "input" not in sql and "error" not in sql and "checkpoint" not in sql and "to_jsonb" not in sql
    assert 's."clicked_at"' in sql and 's."trigger"' not in sql and "NULL" in sql
    assert sql.count(",") == len(lt.SESSION_COLUMNS)  # job_id + one per column
    assert sql.endswith("AND s.first_interview_created_at IS NOT NULL")
    assert "IS NOT NULL" not in lt._sessions_sql({"job_id"}, launched_only=False)
    # A table that cannot answer is skipped, not queried into an error.
    assert lt._sessions_sql({"id", "clicked_at"}, launched_only=False) is None
    assert lt._sessions_sql({"job_id", "clicked_at"}, launched_only=True) is None


# ---------------------------------------------------------------------------
# New Relic event
# ---------------------------------------------------------------------------
def test_record_timing_event_sends_ids_durations_and_counts_only(monkeypatch):
    from core import newrelic

    sent = []
    monkeypatch.setattr(newrelic, "record_custom_event", lambda event, params=None: sent.append((event, params)))
    attrs = lt.record_timing_event(
        _session(error="Pairbot said no for jane@example.com", contact={"phone": "5551234567"}),
        job_added_at=_utc(2026, 10, 1, 9, 0),
        job_first_launch_at=_utc(2026, 10, 1, 10, 9),   # this session created the job's first interview
    )
    assert sent == [("PairLaunchTiming", attrs)]
    assert attrs["job_id"] == "31990001" and attrs["session_id"] == "s-1"
    assert attrs["added_to_click_s"] == 3600.0
    assert attrs["click_to_launch_s"] == 540.0 and attrs["pairbot_s"] == 120.0
    assert attrs["count_pool"] == 120 and attrs["count_skipped_no_contact"] == 2
    # No PII and nothing free-text: only ids, enums, numbers.
    blob = repr(attrs)
    for leak in ("priya", "@", "jane", "5551234567", "requested_by", "error"):
        assert leak not in blob
    assert all(isinstance(v, (str, int, float)) for v in attrs.values())
    assert all(v is not None for v in attrs.values())


def test_record_timing_event_marks_a_relaunch_and_drops_its_added_to_click(monkeypatch):
    """A session clicked after the job's first launch is a relaunch: its
    added → click is not the job's A, so the event must not carry one."""
    from core import newrelic

    monkeypatch.setattr(newrelic, "record_custom_event", lambda *_a, **_k: None)
    added = _utc(2026, 8, 1)
    relaunch = lt.record_timing_event(_session(), job_added_at=added, job_first_launch_at=_utc(2026, 8, 2))
    assert relaunch["relaunch"] is True and "added_to_click_s" not in relaunch
    assert relaunch["click_to_launch_s"] == 540.0  # the session's own B still goes out
    # This session produced the first launch (T2 after its click): not a relaunch.
    first = lt.record_timing_event(_session(), job_added_at=added, job_first_launch_at=_utc(2026, 10, 1, 10, 9))
    assert first["relaunch"] is False and first["first_launch"] is True
    assert first["added_to_click_s"] == 5_306_400.0  # 61 days
    # No T2 (the session launched nobody, or it was not readable): relaunch is
    # still a boolean, and the job-level A is not sent — a session that
    # launched nobody and its needs-contact child must not both report it.
    nobody = lt.record_timing_event(_session(counts={"sent": 0}), job_added_at=added)
    assert nobody["relaunch"] is False and nobody["first_launch"] is False
    assert "added_to_click_s" not in nobody
    assert "added_to_click_s" not in lt.record_timing_event(_session(), job_added_at=added)


def test_record_timing_event_uses_the_rows_own_added_at_and_never_raises(monkeypatch):
    from core import newrelic

    def boom(*_a, **_k):
        raise RuntimeError("agent exploded")

    monkeypatch.setattr(newrelic, "record_custom_event", boom)
    attrs = lt.record_timing_event(
        _session(job_added_at="2026-10-01T09:30:00+00:00"), job_first_launch_at=_utc(2026, 10, 1, 10, 9),
    )
    assert attrs["added_to_click_s"] == 1800.0
    # A row that is not even a mapping: logged, None, no exception.
    assert lt.record_timing_event(None) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Stamps (fakes)
# ---------------------------------------------------------------------------
class _StampConn:
    def __init__(self, row=("31990001",), fail: Optional[Exception] = None):
        self.cur = MagicMock()
        self.cur.fetchone.return_value = row
        if fail is not None:
            def execute(sql, params=None):
                if "UPDATE" in sql:
                    raise fail
            self.cur.execute.side_effect = execute
        self.committed = self.closed = False

    def cursor(self):
        ctx = MagicMock()
        ctx.__enter__.return_value = self.cur
        return ctx

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


def test_stamp_launch_clicked_resolves_either_key_and_keeps_the_earliest(monkeypatch):
    conn = _StampConn()
    monkeypatch.setattr(lt, "_connect", lambda: conn)
    assert lt.stamp_launch_clicked("26-001") == "31990001"
    sql, params = conn.cur.execute.call_args_list[-1][0]
    sql = " ".join(sql.split())
    assert "SET first_launch_clicked_at = COALESCE(%s::timestamptz, now())" in sql
    # Written only when blank or earlier: per-batch callers don't churn the row.
    assert "(mj.first_launch_clicked_at IS NULL OR mj.first_launch_clicked_at > COALESCE(%s::timestamptz, now()))" in sql
    # ...and never after the job's first launch: a relaunch is not the first click.
    assert "(mj.first_launch_at IS NULL OR mj.first_launch_at >= COALESCE(%s::timestamptz, now()))" in sql
    assert "job_id::text = %s OR jobdiva_id = %s" in sql
    # The launch code's own tie-break (routers/engagement.py job lookup).
    assert "ORDER BY (job_id::text ~ '^[0-9]+$') DESC, created_at DESC" in sql
    assert params == ("26-001", "26-001", None, None, None)
    assert conn.committed and conn.closed


def test_stamp_first_launch_reads_a_naive_at_as_the_audit_zone(monkeypatch):
    conn = _StampConn()
    monkeypatch.setattr(lt, "_connect", lambda: conn)
    monkeypatch.setattr(lt, "_DB_TIMEZONE", "America/New_York")
    lt.stamp_first_launch("31990001", at=D(2026, 10, 1, 10, 0))
    sql, params = conn.cur.execute.call_args_list[-1][0]
    assert "SET first_launch_at = COALESCE(%s::timestamptz, now())" in sql
    assert params[2] == params[3] == D(2026, 10, 1, 14, 0, tzinfo=UTC)
    assert len(params) == 4
    # The click guard is T1's alone.
    assert ">= COALESCE" not in sql


@pytest.mark.parametrize("args", [
    (MagicMock(name="conn"), "31990001"),      # the spec's old (conn, job_id, at) shape
    ("31990001", "2026-10-01T10:00:00Z"),      # at as a string
    (True, None),
    (b"31990001", None),
])
def test_stamps_reject_a_wrong_call_loudly(monkeypatch, args):
    """A misuse is a bug in the caller: it must fail in its tests, not log a
    warning and lose T1 / T2 for good."""
    monkeypatch.setattr(lt, "_connect", lambda: pytest.fail("a wrong call reached the DB"))
    for stamp in (lt.stamp_launch_clicked, lt.stamp_first_launch):
        with pytest.raises(TypeError):
            stamp(*args)


def test_stamps_accept_an_int_job_id(monkeypatch):
    conn = _StampConn()
    monkeypatch.setattr(lt, "_connect", lambda: conn)
    assert lt.stamp_first_launch(31990001, at=_utc(2026, 10, 1)) == "31990001"
    assert conn.cur.execute.call_args_list[-1][0][1][:2] == ("31990001", "31990001")


def test_stamps_are_fail_open(monkeypatch):
    conn = _StampConn(fail=psycopg2.errors.UndefinedColumn("column first_launch_at does not exist"))
    monkeypatch.setattr(lt, "_connect", lambda: conn)
    assert lt.stamp_first_launch("31990001") is None
    assert conn.closed

    def no_pool():
        raise RuntimeError("pool exhausted")

    monkeypatch.setattr(lt, "_connect", no_pool)
    assert lt.stamp_launch_clicked("31990001") is None
    # Blank keys never touch the DB.
    monkeypatch.setattr(lt, "_connect", lambda: pytest.fail("connected for a blank key"))
    assert lt.stamp_launch_clicked("  ") is None and lt.stamp_first_launch(None) is None


def test_stamp_unknown_job_returns_none(monkeypatch):
    monkeypatch.setattr(lt, "_connect", lambda: _StampConn(row=None))
    assert lt.stamp_launch_clicked("nope") is None


def test_timings_for_jobs_skips_blank_input_without_a_connection(monkeypatch):
    monkeypatch.setattr(lt, "_connect", lambda: pytest.fail("connected for nothing"))
    assert lt.timings_for_jobs([None, "", "  "]) == {}


# ---------------------------------------------------------------------------
# Backfill (fakes): it must never raise and never loop forever
# ---------------------------------------------------------------------------
class _DataError(Exception):
    pgcode = "22008"  # datetime field overflow


class _ScriptedConn:
    """Dispatches on SQL text. ``ids`` is the keyset the id SELECT pages over."""

    def __init__(self, ids: List[str], *, batch_error: Optional[Exception] = None,
                 bad_row: Optional[str] = None, ids_error: Optional[Exception] = None):
        self.ids = sorted(ids)
        self.batch_error = batch_error
        self.bad_row = bad_row
        self.ids_error = ids_error
        self.updates: List[Any] = []
        self.rollbacks = 0
        self.closed = False
        self._rows: List[Any] = []
        self.rowcount = 0

    # cursor protocol
    def cursor(self):
        conn = self

        class _Cur:
            rowcount = 0

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def execute(self, sql, params=None):
                conn._execute(self, sql, params)

            def fetchall(self):
                return conn._rows

            def fetchone(self):
                return conn._rows[0] if conn._rows else None

        return _Cur()

    def _execute(self, cur, sql, params):
        if "FROM pg_attribute" in sql:
            self._rows = [("job_id",), ("pair_added_at",)]  # no first_launch_at column yet
        elif "to_regclass('engage_interview_audit')" in sql:
            self._rows = [(True, False)]
        elif sql.startswith("SELECT job_id::text FROM monitored_jobs"):
            if self.ids_error:
                raise self.ids_error
            after, limit = params[1], params[2]
            self._rows = [(i,) for i in self.ids if i > after][:limit]
        elif sql.startswith("UPDATE monitored_jobs SET pair_added_at"):
            target = params[0]
            if isinstance(target, list):
                if self.batch_error and (self.bad_row is None or self.bad_row in target):
                    raise self.batch_error
                self.updates.append(list(target))
                cur.rowcount = len(target)
            else:
                if target == self.bad_row:
                    raise self.batch_error
                self.updates.append(target)
                cur.rowcount = 1

    def commit(self):
        pass

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


def test_backfill_pages_with_a_keyset_and_skips_a_columnless_backfill():
    conn = _ScriptedConn(["a", "b", "c", "d", "e"])
    summary = lt.backfill_once(batch_size=2, connect=lambda: conn)
    assert conn.updates == [["a", "b"], ["c", "d"], ["e"]]
    assert summary["pair_added_at"] == 5 and summary["batches"] == 3
    # first_launch_at is not in the catalog yet: that half is skipped, not failed.
    assert summary["first_launch_at"] == 0 and summary["failed_batches"] == 0
    assert conn.closed


def test_backfill_retries_a_batch_with_an_impossible_date_row_by_row():
    conn = _ScriptedConn(["a", "b", "c"], batch_error=_DataError("date/time field value out of range"), bad_row="b")
    summary = lt.backfill_once(connect=lambda: conn)
    assert conn.updates == ["a", "c"]
    assert summary["pair_added_at"] == 2 and summary["unparseable_rows"] == 1


def test_backfill_skips_a_locked_batch_and_moves_on():
    conn = _ScriptedConn(["a", "b", "c", "d"], batch_error=RuntimeError("lock timeout"), bad_row="a")
    summary = lt.backfill_once(batch_size=2, connect=lambda: conn)
    assert conn.updates == [["c", "d"]]
    assert summary["failed_batches"] == 1 and summary["unparseable_rows"] == 0


def test_backfill_stops_when_it_cannot_page_and_never_raises():
    conn = _ScriptedConn(["a"], ids_error=RuntimeError("statement timeout"))
    assert lt.backfill_once(connect=lambda: conn)["pair_added_at"] == 0
    assert conn.closed

    def no_pool():
        raise RuntimeError("pool exhausted")

    assert lt.backfill_once(connect=no_pool) == {
        "pair_added_at": 0, "first_launch_at": 0, "batches": 0, "failed_batches": 0, "unparseable_rows": 0,
    }


# ---------------------------------------------------------------------------
# Postgres
# ---------------------------------------------------------------------------
_TEST_DSN = os.getenv("LAUNCH_REPORT_TEST_DSN", "dbname=postgres")

_FIXTURE_SQL = """
CREATE TEMP TABLE monitored_jobs (
    job_id TEXT PRIMARY KEY,
    jobdiva_id TEXT,
    title TEXT,
    parent_job_id TEXT,
    version INTEGER DEFAULT 1,
    created_at TEXT,
    updated_at TEXT,
    pair_launched_at TIMESTAMP NULL
) ON COMMIT DROP;
CREATE TEMP TABLE engage_interview_audit (
    id SERIAL PRIMARY KEY,
    candidate_id TEXT,
    jobdiva_id TEXT,
    interview_id TEXT,
    status TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ON COMMIT DROP;
"""

# Spec columns of launch_sessions (Unit D) that the reads use.
_SESSIONS_SQL = """
CREATE TEMP TABLE launch_sessions (
    id UUID PRIMARY KEY,
    job_id TEXT NOT NULL,
    trigger TEXT NOT NULL DEFAULT 'button',
    mode TEXT NOT NULL DEFAULT 'full_search',
    status TEXT NOT NULL DEFAULT 'completed',
    input JSONB NOT NULL DEFAULT '{}',
    checkpoint JSONB NOT NULL DEFAULT '{}',
    counts JSONB NOT NULL DEFAULT '{}',
    error TEXT,
    attempt INT NOT NULL DEFAULT 0,
    clicked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    launch_started_at TIMESTAMPTZ,
    first_interview_created_at TIMESTAMPTZ,
    human_wait_ms BIGINT NOT NULL DEFAULT 0
) ON COMMIT DROP;
"""


class _Session:
    """The test's one session standing in for pool connections (the temp
    tables live there): commit/rollback/close work on a savepoint so the
    fixture data survives and a failed borrower cannot poison the session."""

    def __init__(self, conn):
        self._conn = conn
        self._exec("SAVEPOINT lt_pool")

    def _exec(self, sql):
        with self._conn.cursor() as cur:
            cur.execute(sql)

    def cursor(self, *args, **kwargs):
        return self._conn.cursor(*args, **kwargs)

    def commit(self):
        self._exec("RELEASE SAVEPOINT lt_pool")
        self._exec("SAVEPOINT lt_pool")

    def rollback(self):
        self._exec("ROLLBACK TO SAVEPOINT lt_pool")

    def close(self):
        if self._conn.get_transaction_status() == psycopg2.extensions.TRANSACTION_STATUS_INERROR:
            self.rollback()
        self._exec("RELEASE SAVEPOINT lt_pool")


@pytest.fixture()
def pg():
    try:
        conn = psycopg2.connect(_TEST_DSN, connect_timeout=3)
    except psycopg2.OperationalError as exc:
        pytest.skip(f"no Postgres reachable at {_TEST_DSN!r}: {exc}")
    try:
        with conn.cursor() as cur:
            # Deliberately NOT UTC: nothing here may depend on the session zone.
            cur.execute("SET TIME ZONE 'Asia/Kolkata'")
            cur.execute(_FIXTURE_SQL)
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _apply_schema(pg):
    with pg.cursor() as cur:
        for stmt in lt.SCHEMA_STATEMENTS:
            cur.execute(stmt)


def _cols(pg, job_id, *cols):
    with pg.cursor() as cur:
        cur.execute(f"SELECT {', '.join(cols)} FROM monitored_jobs WHERE job_id = %s", (job_id,))
        return cur.fetchone()


def test_pg_schema_leaves_existing_rows_blank_and_stamps_new_ones(pg):
    with pg.cursor() as cur:
        cur.execute("INSERT INTO monitored_jobs (job_id, created_at) VALUES ('old', '2026-09-01 10:00:00')")
    for _ in range(2):  # re-running is a no-op
        _apply_schema(pg)
    with pg.cursor() as cur:
        # An INSERT that names none of the new columns — every legacy site.
        cur.execute("INSERT INTO monitored_jobs (job_id, created_at) VALUES ('new', NOW())")
        cur.execute("SELECT now()")
        now = cur.fetchone()[0]
    assert _cols(pg, "old", *lt.TIMING_COLUMNS) == (None, None, None)
    assert _cols(pg, "new", *lt.TIMING_COLUMNS) == (now, None, None)
    with pg.cursor() as cur:
        cur.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = 'monitored_jobs' AND column_name = ANY(%s) ORDER BY column_name",
            (list(lt.TIMING_COLUMNS),),
        )
        assert {r[1] for r in cur.fetchall()} == {"timestamp with time zone"}


def test_pg_schema_restores_a_missing_default(pg):
    _apply_schema(pg)
    with pg.cursor() as cur:
        cur.execute("ALTER TABLE monitored_jobs ALTER COLUMN pair_added_at DROP DEFAULT")
    _apply_schema(pg)
    with pg.cursor() as cur:
        cur.execute("INSERT INTO monitored_jobs (job_id) VALUES ('x')")
    assert _cols(pg, "x", "pair_added_at")[0] is not None


def test_pg_postgres_reads_ist_as_israel_which_is_why_the_backfill_never_casts_it(pg):
    """The trap the backfill avoids: '… IST'::timestamptz is Israel (+2), not
    India (+5:30), under Postgres' default abbreviation set."""
    with pg.cursor() as cur:
        cur.execute("SELECT '2026-01-21 19:16:55 IST'::timestamptz")
        naive_cast = cur.fetchone()[0]
    india = _utc(2026, 1, 21, 13, 46, 55)
    assert naive_cast != india


def _seed_backfill(pg):
    with pg.cursor() as cur:
        cur.execute("""
            INSERT INTO monitored_jobs (job_id, jobdiva_id, created_at) VALUES
              ('31990001', '26-001', '2026-09-21 19:16:55 IST'),    -- India wall clock
              ('31990002', '26-002', '2026-09-21 13:46:55'),        -- NOW() UTC
              ('31990003', '26-003', '2026-09-21 13:46:55.123456'),
              ('31990004', '26-004', '2026-09-21T13:46:55'),
              ('31990005', '26-005', '2026-02-30 10:00:00'),        -- shape-valid, impossible
              ('31990006', '26-006', 'not a date'),
              ('31990007', '26-007', ''),
              ('31990008', '26-008', NULL),
              ('60', '', '2026-09-22 09:00:00'),                     -- referenceless
              ('61', '', '2026-09-22 09:00:00'),
              ('31990009', '26-009', '2026-09-22 09:00:00')         -- already stamped below
        """)
        cur.execute("""
            INSERT INTO engage_interview_audit (candidate_id, jobdiva_id, interview_id, status, created_at) VALUES
              ('c1', '26-001', '',    'failed',    '2026-09-22 09:00:00'),   -- failed first attempt
              ('c1', '26-001', NULL,  'failed',    '2026-09-22 09:30:00'),
              ('c2', '26-001', '901', 'Initiated', '2026-09-22 10:00:00'),   -- first success, by ref
              ('c3', '31990001', '902', 'Initiated', '2026-09-22 11:00:00'), -- by job_id
              ('',   '26-002', '903', 'Initiated', '2026-09-22 08:00:00'),   -- no candidate: not a launch
              ('g1', '60',     '960', 'Initiated', '2026-09-22 12:00:00'),   -- referenceless, by job_id
              ('g2', '',       '961', 'Initiated', '2026-09-22 07:00:00'),   -- blank key: matches nothing
              ('c9', '26-009', '909', 'Initiated', '2026-09-22 06:00:00')
        """)
    _apply_schema(pg)
    with pg.cursor() as cur:
        cur.execute(
            "UPDATE monitored_jobs SET pair_added_at = '2026-01-01T00:00:00Z', "
            "first_launch_at = '2026-01-02T00:00:00Z' WHERE job_id = '31990009'"
        )


@pytest.mark.parametrize("batch_size", [500, 2])
def test_pg_backfill_fills_both_columns_once(pg, batch_size):
    _seed_backfill(pg)
    summary = lt.backfill_once(batch_size=batch_size, connect=lambda: _Session(pg))

    truth = _utc(2026, 9, 21, 13, 46, 55)
    added = {j: _cols(pg, j, "pair_added_at")[0] for j in
             ("31990001", "31990002", "31990003", "31990004", "31990005", "31990006", "31990007", "31990008")}
    assert added["31990001"] == truth           # " IST" read as India, not Israel
    assert added["31990002"] == truth
    assert added["31990003"] == truth           # fraction truncated, like _ts()
    assert added["31990004"] == truth
    assert added["31990005"] is None            # Feb 30: only that row stays blank
    assert added["31990006"] is None and added["31990007"] is None and added["31990008"] is None
    assert summary["unparseable_rows"] == 1

    launched = {j: _cols(pg, j, "first_launch_at")[0] for j in ("31990001", "31990002", "60", "61")}
    assert launched["31990001"] == _utc(2026, 9, 22, 10)   # failed attempts skipped, both keys
    assert launched["31990002"] is None                     # no candidate id: not a launch
    assert launched["60"] == _utc(2026, 9, 22, 12)
    assert launched["61"] is None                           # '' guard
    # Never overwrites.
    assert _cols(pg, "31990009", "pair_added_at", "first_launch_at") == (
        _utc(2026, 1, 1), _utc(2026, 1, 2),
    )

    again = lt.backfill_once(batch_size=batch_size, connect=lambda: _Session(pg))
    assert again["pair_added_at"] == 0 and again["first_launch_at"] == 0


def test_pg_backfill_reads_the_audit_in_the_report_db_zone(pg, monkeypatch):
    monkeypatch.setattr(lt, "_DB_TIMEZONE", "America/New_York")
    _seed_backfill(pg)
    lt.backfill_once(connect=lambda: _Session(pg))
    assert _cols(pg, "31990001", "first_launch_at")[0] == _utc(2026, 9, 22, 14)


def test_pg_backfill_without_the_columns_is_a_no_op(pg):
    with pg.cursor() as cur:
        cur.execute("INSERT INTO monitored_jobs (job_id, created_at) VALUES ('1', '2026-09-21 13:46:55')")
    assert lt.backfill_once(connect=lambda: _Session(pg))["batches"] == 0


def test_pg_stamps_keep_the_earliest_on_either_key(pg, monkeypatch):
    _seed_backfill(pg)
    monkeypatch.setattr(lt, "_connect", lambda: _Session(pg))
    assert lt.stamp_launch_clicked("26-001", at=_utc(2026, 9, 22, 9, 50)) == "31990001"
    ctid = _cols(pg, "31990001", "ctid")[0]
    assert lt.stamp_launch_clicked("31990001", at=_utc(2026, 9, 22, 9, 55)) == "31990001"  # later: kept
    assert lt.stamp_launch_clicked("31990001") == "31990001"  # now(): kept
    assert _cols(pg, "31990001", "first_launch_clicked_at")[0] == _utc(2026, 9, 22, 9, 50)
    # ...and not rewritten: the per-batch callers must not churn a hot row.
    assert _cols(pg, "31990001", "ctid")[0] == ctid
    assert lt.stamp_launch_clicked("26-001", at=_utc(2026, 9, 22, 9, 45)) == "31990001"   # earlier wins
    assert _cols(pg, "31990001", "first_launch_clicked_at")[0] == _utc(2026, 9, 22, 9, 45)
    assert lt.stamp_first_launch("60") == "60"
    assert _cols(pg, "60", "first_launch_at")[0] is not None
    assert lt.stamp_launch_clicked("no-such-job") is None


def test_pg_timings_for_jobs_without_sessions(pg):
    _seed_backfill(pg)
    with pg.cursor() as cur:
        cur.execute(
            "UPDATE monitored_jobs SET pair_added_at = '2026-09-22T08:00:00Z', "
            "first_launch_clicked_at = '2026-09-22T09:40:00Z' WHERE job_id = '31990001'"
        )
    t = lt.timings_for_jobs(["31990001", "60", "missing"], pg)
    assert set(t) == {"31990001", "60"}
    j1 = t["31990001"]
    # T2 from the audit even though the column was never backfilled here.
    assert j1["first_launch_at"] == _utc(2026, 9, 22, 10)
    assert j1["added_to_click_minutes"] == 100.0
    assert (j1["click_to_launch_minutes"], j1["click_to_launch_source"]) == (20.0, "job")
    assert t["60"]["click_to_launch_minutes"] is None  # never clicked


def test_pg_timings_for_jobs_with_sessions_and_missing_columns(pg):
    with pg.cursor() as cur:
        cur.execute("INSERT INTO monitored_jobs (job_id, jobdiva_id, created_at) VALUES ('31990001', '26-001', '')")
        cur.execute("INSERT INTO engage_interview_audit (candidate_id, jobdiva_id, interview_id, created_at) "
                    "VALUES ('c1', '26-001', '901', '2026-09-22 11:20:00')")
        cur.execute(_SESSIONS_SQL)
        cur.execute("""
            INSERT INTO launch_sessions (id, job_id, status, clicked_at, launch_started_at,
                                         first_interview_created_at, human_wait_ms, input, counts) VALUES
              ('00000000-0000-0000-0000-000000000001', '31990001', 'failed', '2026-09-22T10:00:00Z', NULL, NULL, 0,
               '{"candidates": [{"name": "Jane"}]}', '{}'),
              ('00000000-0000-0000-0000-000000000002', '31990001', 'completed', '2026-09-22T11:00:00Z',
               '2026-09-22T11:15:00Z', '2026-09-22T11:20:00Z', 60000, '{}', '{"sent": 3}')
        """)
    # The timing columns do not exist yet (the startup ALTER lost its race):
    # the read still works and reports them as not recorded — T1 is the
    # launching session's own click.
    t = lt.timings_for_jobs(["31990001"], pg, include_sessions=True)["31990001"]
    assert t["pair_added_at"] is None and t["added_to_click_minutes"] is None
    assert t["first_launch_clicked_at"] == _utc(2026, 9, 22, 11)
    assert (t["click_to_launch_minutes"], t["click_to_launch_source"]) == (20.0, "session")
    first = t["first_launch_session"]
    assert first["session_id"] == "00000000-0000-0000-0000-000000000002"
    assert (first["pairbot_s"], first["human_wait_s"], first["machine_seconds"]) == (300.0, 60.0, 1140.0)
    assert [s["status"] for s in t["sessions"]] == ["failed", "completed"]
    assert "input" not in repr(t) and "Jane" not in repr(t)
    # The reports' read (no session list) fetches only the sessions that
    # launched, and reaches the same answer.
    lean = lt.timings_for_jobs(["31990001"], pg)["31990001"]
    assert "sessions" not in lean
    assert lean["first_launch_session"]["session_id"] == first["session_id"]
    assert lean["click_to_launch_minutes"] == 20.0


def test_pg_a_sessions_table_without_job_id_is_skipped_not_fatal(pg):
    with pg.cursor() as cur:
        cur.execute("INSERT INTO monitored_jobs (job_id, jobdiva_id) VALUES ('1', '26-1')")
        cur.execute("INSERT INTO engage_interview_audit (candidate_id, jobdiva_id, interview_id, created_at) "
                    "VALUES ('c1', '26-1', '901', '2026-09-22 11:20:00')")
        cur.execute("CREATE TEMP TABLE launch_sessions (id UUID) ON COMMIT DROP")
    t = lt.timings_for_jobs(["1"], pg, include_sessions=True)["1"]
    assert t["first_launch_at"] == _utc(2026, 9, 22, 11, 20) and t["sessions"] == []


def test_pg_a_failed_read_does_not_abort_the_callers_transaction(pg, monkeypatch):
    with pg.cursor() as cur:
        cur.execute("INSERT INTO monitored_jobs (job_id) VALUES ('1')")
    monkeypatch.setattr(lt, "_TABLES_SQL", "SELECT * FROM no_such_table")
    with pytest.raises(psycopg2.Error):
        lt.timings_for_jobs(["1"], pg)
    with pg.cursor() as cur:
        cur.execute("SELECT count(*) FROM monitored_jobs")
        assert cur.fetchone()[0] == 1


def test_pg_reads_work_on_a_dict_cursor_connection(pg):
    """A caller holding a get_dict_cursor_connection() connection (RealDictCursor
    by default) gets the same timings, not a blank row."""
    import psycopg2.extras

    _seed_backfill(pg)
    with pg.cursor() as cur:
        cur.execute(_SESSIONS_SQL)
        cur.execute(
            "UPDATE monitored_jobs SET pair_added_at = '2026-09-22T08:00:00Z', "
            "first_launch_clicked_at = '2026-09-22T09:40:00Z' WHERE job_id = '31990001'"
        )
    expected = lt.timings_for_jobs(["31990001"], pg, include_sessions=True)
    pg.cursor_factory = psycopg2.extras.RealDictCursor
    try:
        got = lt.timings_for_jobs(["31990001"], pg, include_sessions=True)
    finally:
        pg.cursor_factory = None
    assert got == expected
    assert got["31990001"]["added_to_click_minutes"] == 100.0


def _seed_relaunched_job(pg):
    """The review's probe: added 08-01, launched 08-02 by the legacy flow
    (no click recorded), relaunched on 10-11 after the tracking shipped."""
    with pg.cursor() as cur:
        cur.execute("INSERT INTO monitored_jobs (job_id, jobdiva_id, created_at) "
                    "VALUES ('100', '26-00100', '2026-08-01 10:00:00')")
        cur.execute("INSERT INTO engage_interview_audit (candidate_id, jobdiva_id, interview_id, created_at) "
                    "VALUES ('c1', '26-00100', 'iv1', '2026-08-02 10:00:00')")
    _apply_schema(pg)


def test_pg_a_relaunch_click_after_the_backfill_is_never_stamped(pg, monkeypatch):
    _seed_relaunched_job(pg)
    lt.backfill_once(connect=lambda: _Session(pg))
    assert _cols(pg, "100", "pair_added_at", "first_launch_at") == (_utc(2026, 8, 1, 10), _utc(2026, 8, 2, 10))
    monkeypatch.setattr(lt, "_connect", lambda: _Session(pg))
    assert lt.stamp_launch_clicked("26-00100", at=_utc(2026, 10, 11, 10)) == "100"
    assert lt.stamp_launch_clicked("26-00100") == "100"  # now()
    assert _cols(pg, "100", "first_launch_clicked_at")[0] is None
    t = lt.timings_for_jobs(["100"], pg)["100"]
    assert t["first_launch_clicked_at"] is None
    assert t["added_to_click_minutes"] is None and t["click_to_launch_minutes"] is None


def test_pg_a_relaunch_click_before_the_backfill_is_not_read_as_the_first(pg, monkeypatch):
    """The backfill has not reached the job yet, so the stamp lands; the read
    still knows from the audit that it came after the first launch."""
    _seed_relaunched_job(pg)
    with pg.cursor() as cur:
        cur.execute("UPDATE monitored_jobs SET pair_added_at = '2026-08-01T10:00:00Z'")
        cur.execute(_SESSIONS_SQL)
        cur.execute("""
            INSERT INTO launch_sessions (id, job_id, clicked_at, launch_started_at, first_interview_created_at)
            VALUES ('00000000-0000-0000-0000-000000000009', '100', '2026-10-11T10:00:00Z',
                    '2026-10-11T10:15:00Z', '2026-10-11T10:20:00Z')
        """)
    monkeypatch.setattr(lt, "_connect", lambda: _Session(pg))
    assert lt.stamp_launch_clicked("26-00100", at=_utc(2026, 10, 11, 10)) == "100"
    assert _cols(pg, "100", "first_launch_clicked_at")[0] == _utc(2026, 10, 11, 10)
    for include_sessions in (False, True):
        t = lt.timings_for_jobs(["100"], pg, include_sessions=include_sessions)["100"]
        assert t["first_launch_at"] == _utc(2026, 8, 2, 10)
        assert t["first_launch_clicked_at"] is None and t["click_recorded_after_launch"] is True
        assert t["added_to_click_minutes"] is None
        assert t["click_to_launch_minutes"] is None and t["click_to_launch_source"] is None


def test_pg_stamps_land_on_the_row_the_launch_uses(pg, monkeypatch):
    """A ref-keyed duplicate row (job_id = jobdiva_id = ref) next to the
    numeric row: the stamp, like routers/engagement.py, picks the numeric one."""
    with pg.cursor() as cur:
        cur.execute("""
            INSERT INTO monitored_jobs (job_id, jobdiva_id, created_at) VALUES
              ('26-00200', '26-00200', '2026-09-02 10:00:00'),
              ('31990200', '26-00200', '2026-09-01 10:00:00'),
              ('31990199', '26-00200', '2026-08-01 10:00:00')
        """)
    _apply_schema(pg)
    monkeypatch.setattr(lt, "_connect", lambda: _Session(pg))
    assert lt.stamp_first_launch("26-00200", at=_utc(2026, 9, 3)) == "31990200"
    assert _cols(pg, "31990200", "first_launch_at")[0] == _utc(2026, 9, 3)
    assert _cols(pg, "26-00200", "first_launch_at")[0] is None
    assert lt.job_timing("26-00200")["job_id"] == "31990200"
    # An exact numeric id still resolves to itself.
    assert lt.stamp_launch_clicked("31990199", at=_utc(2026, 8, 2)) == "31990199"


def test_pg_job_timing_resolves_a_ref_and_is_json_ready(pg, monkeypatch):
    _seed_backfill(pg)
    with pg.cursor() as cur:
        cur.execute("UPDATE monitored_jobs SET first_launch_clicked_at = '2026-09-22T09:40:00Z' WHERE job_id = '31990001'")
    monkeypatch.setattr(lt, "_connect", lambda: _Session(pg))
    data = lt.job_timing("26-001")
    assert data["job_id"] == "31990001" and data["jobdiva_id"] == "26-001"
    assert data["first_launch_clicked_at"] == "2026-09-22T09:40:00+00:00"
    assert data["first_launch_at"] == "2026-09-22T10:00:00+00:00"
    assert data["click_to_launch_minutes"] == 20.0 and data["sessions"] == []
    assert lt.job_timing("no-such-job") is None


def test_pg_new_version_clone_restarts_the_timings(pg, monkeypatch):
    """v2 must not inherit v1's T0/T1/T2: its own Added → Click would read
    negative or zero."""
    from routers import jobs as jobs_mod

    with pg.cursor() as cur:
        cur.execute(
            "INSERT INTO monitored_jobs (job_id, jobdiva_id, title, created_at) "
            "VALUES ('31990001', '26-001', 'Data Engineer', '2026-09-01 10:00:00 IST')"
        )
    _apply_schema(pg)
    with pg.cursor() as cur:
        cur.execute(
            "UPDATE monitored_jobs SET pair_added_at = '2026-09-01T04:30:00Z', "
            "first_launch_clicked_at = '2026-09-02T00:00:00Z', first_launch_at = '2026-09-02T00:10:00Z'"
        )
        cur.execute("SELECT now()")
        now = cur.fetchone()[0]
    monkeypatch.setattr(jobs_mod, "get_db_connection", lambda: _Session(pg))
    result = jobs_mod._create_job_version_sync("26-001")
    assert result["status"] == "success" and result["new_job_id"] == "26-001-v2"
    assert _cols(pg, "26-001-v2", *lt.TIMING_COLUMNS) == (now, None, None)
    assert _cols(pg, "26-001-v2", "title")[0] == "Data Engineer"
    # v1 untouched.
    assert _cols(pg, "31990001", "first_launch_clicked_at")[0] == _utc(2026, 9, 2)


def test_pg_first_launch_event_fires_once_per_job(pg, monkeypatch):
    """PairJobFirstLaunch: the once-per-job New Relic sample, sent by the call
    that sets T2 for the FIRST time — never by later batches, relaunches, or
    an earlier-dated correction (those would double-count the job)."""
    _seed_backfill(pg)
    monkeypatch.setattr(lt, "_connect", lambda: _Session(pg))
    events = []
    monkeypatch.setattr(lt, "record_first_launch_event", lambda job: events.append(job))
    assert lt.stamp_first_launch("60", at=_utc(2026, 9, 22, 10, 0)) == "60"
    assert events == ["60"]
    assert lt.stamp_first_launch("60", at=_utc(2026, 9, 22, 11, 0)) == "60"     # later batch / relaunch
    assert lt.stamp_first_launch("60", at=_utc(2026, 9, 22, 9, 0)) == "60"      # earlier correction
    assert _cols(pg, "60", "first_launch_at")[0] == _utc(2026, 9, 22, 9, 0)
    assert events == ["60"]


def test_first_launch_event_carries_job_level_spans_only(monkeypatch):
    from core import newrelic

    sent = []
    monkeypatch.setattr(newrelic, "record_custom_event", lambda event, params=None: sent.append((event, params)))
    monkeypatch.setattr(lt, "timings_for_jobs", lambda ids: {"7": {
        **lt.empty_timing("7", "26-7"),
        "pair_added_at": _utc(2026, 10, 1, 8, 0), "first_launch_clicked_at": _utc(2026, 10, 1, 9, 0),
        "first_launch_at": _utc(2026, 10, 1, 9, 30), "added_to_click_seconds": 3600.0,
        "click_to_launch_seconds": 1800.0, "click_to_launch_source": "job",
    }})
    attrs = lt.record_first_launch_event("7")
    assert sent == [("PairJobFirstLaunch", attrs)]
    assert attrs == {"job_id": "7", "jobdiva_id": "26-7", "added_to_click_s": 3600.0, "click_to_launch_s": 1800.0,
                     "added_to_launch_s": 5400.0, "click_recorded": True, "click_to_launch_source": "job"}
    monkeypatch.setattr(lt, "timings_for_jobs", lambda ids: (_ for _ in ()).throw(RuntimeError("db down")))
    assert lt.record_first_launch_event("7") is None                                # never raises
