"""Wiring tests for routers/jobs.py's _notify_newly_added_recruiters (the
diff-and-queue helper) and _fetch_old_recruiter_emails_for_diff_sync (its
before-state lookup), both called from the job wizard's save endpoints
(save_job_draft / save_job_to_monitored_jobs_only) to fire in-app
"added as a recruiter" notifications. Covers this diff logic itself (what
counts as "newly added") rather than the full save endpoints, which have no
existing test harness and are out of scope here.
"""
import json

from routers.jobs import _fetch_old_recruiter_emails_for_diff_sync, _notify_newly_added_recruiters
from services.notifications_service import create_recruiter_assigned_notifications


class FakeBackgroundTasks:
    def __init__(self):
        self.tasks = []

    def add_task(self, func, *args, **kwargs):
        self.tasks.append((func, args, kwargs))


def test_notifies_recruiters_newly_added_to_an_existing_job():
    bg = FakeBackgroundTasks()
    _notify_newly_added_recruiters(
        bg, "J1", "26-14907", "MS SQL Developer",
        old_recruiter_emails=["existing@x.com"],
        new_recruiter_emails=["existing@x.com", "NEW@x.com"],
        acting_user_email="existing@x.com",
    )

    assert len(bg.tasks) == 1
    func, args, kwargs = bg.tasks[0]
    assert func is create_recruiter_assigned_notifications
    assert kwargs["job_id"] == "J1"
    assert kwargs["jobdiva_id"] == "26-14907"
    assert kwargs["job_title"] == "MS SQL Developer"
    assert kwargs["assigned_emails"] == ["new@x.com"]  # lowercased


def test_notifies_every_recruiter_on_a_brand_new_job():
    """old_recruiter_emails empty/missing (no prior row) — every recruiter on
    the new job counts as newly added, covering "assigned to a new
    requisition"."""
    bg = FakeBackgroundTasks()
    _notify_newly_added_recruiters(
        bg, "J2", "26-99999", "Data Engineer",
        old_recruiter_emails=[],
        new_recruiter_emails=["a@x.com", "b@x.com"],
        acting_user_email="creator@x.com",
    )

    assert len(bg.tasks) == 1
    _, _, kwargs = bg.tasks[0]
    assert set(kwargs["assigned_emails"]) == {"a@x.com", "b@x.com"}


def test_excludes_the_acting_user_even_when_newly_added():
    """The person making the change is never notified for assigning
    themselves — whether they added themselves explicitly or
    _ensure_user_in_recruiter_emails did it for them."""
    bg = FakeBackgroundTasks()
    _notify_newly_added_recruiters(
        bg, "J1", "26-14907", "MS SQL Developer",
        old_recruiter_emails=[],
        new_recruiter_emails=["creator@x.com"],
        acting_user_email="creator@x.com",
    )

    assert bg.tasks == []


def test_noop_when_recruiter_list_is_unchanged():
    """The common case: the wizard autosaves repeatedly with the same
    recruiter_emails — must not queue a notification on every save."""
    bg = FakeBackgroundTasks()
    _notify_newly_added_recruiters(
        bg, "J1", "26-14907", "MS SQL Developer",
        old_recruiter_emails=["a@x.com", "b@x.com"],
        new_recruiter_emails=["b@x.com", "a@x.com"],  # same set, different order
        acting_user_email="a@x.com",
    )

    assert bg.tasks == []


def test_noop_when_a_recruiter_is_removed_not_added():
    bg = FakeBackgroundTasks()
    _notify_newly_added_recruiters(
        bg, "J1", "26-14907", "MS SQL Developer",
        old_recruiter_emails=["a@x.com", "b@x.com"],
        new_recruiter_emails=["a@x.com"],
        acting_user_email="a@x.com",
    )

    assert bg.tasks == []


def test_case_insensitive_diff_does_not_treat_same_email_as_new():
    bg = FakeBackgroundTasks()
    _notify_newly_added_recruiters(
        bg, "J1", "26-14907", "MS SQL Developer",
        old_recruiter_emails=["Existing@X.com"],
        new_recruiter_emails=["existing@x.com"],
        acting_user_email="someone-else@x.com",
    )

    assert bg.tasks == []


def test_skips_the_diff_entirely_when_old_state_is_unknown():
    """old_recruiter_emails=None means _fetch_old_recruiter_emails_for_diff_sync
    couldn't determine the prior state (a DB hiccup, not "no prior
    recruiters") — PR review follow-up: treating that as empty would risk
    falsely notifying a recruiter who was already assigned before this
    save. The diff must be skipped entirely, not run against an assumed-
    empty old list."""
    bg = FakeBackgroundTasks()
    _notify_newly_added_recruiters(
        bg, "J1", "26-14907", "MS SQL Developer",
        old_recruiter_emails=None,
        new_recruiter_emails=["a@x.com"],
        acting_user_email="someone-else@x.com",
    )

    assert bg.tasks == []


# --- _fetch_old_recruiter_emails_for_diff_sync -------------------------------

class _FakeCursor:
    def __init__(self, fetchone_result=None, raise_on_execute=None):
        self._fetchone_result = fetchone_result
        self._raise_on_execute = raise_on_execute

    def execute(self, sql, params=None):
        if self._raise_on_execute:
            raise self._raise_on_execute

    def fetchone(self):
        return self._fetchone_result

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_fetch_old_recruiter_emails_returns_parsed_list_on_success(monkeypatch):
    cur = _FakeCursor(fetchone_result=(json.dumps(["a@x.com", "b@x.com"]),))
    monkeypatch.setattr("routers.jobs.get_db_connection", lambda: _FakeConn(cur))

    assert _fetch_old_recruiter_emails_for_diff_sync("J1") == ["a@x.com", "b@x.com"]


def test_fetch_old_recruiter_emails_returns_empty_list_when_job_not_found(monkeypatch):
    """A brand-new job has no row yet — [] (known-empty), not None
    (unknown) — every recruiter on the new job should still count as
    "newly added" in this case."""
    cur = _FakeCursor(fetchone_result=None)
    monkeypatch.setattr("routers.jobs.get_db_connection", lambda: _FakeConn(cur))

    assert _fetch_old_recruiter_emails_for_diff_sync("J-does-not-exist") == []


def test_fetch_old_recruiter_emails_returns_empty_list_for_null_column(monkeypatch):
    cur = _FakeCursor(fetchone_result=(None,))
    monkeypatch.setattr("routers.jobs.get_db_connection", lambda: _FakeConn(cur))

    assert _fetch_old_recruiter_emails_for_diff_sync("J1") == []


def test_fetch_old_recruiter_emails_returns_none_on_db_failure(monkeypatch):
    """PR review follow-up: a DB hiccup here must surface as None (unknown),
    not crash the save and not silently look like "no prior recruiters"."""
    cur = _FakeCursor(raise_on_execute=RuntimeError("db unavailable"))
    monkeypatch.setattr("routers.jobs.get_db_connection", lambda: _FakeConn(cur))

    assert _fetch_old_recruiter_emails_for_diff_sync("J1") is None
