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
            {"total_candidates": 0, "duplicate_candidate_count": 0, "invalid_contact_count": 0},
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
