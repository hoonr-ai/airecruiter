import asyncio
from unittest.mock import MagicMock, patch

import pytest


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


def _audit_row(iid, status, *, response="{}", cid="c1"):
    return (iid, status, response, cid)


def _sourced_row(cid, iid, status, phase, score=None, hf=None):
    return (cid, iid, status, phase, score, hf, None, None, None)


def _stub_outreach_db(mock_db_connection, launched_rows, sourced_rows=None):
    conn = mock_db_connection.return_value
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = ("jobdiva_123", "job_123")
    cur.fetchall.side_effect = [list(launched_rows), list(sourced_rows or [])]
    return cur


def test_get_job_outreach_stats_live_api_wins(
    mock_db_connection, mock_verify_job_access, mock_fetch_all_outreach, mock_get_current_user
):
    from routers.jobs import get_job_outreach_stats

    async def _test():
        _stub_outreach_db(
            mock_db_connection,
            [_audit_row("int_1", "fail", response='{"status": "fail"}')],
            [_sourced_row("c1", "int_1", "in_progress", "phase1")],
        )
        mock_fetch_all_outreach.return_value = {
            "int_1": {"outreach_status": "pass", "outreach_phase": "phase3"}
        }

        result = await get_job_outreach_stats("job_123", user=MagicMock())

        # Live API wins; Pair Bot phase3 is rankings Phase 4
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
        _stub_outreach_db(
            mock_db_connection,
            [_audit_row("int_1", "in_progress", response='{"status": "in_progress", "outreach_channel": "sms"}')],
            [_sourced_row("c1", "int_1", "sent", "phase2")],
        )
        mock_fetch_all_outreach.return_value = {}

        result = await get_job_outreach_stats("job_123", user=MagicMock())

        # Audit fallback wins (in_progress)
        assert result["buckets"]["in_progress"] == 1
        assert result["channels"]["sms"] == 1
        assert result["phases"]["phase3"] == 1  # Pair Bot phase2 -> rankings Phase 3

    asyncio.run(_test())


def test_get_job_outreach_stats_does_not_regress_to_later_pending_audit(
    mock_db_connection, mock_verify_job_access, mock_fetch_all_outreach, mock_get_current_user
):
    """A stale Pending audit event must not hide an earlier In Progress state."""
    from routers.jobs import get_job_outreach_stats

    async def _test():
        cur = _stub_outreach_db(
            mock_db_connection,
            [
                # DB query is newest first: the stale Pending write arrives last.
                _audit_row("int_1", "pending", cid="c1"),
                _audit_row("int_1", "in progress", cid="c1"),
            ],
            [_sourced_row("c1", "int_1", "pending", "phase1")],
        )
        mock_fetch_all_outreach.return_value = {}

        result = await get_job_outreach_stats("job_123", user=MagicMock())

        assert result["buckets"]["in_progress"] == 1
        assert result["buckets"]["pending"] == 0
        launched_query = " ".join(cur.execute.call_args_list[1].args[0].split())
        assert "DISTINCT ON" not in launched_query

    asyncio.run(_test())


def test_get_job_outreach_stats_empty_zero_buckets(
    mock_db_connection, mock_verify_job_access, mock_fetch_all_outreach, mock_get_current_user
):
    from routers.jobs import get_job_outreach_stats

    async def _test():
        _stub_outreach_db(mock_db_connection, [], [])
        result = await get_job_outreach_stats("job_123", user=MagicMock())
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


