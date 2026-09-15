import pytest
from fastapi import HTTPException

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

    res = await cand_module.save_candidate_feedback(
        job_id_or_ref="26-12137",
        candidate_id="55148489",
        request=request,
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

    res = await cand_module.save_candidate_feedback(
        job_id_or_ref="26-12137",
        candidate_id="55148489",
        request=request,
        user=user
    )

    assert res["status"] == "success"
    assert res["action_string"] == "PAIR External Submission"
    assert res["submission_type"] == "external"
    assert len(jobdiva_calls) == 1
    assert jobdiva_calls[0]["action"] == "PAIR External Submission"


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

    res = await cand_module.save_candidate_feedback(
        job_id_or_ref="26-12137",
        candidate_id="55148489",
        request=request,
        user=user
    )

    assert res["status"] == "success"
    assert res["action_string"] == "PAIR Reject - Skills do not meet requirements"
    assert len(jobdiva_calls) == 1
    assert jobdiva_calls[0]["action"] == "PAIR Reject - Skills do not meet requirements"


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
            user=user
        )

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == "Failed to persist candidate feedback in database. Please try again."
    # Verify that because DB failed first, no JobDiva note was created
    assert len(jobdiva_calls) == 0
