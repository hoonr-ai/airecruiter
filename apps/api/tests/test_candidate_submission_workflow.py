import pytest
from fastapi import BackgroundTasks, HTTPException

from core.auth import UserIdentity
from core.email import notify_internal_submission_to_manager
from models import CandidateFeedbackRequest
from routers.candidates import _manager_email_allowed


def test_feedback_request_defaults_submit_to_external_mode():
    request = CandidateFeedbackRequest(feedback_type="Submit")

    assert request.feedback_type == "Submit"
    assert request.submission_type == "external"
    assert request.manager_email is None
    assert request.recruiter_notes is None


def test_feedback_request_allows_internal_submission_metadata():
    request = CandidateFeedbackRequest(
        feedback_type="Submit",
        submission_type="internal",
        manager_email="manager@pyramidci.com",
        recruiter_notes="Strong candidate for manager review.",
    )

    assert request.submission_type == "internal"
    assert request.manager_email == "manager@pyramidci.com"
    assert request.recruiter_notes == "Strong candidate for manager review."


def test_manager_email_domain_allowlist_accepts_domain_and_exact_email(monkeypatch):
    monkeypatch.setenv("PAIR_MANAGER_EMAIL_DOMAINS", "pyramidci.com, reviewer@celsiortech.com")

    assert _manager_email_allowed("lead@pyramidci.com")
    assert _manager_email_allowed("reviewer@celsiortech.com")
    assert not _manager_email_allowed("reviewer@example.com")


def test_manager_email_domain_allowlist_strips_quotes_and_padding(monkeypatch):
    monkeypatch.setenv("PAIR_MANAGER_EMAIL_DOMAINS", ' "pyramidci.com" , \'celsiortech.com\' ,  genspark.net  ')

    assert _manager_email_allowed("manager@pyramidci.com")
    assert _manager_email_allowed("lead@celsiortech.com")
    assert _manager_email_allowed("admin@genspark.net")
    assert not _manager_email_allowed("other@external.com")


def test_internal_submission_email_is_passive_review_link(monkeypatch):
    sent = {}

    def fake_send(to_list, subject, html_body, plain_body, **_kwargs):
        sent["to_list"] = to_list
        sent["subject"] = subject
        sent["html_body"] = html_body
        sent["plain_body"] = plain_body
        return True

    monkeypatch.setattr("core.email._send", fake_send)

    assert notify_internal_submission_to_manager(
        manager_email="manager@pyramidci.com",
        recruiter_name="Recruiter One",
        recruiter_email="recruiter@pyramidci.com",
        candidate_name="Lakshman Teja",
        candidate_id="21562841721070",
        job_id_or_ref="33087136",
        job_title="Business Systems Analyst 2",
        customer_name="Samsung Electronics America",
        recruiter_notes="Hi test 1",
        app_base_url="https://pairqa.pyramidci.com",
    )

    assert "manager@pyramidci.com" in sent["to_list"]
    assert "Internal Candidate Submission" in sent["subject"]
    assert "https://pairqa.pyramidci.com/jobs/33087136/report?candidateId=21562841721070" in sent["html_body"]
    assert "Review Candidate" in sent["html_body"]
    assert "Opening this link will not submit the candidate externally" in sent["html_body"]
    assert "only from the report page" in sent["plain_body"]


class _FakeCursor:
    def __init__(self, select_row=None, fail_on_update=False):
        self.select_row = select_row
        self.fail_on_update = fail_on_update
        self.executed_queries = []

    def execute(self, query, params=None):
        self.executed_queries.append((query, params))
        if self.fail_on_update and "UPDATE" in query:
            raise RuntimeError("Database connection timed out during update")

    def fetchone(self):
        return self.select_row

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


class _FakeConnection:
    def __init__(self, select_row=None, fail_on_update=False):
        self.select_row = select_row
        self.fail_on_update = fail_on_update
        self.committed = False
        self.closed = False

    def cursor(self):
        return _FakeCursor(self.select_row, self.fail_on_update)

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


