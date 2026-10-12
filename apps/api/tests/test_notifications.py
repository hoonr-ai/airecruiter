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
"""
import asyncio
import json
from unittest.mock import Mock

import pytest

from services.notifications_service import (
    _parse_recruiter_emails,
    _insert_notification,
    create_candidate_passed_notifications,
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
        cur, recipient_email="a@x.com", job_id="J1", jobdiva_id="JD1",
        candidate_id="C1", title="t", body=None, score=90.0,
    )
    assert inserted is True
    sql, params = cur.executed[0]
    assert "ON CONFLICT" in sql and "DO NOTHING" in sql and "RETURNING id" in sql


def test_insert_notification_returns_false_on_conflict():
    cur = FakeCursor(fetchone_results=[None])
    inserted = _insert_notification(
        cur, recipient_email="a@x.com", job_id="J1", jobdiva_id="JD1",
        candidate_id="C1", title="t", body=None, score=90.0,
    )
    assert inserted is False


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

def test_clear_notifications_scopes_delete_to_caller_email(monkeypatch):
    from routers.notifications import _clear_notifications_sync

    cur = FakeCursor()
    conn = FakeConn(cur)
    monkeypatch.setattr("routers.notifications.get_db_connection", lambda: conn)

    _clear_notifications_sync("me@x.com")

    delete_sql, params = cur.executed[0]
    assert "DELETE FROM notifications" in delete_sql
    assert "recipient_email = %s" in delete_sql
    assert params == ("me@x.com",)
    assert conn.committed is True
