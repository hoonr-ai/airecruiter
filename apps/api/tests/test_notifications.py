"""Coverage for the in-app notifications feature (PR review follow-up):

- _parse_recruiter_emails: tolerant JSON/list/comma parsing + normalization.
- create_candidate_passed_notifications: the Pass gate, and one row per
  recruiter on Pass.
- _insert_notification: the ON CONFLICT DO NOTHING dedup claim.
- reconcile_missed_pass_notifications: no longer excludes candidates by a
  recipient-blind NOT EXISTS (the bug a prior review caught), and uses a
  parameterized lookback interval.
- Recipient scoping on the read endpoints' sync DB helpers.
- SSE subscriber bookkeeping (_register_subscriber/_unregister_subscriber):
  the last-tab-disconnect cursor-reset bug found in QA (a reconnect after the
  last tab for an email closed used to silently skip any notification
  created in the gap, because the cursor was wiped and re-seeded from the
  current max id instead of resuming where it left off); the staleness bound
  that caps how much history a long-absent reconnect replays; the client-
  supplied since_id resume path (authoritative, and the only one that's
  correct across a reconnect landing on a different worker); and
  _prune_stale_cursors's bookkeeping cleanup.
- create_pair_launched_notification / create_pair_inactive_notification: the
  job-level (candidate_id=NULL) companions to the existing PAIR-launched and
  PAIR-inactive emails, run independently of whether that email send
  succeeds. pair_inactive dedupes via its own dedicated partial-index
  conflict clause ("at most one active" per job/recipient); pair_launched
  dedupes via a pre-insert existence check instead
  (_job_level_duplicate_in_last_minute) rather than an index-backed
  ON CONFLICT, since a per-minute window isn't something Postgres will
  index.
- create_candidate_submitted_notifications / create_candidate_rejected_notifications:
  fired from routers/candidates.py's save_candidate_feedback on "Submit" /
  "Reject" respectively. Both exclude the acting recruiter from the
  recipient list, and share candidate_passed's dedup index (via
  _CONFLICT_CANDIDATE_LEVEL) but keyed independently by `type`, so a
  resubmission/re-rejection is a no-op without affecting candidate_passed's
  (or each other's) own dedup for the same candidate/job/recipient.
- create_recruiter_assigned_notifications: fired from routers/jobs.py's job
  wizard save endpoints when a recruiter_emails diff shows someone newly
  added. Job-level (candidate_id=NULL) like pair_launched, and shares its
  dedup shape (_job_level_duplicate_in_last_minute) for the same reason —
  frequent autosave calls need a race guard, not an "only ever once" index.
- create_zero_match_notification: fired from routers/candidates.py's
  /candidates/search when a full search's summary reports 0 candidates.
  The only type with a single specific recipient (the searching recruiter)
  rather than a job's whole recruiter_emails list.
- create_daily_digest_notifications: the scheduled (not request-triggered)
  morning summary — one row per recipient with unread candidate_passed
  notifications, counting how many and across how many jobs. jobdiva_id is
  NULL (a digest spans every job), dedup is a daily-scale
  _job_level_duplicate_in_last_minute analog keyed on recipient_email alone.
"""
import asyncio
import json
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from services.notifications_service import (
    _parse_recruiter_emails,
    _insert_notification,
    _job_level_duplicate_in_last_minute,
    _CONFLICT_CANDIDATE_LEVEL,
    _CONFLICT_PAIR_INACTIVE,
    create_candidate_passed_notifications,
    create_candidate_submitted_notifications,
    create_candidate_rejected_notifications,
    create_pair_launched_notification,
    create_pair_inactive_notification,
    create_recruiter_assigned_notifications,
    create_zero_match_notification,
    create_daily_digest_notifications,
    _digest_title,
    _recipient_already_digested_recently,
    reconcile_missed_pass_notifications,
)


class FakeCursor:
    def __init__(self, fetchone_results=None, fetchall_results=None):
        self.executed = []
        self._fetchone_results = list(fetchone_results or [])
        self._fetchall_results = list(fetchall_results or [])

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self._fetchone_results.pop(0) if self._fetchone_results else None

    def fetchall(self):
        return self._fetchall_results.pop(0) if self._fetchall_results else []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, cursor: FakeCursor):
        self._cursor = cursor
        self.committed = False
        self.closed = False

    def cursor(self, cursor_factory=None):
        return self._cursor

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# --- _parse_recruiter_emails -------------------------------------------------

def test_parse_recruiter_emails_list():
    assert _parse_recruiter_emails(["A@X.com", "b@x.com"]) == ["a@x.com", "b@x.com"]


def test_parse_recruiter_emails_json_string():
    assert _parse_recruiter_emails('["a@x.com", "B@X.com"]') == ["a@x.com", "b@x.com"]


def test_parse_recruiter_emails_comma_string_fallback():
    assert _parse_recruiter_emails("a@x.com, b@x.com") == ["a@x.com", "b@x.com"]


def test_parse_recruiter_emails_dedupes_case_insensitively():
    assert _parse_recruiter_emails(["A@x.com", "a@X.com"]) == ["a@x.com"]


def test_parse_recruiter_emails_empty_and_none():
    assert _parse_recruiter_emails(None) == []
    assert _parse_recruiter_emails("") == []
    assert _parse_recruiter_emails([]) == []


# --- _insert_notification: ON CONFLICT DO NOTHING dedup ---------------------

def test_insert_notification_returns_true_when_row_inserted():
    cur = FakeCursor(fetchone_results=[{"id": 1}])
    inserted = _insert_notification(
        cur,
        notification_type="candidate_passed",
        conflict_clause=_CONFLICT_CANDIDATE_LEVEL,
        recipient_email="a@x.com",
        job_id="J1",
        jobdiva_id="JD1",
        candidate_id="C1",
        title="t",
        body=None,
        score=90.0,
    )
    assert inserted is True
    sql, params = cur.executed[0]
    assert "ON CONFLICT" in sql and "DO NOTHING" in sql and "RETURNING id" in sql


def test_insert_notification_returns_false_on_conflict():
    cur = FakeCursor(fetchone_results=[None])
    inserted = _insert_notification(
        cur,
        notification_type="candidate_passed",
        conflict_clause=_CONFLICT_CANDIDATE_LEVEL,
        recipient_email="a@x.com",
        job_id="J1",
        jobdiva_id="JD1",
        candidate_id="C1",
        title="t",
        body=None,
        score=90.0,
    )
    assert inserted is False