def _queued_kwargs(background_tasks: BackgroundTasks, func) -> list:
    """save_candidate_feedback queues its notification helpers (rejected,
    internal-review-requested, externally-submitted) via
    background_tasks.add_task rather than awaiting them
    (PR review follow-up — keeps the response fast; the notification
    helpers already swallow their own errors). Pulls out the kwargs of
    every queued call to `func` without actually running it."""
    return [t.kwargs for t in background_tasks.tasks if t.func is func]


@pytest.mark.anyio
async def test_save_candidate_feedback_internal_submit_route(monkeypatch):
    from routers import candidates as cand_module
    from services.jobdiva import jobdiva_service

    monkeypatch.setattr(cand_module, "_verify_job_access_by_id", lambda *a, **kw: None)

    fake_cand_row = (
        55148489,             # sc.id
        "21562841721070",     # sc.candidate_id
        "26-12137",           # sc.jobdiva_id
        {"jobdiva_candidate_id": "21562841721070"}, # sc.data
        "33087136",           # mj.job_id
        "Srinivasan Subramanian", # sc.name
        "Senior Java Developer", # mj.title
        "Pyramid Consulting"  # mj.customer_name
    )

    fake_conn = _FakeConnection(select_row=fake_cand_row)
    monkeypatch.setattr(cand_module, "get_db_connection", lambda: fake_conn)
    monkeypatch.setenv("PAIR_MANAGER_EMAIL_DOMAINS", "pyramidci.com, celsiortech.com")

    jobdiva_calls = []
    async def fake_create_note(**kwargs):
        jobdiva_calls.append(kwargs)
        return {"status": "success"}

    monkeypatch.setattr(jobdiva_service, "create_candidate_note", fake_create_note)

    email_calls = []
    def fake_notify_manager(**kwargs):
        email_calls.append(kwargs)
        return True

    monkeypatch.setattr("core.email.notify_internal_submission_to_manager", fake_notify_manager)
    monkeypatch.setattr(cand_module, "refresh_feedback_metrics_sync", lambda job_ref: {"feedback_completed": 1, "pair_submits": 1})

    user = UserIdentity(
        email="ronak.jain@celsiortech.com",
        role="recruiter"
    )

    request = CandidateFeedbackRequest(
        feedback_type="Submit",
        submission_type="internal",
        manager_email="Biswajit.Jena@celsiortech.com",
        recruiter_notes="Test notes for candidate review."
    )

    background_tasks = BackgroundTasks()
    res = await cand_module.save_candidate_feedback(
        job_id_or_ref="26-12137",
        candidate_id="55148489",
        request=request,
        background_tasks=background_tasks,
        user=user
    )

    assert res["status"] == "success"
    assert res["action_string"] == "PAIR Internal Submission"
    assert res["submission_type"] == "internal"
    assert res["jobdiva_sync"] == "success"
    assert res["manager_email_sent"] is True

    assert len(jobdiva_calls) == 1
    assert jobdiva_calls[0]["action"] == "PAIR Internal Submission"
    assert "Click Here" in jobdiva_calls[0]["note_text"]
    assert "https://pair.pyramidci.com/jobs/33087136/report?candidateId=21562841721070" in jobdiva_calls[0]["note_text"]

    assert len(email_calls) == 1
    assert email_calls[0]["manager_email"] == "Biswajit.Jena@celsiortech.com"
    assert email_calls[0]["candidate_name"] == "Srinivasan Subramanian"
    # The manager email links the same JobDiva profile the panel/rank list open.
    assert email_calls[0]["jobdiva_candidate_id"] == "21562841721070"

    # No generic candidate_submitted notification — an internal Submit's
    # own notification is the more specific one below, to the named manager
    # (not every other co-recruiter on the job).
    review_calls = _queued_kwargs(
        background_tasks, cand_module.create_candidate_internal_review_requested_notification
    )
    assert len(review_calls) == 1
    assert review_calls[0]["job_id"] == "33087136"
    assert review_calls[0]["candidate_id"] == "21562841721070"
    assert review_calls[0]["manager_email"] == "Biswajit.Jena@celsiortech.com"

    # Not the external-submission follow-through notification — this request
    # itself is the internal submission, not a resolution of an earlier one.
    assert _queued_kwargs(background_tasks, cand_module.create_candidate_externally_submitted_notification) == []


