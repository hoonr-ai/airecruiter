"""Wiring tests for routers/jobs.py's _notify_newly_added_recruiters — the
diff-and-queue helper called from the job wizard's save endpoints
(save_job_draft / save_job_to_monitored_jobs_only) to fire in-app
"added as a recruiter" notifications. Covers the diff logic itself (what
counts as "newly added") rather than the full save endpoints, which have no
existing test harness and are out of scope here.
"""
from routers.jobs import _notify_newly_added_recruiters
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


def test_tolerates_non_list_old_value():
    """old_recruiter_emails can be None (e.g. _get_job_draft_sync returned an
    error dict) — must be treated as empty, not raise."""
    bg = FakeBackgroundTasks()
    _notify_newly_added_recruiters(
        bg, "J1", "26-14907", "MS SQL Developer",
        old_recruiter_emails=None,
        new_recruiter_emails=["a@x.com"],
        acting_user_email="someone-else@x.com",
    )

    assert len(bg.tasks) == 1
    assert bg.tasks[0][2]["assigned_emails"] == ["a@x.com"]