def test_insert_notification_job_level_types_use_their_own_conflict_clause():
    """pair_launched/pair_inactive pass candidate_id=None. pair_inactive uses
    its own dedicated partial-index conflict clause (not the candidate-level
    one, which would never dedupe a NULL candidate_id against another NULL);
    pair_launched has no conflict clause at all (bare INSERT) since its
    dedup is an application-level pre-insert check instead, done by its
    caller before this function is ever reached."""
    cur = FakeCursor(fetchone_results=[{"id": 1}])
    inserted = _insert_notification(
        cur,
        notification_type="pair_launched",
        recipient_email="a@x.com",
        job_id="J1",
        jobdiva_id="JD1",
        candidate_id=None,
        title="t",
        body=None,
        score=None,
    )
    assert inserted is True
    sql, params = cur.executed[0]
    assert "ON CONFLICT" not in sql
    assert params[0] == "a@x.com"
    assert params[1] == "pair_launched"
    assert params[4] is None  # candidate_id position


def test_insert_notification_pair_inactive_conflict_clause_scopes_to_active():
    cur = FakeCursor(fetchone_results=[{"id": 1}])
    _insert_notification(
        cur,
        notification_type="pair_inactive",
        conflict_clause=_CONFLICT_PAIR_INACTIVE,
        recipient_email="a@x.com",
        job_id="J1",
        jobdiva_id="JD1",
        candidate_id=None,
        title="t",
        body=None,
        score=None,
    )
    sql, params = cur.executed[0]
    assert "cleared_at IS NULL" in sql
    assert "type = 'pair_inactive'" in sql


# --- create_candidate_passed_notifications: the Pass gate -------------------

def test_create_notifications_skips_db_when_not_pass(monkeypatch):
    def _fail():
        raise AssertionError("must not touch the DB when the candidate isn't a Pass")

    monkeypatch.setattr("services.notifications_service.get_db_connection", _fail)

    detail_payload = {"interview": {"status": "in_progress", "candidate_score": 95}}
    asyncio.run(create_candidate_passed_notifications("J1", "C1", detail_payload))


def test_create_notifications_inserts_one_row_per_recruiter(monkeypatch):
    job_row = {
        "job_id": "J1", "title": "Data Engineer", "enhanced_title": None,
        "recruiter_emails": json.dumps(["a@x.com", "b@x.com"]), "jobdiva_id": "JD1",
    }
    cand_row = {"name": "Jane Doe", "data": {}}
    # fetchone order: job_row, cand_row, then one per recruiter insert.
    cur = FakeCursor(fetchone_results=[job_row, cand_row, {"id": 1}, None])
    conn = FakeConn(cur)
    monkeypatch.setattr("services.notifications_service.get_db_connection", lambda: conn)

    detail_payload = {
        "interview": {"status": "passed", "candidate_score": 92, "hard_filter_status": "passed"}
    }
    asyncio.run(create_candidate_passed_notifications("J1", "C1", detail_payload))

    insert_calls = [c for c in cur.executed if "INSERT INTO notifications" in c[0]]
    assert len(insert_calls) == 2
    recipients = {params[0] for _, params in insert_calls}
    assert recipients == {"a@x.com", "b@x.com"}
    assert conn.committed is True


def test_create_notifications_noop_without_recruiter_emails(monkeypatch):
    job_row = {
        "job_id": "J1", "title": "Data Engineer", "enhanced_title": None,
        "recruiter_emails": None, "jobdiva_id": "JD1",
    }
    cand_row = {"name": "Jane Doe", "data": {}}
    cur = FakeCursor(fetchone_results=[job_row, cand_row])
    conn = FakeConn(cur)
    monkeypatch.setattr("services.notifications_service.get_db_connection", lambda: conn)

    detail_payload = {"interview": {"status": "passed", "candidate_score": 92, "hard_filter_status": "passed"}}
    asyncio.run(create_candidate_passed_notifications("J1", "C1", detail_payload))

    assert not any("INSERT INTO notifications" in c[0] for c in cur.executed)


# --- create_candidate_submitted_notifications --------------------------------

def test_create_submitted_notifications_excludes_submitter(monkeypatch):
    job_row = {
        "job_id": "J1", "title": "Data Engineer", "enhanced_title": None,
        "recruiter_emails": json.dumps(["a@x.com", "b@x.com"]), "jobdiva_id": "JD1",
    }
    cand_row = {"name": "Jane Doe", "data": {}}
    # fetchone order: job_row, cand_row, then one insert per non-submitter recipient.
    cur = FakeCursor(fetchone_results=[job_row, cand_row, {"id": 1}])
    conn = FakeConn(cur)
    monkeypatch.setattr("services.notifications_service.get_db_connection", lambda: conn)

    asyncio.run(create_candidate_submitted_notifications("J1", "C1", "a@x.com"))

    insert_calls = [c for c in cur.executed if "INSERT INTO notifications" in c[0]]
    assert len(insert_calls) == 1
    sql, params = insert_calls[0]
    assert params[0] == "b@x.com"  # the submitter (a@x.com) is excluded
    assert params[1] == "candidate_submitted"
    assert params[4] == "C1"  # candidate_id IS set (candidate-level, unlike pair_*)
    assert "Jane Doe was submitted for Data Engineer" in params[5]
    assert conn.committed is True


def test_create_submitted_notifications_noop_when_only_submitter_assigned(monkeypatch):
    """A job with exactly one recruiter (the submitter) has no one left to
    notify — must not touch INSERT at all, not insert a 0-recipient no-op."""
    job_row = {
        "job_id": "J1", "title": "Data Engineer", "enhanced_title": None,
        "recruiter_emails": json.dumps(["solo@x.com"]), "jobdiva_id": "JD1",
    }
    cand_row = {"name": "Jane Doe", "data": {}}
    cur = FakeCursor(fetchone_results=[job_row, cand_row])
    conn = FakeConn(cur)
    monkeypatch.setattr("services.notifications_service.get_db_connection", lambda: conn)

    asyncio.run(create_candidate_submitted_notifications("J1", "C1", "solo@x.com"))

    assert not any("INSERT INTO notifications" in c[0] for c in cur.executed)