@pytest.mark.anyio
async def test_save_candidate_feedback_external_submit_route(monkeypatch):
    from routers import candidates as cand_module
    from services.jobdiva import jobdiva_service

    monkeypatch.setattr(cand_module, "_verify_job_access_by_id", lambda *a, **kw: None)

    fake_cand_row = (
        55148489, "21562841721070", "26-12137", {}, "33087136",
        "Srinivasan Subramanian", "Senior Java Developer", "Pyramid Consulting"
    )
    fake_conn = _FakeConnection(select_row=fake_cand_row)
    monkeypatch.setattr(cand_module, "get_db_connection", lambda: fake_conn)

    jobdiva_calls = []
    async def fake_create_note(**kwargs):
        jobdiva_calls.append(kwargs)
        return {"status": "success"}

    monkeypatch.setattr(jobdiva_service, "create_candidate_note", fake_create_note)
    monkeypatch.setattr(cand_module, "refresh_feedback_metrics_sync", lambda job_ref: {"feedback_completed": 1, "pair_submits": 1})

    user = UserIdentity(
        email="recruiter@pyramidci.com",
        role="recruiter"
    )

    request = CandidateFeedbackRequest(
        feedback_type="Submit",
        submission_type="external"
    )

    background_tasks = BackgroundTasks()
    res = await cand_module.save_candidate_feedback(
        job_id_or_ref="26-12137",
        candidate_id="55148489",
        request=request,
        background_tasks=background_tasks,
        user=user
    )

    assert res["status"] == "success"
    assert res["action_string"] == "PAIR External Submission"
    assert res["submission_type"] == "external"
    assert len(jobdiva_calls) == 1
    assert jobdiva_calls[0]["action"] == "PAIR External Submission"

    # No prior feedback on this candidate (data={}) — a fresh external
    # submission, not the resolution of an earlier internal one, and no
    # generic candidate_submitted notification exists to fall back on. This
    # path queues no candidate-level notification at all (PR #812 review
    # follow-up — a plain external Submit with no pending internal review
    # to resolve notifies nobody, which is the intended, acknowledged
    # behavior change from retiring candidate_submitted).
    assert _queued_kwargs(background_tasks, cand_module.create_candidate_externally_submitted_notification) == []
    assert _queued_kwargs(background_tasks, cand_module.create_candidate_internal_review_requested_notification) == []
    assert _queued_kwargs(background_tasks, cand_module.create_candidate_rejected_notifications) == []


