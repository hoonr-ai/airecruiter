"""Regression tests for the recruiter-email launch gate.

Step 1's "Recruiter Email is required" check only lives inside that step's
own Next button. Reopening an existing draft, a direct ?step=5 link, or the
step-indicator can all reach Step 5 (and POST /candidates/save) without ever
re-running it, since current_step is restored/overridden independently of
recruiter_emails — production job 26-31333 launched 106 candidates this way
with an empty recruiter_emails. _enforce_recruiter_email_gate closes that gap
as the one place a launch always passes through.

These tests pin: the block on an empty/missing recruiter_emails, that only
the GENERAL_SOURCING sentinel (not an arbitrary unresolvable job id) skips the
check, and failing closed (503, not a silently-swallowed bypass) when the
lookup itself errors.
"""
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from core.auth import parse_recruiter_emails
from routers.candidates import _enforce_recruiter_email_gate


@pytest.mark.anyio
@patch("routers.candidates._get_job_draft_sync")
async def test_blocks_launch_when_recruiter_emails_is_empty(mock_get_draft):
    mock_get_draft.return_value = {"status": "success", "data": {"recruiter_emails": []}}
    with pytest.raises(HTTPException) as excinfo:
        await _enforce_recruiter_email_gate("26-31333")
    assert excinfo.value.status_code == 400


@pytest.mark.anyio
@patch("routers.candidates._get_job_draft_sync")
async def test_blocks_launch_when_recruiter_emails_is_missing_entirely(mock_get_draft):
    mock_get_draft.return_value = {"status": "success", "data": {}}
    with pytest.raises(HTTPException) as excinfo:
        await _enforce_recruiter_email_gate("26-31333")
    assert excinfo.value.status_code == 400


@pytest.mark.anyio
@patch("routers.candidates._get_job_draft_sync")
async def test_allows_launch_when_a_recruiter_is_assigned(mock_get_draft):
    mock_get_draft.return_value = {
        "status": "success",
        "data": {"recruiter_emails": ["recruiter@example.com"]},
    }
    await _enforce_recruiter_email_gate("26-31333")  # must not raise


@pytest.mark.anyio
@patch("routers.candidates._get_job_draft_sync")
async def test_general_sourcing_sentinel_skips_without_hitting_the_db(mock_get_draft):
    """Candidates launched with no job attached (the master pool) post this
    exact sentinel — it has no monitored_jobs row and never will, so the gate
    must not even attempt the lookup."""
    await _enforce_recruiter_email_gate("GENERAL_SOURCING")  # must not raise
    mock_get_draft.assert_not_called()


@pytest.mark.anyio
@patch("routers.candidates._get_job_draft_sync")
async def test_unresolvable_job_id_other_than_the_sentinel_is_blocked_not_skipped(mock_get_draft):
    """A mistyped, stale, or deleted job id also has no monitored_jobs row —
    but unlike GENERAL_SOURCING it is not a legitimate "no job attached"
    launch, so it must be blocked rather than silently waved through."""
    mock_get_draft.return_value = {"status": "error", "message": "No data found for job 99-00000"}
    with pytest.raises(HTTPException) as excinfo:
        await _enforce_recruiter_email_gate("99-00000")
    assert excinfo.value.status_code == 400


@pytest.mark.anyio
@patch("routers.candidates._get_job_draft_sync")
async def test_fails_closed_when_the_lookup_itself_errors(mock_get_draft):
    """A transient DB problem must not silently let the launch through — the
    earlier version of this gate only printed the exception and fell through,
    which made the gate itself a bypass under failure."""
    mock_get_draft.side_effect = RuntimeError("connection reset")
    with pytest.raises(HTTPException) as excinfo:
        await _enforce_recruiter_email_gate("26-31333")
    assert excinfo.value.status_code == 503


# ── parse_recruiter_emails: every shape recruiter_emails is stored/read as ──

def test_parse_recruiter_emails_json_string():
    assert parse_recruiter_emails('["A@Example.com", "b@example.com"]') == [
        "a@example.com",
        "b@example.com",
    ]


def test_parse_recruiter_emails_bare_string():
    assert parse_recruiter_emails("recruiter@example.com") == ["recruiter@example.com"]


def test_parse_recruiter_emails_native_list():
    assert parse_recruiter_emails(["One@Example.com", "two@example.com"]) == [
        "one@example.com",
        "two@example.com",
    ]


def test_parse_recruiter_emails_none_and_other_types():
    assert parse_recruiter_emails(None) == []
    assert parse_recruiter_emails(42) == []
    assert parse_recruiter_emails({}) == []


def test_parse_recruiter_emails_ignores_whitespace_only_entries():
    assert parse_recruiter_emails(["  ", "", "real@example.com", None]) == ["real@example.com"]


def test_parse_recruiter_emails_deduplicates_case_insensitively():
    assert parse_recruiter_emails(["Same@Example.com", "same@example.com", "other@example.com"]) == [
        "same@example.com",
        "other@example.com",
    ]


def test_parse_recruiter_emails_malformed_json_falls_back_to_bare_string():
    """A string that looks list-ish but isn't valid JSON (e.g. legacy data)
    is treated as a single bare email rather than raising."""
    assert parse_recruiter_emails("[not valid json") == ["[not valid json"]


# ── _get_job_draft_sync: zero-padded numeric job ids ────────────────────────

class _FakeCursor:
    def __init__(self, match_id: str, row: dict):
        self._match_id, self._row = match_id, row
        self._last_result = None

    def execute(self, _sql, params):
        requested_id = params[0]
        self._last_result = self._row if requested_id == self._match_id else None

    def fetchone(self):
        return self._last_result

    def close(self):
        pass


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def close(self):
        pass


@patch("routers._helpers.get_dict_cursor_connection")
def test_get_job_draft_sync_falls_back_to_lstripped_zero_padded_id(mock_get_conn):
    """A caller passing a zero-padded numeric job_id (e.g. from a legacy
    integration) must still resolve, since the stored row is keyed by the
    unpadded id."""
    from routers._helpers import _get_job_draft_sync

    row = {"jobdiva_id": "33087136", "recruiter_emails": '["recruiter@example.com"]'}
    mock_get_conn.return_value = _FakeConn(_FakeCursor(match_id="33087136", row=row))

    result = _get_job_draft_sync("0033087136")
    assert result["status"] == "success"
    assert result["data"]["recruiter_emails"] == ["recruiter@example.com"]