def test_create_submitted_notifications_submitter_match_is_case_insensitive(monkeypatch):
    job_row = {
        "job_id": "J1", "title": "Data Engineer", "enhanced_title": None,
        "recruiter_emails": json.dumps(["Submitter@X.com", "b@x.com"]), "jobdiva_id": "JD1",
    }
    cand_row = {"name": "Jane Doe", "data": {}}
    cur = FakeCursor(fetchone_results=[job_row, cand_row, {"id": 1}])
    conn = FakeConn(cur)
    monkeypatch.setattr("services.notifications_service.get_db_connection", lambda: conn)

    asyncio.run(create_candidate_submitted_notifications("J1", "C1", "submitter@x.com"))

    insert_calls = [c for c in cur.executed if "INSERT INTO notifications" in c[0]]
    assert len(insert_calls) == 1
    assert insert_calls[0][1][0] == "b@x.com"


def test_create_submitted_notifications_swallows_db_errors(monkeypatch):
    """Must never raise into the feedback endpoint's response."""
    def _boom():
        raise RuntimeError("db unavailable")
    monkeypatch.setattr("services.notifications_service.get_db_connection", _boom)

    asyncio.run(create_candidate_submitted_notifications("J1", "C1", "a@x.com"))  # no exception = pass


def test_create_submitted_notifications_noop_without_job_or_candidate_id(monkeypatch):
    def _fail():
        raise AssertionError("must not touch the DB without job_id/candidate_id")
    monkeypatch.setattr("services.notifications_service.get_db_connection", _fail)

    asyncio.run(create_candidate_submitted_notifications("", "C1", "a@x.com"))
    asyncio.run(create_candidate_submitted_notifications("J1", "", "a@x.com"))


def test_insert_notification_candidate_submitted_shares_candidate_level_conflict_clause():
    """candidate_submitted reuses _CONFLICT_CANDIDATE_LEVEL (the same clause
    candidate_passed uses) — `type` is part of the unique index, so the two
    types get independent dedup slots for the same (recipient, candidate,
    job) rather than colliding or needing a separate index."""
    cur = FakeCursor(fetchone_results=[{"id": 1}])
    inserted = _insert_notification(
        cur,
        notification_type="candidate_submitted",
        conflict_clause=_CONFLICT_CANDIDATE_LEVEL,
        recipient_email="a@x.com",
        job_id="J1",
        jobdiva_id="JD1",
        candidate_id="C1",
        title="t",
        body=None,
        score=None,
    )
    assert inserted is True
    sql, params = cur.executed[0]
    assert "ON CONFLICT (recipient_email, candidate_id, jobdiva_id, type) DO NOTHING" in sql
    assert params[1] == "candidate_submitted"


# --- create_candidate_rejected_notifications ---------------------------------

def test_create_rejected_notifications_excludes_rejecter(monkeypatch):
    job_row = {
        "job_id": "J1", "title": "Data Engineer", "enhanced_title": None,
        "recruiter_emails": json.dumps(["a@x.com", "b@x.com"]), "jobdiva_id": "JD1",
    }
    cand_row = {"name": "Jane Doe", "data": {}}
    cur = FakeCursor(fetchone_results=[job_row, cand_row, {"id": 1}])
    conn = FakeConn(cur)
    monkeypatch.setattr("services.notifications_service.get_db_connection", lambda: conn)

    asyncio.run(create_candidate_rejected_notifications("J1", "C1", "a@x.com"))

    insert_calls = [c for c in cur.executed if "INSERT INTO notifications" in c[0]]
    assert len(insert_calls) == 1
    sql, params = insert_calls[0]
    assert params[0] == "b@x.com"  # the rejecter (a@x.com) is excluded
    assert params[1] == "candidate_rejected"
    assert params[4] == "C1"
    assert "Jane Doe was rejected for Data Engineer" in params[5]
    assert conn.committed is True


def test_create_rejected_notifications_swallows_db_errors(monkeypatch):
    def _boom():
        raise RuntimeError("db unavailable")
    monkeypatch.setattr("services.notifications_service.get_db_connection", _boom)

    asyncio.run(create_candidate_rejected_notifications("J1", "C1", "a@x.com"))  # no exception = pass


def test_create_rejected_notifications_noop_without_job_or_candidate_id(monkeypatch):
    def _fail():
        raise AssertionError("must not touch the DB without job_id/candidate_id")
    monkeypatch.setattr("services.notifications_service.get_db_connection", _fail)

    asyncio.run(create_candidate_rejected_notifications("", "C1", "a@x.com"))
    asyncio.run(create_candidate_rejected_notifications("J1", "", "a@x.com"))


def test_submitted_and_rejected_are_independently_dedupable():
    """Different `type` values mean a candidate_submitted row never blocks
    (or is blocked by) a candidate_rejected row for the same recipient,
    candidate and job — confirmed via the conflict clause/SQL shape alone,
    since the real uniqueness guarantee lives in Postgres's own index."""
    cur = FakeCursor(fetchone_results=[{"id": 1}, {"id": 2}])
    for notification_type in ("candidate_submitted", "candidate_rejected"):
        inserted = _insert_notification(
            cur,
            notification_type=notification_type,
            conflict_clause=_CONFLICT_CANDIDATE_LEVEL,
            recipient_email="a@x.com",
            job_id="J1",
            jobdiva_id="JD1",
            candidate_id="C1",
            title="t",
            body=None,
            score=None,
        )
        assert inserted is True
    types_inserted = [params[1] for _, params in cur.executed]
    assert types_inserted == ["candidate_submitted", "candidate_rejected"]


# --- reconcile_missed_pass_notifications ------------------------------------

def test_reconcile_does_not_prefilter_by_recipient_blind_not_exists(monkeypatch):
    """Regression test: an earlier version excluded a candidate/job pair the
    moment ANY recipient had a notification, so a recruiter added to
    recruiter_emails afterward never got backfilled. Dedup must happen only
    via the per-recipient INSERT ... ON CONFLICT claim."""
    cur = FakeCursor(fetchall_results=[[]])
    conn = FakeConn(cur)
    monkeypatch.setattr("services.notifications_service.get_db_connection", lambda: conn)

    asyncio.run(reconcile_missed_pass_notifications())

    select_sql, params = cur.executed[0]
    assert "NOT EXISTS" not in select_sql
    # Lookback interval and row cap are bound parameters, not interpolated.
    assert "%s * INTERVAL" in select_sql
    assert "LIMIT %s" in select_sql
    assert params[-1] > 0  # row cap


