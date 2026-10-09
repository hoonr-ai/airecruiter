"""Regression tests for the recruiter-email launch gate (PR #782).

Step 1's "Recruiter Email is required" check only lives inside that step's
own Next button. Reopening an existing draft, a direct ?step=5 link, or the
step-indicator can all reach Step 5 (and POST /candidates/save) without ever
re-running it, since current_step is restored/overridden independently of
recruiter_emails — production job 26-31333 launched 106 candidates this way
with an empty recruiter_emails. _enforce_recruiter_email_gate closes that gap
as the one place a launch always passes through.

These tests pin: the block on an empty/missing recruiter_emails, the
GENERAL_SOURCING / no-row skip (a job with no monitored_jobs row at all has no
recruiter assignment to be missing, so it must not be treated as empty), and
failing closed (503, not a silently-swallowed bypass) when the lookup itself
errors.
"""
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from routers.candidates import _enforce_recruiter_email_gate


@patch("routers.candidates._get_job_draft_sync")
def test_blocks_launch_when_recruiter_emails_is_empty(mock_get_draft):
    mock_get_draft.return_value = {"status": "success", "data": {"recruiter_emails": []}}
    with pytest.raises(HTTPException) as excinfo:
        _enforce_recruiter_email_gate("26-31333")
    assert excinfo.value.status_code == 400


@patch("routers.candidates._get_job_draft_sync")
def test_blocks_launch_when_recruiter_emails_is_missing_entirely(mock_get_draft):
    mock_get_draft.return_value = {"status": "success", "data": {}}
    with pytest.raises(HTTPException) as excinfo:
        _enforce_recruiter_email_gate("26-31333")
    assert excinfo.value.status_code == 400


@patch("routers.candidates._get_job_draft_sync")
def test_allows_launch_when_a_recruiter_is_assigned(mock_get_draft):
    mock_get_draft.return_value = {
        "status": "success",
        "data": {"recruiter_emails": ["recruiter@example.com"]},
    }
    _enforce_recruiter_email_gate("26-31333")  # must not raise


@patch("routers.candidates._get_job_draft_sync")
def test_skips_rather_than_blocks_when_no_monitored_jobs_row_exists(mock_get_draft):
    """The GENERAL_SOURCING sentinel (candidates launched with no job attached)
    has no monitored_jobs row at all — _get_job_draft_sync reports status
    "error" in that case, not an empty recruiter_emails list. Must not be
    conflated with a real job that has zero recruiters assigned."""
    mock_get_draft.return_value = {"status": "error", "message": "No data found for job GENERAL_SOURCING"}
    _enforce_recruiter_email_gate("GENERAL_SOURCING")  # must not raise


@patch("routers.candidates._get_job_draft_sync")
def test_fails_closed_when_the_lookup_itself_errors(mock_get_draft):
    """A transient DB problem must not silently let the launch through — the
    earlier version of this gate only printed the exception and fell through,
    which made the gate itself a bypass under failure."""
    mock_get_draft.side_effect = RuntimeError("connection reset")
    with pytest.raises(HTTPException) as excinfo:
        _enforce_recruiter_email_gate("26-31333")
    assert excinfo.value.status_code == 503