@pytest.mark.anyio
async def test_save_candidate_feedback_external_submit_after_internal_notifies_original_requester(monkeypatch):
    """The manager approves an internally-submitted candidate by externally
    submitting it themselves — the recruiter who originally requested that
    review should be told, per the enhancement requested after QA testing
    the Submit/Reject flow end to end."""
    from routers import candidates as cand_module
    from services.jobdiva import jobdiva_service

    monkeypatch.setattr(cand_module, "_verify_job_access_by_id", lambda *a, **kw: None)

    # This candidate's stored data reflects the PRIOR (internal) submission —
    # exactly what save_candidate_feedback's own persist step would have
    # left behind before this (external, by the manager) request runs.
    fake_cand_row = (
        55148489, "21562841721070", "26-12137",
        {"submission_type": "internal", "internal_review_requested_by": "ronak.jain@celsiortech.com"},
        "33087136", "Srinivasan Subramanian", "Senior Java Developer", "Pyramid Consulting",
    )
    fake_conn = _FakeConnection(select_row=fake_cand_row)
    monkeypatch.setattr(cand_module, "get_db_connection", lambda: fake_conn)

    async def fake_create_note(**kwargs):
        return {"status": "success"}
    monkeypatch.setattr(jobdiva_service, "create_candidate_note", fake_create_note)
    monkeypatch.setattr(cand_module, "refresh_feedback_metrics_sync", lambda job_ref: {"feedback_completed": 1, "pair_submits": 1})

    manager = UserIdentity(email="biswajit.jena@celsiortech.com", role="recruiter")
    request = CandidateFeedbackRequest(feedback_type="Submit", submission_type="external")

    background_tasks = BackgroundTasks()
    res = await cand_module.save_candidate_feedback(
        job_id_or_ref="26-12137", candidate_id="55148489",
        request=request, background_tasks=background_tasks, user=manager,
    )

    assert res["status"] == "success"
    follow_through = _queued_kwargs(background_tasks, cand_module.create_candidate_externally_submitted_notification)
    assert len(follow_through) == 1
    assert follow_through[0]["job_id"] == "33087136"
    assert follow_through[0]["candidate_id"] == "21562841721070"
    assert follow_through[0]["recipient_email"] == "ronak.jain@celsiortech.com"


@pytest.mark.anyio
async def test_save_candidate_feedback_external_submit_skips_follow_through_when_submitter_is_the_same_person(monkeypatch):
    """If the SAME recruiter who did the internal submission later submits
    the same candidate externally themselves, there's nothing to tell them
    that they don't already know — must not notify them about their own
    action."""
    from routers import candidates as cand_module
    from services.jobdiva import jobdiva_service

    monkeypatch.setattr(cand_module, "_verify_job_access_by_id", lambda *a, **kw: None)

    fake_cand_row = (
        55148489, "21562841721070", "26-12137",
        {"submission_type": "internal", "internal_review_requested_by": "ronak.jain@celsiortech.com"},
        "33087136", "Srinivasan Subramanian", "Senior Java Developer", "Pyramid Consulting",
    )
    fake_conn = _FakeConnection(select_row=fake_cand_row)
    monkeypatch.setattr(cand_module, "get_db_connection", lambda: fake_conn)

    async def fake_create_note(**kwargs):
        return {"status": "success"}
    monkeypatch.setattr(jobdiva_service, "create_candidate_note", fake_create_note)
    monkeypatch.setattr(cand_module, "refresh_feedback_metrics_sync", lambda job_ref: {"feedback_completed": 1, "pair_submits": 1})

    same_recruiter = UserIdentity(email="Ronak.Jain@celsiortech.com", role="recruiter")
    request = CandidateFeedbackRequest(feedback_type="Submit", submission_type="external")

    background_tasks = BackgroundTasks()
    await cand_module.save_candidate_feedback(
        job_id_or_ref="26-12137", candidate_id="55148489",
        request=request, background_tasks=background_tasks, user=same_recruiter,
    )

    assert _queued_kwargs(background_tasks, cand_module.create_candidate_externally_submitted_notification) == []