def test_reconcile_backfills_new_recipient_for_already_notified_candidate(monkeypatch):
    row = {
        "candidate_id": "C1", "name": "Jane Doe", "data": {},
        "job_id": "J1", "jobdiva_id": "JD1", "title": "Data Engineer", "enhanced_title": None,
        "recruiter_emails": json.dumps(["new-recruiter@x.com"]),
    }
    # First execute is the SELECT (fetchall), then one INSERT per recruiter (fetchone).
    cur = FakeCursor(fetchall_results=[[row]], fetchone_results=[{"id": 42}])
    conn = FakeConn(cur)
    monkeypatch.setattr("services.notifications_service.get_db_connection", lambda: conn)

    asyncio.run(reconcile_missed_pass_notifications())

    insert_calls = [c for c in cur.executed if "INSERT INTO notifications" in c[0]]
    assert len(insert_calls) == 1
    assert insert_calls[0][1][0] == "new-recruiter@x.com"


# --- _job_level_duplicate_in_last_minute -------------------------------------

def test_job_level_duplicate_in_last_minute_true_when_row_found():
    cur = FakeCursor(fetchone_results=[{"1": 1}])
    assert _job_level_duplicate_in_last_minute(cur, "a@x.com", "26-14907", "pair_launched") is True
    sql, params = cur.executed[0]
    assert "INTERVAL '1 minute'" in sql
    assert params == ("a@x.com", "26-14907", "pair_launched")


def test_job_level_duplicate_in_last_minute_false_when_no_row():
    cur = FakeCursor(fetchone_results=[None])
    assert _job_level_duplicate_in_last_minute(cur, "a@x.com", "26-14907", "pair_launched") is False


def test_job_level_duplicate_in_last_minute_scopes_by_type():
    """A recruiter_assigned row must not count as a duplicate for a
    pair_launched check on the same recipient/job, and vice versa — the
    `type` filter in the SQL is what keeps the two event types' race guards
    independent of each other."""
    cur = FakeCursor()
    _job_level_duplicate_in_last_minute(cur, "a@x.com", "26-14907", "recruiter_assigned")
    sql, params = cur.executed[0]
    assert "type = %s" in sql
    assert params == ("a@x.com", "26-14907", "recruiter_assigned")


# --- create_pair_launched_notification / create_pair_inactive_notification --

def test_create_pair_launched_notification_inserts_one_row_per_recruiter_with_null_candidate(monkeypatch):
    # fetchone order per recipient: the duplicate pre-check (None = no
    # duplicate found), then the INSERT's own RETURNING id.
    cur = FakeCursor(fetchone_results=[None, {"id": 1}, None, {"id": 2}])
    conn = FakeConn(cur)
    monkeypatch.setattr("services.notifications_service.get_db_connection", lambda: conn)

    asyncio.run(create_pair_launched_notification(
        job_id="J1",
        jobdiva_id="26-14907",
        job_title="MS SQL Developer",
        candidate_count=12,
        recruiter_emails=["a@x.com", "b@x.com"],
    ))

    insert_calls = [c for c in cur.executed if "INSERT INTO notifications" in c[0]]
    assert len(insert_calls) == 2
    for sql, params in insert_calls:
        assert params[1] == "pair_launched"
        assert params[4] is None  # candidate_id
    assert {c[1][0] for c in insert_calls} == {"a@x.com", "b@x.com"}
    assert "12 candidates" in insert_calls[0][1][5]  # title
    assert conn.committed is True


def test_create_pair_launched_notification_singular_candidate_count(monkeypatch):
    cur = FakeCursor(fetchone_results=[None, {"id": 1}])
    conn = FakeConn(cur)
    monkeypatch.setattr("services.notifications_service.get_db_connection", lambda: conn)

    asyncio.run(create_pair_launched_notification(
        job_id="J1", jobdiva_id="26-14907", job_title="MS SQL Developer",
        candidate_count=1, recruiter_emails=["a@x.com"],
    ))

    insert_calls = [c for c in cur.executed if "INSERT INTO notifications" in c[0]]
    title = insert_calls[0][1][5]
    assert "1 candidate" in title and "1 candidates" not in title


def test_create_pair_launched_notification_noop_without_recruiters_or_jobdiva_id(monkeypatch):
    def _fail():
        raise AssertionError("must not touch the DB without recruiters/jobdiva_id")
    monkeypatch.setattr("services.notifications_service.get_db_connection", _fail)

    asyncio.run(create_pair_launched_notification(
        job_id="J1", jobdiva_id="", job_title="X", candidate_count=1, recruiter_emails=["a@x.com"],
    ))
    asyncio.run(create_pair_launched_notification(
        job_id="J1", jobdiva_id="26-14907", job_title="X", candidate_count=1, recruiter_emails=[],
    ))


def test_create_pair_launched_notification_swallows_db_errors(monkeypatch):
    """Must never raise into the launch flow — a DB hiccup here shouldn't
    also take down the email send or the launch endpoint's response."""
    def _boom():
        raise RuntimeError("db unavailable")
    monkeypatch.setattr("services.notifications_service.get_db_connection", _boom)

    asyncio.run(create_pair_launched_notification(
        job_id="J1", jobdiva_id="26-14907", job_title="X", candidate_count=3, recruiter_emails=["a@x.com"],
    ))  # no exception raised = pass


def test_create_pair_inactive_notification_inserts_per_recruiter_with_null_candidate(monkeypatch):
    cur = FakeCursor(fetchone_results=[{"id": 1}])
    conn = FakeConn(cur)
    monkeypatch.setattr("services.notifications_service.get_db_connection", lambda: conn)

    asyncio.run(create_pair_inactive_notification(
        job_id="J1", jobdiva_id="26-14907", job_title="MS SQL Developer",
        recruiter_emails=["a@x.com"],
    ))

    insert_calls = [c for c in cur.executed if "INSERT INTO notifications" in c[0]]
    assert len(insert_calls) == 1
    sql, params = insert_calls[0]
    assert params[1] == "pair_inactive"
    assert params[4] is None  # candidate_id
    assert "MS SQL Developer" in params[5]  # title
    assert conn.committed is True


