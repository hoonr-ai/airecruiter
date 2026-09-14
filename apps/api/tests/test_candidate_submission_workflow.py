from models import CandidateFeedbackRequest


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
	from routers.candidates import _manager_email_allowed

	monkeypatch.setenv("PAIR_MANAGER_EMAIL_DOMAINS", "pyramidci.com, reviewer@celsiortech.com")

	assert _manager_email_allowed("lead@pyramidci.com")
	assert _manager_email_allowed("reviewer@celsiortech.com")
	assert not _manager_email_allowed("reviewer@example.com")


def test_internal_submission_email_is_passive_review_link(monkeypatch):
	from core.email import notify_internal_submission_to_manager

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
