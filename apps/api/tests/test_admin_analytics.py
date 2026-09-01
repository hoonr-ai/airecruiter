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