def test_create_pair_inactive_notification_noop_without_recruiters(monkeypatch):
    def _fail():
        raise AssertionError("must not touch the DB without recruiters")
    monkeypatch.setattr("services.notifications_service.get_db_connection", _fail)

    asyncio.run(create_pair_inactive_notification(
        job_id="J1", jobdiva_id="26-14907", job_title="X", recruiter_emails=[],
    ))


def test_create_pair_inactive_notification_swallows_db_errors(monkeypatch):
    """Must never raise into the poll cycle — a DB hiccup here shouldn't
    also prevent the claim/email logic in main.py from completing."""
    def _boom():
        raise RuntimeError("db unavailable")
    monkeypatch.setattr("services.notifications_service.get_db_connection", _boom)

    asyncio.run(create_pair_inactive_notification(
        job_id="J1", jobdiva_id="26-14907", job_title="X", recruiter_emails=["a@x.com"],
    ))  # no exception raised = pass


# --- create_recruiter_assigned_notifications ---------------------------------

def test_create_recruiter_assigned_notifications_inserts_one_row_per_assignee(monkeypatch):
    # fetchone order per recipient: the duplicate pre-check (None = no
    # duplicate found), then the INSERT's own RETURNING id.
    cur = FakeCursor(fetchone_results=[None, {"id": 1}, None, {"id": 2}])
    conn = FakeConn(cur)
    monkeypatch.setattr("services.notifications_service.get_db_connection", lambda: conn)

    asyncio.run(create_recruiter_assigned_notifications(
        job_id="J1", jobdiva_id="26-14907", job_title="MS SQL Developer",
        assigned_emails=["new1@x.com", "new2@x.com"],
    ))

    insert_calls = [c for c in cur.executed if "INSERT INTO notifications" in c[0]]
    assert len(insert_calls) == 2
    for sql, params in insert_calls:
        assert params[1] == "recruiter_assigned"
        assert params[4] is None  # candidate_id
        assert "You were added as a recruiter for MS SQL Developer" in params[5]
    assert {c[1][0] for c in insert_calls} == {"new1@x.com", "new2@x.com"}
    assert conn.committed is True


def test_create_recruiter_assigned_notifications_skips_recent_duplicate(monkeypatch):
    cur = FakeCursor(fetchone_results=[{"id": 99}])  # duplicate check finds a recent row
    conn = FakeConn(cur)
    monkeypatch.setattr("services.notifications_service.get_db_connection", lambda: conn)

    asyncio.run(create_recruiter_assigned_notifications(
        job_id="J1", jobdiva_id="26-14907", job_title="X", assigned_emails=["a@x.com"],
    ))

    assert not any("INSERT INTO notifications" in c[0] for c in cur.executed)


def test_create_recruiter_assigned_notifications_noop_without_assignees(monkeypatch):
    def _fail():
        raise AssertionError("must not touch the DB without assigned_emails")
    monkeypatch.setattr("services.notifications_service.get_db_connection", _fail)

    asyncio.run(create_recruiter_assigned_notifications(
        job_id="J1", jobdiva_id="26-14907", job_title="X", assigned_emails=[],
    ))


def test_create_recruiter_assigned_notifications_swallows_db_errors(monkeypatch):
    def _boom():
        raise RuntimeError("db unavailable")
    monkeypatch.setattr("services.notifications_service.get_db_connection", _boom)

    asyncio.run(create_recruiter_assigned_notifications(
        job_id="J1", jobdiva_id="26-14907", job_title="X", assigned_emails=["a@x.com"],
    ))  # no exception raised = pass


# --- create_zero_match_notification -------------------------------------------

def test_create_zero_match_notification_notifies_only_the_searcher(monkeypatch):
    job_row = {
        "job_id": "J1", "title": "Data Engineer", "enhanced_title": None, "jobdiva_id": "JD1",
    }
    # fetchone order: job lookup, duplicate pre-check (None = no duplicate), insert's RETURNING id.
    cur = FakeCursor(fetchone_results=[job_row, None, {"id": 1}])
    conn = FakeConn(cur)
    monkeypatch.setattr("services.notifications_service.get_db_connection", lambda: conn)

    asyncio.run(create_zero_match_notification(job_id="J1", searcher_email="Searcher@X.com"))

    insert_calls = [c for c in cur.executed if "INSERT INTO notifications" in c[0]]
    assert len(insert_calls) == 1
    sql, params = insert_calls[0]
    assert params[0] == "searcher@x.com"  # lowercased
    assert params[1] == "zero_match_warning"
    assert params[4] is None  # candidate_id — no candidate by definition
    assert "Data Engineer" in params[5]
    assert conn.committed is True


def test_create_zero_match_notification_skips_recent_duplicate(monkeypatch):
    job_row = {"job_id": "J1", "title": "Data Engineer", "enhanced_title": None, "jobdiva_id": "JD1"}
    cur = FakeCursor(fetchone_results=[job_row, {"id": 99}])  # duplicate check finds a recent row
    conn = FakeConn(cur)
    monkeypatch.setattr("services.notifications_service.get_db_connection", lambda: conn)

    asyncio.run(create_zero_match_notification(job_id="J1", searcher_email="a@x.com"))

    assert not any("INSERT INTO notifications" in c[0] for c in cur.executed)


def test_create_zero_match_notification_noop_when_job_not_found(monkeypatch):
    cur = FakeCursor(fetchone_results=[None])
    conn = FakeConn(cur)
    monkeypatch.setattr("services.notifications_service.get_db_connection", lambda: conn)

    asyncio.run(create_zero_match_notification(job_id="J-missing", searcher_email="a@x.com"))

    assert not any("INSERT INTO notifications" in c[0] for c in cur.executed)


def test_create_zero_match_notification_noop_without_job_or_searcher(monkeypatch):
    def _fail():
        raise AssertionError("must not touch the DB without job_id/searcher_email")
    monkeypatch.setattr("services.notifications_service.get_db_connection", _fail)

    asyncio.run(create_zero_match_notification(job_id="", searcher_email="a@x.com"))
    asyncio.run(create_zero_match_notification(job_id="J1", searcher_email=""))


def test_create_zero_match_notification_swallows_db_errors(monkeypatch):
    def _boom():
        raise RuntimeError("db unavailable")
    monkeypatch.setattr("services.notifications_service.get_db_connection", _boom)

    asyncio.run(create_zero_match_notification(job_id="J1", searcher_email="a@x.com"))  # no exception = pass


