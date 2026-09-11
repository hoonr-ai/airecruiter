"""
Tests for internal vs external candidate submission feedback handling.
Verifies JobDiva action strings, database payload formatting, and manager notification dispatch.
"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from models import CandidateFeedbackRequest


def test_candidate_feedback_request_model_defaults():
    req = CandidateFeedbackRequest(feedback_type="Submit")
    assert req.feedback_type == "Submit"
    assert req.submission_type == "external"
    assert req.manager_email is None
    assert req.recruiter_notes is None


def test_candidate_feedback_request_model_internal():
    req = CandidateFeedbackRequest(
        feedback_type="Submit",
        submission_type="internal",
        manager_email="manager@pyramidci.com",
        recruiter_notes="Strong candidate on React/Python."
    )
    assert req.feedback_type == "Submit"
    assert req.submission_type == "internal"
    assert req.manager_email == "manager@pyramidci.com"
    assert req.recruiter_notes == "Strong candidate on React/Python."


def test_notify_internal_submission_to_manager_email_formatting():
    from core.email import notify_internal_submission_to_manager
    
    with patch("core.email._send", return_value=True) as mock_send:
        result = notify_internal_submission_to_manager(
            manager_email="manager@pyramidci.com",
            recruiter_name="John Recruiter",
            recruiter_email="john@pyramidci.com",
            candidate_name="Jane Doe",
            candidate_id="12345",
            job_id_or_ref="26-28150",
            job_title="Senior Software Engineer",
            customer_name="Acme Corp",
            recruiter_notes="Candidate looks great.",
            app_base_url="https://pairqa.pyramidci.com",
        )
        assert result is True
        assert mock_send.called
        to_list, subject, html_body, plain_body = mock_send.call_args[0][:4]
        assert "manager@pyramidci.com" in to_list
        assert "john@pyramidci.com" in to_list
        assert "Internal Candidate Submission: Jane Doe" in subject
        assert "https://pairqa.pyramidci.com/jobs/26-28150/report?candidateId=12345" in html_body
        assert "Candidate looks great." in html_body
        assert "Review Candidate &amp; Submit Externally" in html_body


def test_notify_internal_submission_empty_manager_email():
    from core.email import notify_internal_submission_to_manager
    
    result = notify_internal_submission_to_manager(
        manager_email="",
        recruiter_name="John",
        recruiter_email="john@pyramidci.com",
        candidate_name="Jane",
        candidate_id="123",
        job_id_or_ref="26-100",
        job_title="Dev",
    )
    assert result is False
