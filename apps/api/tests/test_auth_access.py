import pytest
from fastapi import HTTPException
from unittest.mock import patch
from core.auth import verify_job_access, UserIdentity
from routers.jobs import _verify_job_access_by_id

def test_verify_job_access_allows_admin():
    user = UserIdentity(email="test@example.com", role="admin")
    job_data = {"recruiter_emails": ["other@example.com"]}
    # Should not raise exception
    verify_job_access(job_data, user)

def test_verify_job_access_allows_assigned():
    user = UserIdentity(email="test@example.com", role="recruiter")
    job_data = {"recruiter_emails": ["test@example.com", "other@example.com"]}
    # Should not raise exception
    verify_job_access(job_data, user)

def test_verify_job_access_denies_unassigned():
    user = UserIdentity(email="test@example.com", role="recruiter")
    job_data = {"recruiter_emails": ["other@example.com"]}
    with pytest.raises(HTTPException) as excinfo:
        verify_job_access(job_data, user)
    assert excinfo.value.status_code == 403
    assert "Access denied. You (test@example.com) are not assigned" in str(excinfo.value.detail)

def test_verify_job_access_allows_unassigned_job():
    user = UserIdentity(email="test@example.com", role="recruiter")
    job_data = {"recruiter_emails": []}
    # Should not raise exception
    verify_job_access(job_data, user)

@patch("routers.jobs._get_job_draft_sync")
def test_verify_job_access_by_id_duplicate_launch(mock_get_draft):
    user = UserIdentity(email="test@example.com", role="recruiter")
    # Mock job draft already launched by another recruiter
    mock_get_draft.return_value = {
        "status": "success",
        "data": {"recruiter_emails": ["other@example.com"]}
    }
    
    # Standard check throws generic 403
    with pytest.raises(HTTPException) as excinfo:
        _verify_job_access_by_id("123", user)
    assert excinfo.value.status_code == 403
    assert "Access denied" in str(excinfo.value.detail)
    
    # Check duplicate launch flag throws structured error
    with pytest.raises(HTTPException) as excinfo2:
        _verify_job_access_by_id("123", user, check_duplicate_launch=True)
    
    assert excinfo2.value.status_code == 403
    detail = excinfo2.value.detail
    assert isinstance(detail, dict)
    assert detail["code"] == "JOB_ALREADY_LAUNCHED"
    assert detail["recruiter_email"] == "other@example.com"