def test_unlaunched_sourced_engage_status_does_not_inflate_pending(
    mock_db_connection, mock_verify_job_access, mock_fetch_all_outreach, mock_get_current_user
):
    """Rankings used to FULL OUTER JOIN every sourced row with engage_status."""
    from routers.jobs import get_job_outreach_stats

    async def _test():
        _stub_outreach_db(
            mock_db_connection,
            [
                _audit_row("int_1", "pending", cid="launched"),
                _audit_row("int_2", "completed", cid="done"),
            ],
            [
                _sourced_row("launched", "int_1", "pending", "phase1"),
                _sourced_row("done", "int_2", "completed", "phase1"),
                _sourced_row("sourced_only", None, "pending", None),
                _sourced_row("also_sourced", None, "initiated", None),
            ],
        )
        mock_fetch_all_outreach.return_value = {
            "int_1": {"outreach_status": "pending", "outreach_phase": "phase1"},
            "int_2": {"outreach_status": "completed", "outreach_phase": "phase1"},
        }
        result = await get_job_outreach_stats("job_123", user=MagicMock())
        assert result["buckets"]["pending"] == 1
        assert result["buckets"]["completed"] == 1
        assert result["buckets"]["passed"] == 1
        assert result["buckets"]["failed"] == 0

    asyncio.run(_test())


def test_two_interviews_same_candidate_both_count(
    mock_db_connection, mock_verify_job_access, mock_fetch_all_outreach, mock_get_current_user
):
    from routers.jobs import get_job_outreach_stats

    async def _test():
        _stub_outreach_db(
            mock_db_connection,
            [
                _audit_row("int_1", "completed", cid="c1"),
                _audit_row("int_2", "pending", cid="c1"),
            ],
            [_sourced_row("c1", "int_2", "pending", "phase1")],
        )
        mock_fetch_all_outreach.return_value = {
            "int_1": {"outreach_status": "completed"},
            "int_2": {"outreach_status": "pending", "outreach_phase": "phase1"},
        }
        result = await get_job_outreach_stats("job_123", user=MagicMock())
        assert result["buckets"]["completed"] == 1
        assert result["buckets"]["pending"] == 1

    asyncio.run(_test())


def test_outreach_stats_selects_sourced_rows_per_interview_not_candidate(
    mock_db_connection, mock_verify_job_access, mock_fetch_all_outreach, mock_get_current_user
):
    """A repeat launch for one person must not drop an interview from Rankings."""
    from routers.jobs import get_job_outreach_stats

    async def _test():
        cur = _stub_outreach_db(
            mock_db_connection,
            [_audit_row("int_1", "pending", cid="c1")],
            [
                _sourced_row("c1", "int_1", "pending", "phase1"),
                _sourced_row("c1", "int_2", "pending", "phase3"),
            ],
        )
        mock_fetch_all_outreach.return_value = {
            "int_1": {"outreach_status": "pending", "outreach_phase": "phase1"},
            "int_2": {"outreach_status": "in_progress", "outreach_phase": "phase3"},
        }

        result = await get_job_outreach_stats("job_123", user=MagicMock())

        assert result["buckets"]["pending"] == 1
        assert result["buckets"]["in_progress"] == 1
        assert result["phases"]["phase1"] == 1
        assert result["phases"]["phase4"] == 1

        sourced_query = " ".join(cur.execute.call_args_list[2].args[0].split())
        assert "DISTINCT ON ( COALESCE(NULLIF(data->>'engage_interview_id', ''), candidate_id) )" in sourced_query

    asyncio.run(_test())


def test_uncovered_jsonb_pass_counts_like_launch_report(
    mock_db_connection, mock_verify_job_access, mock_fetch_all_outreach, mock_get_current_user
):
    from routers.jobs import get_job_outreach_stats

    async def _test():
        _stub_outreach_db(
            mock_db_connection,
            [_audit_row("int_1", "in_progress", cid="c1")],
            [
                _sourced_row("c1", "int_1", "in_progress", "phase1"),
                _sourced_row("c2", None, "passed", None),
            ],
        )
        mock_fetch_all_outreach.return_value = {
            "int_1": {"outreach_status": "in_progress", "outreach_phase": "phase1"},
        }
        result = await get_job_outreach_stats("job_123", user=MagicMock())
        assert result["buckets"]["in_progress"] == 1
        assert result["buckets"]["passed"] == 1
        assert result["buckets"]["pending"] == 0

    asyncio.run(_test())


