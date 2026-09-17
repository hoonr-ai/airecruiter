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


def _stub_one_launched_candidate(
    mock_db_connection,
    *,
    iid="int_1",
    status="pending",
    sc_phase="phase2",
):
    conn = mock_db_connection.return_value
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = ("jobdiva_123", "job_123")
    cur.fetchall.return_value = [
        (iid, status, "{}", status, sc_phase, None, None, None, None, None),
    ]
    return cur


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
        # (iid, status_val, raw_resp, sc_status, sc_phase, score, hf, completed, first_completed, updated)
        cur.fetchall.return_value = [
            ("int_1", "fail", '{"status": "fail"}', "in_progress", "phase1", None, None, None, None, None)
        ]
        
        # Mock live API
        mock_fetch_all_outreach.return_value = {
            "int_1": {"outreach_status": "pass", "outreach_phase": "phase3"}
        }
        
        user_mock = MagicMock()
        result = await get_job_outreach_stats("job_123", user=user_mock)
        
        # Live API should win (pass -> completed bucket).
        # Rankings/launch-report shift: PairBot phase3 (P4) -> phase4 column.
        assert result["buckets"]["passed"] == 1
        assert result["buckets"]["failed"] == 0
        assert result["phases"]["phase4"] == 1
        assert result["phases"]["phase3"] == 0
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
        
        # (iid, status_val, raw_resp, sc_status, sc_phase, score, hf, completed, first_completed, updated)
        cur.fetchall.return_value = [
            ("int_1", "in_progress", '{"status": "in_progress", "outreach_channel": "sms"}', "sent", "phase2", None, None, None, None, None)
        ]
        
        # Mock live API returning empty (404/Timeout)
        mock_fetch_all_outreach.return_value = {}
        
        user_mock = MagicMock()
        result = await get_job_outreach_stats("job_123", user=user_mock)
        
        # Audit fallback wins (in_progress). PairBot phase2 (P3) shifts to phase3.
        assert result["buckets"]["in_progress"] == 1
        assert result["channels"]["sms"] == 1
        assert result["phases"]["phase3"] == 1
        assert result["phases"]["phase2"] == 0
        
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


def test_empty_outreach_stats_returns_independent_nested_dicts():
    from routers.jobs import _empty_outreach_stats

    first = _empty_outreach_stats()
    second = _empty_outreach_stats()
    first["phases"]["phase1"] += 1
    assert second["phases"]["phase1"] == 0
    assert first is not second
    assert first["phases"] is not second["phases"]
    assert first["buckets"] is not second["buckets"]


def test_get_job_outreach_stats_extra_phases_match_launch_report(
    mock_db_connection, mock_verify_job_access, mock_fetch_all_outreach, mock_get_current_user
):
    from routers.jobs import get_job_outreach_stats

    async def _test():
        conn = mock_db_connection.return_value
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = ("jobdiva_123", "job_123")
        cur.fetchall.return_value = [
            ("int_1", "in_progress", "{}", "in_progress", "phase1", None, None, None, None, None),
            ("int_2", "in_progress", "{}", "in_progress", "phase1_extra", None, None, None, None, None),
            ("int_3", "in_progress", "{}", "in_progress", "phase1_6hr", None, None, None, None, None),
            ("int_4", "in_progress", "{}", "in_progress", "phase1_6hr_extra", None, None, None, None, None),
        ]
        mock_fetch_all_outreach.return_value = {
            "int_1": {"outreach_status": "in_progress", "outreach_phase": "phase1"},
            "int_2": {"outreach_status": "in_progress", "outreach_phase": "phase1_extra"},
            "int_3": {"outreach_status": "in_progress", "outreach_phase": "phase1_6hr"},
            "int_4": {"outreach_status": "in_progress", "outreach_phase": "phase1_6hr_extra"},
        }

        result = await get_job_outreach_stats("job_123", user=MagicMock())
        assert result["phases"]["phase1"] == 1
        assert result["phases"]["extra1"] == 1
        assert result["phases"]["phase2"] == 1
        assert result["phases"]["extra2"] == 1
        assert result["phases"]["extra"] == 2

    asyncio.run(_test())


def test_get_job_outreach_stats_promotes_phase1_to_extra1_from_live_jobs(
    mock_db_connection, mock_verify_job_access, mock_fetch_all_outreach, mock_get_current_user
):
    from routers.jobs import get_job_outreach_stats

    async def _test():
        conn = mock_db_connection.return_value
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = ("jobdiva_123", "job_123")
        cur.fetchall.return_value = [
            ("int_1", "in_progress", "{}", "in_progress", "phase1", None, None, None, None, None),
        ]
        mock_fetch_all_outreach.return_value = {
            "int_1": {
                "outreach": {"outreach_status": "in_progress", "outreach_phase": "phase1"},
                "scheduled_jobs": [
                    {
                        "status": "processing",
                        "payload": {
                            "is_high_score_extra": True,
                            "high_score_phase": "phase1",
                            "reminder_type": "high_score_extra",
                        },
                    }
                ],
            }
        }

        result = await get_job_outreach_stats("job_123", user=MagicMock())
        assert result["phases"]["extra1"] == 1
        assert result["phases"]["phase1"] == 0
        assert result["phases"]["extra"] == 1

    asyncio.run(_test())


