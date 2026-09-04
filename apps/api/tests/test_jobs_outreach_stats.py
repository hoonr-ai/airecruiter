import pytest
from unittest.mock import patch, MagicMock

@pytest.fixture
def mock_db_connection():
    with patch("routers.jobs.get_db_connection") as mock_conn:
        yield mock_conn

@pytest.fixture
def mock_verify_job_access():
    with patch("routers.jobs._verify_job_access_by_id") as mock_verify:
        yield mock_verify

@pytest.fixture
def mock_fetch_all_outreach():
    with patch("routers.jobs._fetch_all_outreach") as mock_fetch:
        yield mock_fetch

@pytest.fixture
def mock_get_current_user():
    with patch("routers.jobs.get_current_user") as mock_user:
        yield mock_user

import asyncio

def test_get_job_outreach_stats_live_api_wins(
    mock_db_connection, mock_verify_job_access, mock_fetch_all_outreach, mock_get_current_user
):
    from routers.jobs import get_job_outreach_stats

    async def _test():
        # Setup DB mock to return one row
        conn = mock_db_connection.return_value
        cur = conn.cursor.return_value.__enter__.return_value
        
        # First execute for jobdiva_id
        cur.fetchone.return_value = ("jobdiva_123", "job_123")
        
        # Second execute for launched rows
        # (iid, status_val, raw_resp, sc_status, sc_phase)
        cur.fetchall.return_value = [
            ("int_1", "fail", '{"status": "fail"}', "in_progress", "phase1")
        ]
        
        # Mock live API
        mock_fetch_all_outreach.return_value = {
            "int_1": {"outreach_status": "pass", "outreach_phase": "phase3"}
        }
        
        user_mock = MagicMock()
        result = await get_job_outreach_stats("job_123", user=user_mock)
        
        # Live API should win (pass -> completed bucket, phase3 -> phase3 bucket)
        assert result["buckets"]["passed"] == 1
        assert result["buckets"]["failed"] == 0
        assert result["phases"]["phase3"] == 1
        assert result["phases"]["phase1"] == 0
        
    asyncio.run(_test())

def test_get_job_outreach_stats_fallback_wins_when_live_api_empty(
    mock_db_connection, mock_verify_job_access, mock_fetch_all_outreach, mock_get_current_user
):
    from routers.jobs import get_job_outreach_stats

    async def _test():
        # Setup DB mock to return one row
        conn = mock_db_connection.return_value
        cur = conn.cursor.return_value.__enter__.return_value
        
        cur.fetchone.return_value = ("jobdiva_123", "job_123")
        
        # (iid, status_val, raw_resp, sc_status, sc_phase)
        cur.fetchall.return_value = [
            ("int_1", "in_progress", '{"status": "in_progress", "outreach_channel": "sms"}', "sent", "phase2")
        ]
        
        # Mock live API returning empty (404/Timeout)
        mock_fetch_all_outreach.return_value = {}
        
        user_mock = MagicMock()
        result = await get_job_outreach_stats("job_123", user=user_mock)
        
        # Audit fallback wins (in_progress)
        assert result["buckets"]["in_progress"] == 1
        assert result["channels"]["sms"] == 1
        assert result["phases"]["phase2"] == 1 # cand_fallback phase wins if audit doesn't have it
        
    asyncio.run(_test())

def test_get_job_outreach_stats_empty_zero_buckets(
    mock_db_connection, mock_verify_job_access, mock_fetch_all_outreach, mock_get_current_user
):
    from routers.jobs import get_job_outreach_stats

    async def _test():
        # Setup DB mock to return one row
        conn = mock_db_connection.return_value
        cur = conn.cursor.return_value.__enter__.return_value
        
        cur.fetchone.return_value = ("jobdiva_123", "job_123")
        
        # No candidates launched
        cur.fetchall.return_value = []
        
        user_mock = MagicMock()
        result = await get_job_outreach_stats("job_123", user=user_mock)
        
        # Should return all zeros
        assert result["buckets"]["in_progress"] == 0
        assert result["buckets"]["passed"] == 0
        assert result["phases"]["phase1"] == 0

    asyncio.run(_test())