def test_get_job_outreach_stats_pairbot_phase4_counts_phase4(
    mock_db_connection, mock_verify_job_access, mock_fetch_all_outreach, mock_get_current_user
):
    """Pair Bot Interviews 'Phase 4' (phase3) must land in rankings Phase 4, not Phase 3."""
    from routers.jobs import get_job_outreach_stats

    async def _test():
        _stub_outreach_db(
            mock_db_connection,
            [_audit_row("int_1", "pending", cid="c1")],
            [_sourced_row("c1", "int_1", "pending", "phase3")],
        )
        mock_fetch_all_outreach.return_value = {
            "int_1": {"outreach_status": "pending", "outreach_phase": "phase3"},
        }
        result = await get_job_outreach_stats("job_123", user=MagicMock())
        assert result["phases"]["phase4"] == 1
        assert result["phases"]["phase3"] == 0

    asyncio.run(_test())


def test_get_job_outreach_stats_extra2_stays_extra2(
    mock_db_connection, mock_verify_job_access, mock_fetch_all_outreach, mock_get_current_user
):
    from routers.jobs import get_job_outreach_stats

    async def _test():
        _stub_outreach_db(
            mock_db_connection,
            [_audit_row("int_1", "pending", cid="c1")],
            [_sourced_row("c1", "int_1", "pending", "phase1_6hr_extra")],
        )
        mock_fetch_all_outreach.return_value = {
            "int_1": {"outreach_status": "pending", "outreach_phase": "phase1_6hr_extra"},
        }
        result = await get_job_outreach_stats("job_123", user=MagicMock())
        assert result["phases"]["extra2"] == 1
        assert result["phases"]["extra"] == 1
        assert result["phases"]["phase2"] == 0

    asyncio.run(_test())


def test_get_job_outreach_stats_pending_extra_stays_phase1(
    mock_db_connection, mock_verify_job_access, mock_fetch_all_outreach, mock_get_current_user
):
    from routers.jobs import get_job_outreach_stats

    async def _test():
        _stub_outreach_db(
            mock_db_connection,
            [_audit_row("int_1", "pending", cid="c1")],
            [_sourced_row("c1", "int_1", "pending", "phase1")],
        )
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

    asyncio.run(_test())


def test_candidate_with_no_audit_row_uses_live_api_status(
    mock_db_connection, mock_verify_job_access, mock_fetch_all_outreach, mock_get_current_user
):
    """Regression: candidate with engage_interview_id but no audit row must be
    counted using the live Pair Bot status, not silently dropped.

    This covers the 'audit webhook lagged or failed' scenario described in
    PR #673: the header showed Pending: 4 while the table showed 2 In Progress
    + 2 Pending, because candidates with no audit row were excluded from
    collect_merged_outreach_payloads even when their live API status was known.
    """
    from routers.jobs import get_job_outreach_stats

    async def _test():
        _stub_outreach_db(
            mock_db_connection,
            # Only c1 has an audit row — c2 was launched but the webhook never fired.
            [_audit_row("int_1", "pending", cid="c1")],
            [
                _sourced_row("c1", "int_1", "pending", "phase1"),
                # c2 has an engage_interview_id but NO audit row.
                _sourced_row("c2", "int_2", "pending", "phase1"),
            ],
        )
        mock_fetch_all_outreach.return_value = {
            "int_1": {"outreach_status": "pending", "outreach_phase": "phase1"},
            # Live API says c2 is in_progress — this must be reflected in the header.
            "int_2": {"outreach_status": "in_progress", "outreach_phase": "phase1"},
        }

        result = await get_job_outreach_stats("job_123", user=MagicMock())

        # c1 → Pending (audit + live both say pending)
        # c2 → In Progress (no audit row, but live API says in_progress)
        assert result["buckets"]["pending"] == 1, (
            f"expected 1 Pending, got {result['buckets']['pending']} — "
            "c1 should be Pending"
        )
        assert result["buckets"]["in_progress"] == 1, (
            f"expected 1 In Progress, got {result['buckets']['in_progress']} — "
            "c2 has no audit row but live API says in_progress; "
            "collect_merged_outreach_payloads must include it"
        )

    asyncio.run(_test())