def test_get_job_outreach_stats_pending_extra_stays_phase1(
    mock_db_connection, mock_verify_job_access, mock_fetch_all_outreach, mock_get_current_user
):
    """Opened Extra (Pair Bot E1 Opened) must count as Phase 1 on rankings."""
    from routers.jobs import get_job_outreach_stats

    async def _test():
        _stub_one_launched_candidate(mock_db_connection, status="pending", sc_phase="phase1")
        mock_fetch_all_outreach.return_value = {
            "int_1": {
                "outreach": {"outreach_status": "pending", "outreach_phase": "phase1"},
                "scheduled_jobs": [
                    {
                        "status": "pending",
                        "payload": {
                            "is_high_score_extra": True,
                            "high_score_phase": "phase1",
                            "reminder_type": "high_score_extra",
                        },
                    }
                ],
            }
        }
        result = await get_job_outreach_stats("job_123", user=MagicMock())
        assert result["phases"]["phase1"] == 1
        assert result["phases"]["extra1"] == 0
        assert result["phases"]["extra"] == 0

    asyncio.run(_test())


def test_get_job_outreach_stats_promotes_phase2_to_extra3_from_comms(
    mock_db_connection, mock_verify_job_access, mock_fetch_all_outreach, mock_get_current_user
):
    from routers.jobs import get_job_outreach_stats

    async def _test():
        conn = mock_db_connection.return_value
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = ("jobdiva_123", "job_123")
        cur.fetchall.return_value = [
            ("int_1", "pending", "{}", "pending", "phase2", None, None, None, None, None),
        ]
        mock_fetch_all_outreach.return_value = {
            "int_1": {
                "outreach": {"outreach_status": "pending", "outreach_phase": "phase2"},
                "scheduled_jobs": [],
                "communications": [
                    {"phase": "phase2_extra", "channel": "email"},
                    {"phase": "phase2_extra", "channel": "sms"},
                ],
            }
        }

        result = await get_job_outreach_stats("job_123", user=MagicMock())
        assert result["phases"]["extra3"] == 1
        assert result["phases"]["phase3"] == 0
        assert result["phases"]["extra"] == 1

    asyncio.run(_test())


@pytest.mark.parametrize(
    "live_payload",
    [
        pytest.param(
            {
                "outreach": {
                    "outreach_status": "pending",
                    "outreach_phase": "phase2",
                    "stored_outreach_phase": "phase2",
                },
                "scheduled_jobs": [
                    {
                        "status": "completed",
                        "payload": {
                            "is_high_score_extra": True,
                            "high_score_phase": "phase2",
                            "reminder_type": "high_score_extra",
                        },
                    }
                ],
                "communications": [
                    {"phase": "phase2", "channel": "email"},
                    {"phase": "phase2", "channel": "sms"},
                ],
            },
            id="completed_extra_job",
        ),
        pytest.param(
            {
                "outreach": {
                    "outreach_status": "pending",
                    "outreach_phase": "phase2_extra",
                    "stored_outreach_phase": "phase2",
                },
                "scheduled_jobs": [],
                "communications": [{"phase": "phase2", "channel": "email"}],
            },
            id="canonical_phase2_extra",
        ),
    ],
)
def test_get_job_outreach_stats_counts_extra3_for_pairbot_extra_phase_3(
    mock_db_connection,
    mock_verify_job_access,
    mock_fetch_all_outreach,
    mock_get_current_user,
    live_payload,
):
    """Extra Outreach Phase 3 must count Extra 3, not Phase 3.

    Covers completed Extra jobs (comms still say phase2) and live API
    already returning canonical phase2_extra.
    """
    from routers.jobs import get_job_outreach_stats

    async def _test():
        _stub_one_launched_candidate(mock_db_connection, status="pending", sc_phase="phase2")
        mock_fetch_all_outreach.return_value = {"int_1": live_payload}
        result = await get_job_outreach_stats("job_123", user=MagicMock())
        assert result["phases"]["extra3"] == 1
        assert result["phases"]["phase3"] == 0
        assert result["phases"]["extra"] == 1
        assert result["buckets"]["pending"] == 1
        assert result["buckets"]["passed"] == 0
        assert result["buckets"]["failed"] == 0

    asyncio.run(_test())
