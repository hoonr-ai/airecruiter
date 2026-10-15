"""Regression guard for GET /jobs/{job_id}/candidates (routers/candidates.py).

PR #813: the auto-email visibility filter built its terminal-status list as a
plain `", ".join(...)` SQL fragment and spliced it into the query text with
`{_TERMINAL_STATUSES_SQL}` — but the count query's triple-quoted string wasn't
an f-string, so Postgres received the literal placeholder text and raised
"syntax error at or near "{"" on every call. The fix passes the status list as
a bind param (`= ANY(%s)`) instead of interpolating it into the SQL text, so
there is nothing left to forget an `f` prefix for.

These tests mock the DB layer (per conftest's `_block_real_db_connections`)
and assert on what was actually sent to `cursor.execute`, which is the only
way to catch "a forgotten `f` prefix" / "a stray `{...}` left in the SQL
text" without a real Postgres instance.
"""
import asyncio
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_db_connection():
    with patch("routers.candidates.get_db_connection") as mock_conn:
        conn = mock_conn.return_value
        cur = conn.cursor.return_value.__enter__.return_value
        # 1st execute: resolve monitored_jobs -> (jobdiva_id, job_id, screening_level, customer_name)
        # 2nd execute: the counts query (RealDictCursor) -> fetchone()
        cur.fetchone.side_effect = [
            ("jobdiva_123", "job_123", "", ""),
            {"total_candidates": 0, "duplicate_candidate_count": 0, "invalid_contact_count": 0},
        ]
        # 3rd execute: the main select -> fetchall()
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


def _run(mock_db_connection, mock_verify_job_access, mock_count_launched):
    from routers.candidates import get_job_candidates

    return asyncio.run(
        get_job_candidates("job_123", limit=None, offset=0, user=MagicMock())
    )


def _executed_sql(mock_db_connection) -> list:
    cur = mock_db_connection.return_value.cursor.return_value.__enter__.return_value
    return [call.args[0] for call in cur.execute.call_args_list]


def test_candidates_query_has_no_unresolved_braces(
    mock_db_connection, mock_verify_job_access, mock_count_launched
):
    """No `{...}` placeholder text reaches Postgres (the PR #813 bug class).

    A query string missing its `f` prefix sends `{_TERMINAL_STATUSES_SQL}`
    verbatim; a correctly interpolated/parameterised query never has a bare
    `{` in its SQL text.
    """
    result = _run(mock_db_connection, mock_verify_job_access, mock_count_launched)

    assert result["status"] == "success"
    sql_statements = _executed_sql(mock_db_connection)
    assert len(sql_statements) == 3  # monitored_jobs lookup, counts, main select
    for sql in sql_statements:
        assert "{" not in sql and "}" not in sql, f"unresolved placeholder left in SQL: {sql!r}"


def test_terminal_statuses_passed_as_bind_param_not_interpolated(
    mock_db_connection, mock_verify_job_access, mock_count_launched
):
    """The terminal-status list travels as a query parameter (`= ANY(%s)`),
    not spliced into the SQL text — so it can never reproduce the f-string bug."""
    from routers.candidates import _TERMINAL_STATUSES

    _run(mock_db_connection, mock_verify_job_access, mock_count_launched)

    cur = mock_db_connection.return_value.cursor.return_value.__enter__.return_value
    calls = cur.execute.call_args_list
    # Call 2 = counts query, call 3 = main select; both filter on engage_status.
    counts_sql, counts_params = calls[1].args
    main_sql, main_params = calls[2].args

    assert "= ANY(%s)" in counts_sql
    assert "= ANY(%s)" in main_sql
    assert _TERMINAL_STATUSES in counts_params
    assert _TERMINAL_STATUSES in tuple(main_params)
