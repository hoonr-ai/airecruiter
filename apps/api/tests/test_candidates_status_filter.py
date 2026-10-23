"""Regression guard for the `status` query param on GET /jobs/{job_id}/candidates
(routers/candidates.py), added so the Rankings STATUS dropdown filters in SQL —
before pagination — instead of the caller filtering whatever page had already
loaded into the browser (see PR #835).

These tests mock the DB layer (per conftest's `_block_real_db_connections`)
and assert on what was actually sent to `cursor.execute`: the only way to
catch a misordered bind param or a missing WHERE/FILTER clause without a real
Postgres instance.
"""
import asyncio
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException


@pytest.fixture
def mock_db_connection():
    with patch("routers.candidates.get_db_connection") as mock_conn:
        conn = mock_conn.return_value
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.side_effect = [
            ("jobdiva_123", "job_123", "", ""),
            # total_candidates (10, status-scoped when a filter is active) and
            # job_total_candidates (76, always the whole job) deliberately
            # differ, so a test that conflates them fails loudly.
            {"total_candidates": 10, "job_total_candidates": 76, "duplicate_candidate_count": 0, "invalid_contact_count": 0},
        ]
        cur.fetchall.return_value = []
        yield mock_conn


@pytest.fixture
def mock_verify_job_access():
    with patch("routers.candidates._verify_job_access_by_id") as mock_verify:
        yield mock_verify


@pytest.fixture
def mock_count_launched():
    with patch("routers.candidates.count_launched_candidates", return_value=0) as mock_count:
        yield mock_count


def _run(mock_db_connection, mock_verify_job_access, mock_count_launched, status=None):
    from routers.candidates import get_job_candidates

    return asyncio.run(
        get_job_candidates("job_123", limit=None, offset=0, status=status, user=MagicMock())
    )


def _calls(mock_db_connection):
    cur = mock_db_connection.return_value.cursor.return_value.__enter__.return_value
    return cur.execute.call_args_list


def test_status_none_leaves_query_unfiltered(
    mock_db_connection, mock_verify_job_access, mock_count_launched
):
    """No `status` -> no FILTER/WHERE clause added to either query (the
    pre-existing, unfiltered behaviour must be unchanged)."""
    result = _run(mock_db_connection, mock_verify_job_access, mock_count_launched, status=None)

    assert result["status"] == "success"
    counts_sql, _ = _calls(mock_db_connection)[1].args
    main_sql, _ = _calls(mock_db_connection)[2].args
    assert "FROM sourced_candidates newer" not in counts_sql
    assert "WHERE (CASE WHEN LOWER" not in main_sql


def test_status_pass_adds_filter_clause_with_correctly_ordered_params(
    mock_db_connection, mock_verify_job_access, mock_count_launched
):
    """status=pass adds the engage_display_sql() FILTER (counts query) and
    WHERE (main query), each with its own %s bound to the 'Pass' label in the
    position it appears in the SQL text — a reordering here sends the wrong
    value to the wrong placeholder without raising, so this has to check
    position, not just presence."""
    _run(mock_db_connection, mock_verify_job_access, mock_count_launched, status="pass")

    counts_sql, counts_params = _calls(mock_db_connection)[1].args
    main_sql, main_params = _calls(mock_db_connection)[2].args

    assert "engage_status" in counts_sql and "FILTER (" in counts_sql
    assert "FROM sourced_candidates newer" in counts_sql  # dedup guard (one row per candidate_id)
    assert "WHERE (CASE WHEN LOWER" in main_sql  # engage_display_sql('sc.data') inline

    # The status FILTER's own %s is the first placeholder in the counts
    # query's SELECT list, so "Pass" must be the first bound param.
    assert counts_params[0] == "Pass"
    # The main query's status WHERE sits after the whole dedup CTE, so its
    # %s is the last placeholder before LIMIT/OFFSET (no limit here).
    assert list(main_params)[-1] == "Pass"


def test_status_unrecognised_value_raises_422(
    mock_db_connection, mock_verify_job_access, mock_count_launched
):
    """A typo'd or stale STATUS value (e.g. "Passd") is a caller error, not
    silently "no filter" — that would read as the filter being broken rather
    than the request being wrong."""
    with pytest.raises(HTTPException) as exc_info:
        _run(mock_db_connection, mock_verify_job_access, mock_count_launched, status="Passd")

    assert exc_info.value.status_code == 422


@pytest.mark.parametrize("raw,expected", [("PASS", "Pass"), (" pass ", "Pass"), ("Fail", "Fail")])
def test_status_is_case_and_whitespace_insensitive(
    mock_db_connection, mock_verify_job_access, mock_count_launched, raw, expected
):
    _run(mock_db_connection, mock_verify_job_access, mock_count_launched, status=raw)

    counts_params = _calls(mock_db_connection)[1].args[1]
    assert counts_params[0] == expected