@pytest.mark.anyio
async def test_save_candidate_feedback_external_submit_after_internal_then_reject_still_notifies_original_requester(monkeypatch):
    """Regression for a PR review finding: an intervening Reject by a
    DIFFERENT person between the internal submission and its eventual
    external resolution used to leave "submitted_by" pointing at the
    rejecter, so the follow-through notification went to the wrong person.
    internal_review_requested_by is set only by the internal Submit and
    left untouched by Reject/Unreachable, so it still names the original
    requester here even though submitted_by (and the stale submission_type
    left over from the internal Submit) now reflect the rejecter's action."""
    from routers import candidates as cand_module
    from services.jobdiva import jobdiva_service

    monkeypatch.setattr(cand_module, "_verify_job_access_by_id", lambda *a, **kw: None)

    fake_cand_row = (
        55148489, "21562841721070", "26-12137",
        {
            "feedback_type": "Reject",
            "feedback_reason": "Candidate withdrew interest",
            "submitted_by": "other.recruiter@celsiortech.com",   # the rejecter, not the original requester
            "submission_type": "internal",                        # stale leftover from the earlier internal Submit
            "manager_email": "biswajit.jena@celsiortech.com",
            "internal_review_requested_by": "ronak.jain@celsiortech.com",  # untouched by the Reject
        },
        "33087136", "Srinivasan Subramanian", "Senior Java Developer", "Pyramid Consulting",
    )
    fake_conn = _FakeConnection(select_row=fake_cand_row)
    monkeypatch.setattr(cand_module, "get_db_connection", lambda: fake_conn)

    async def fake_create_note(**kwargs):
        return {"status": "success"}
    monkeypatch.setattr(jobdiva_service, "create_candidate_note", fake_create_note)
    monkeypatch.setattr(cand_module, "refresh_feedback_metrics_sync", lambda job_ref: {"feedback_completed": 1, "pair_submits": 1})

    manager = UserIdentity(email="biswajit.jena@celsiortech.com", role="recruiter")
    request = CandidateFeedbackRequest(feedback_type="Submit", submission_type="external")

    background_tasks = BackgroundTasks()
    res = await cand_module.save_candidate_feedback(
        job_id_or_ref="26-12137", candidate_id="55148489",
        request=request, background_tasks=background_tasks, user=manager,
    )

    assert res["status"] == "success"
    follow_through = _queued_kwargs(background_tasks, cand_module.create_candidate_externally_submitted_notification)
    assert len(follow_through) == 1
    assert follow_through[0]["recipient_email"] == "ronak.jain@celsiortech.com"


@pytest.mark.anyio
async def test_save_candidate_feedback_rejection_route(monkeypatch):
    from routers import candidates as cand_module
    from services.jobdiva import jobdiva_service

    monkeypatch.setattr(cand_module, "_verify_job_access_by_id", lambda *a, **kw: None)

    fake_cand_row = (
        55148489, "21562841721070", "26-12137", {}, "33087136",
        "Srinivasan Subramanian", "Senior Java Developer", "Pyramid Consulting"
    )
    fake_conn = _FakeConnection(select_row=fake_cand_row)
    monkeypatch.setattr(cand_module, "get_db_connection", lambda: fake_conn)

    jobdiva_calls = []
    async def fake_create_note(**kwargs):
        jobdiva_calls.append(kwargs)
        return {"status": "success"}

    monkeypatch.setattr(jobdiva_service, "create_candidate_note", fake_create_note)
    monkeypatch.setattr(cand_module, "refresh_feedback_metrics_sync", lambda job_ref: {"feedback_completed": 1, "pair_submits": 0})

    user = UserIdentity(
        email="recruiter@pyramidci.com",
        role="recruiter"
    )

    request = CandidateFeedbackRequest(
        feedback_type="Reject",
        reason="Skills do not meet requirements"
    )

    background_tasks = BackgroundTasks()
    res = await cand_module.save_candidate_feedback(
        job_id_or_ref="26-12137",
        candidate_id="55148489",
        request=request,
        background_tasks=background_tasks,
        user=user
    )

    assert res["status"] == "success"
    assert res["action_string"] == "PAIR Reject - Skills do not meet requirements"
    assert len(jobdiva_calls) == 1
    assert jobdiva_calls[0]["action"] == "PAIR Reject - Skills do not meet requirements"

    rejected_calls = _queued_kwargs(background_tasks, cand_module.create_candidate_rejected_notifications)
    assert len(rejected_calls) == 1
    assert rejected_calls[0]["job_id"] == "33087136"
    assert rejected_calls[0]["candidate_id"] == "21562841721070"
    assert rejected_calls[0]["rejecter_email"] == "recruiter@pyramidci.com"