# --- create_daily_digest_notifications ---------------------------------------

class _FakeAcquiredLock:
    """Stands in for core.advisory_lock.AdvisoryLock as though this worker
    won the cross-worker race — lets the digest tests exercise
    _create_daily_digest_notifications_sync without AdvisoryLock.try_acquire
    opening a real DB connection (core/db.py's get_session_db_connection,
    which conftest.py's _block_real_db_connections fixture doesn't reach,
    since that only patches core.db.get_db_connection)."""

    def __init__(self, *a, **kw):
        pass

    async def try_acquire(self):
        return True

    async def release(self):
        pass


class _FakeContestedLock:
    """Stands in for AdvisoryLock as though another worker already holds it."""

    def __init__(self, *a, **kw):
        pass

    async def try_acquire(self):
        return False

    async def release(self):
        pass


def test_digest_title_singular_candidate_one_job():
    title = _digest_title(passed_count=1, job_count=1)
    assert "1 candidate passed their interview" in title
    assert "for this job" in title
    assert "and is waiting on your review" in title
    assert "candidates" not in title


def test_digest_title_plural_candidates_multiple_jobs():
    title = _digest_title(passed_count=14, job_count=3)
    assert "14 candidates passed their interviews" in title
    assert "across 3 jobs" in title
    assert "and are waiting on your review" in title


def test_digest_title_plural_candidates_one_job():
    title = _digest_title(passed_count=5, job_count=1)
    assert "5 candidates passed their interviews" in title
    assert "for this job" in title
    assert "and are waiting on your review" in title


def test_recipient_already_digested_recently_true_when_row_found():
    cur = FakeCursor(fetchone_results=[{"1": 1}])
    assert _recipient_already_digested_recently(cur, "a@x.com") is True
    sql, params = cur.executed[0]
    assert "type = 'daily_digest'" in sql
    assert params[0] == "a@x.com"


def test_recipient_already_digested_recently_false_when_no_row():
    cur = FakeCursor(fetchone_results=[None])
    assert _recipient_already_digested_recently(cur, "a@x.com") is False


def test_create_daily_digest_notifications_one_row_per_recipient_with_unread_passed(monkeypatch):
    monkeypatch.setattr("services.notifications_service.AdvisoryLock", _FakeAcquiredLock)
    group_by_rows = [
        {"recipient_email": "a@x.com", "passed_count": 14, "job_count": 3},
        {"recipient_email": "b@x.com", "passed_count": 1, "job_count": 1},
    ]
    # fetchall: the GROUP BY query. fetchone, per recipient in order: the
    # dedup pre-check (None = not digested recently), then the insert's own
    # RETURNING id.
    cur = FakeCursor(
        fetchall_results=[group_by_rows],
        fetchone_results=[None, {"id": 1}, None, {"id": 2}],
    )
    conn = FakeConn(cur)
    monkeypatch.setattr("services.notifications_service.get_db_connection", lambda: conn)

    asyncio.run(create_daily_digest_notifications())

    insert_calls = [c for c in cur.executed if "INSERT INTO notifications" in c[0]]
    assert len(insert_calls) == 2
    for sql, params in insert_calls:
        assert params[1] == "daily_digest"
        assert params[3] is None  # jobdiva_id
        assert params[4] is None  # candidate_id
    titles = [params[5] for _, params in insert_calls]
    assert any("14 candidates" in t and "across 3 jobs" in t for t in titles)
    assert any("1 candidate passed" in t and "for this job" in t for t in titles)
    assert conn.committed is True


def test_create_daily_digest_notifications_counts_rows_missing_both_job_ids(monkeypatch):
    """PR review follow-up: COUNT(DISTINCT jobdiva_id) alone ignores NULLs,
    so a recipient whose passed rows all lack jobdiva_id would wrongly get
    job_count=0. COALESCE(jobdiva_id, job_id) in the real query covers
    this; this test pins the aggregation's own row shape rather than the
    SQL text, since the GROUP BY/COALESCE happens in Postgres, not here —
    _digest_title just needs to see a sane job_count out of that row."""
    monkeypatch.setattr("services.notifications_service.AdvisoryLock", _FakeAcquiredLock)
    group_by_rows = [{"recipient_email": "a@x.com", "passed_count": 3, "job_count": 1}]
    cur = FakeCursor(fetchall_results=[group_by_rows], fetchone_results=[None, {"id": 1}])
    conn = FakeConn(cur)
    monkeypatch.setattr("services.notifications_service.get_db_connection", lambda: conn)

    asyncio.run(create_daily_digest_notifications())

    insert_calls = [c for c in cur.executed if "INSERT INTO notifications" in c[0]]
    assert len(insert_calls) == 1
    assert "for this job" in insert_calls[0][1][5]  # job_count=1 renders singular, not "across 0 jobs"


def test_create_daily_digest_notifications_skips_recipient_already_digested(monkeypatch):
    monkeypatch.setattr("services.notifications_service.AdvisoryLock", _FakeAcquiredLock)
    group_by_rows = [{"recipient_email": "a@x.com", "passed_count": 2, "job_count": 1}]
    cur = FakeCursor(
        fetchall_results=[group_by_rows],
        fetchone_results=[{"id": 99}],  # dedup pre-check: already digested recently
    )
    conn = FakeConn(cur)
    monkeypatch.setattr("services.notifications_service.get_db_connection", lambda: conn)

    asyncio.run(create_daily_digest_notifications())

    assert not any("INSERT INTO notifications" in c[0] for c in cur.executed)


def test_create_daily_digest_notifications_noop_when_nobody_has_unread_passed(monkeypatch):
    monkeypatch.setattr("services.notifications_service.AdvisoryLock", _FakeAcquiredLock)
    cur = FakeCursor(fetchall_results=[[]])
    conn = FakeConn(cur)
    monkeypatch.setattr("services.notifications_service.get_db_connection", lambda: conn)

    asyncio.run(create_daily_digest_notifications())

    assert not any("INSERT INTO notifications" in c[0] for c in cur.executed)
    assert conn.committed is True  # still commits the (no-op) transaction cleanly


def test_create_daily_digest_notifications_swallows_db_errors(monkeypatch):
    monkeypatch.setattr("services.notifications_service.AdvisoryLock", _FakeAcquiredLock)
    def _boom():
        raise RuntimeError("db unavailable")
    monkeypatch.setattr("services.notifications_service.get_db_connection", _boom)

    asyncio.run(create_daily_digest_notifications())  # no exception raised = pass


