"""Wiring test for main.py's _fire_pair_inactive_notification: the email and
the in-app notification are independent channels (PR review follow-up — see
services/notifications_service.py's create_pair_inactive_notification
docstring). A failure in the email send must not prevent the in-app
notification from firing, and must not prevent the claim-release-for-retry
logic from running either.
"""
import asyncio
from unittest.mock import AsyncMock

import main as main_module


class FakeCursor:
    def __init__(self, fetchone_results=None):
        self.executed = []
        self._fetchone_results = list(fetchone_results or [])

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self._fetchone_results.pop(0) if self._fetchone_results else None


class FakeConn:
    def __init__(self, cursor: FakeCursor):
        self._cursor = cursor
        self.committed = 0
        self.closed = False

    def cursor(self, cursor_factory=None):
        return self._cursor

    def commit(self):
        self.committed += 1

    def close(self):
        self.closed = True


def _claim_row():
    return {
        "recruiter_emails": ["a@x.com"],
        "jobdiva_id": "26-14907",
        "title": "MS SQL Developer",
        "enhanced_title": None,
    }


def test_in_app_notification_fires_even_when_email_send_fails(monkeypatch):
    cur = FakeCursor(fetchone_results=[_claim_row()])
    conn = FakeConn(cur)
    monkeypatch.setattr(main_module, "get_db_connection", lambda: conn)

    def _boom(**kwargs):
        raise RuntimeError("smtp down")
    monkeypatch.setattr(main_module, "notify_pair_inactive", _boom)

    in_app_mock = AsyncMock()
    monkeypatch.setattr(main_module, "create_pair_inactive_notification", in_app_mock)

    asyncio.run(main_module._fire_pair_inactive_notification("J1"))

    # The in-app call must have run despite the email raising.
    in_app_mock.assert_awaited_once()
    kwargs = in_app_mock.await_args.kwargs
    assert kwargs["job_id"] == "J1"
    assert kwargs["jobdiva_id"] == "26-14907"
    assert kwargs["job_title"] == "MS SQL Developer"
    assert kwargs["recruiter_emails"] == ["a@x.com"]

    # The existing release-for-retry behavior must still have run too.
    release_calls = [c for c in cur.executed if "pair_inactive_notified_at = NULL" in c[0]]
    assert len(release_calls) == 1


def test_in_app_notification_fires_when_email_succeeds(monkeypatch):
    cur = FakeCursor(fetchone_results=[_claim_row()])
    conn = FakeConn(cur)
    monkeypatch.setattr(main_module, "get_db_connection", lambda: conn)
    monkeypatch.setattr(main_module, "notify_pair_inactive", lambda **kwargs: True)

    in_app_mock = AsyncMock()
    monkeypatch.setattr(main_module, "create_pair_inactive_notification", in_app_mock)

    asyncio.run(main_module._fire_pair_inactive_notification("J1"))

    in_app_mock.assert_awaited_once()
    # No release-for-retry on the success path.
    assert not any("pair_inactive_notified_at = NULL" in c[0] for c in cur.executed)


def test_no_in_app_call_when_claim_is_already_held(monkeypatch):
    """Another worker already claimed this job — RETURNING gives no row, so
    neither the email nor the in-app notification should fire again."""
    cur = FakeCursor(fetchone_results=[None])
    conn = FakeConn(cur)
    monkeypatch.setattr(main_module, "get_db_connection", lambda: conn)
    monkeypatch.setattr(main_module, "notify_pair_inactive", lambda **kwargs: True)

    in_app_mock = AsyncMock()
    monkeypatch.setattr(main_module, "create_pair_inactive_notification", in_app_mock)

    asyncio.run(main_module._fire_pair_inactive_notification("J1"))

    in_app_mock.assert_not_awaited()