@pytest.mark.anyio
async def test_save_candidate_feedback_db_error_raises_500(monkeypatch):
    from routers import candidates as cand_module
    from services.jobdiva import jobdiva_service

    monkeypatch.setattr(cand_module, "_verify_job_access_by_id", lambda *a, **kw: None)

    fake_cand_row = (
        55148489, "21562841721070", "26-12137", {}, "33087136",
        "Srinivasan Subramanian", "Senior Java Developer", "Pyramid Consulting"
    )
    fake_conn = _FakeConnection(select_row=fake_cand_row, fail_on_update=True)
    monkeypatch.setattr(cand_module, "get_db_connection", lambda: fake_conn)

    jobdiva_calls = []
    async def fake_create_note(**kwargs):
        jobdiva_calls.append(kwargs)
        return {"status": "success"}

    monkeypatch.setattr(jobdiva_service, "create_candidate_note", fake_create_note)

    user = UserIdentity(
        email="recruiter@pyramidci.com",
        role="recruiter"
    )

    request = CandidateFeedbackRequest(
        feedback_type="Submit",
        submission_type="external"
    )

    with pytest.raises(HTTPException) as exc_info:
        await cand_module.save_candidate_feedback(
            job_id_or_ref="26-12137",
            candidate_id="55148489",
            request=request,
            background_tasks=BackgroundTasks(),
            user=user
        )

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == "Failed to persist candidate feedback in database. Please try again."
    # Verify that because DB failed first, no JobDiva note was created
    assert len(jobdiva_calls) == 0


@pytest.mark.anyio
async def test_save_candidate_feedback_with_numeric_candidate_id_string(monkeypatch):
    from routers import candidates as cand_module
    from services.jobdiva import jobdiva_service

    monkeypatch.setattr(cand_module, "_verify_job_access_by_id", lambda *a, **kw: None)

    fake_cand_row = (
        123, "21574563071254", "26-12137", {}, "32163479",
        "Srinivasan Subramanian", "Dummy - Java Developer", "Pyramid"
    )
    fake_conn = _FakeConnection(select_row=fake_cand_row)
    monkeypatch.setattr(cand_module, "get_db_connection", lambda: fake_conn)

    jobdiva_calls = []
    async def fake_create_note(**kwargs):
        jobdiva_calls.append(kwargs)
        return {"status": "success"}

    monkeypatch.setattr(jobdiva_service, "create_candidate_note", fake_create_note)
    monkeypatch.setattr(cand_module, "refresh_feedback_metrics_sync", lambda job_ref: {"feedback_completed": 1, "pair_submits": 1})

    user = UserIdentity(
        email="recruiter@pyramidci.com",
        role="recruiter"
    )

    request = CandidateFeedbackRequest(
        feedback_type="Submit",
        submission_type="external"
    )

    # Calling with JobDiva numeric candidate_id string (from report page)
    background_tasks = BackgroundTasks()
    res = await cand_module.save_candidate_feedback(
        job_id_or_ref="32163479",
        candidate_id="21574563071254",
        request=request,
        background_tasks=background_tasks,
        user=user
    )

    assert res["status"] == "success"
    assert res["action_string"] == "PAIR External Submission"
    assert res["jobdiva_candidate_id"] == "21574563071254"
    assert res["jobdiva_job_ref"] == "32163479"
    assert len(jobdiva_calls) == 1
    assert jobdiva_calls[0]["candidate_id"] == "21574563071254"
    assert jobdiva_calls[0]["job_id"] == "32163479"
    assert jobdiva_calls[0]["action"] == "PAIR External Submission"
    # No prior internal review and no generic candidate_submitted
    # notification to fall back on — nothing to queue on this path; ID
    # resolution is already verified via jobdiva_calls/res above.