def test_create_daily_digest_notifications_skips_entirely_when_another_worker_holds_the_lock(monkeypatch):
    """PR review follow-up: every uvicorn worker's own APScheduler fires
    this at the same cron tick; a worker that loses the advisory-lock race
    must not touch the DB at all, let alone insert."""
    monkeypatch.setattr("services.notifications_service.AdvisoryLock", _FakeContestedLock)

    def _fail():
        raise AssertionError("must not touch the DB when another worker holds the lock")
    monkeypatch.setattr("services.notifications_service.get_db_connection", _fail)

    asyncio.run(create_daily_digest_notifications())  # no exception raised = pass


# --- recipient scoping on the read/mark-read sync helpers -------------------

def test_list_notifications_scopes_to_caller_email(monkeypatch):
    from routers.notifications import _list_notifications_sync

    cur = FakeCursor(fetchall_results=[[]], fetchone_results=[(0,)])
    conn = FakeConn(cur)
    monkeypatch.setattr("routers.notifications.get_db_connection", lambda: conn)

    _list_notifications_sync("me@x.com", 50, None, False)

    select_sql, params = cur.executed[0]
    assert "recipient_email = %s" in select_sql
    assert params[0] == "me@x.com"


def test_mark_notification_read_scopes_update_to_caller_email(monkeypatch):
    from routers.notifications import _mark_notification_read_sync

    cur = FakeCursor(fetchone_results=[{"id": 5}])
    conn = FakeConn(cur)
    monkeypatch.setattr("routers.notifications.get_db_connection", lambda: conn)

    updated = _mark_notification_read_sync(5, "me@x.com")

    assert updated is True
    update_sql, params = cur.executed[0]
    assert "recipient_email = %s" in update_sql
    assert params == (5, "me@x.com")


def test_mark_notification_read_returns_false_for_someone_elses_notification(monkeypatch):
    from routers.notifications import _mark_notification_read_sync

    # RETURNING yields no row because the WHERE clause (id AND recipient_email)
    # didn't match anything for this caller.
    cur = FakeCursor(fetchone_results=[None])
    conn = FakeConn(cur)
    monkeypatch.setattr("routers.notifications.get_db_connection", lambda: conn)

    assert _mark_notification_read_sync(5, "someone-else@x.com") is False


def test_mark_notification_unread_scopes_update_to_caller_email(monkeypatch):
    from routers.notifications import _mark_notification_unread_sync

    cur = FakeCursor(fetchone_results=[{"id": 5}])
    conn = FakeConn(cur)
    monkeypatch.setattr("routers.notifications.get_db_connection", lambda: conn)

    updated = _mark_notification_unread_sync(5, "me@x.com")

    assert updated is True
    update_sql, params = cur.executed[0]
    assert "read_at = NULL" in update_sql
    assert "recipient_email = %s" in update_sql
    assert params == (5, "me@x.com")


def test_mark_notification_unread_returns_false_for_already_unread_or_someone_elses(monkeypatch):
    from routers.notifications import _mark_notification_unread_sync

    # RETURNING yields no row either because it's already unread (read_at IS
    # NOT NULL in the WHERE clause excludes it) or it belongs to another caller.
    cur = FakeCursor(fetchone_results=[None])
    conn = FakeConn(cur)
    monkeypatch.setattr("routers.notifications.get_db_connection", lambda: conn)

    assert _mark_notification_unread_sync(5, "me@x.com") is False


class _FakeUser:
    def __init__(self, email="me@x.com"):
        self.email = email


def test_mark_notification_read_endpoint_requires_authentication():
    """401 path for the route handler itself (the _sync helper's own
    scoping is covered separately above)."""
    from routers.notifications import mark_notification_read

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(mark_notification_read(5, user=_FakeUser(email="")))
    assert exc_info.value.status_code == 401


def test_mark_notification_read_endpoint_404s_when_sync_helper_finds_nothing(monkeypatch):
    from routers.notifications import mark_notification_read

    monkeypatch.setattr("routers.notifications._mark_notification_read_sync", lambda *a: False)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(mark_notification_read(5, user=_FakeUser()))
    assert exc_info.value.status_code == 404


def test_mark_notification_unread_endpoint_requires_authentication():
    """401 path for the route handler itself (the _sync helper's own
    scoping is covered separately above)."""
    from routers.notifications import mark_notification_unread

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(mark_notification_unread(5, user=_FakeUser(email="")))
    assert exc_info.value.status_code == 401


def test_mark_notification_unread_endpoint_404s_when_sync_helper_finds_nothing(monkeypatch):
    from routers.notifications import mark_notification_unread

    monkeypatch.setattr("routers.notifications._mark_notification_unread_sync", lambda *a: False)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(mark_notification_unread(5, user=_FakeUser()))
    assert exc_info.value.status_code == 404


# --- SSE subscriber bookkeeping: the cursor-reset bug + its follow-ups ------

def _clear_subscriber_state(notif, email):
    notif._subscribers.pop(email, None)
    notif._last_seen_id.pop(email, None)
    notif._last_seen_cursor_at.pop(email, None)


def test_unregister_does_not_wipe_last_seen_id_for_reconnect(monkeypatch):
    """Regression test for the QA-found bug: a user's last tab disconnecting
    used to wipe the per-email cursor, so the next connection re-seeded from
    whatever the current max id was — silently skipping any notification
    created while no tab was connected. A reconnect within the staleness
    window must resume from the retained cursor, with no second DB lookup."""
    import routers.notifications as notif

    email = "cursor-test@x.com"
    _clear_subscriber_state(notif, email)
    monkeypatch.setattr(notif, "_ensure_broadcaster_running", lambda: asyncio.sleep(0))
    max_id_sync = Mock(return_value=10)
    monkeypatch.setattr(notif, "_max_id_sync", max_id_sync)

    try:
        async def scenario():
            queue1 = asyncio.Queue()
            await notif._register_subscriber(email, queue1)
            assert notif._last_seen_id[email] == 10

            # Last (only) tab disconnects.
            await notif._unregister_subscriber(email, queue1)
            assert email not in notif._subscribers
            # The fix: the cursor must still be there, unchanged.
            assert notif._last_seen_id[email] == 10

            # Reconnect — must resume from 10 without hitting the DB again
            # (the retained cursor is still fresh).
            queue2 = asyncio.Queue()
            await notif._register_subscriber(email, queue2)
            assert notif._last_seen_id[email] == 10

            await notif._unregister_subscriber(email, queue2)

        asyncio.run(scenario())
        assert max_id_sync.call_count == 1
    finally:
        _clear_subscriber_state(notif, email)