def test_status_pending_adds_a_launched_only_restriction(
    mock_db_connection, mock_verify_job_access, mock_count_launched
):
    """format_engage_status()'s "Pending" is a catch-all that also matches a
    candidate who was never launched at all (no engage_status) and a launch
    attempt that failed before an interview ever existed (engage_status
    'failed', no score) — exactly the population the frontend's N/A bucket
    covers. Without restricting to the launched population (a real interview
    id from sourced_candidates.data or engage_interview_audit, same
    definition as services/launched_candidates.py and the Rankings header's
    own Pending count), status=pending pulls those rows in too and
    "Matching N of total" reads far higher than the header's Pending figure.

    Both queries check for ANY engage_interview_audit row with an interview
    id, not just the latest one (`la.interview_id`, this query's own join) —
    a failed relaunch attempt after an earlier successful one leaves the
    latest row's interview id blank, and checking only that row would make
    the two queries disagree on this candidate, the same symptom this fix is
    for.

    Verified against a real QA job (26-28758): without the restriction,
    status=pending returned 64; the header's Pending figure was 25; the
    other 39 were exactly the job's N/A-bucket (never-launched) candidates.
    """
    _run(mock_db_connection, mock_verify_job_access, mock_count_launched, status="pending")

    counts_sql, counts_params = _calls(mock_db_connection)[1].args
    main_sql, main_params = _calls(mock_db_connection)[2].args

    # "FROM engage_interview_audit ea" also appears once, unconditionally, in
    # the pre-existing invalid_contact_count subquery — Pending's own EXISTS
    # clause makes it two. The main query has no unconditional occurrence
    # (its own latest_audit CTE isn't aliased "ea"), so one there is entirely
    # Pending's own clause.
    assert counts_sql.count("FROM engage_interview_audit ea") == 2
    assert main_sql.count("FROM engage_interview_audit ea") == 1
    assert "engage_interview_id" in counts_sql
    assert "engage_interview_id" in main_sql

    # Counts query: SELECT-list order is status_label, the dedup NOT EXISTS's
    # jobdiva pair, then the launched-check EXISTS's own jobdiva pair.
    assert counts_params[0] == "Pending"
    assert counts_params[1:5] == [
        "jobdiva_123", "job_123",  # dedup NOT EXISTS
        "jobdiva_123", "job_123",  # launched-check EXISTS
    ]
    # Main query: the status WHERE's own %s comes right after the dedup CTE,
    # followed immediately by the launched-check EXISTS's own jobdiva pair.
    assert list(main_params)[-3:] == ["Pending", "jobdiva_123", "job_123"]


@pytest.mark.parametrize("status", ["fail", "in_progress"])
def test_other_statuses_do_not_get_the_pending_launched_restriction(
    mock_db_connection, mock_verify_job_access, mock_count_launched, status
):
    """Fail and In Progress only ever come from a recognised, explicitly-set
    engage_status — never a never-launched candidate's blank one — so they
    don't need (and must not get) the extra launched-only clause."""
    _run(mock_db_connection, mock_verify_job_access, mock_count_launched, status=status)

    counts_sql, _ = _calls(mock_db_connection)[1].args
    main_sql, _ = _calls(mock_db_connection)[2].args
    # Exactly the pre-existing, unconditional occurrence from
    # invalid_contact_count — not the Pending-only extra one.
    assert counts_sql.count("FROM engage_interview_audit ea") == 1
    assert main_sql.count("FROM engage_interview_audit ea") == 0


@pytest.mark.parametrize("status", [None, "pass", "fail", "pending", "in_progress"])
def test_job_total_is_always_the_whole_job_not_the_status_filtered_count(
    mock_db_connection, mock_verify_job_access, mock_count_launched, status
):
    """pagination.total is status-scoped (10, for pagination's own "have I
    loaded everything matching this filter" bookkeeping); pagination.job_total
    must stay the job's whole count (76) regardless of which STATUS bucket is
    active — the Rankings header's "Matching N of total" denominator reads
    job_total, not total, so N doesn't silently become "of itself" for every
    STATUS value except the client-filtered ones (N/A, duplicate, invalid).

    Reproduces the real QA observation: "Matching 10 of 10" for Pass instead
    of "Matching 10 of 76" — the fix is a second, always-unfiltered count
    column (job_total_candidates) alongside the existing status-scoped one.
    """
    result = _run(mock_db_connection, mock_verify_job_access, mock_count_launched, status=status)

    assert result["pagination"]["total"] == 10
    assert result["pagination"]["job_total"] == 76

    counts_sql, _ = _calls(mock_db_connection)[1].args
    assert "job_total_candidates" in counts_sql
