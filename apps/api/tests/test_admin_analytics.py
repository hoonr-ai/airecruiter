from unittest.mock import MagicMock
from routers.admin_analytics import _compute_jobs_timeline

def test_compute_jobs_timeline_removes_200_limit():
    """
    Test that _compute_jobs_timeline does not artificially truncate to 200 records.
    It should now use LIMIT 2000 to prevent unbounded query risks but allow large result sets.
    """
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    
    # Mock total count return
    mock_cursor.fetchone.return_value = (5000,)
    
    # Mock data rows
    mock_cursor.fetchall.return_value = []
    
    _compute_jobs_timeline(mock_conn, scope=None)
    
    # The second execute call is for the main query
    assert mock_cursor.execute.call_count == 2
    query = mock_cursor.execute.call_args_list[1][0][0]
    
    import re
    assert not re.search(r"LIMIT\s+200\b", query), "Query should not be hard-limited to 200 records"
    assert re.search(r"LIMIT\s+2000\b", query), "Query should be bounded to 2000 records to prevent scalability issues"

def test_compute_jobs_timeline_recruiter_emails():
    """
    Test that recruiter_emails are properly parsed and included in the timeline rows.
    """
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    
    # Mock total count return
    mock_cursor.fetchone.return_value = (1,)
    
    # Mock data rows
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    mock_cursor.fetchall.return_value = [
        ("job1", "jd1", "Title", "Cust", "01/01/2026", now, now, now, False, "Reason", "OPEN", 5, 5, 5, "camp1", '["test@example.com", "other@example.com"]', now)
    ]
    
    result = _compute_jobs_timeline(mock_conn, scope=None)
    
    assert len(result["rows"]) == 1
    row = result["rows"][0]
    assert row["recruiter_emails"] == ["test@example.com", "other@example.com"]
    assert row["archive_reason"] == "Reason"


def test_compute_jobs_timeline_dedup_key_is_job_id_not_jobdiva_id():
    """A job edited after launch clones into a new job_id row that keeps the
    same jobdiva_id (see launch_report.py); deduping on jobdiva_id would drop
    the earlier version's own candidates/campaign/timeline data. The
    ROW_NUMBER() partition (and the total_jobs count) must key on job_id,
    not COALESCE(jobdiva_id, job_id::text).
    """
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    mock_cursor.fetchone.return_value = (2,)
    mock_cursor.fetchall.return_value = []

    _compute_jobs_timeline(mock_conn, scope=None)

    count_query = mock_cursor.execute.call_args_list[0][0][0]
    main_query = mock_cursor.execute.call_args_list[1][0][0]
    assert "COUNT(DISTINCT job_id::text)" in count_query
    assert "PARTITION BY job_id::text" in main_query
    assert "COALESCE(jobdiva_id" not in main_query


def test_compute_jobs_timeline_recruiter_emails_scoped_to_team():
    """A job shared across teams must not leak recruiters outside the
    requesting team's scope through recruiter_emails — mirrors the guard
    already applied to the Top Recruiters section.
    """
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    mock_cursor.fetchone.return_value = (1,)

    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    mock_cursor.fetchall.return_value = [
        ("job1", "jd1", "Title", "Cust", "01/01/2026", now, now, now, False, None,
         "OPEN", 5, 5, 5, "camp1", '["team@example.com", "outsider@example.com"]', now)
    ]

    scope = {"job_ids": ["job1"], "sc_keys": [], "emails": ["team@example.com"]}
    result = _compute_jobs_timeline(mock_conn, scope=scope)

    row = result["rows"][0]
    assert row["recruiter_emails"] == ["team@example.com"]
    assert "outsider@example.com" not in row["recruiter_emails"]


def test_compute_jobs_timeline_recruiter_emails_unscoped_keeps_all():
    """No team scope (admin view) must still see every recruiter."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    mock_cursor.fetchone.return_value = (1,)

    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    mock_cursor.fetchall.return_value = [
        ("job1", "jd1", "Title", "Cust", "01/01/2026", now, now, now, False, None,
         "OPEN", 5, 5, 5, "camp1", '["team@example.com", "outsider@example.com"]', now)
    ]

    result = _compute_jobs_timeline(mock_conn, scope=None)

    row = result["rows"][0]
    assert row["recruiter_emails"] == ["team@example.com", "outsider@example.com"]