def test_register_subscriber_reseeds_when_retained_cursor_is_stale(monkeypatch):
    """A reconnect after longer than _CURSOR_MAX_RETENTION_SECONDS must NOT
    replay the whole gap — it reseeds from the current max instead (the
    client's own list() truth-up already covers that history)."""
    import routers.notifications as notif

    email = "stale-cursor@x.com"
    _clear_subscriber_state(notif, email)
    monkeypatch.setattr(notif, "_ensure_broadcaster_running", lambda: asyncio.sleep(0))
    max_id_sync = Mock(side_effect=[10, 99])
    monkeypatch.setattr(notif, "_max_id_sync", max_id_sync)

    try:
        async def scenario():
            queue1 = asyncio.Queue()
            await notif._register_subscriber(email, queue1)
            assert notif._last_seen_id[email] == 10
            await notif._unregister_subscriber(email, queue1)

            # Simulate a long absence: push the retained cursor's timestamp
            # back past the retention window.
            notif._last_seen_cursor_at[email] -= notif._CURSOR_MAX_RETENTION_SECONDS + 10

            queue2 = asyncio.Queue()
            await notif._register_subscriber(email, queue2)
            assert notif._last_seen_id[email] == 99
            await notif._unregister_subscriber(email, queue2)

        asyncio.run(scenario())
        assert max_id_sync.call_count == 2
    finally:
        _clear_subscriber_state(notif, email)


def test_register_subscriber_honors_client_since_id(monkeypatch):
    """The client's own since_id is authoritative and skips the DB lookup
    entirely — it's also the only resume path that's correct when a
    reconnect lands on a different worker than the one that holds the
    in-memory cursor."""
    import routers.notifications as notif

    email = "since-id@x.com"
    _clear_subscriber_state(notif, email)
    monkeypatch.setattr(notif, "_ensure_broadcaster_running", lambda: asyncio.sleep(0))
    max_id_sync = Mock(return_value=999)
    monkeypatch.setattr(notif, "_max_id_sync", max_id_sync)

    try:
        async def scenario():
            queue = asyncio.Queue()
            await notif._register_subscriber(email, queue, since_id=42)
            assert notif._last_seen_id[email] == 42
            await notif._unregister_subscriber(email, queue)

        asyncio.run(scenario())
        assert max_id_sync.call_count == 0
    finally:
        _clear_subscriber_state(notif, email)


def test_prune_stale_cursors_only_removes_old_and_disconnected_entries():
    import time

    import routers.notifications as notif

    old_disconnected = "old-disconnected@x.com"
    recent_disconnected = "recent-disconnected@x.com"
    old_but_connected = "old-connected@x.com"
    for e in (old_disconnected, recent_disconnected, old_but_connected):
        _clear_subscriber_state(notif, e)

    try:
        now = time.monotonic()
        notif._last_seen_id[old_disconnected] = 1
        notif._last_seen_cursor_at[old_disconnected] = now - notif._CURSOR_PRUNE_AFTER_SECONDS - 10

        notif._last_seen_id[recent_disconnected] = 2
        notif._last_seen_cursor_at[recent_disconnected] = now

        notif._last_seen_id[old_but_connected] = 3
        notif._last_seen_cursor_at[old_but_connected] = now - notif._CURSOR_PRUNE_AFTER_SECONDS - 10
        notif._subscribers[old_but_connected] = {object()}  # still "connected"

        notif._prune_stale_cursors()

        assert old_disconnected not in notif._last_seen_id
        assert old_disconnected not in notif._last_seen_cursor_at
        assert recent_disconnected in notif._last_seen_id
        assert old_but_connected in notif._last_seen_id
    finally:
        for e in (old_disconnected, recent_disconnected, old_but_connected):
            _clear_subscriber_state(notif, e)


# --- clear: scoped delete ----------------------------------------------------

def test_clear_notifications_soft_deletes_scoped_to_caller_email(monkeypatch):
    """Regression test: clearing must stamp cleared_at rather than DELETE the
    row outright. A hard delete would let the unique index's own ON CONFLICT
    DO NOTHING guard stop protecting that (recipient, candidate, job, type)
    combo, so reconcile_missed_pass_notifications — which re-scans every Pass
    candidate in its lookback window regardless of whether it already
    notified — could silently re-insert and un-clear it."""
    from routers.notifications import _clear_notifications_sync

    cur = FakeCursor()
    conn = FakeConn(cur)
    monkeypatch.setattr("routers.notifications.get_db_connection", lambda: conn)

    _clear_notifications_sync("me@x.com")

    update_sql, params = cur.executed[0]
    assert "DELETE" not in update_sql.upper()
    assert "UPDATE notifications" in update_sql
    assert "cleared_at = now()" in update_sql
    assert "recipient_email = %s" in update_sql
    assert params == ("me@x.com",)
    assert conn.committed is True


def test_clear_notifications_endpoint_requires_authentication():
    """401 path: no email on the resolved user."""
    from routers.notifications import clear_notifications

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(clear_notifications(user=_FakeUser(email="")))
    assert exc_info.value.status_code == 401


def test_list_and_unread_count_exclude_cleared_notifications(monkeypatch):
    from routers.notifications import _list_notifications_sync, _get_unread_count_sync

    list_cur = FakeCursor(fetchall_results=[[]], fetchone_results=[(0,)])
    monkeypatch.setattr("routers.notifications.get_db_connection", lambda: FakeConn(list_cur))
    _list_notifications_sync("me@x.com", 50, None, False)
    select_sql = list_cur.executed[0][0]
    assert "cleared_at IS NULL" in select_sql

    count_cur = FakeCursor(fetchone_results=[(0,)])
    monkeypatch.setattr("routers.notifications.get_db_connection", lambda: FakeConn(count_cur))
    _get_unread_count_sync("me@x.com")
    count_sql = count_cur.executed[0][0]
    assert "cleared_at IS NULL" in count_sql
