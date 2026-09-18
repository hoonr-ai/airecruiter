"""Rankings header stats (GET /jobs/{id}/outreach-stats).

The header is the launch-report row for this job: the same launched
population (services/launched_candidates.py) through the same aggregation
(launch_report.summarise_launched_candidates). These tests pin the endpoint's
plumbing and the behaviours a recruiter reads off the header:

  * one unit per launched PERSON — a re-launched candidate is not two;
  * a candidate whose audit insert was lost still counts, on live status;
  * a sourced-but-never-launched row never inflates Pending;
  * live pair-bot wins over stored status, stored status covers a silent bot;
  * Pair Bot phase tokens land in the same P1–P4 / Extra 1–3 columns as the
    launch report, and a still-pending Extra job stays Phase 1.
"""
import asyncio
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_db_connection():
    with patch("routers.jobs.get_db_connection") as mock_conn:
        conn = mock_conn.return_value
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = ("jobdiva_123", "job_123")
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
def mock_launched():
    with patch("routers.jobs.fetch_launched_candidates") as mock_rows:
        yield mock_rows


def _launched(cid, iid, *, status=None, phase=None, score=None, hf=None, audit_status=None, audit_response=None):
    """A row as services/launched_candidates.fetch_launched_candidates returns it."""
    return {
        "candidate_id": cid,
        "interview_id": iid,
        "audit_interview_id": iid if audit_status is not None or audit_response is not None else None,
        "audit_status": audit_status,
        "audit_response": audit_response,
        "audit_created_at": None,
        "engage_status": status,
        "engage_score": score,
        "engage_candidate_score": None,
        "engage_hard_filter_status": hf,
        "engage_completed_at": None,
        "engage_updated_at": None,
        "first_attempted_at": None,
        "first_completed_at": None,
        "phase": None,
        "outreach_phase": phase,
        "channel": None,
        "outreach_channel": None,
    }


def _run(mock_launched, mock_fetch_all_outreach, rows, live):
    from routers.jobs import get_job_outreach_stats

    mock_launched.return_value = rows
    mock_fetch_all_outreach.return_value = live
    return asyncio.run(get_job_outreach_stats("job_123", user=MagicMock()))


def test_header_reads_the_shared_population_with_both_job_keys(
    mock_db_connection, mock_verify_job_access, mock_fetch_all_outreach, mock_launched
):
    _run(mock_launched, mock_fetch_all_outreach, [_launched("c1", "int_1", status="pending")], {})
    conn = mock_db_connection.return_value
    mock_launched.assert_called_once_with(conn, ["jobdiva_123", "job_123"])
    mock_fetch_all_outreach.assert_called_once_with(["int_1"])


def test_live_api_wins_over_stored_status(
    mock_db_connection, mock_verify_job_access, mock_fetch_all_outreach, mock_launched
):
    result = _run(
        mock_launched, mock_fetch_all_outreach,
        [_launched("c1", "int_1", status="in_progress", phase="phase1",
                   audit_status="fail", audit_response={"status": "fail"})],
        {"int_1": {"outreach_status": "pass", "outreach_phase": "phase3"}},
    )
    assert result["buckets"]["passed"] == 1
    assert result["buckets"]["failed"] == 0
    assert result["buckets"]["completed"] == 1
    # Pair Bot phase3 is rankings Phase 4
    assert result["phases"]["phase4"] == 1
    assert result["phases"]["phase3"] == 0
    assert result["launched"] == 1


def test_stored_status_covers_a_silent_pair_bot(
    mock_db_connection, mock_verify_job_access, mock_fetch_all_outreach, mock_launched
):
    result = _run(
        mock_launched, mock_fetch_all_outreach,
        [_launched("c1", "int_1", status="sent", phase="phase2",
                   audit_status="in_progress",
                   audit_response={"status": "in_progress", "outreach_channel": "sms"})],
        {},
    )
    assert result["buckets"]["in_progress"] == 1
    assert result["channels"]["sms"] == 1
    assert result["phases"]["phase3"] == 1  # Pair Bot phase2 -> rankings Phase 3


def test_nothing_launched_returns_zero_shape(
    mock_db_connection, mock_verify_job_access, mock_fetch_all_outreach, mock_launched
):
    result = _run(mock_launched, mock_fetch_all_outreach, [], {})
    assert result["buckets"]["in_progress"] == 0
    assert result["buckets"]["passed"] == 0
    assert result["phases"]["phase1"] == 0
    assert result["launched"] == 0
    mock_fetch_all_outreach.assert_not_called()


def test_unknown_job_returns_zero_shape_without_touching_pair_bot(
    mock_db_connection, mock_verify_job_access, mock_fetch_all_outreach, mock_launched
):
    from routers.jobs import get_job_outreach_stats

    cur = mock_db_connection.return_value.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = None
    result = asyncio.run(get_job_outreach_stats("job_123", user=MagicMock()))
    assert result["launched"] == 0 and result["buckets"]["pending"] == 0
    mock_launched.assert_not_called()
    mock_fetch_all_outreach.assert_not_called()


def test_empty_outreach_stats_returns_independent_nested_dicts():
    from routers.jobs import _empty_outreach_stats

    first = _empty_outreach_stats()
    second = _empty_outreach_stats()
    first["phases"]["phase1"] += 1
    assert second["phases"]["phase1"] == 0
    assert first is not second
    assert first["phases"] is not second["phases"]
    assert first["buckets"] is not second["buckets"]


def test_relaunched_candidate_is_one_launched_candidate(
    mock_db_connection, mock_verify_job_access, mock_fetch_all_outreach, mock_launched
):
    """The population is people: the shared SQL already collapsed the person's
    two interviews onto the newest one, so the header shows ONE candidate
    whose status is the newest interview's — exactly the table's row."""
    result = _run(
        mock_launched, mock_fetch_all_outreach,
        [_launched("c1", "int_2", status="pending", phase="phase1", audit_status="Initiated")],
        {
            "int_1": {"outreach_status": "completed"},   # the older interview: never asked for, never counted
            "int_2": {"outreach_status": "pending", "outreach_phase": "phase1"},
        },
    )
    assert result["launched"] == 1
    assert result["buckets"]["pending"] == 1
    assert result["buckets"]["completed"] == 0
    mock_fetch_all_outreach.assert_called_once_with(["int_2"])


def test_candidate_with_no_audit_row_uses_live_api_status(
    mock_db_connection, mock_verify_job_access, mock_fetch_all_outreach, mock_launched
):
    """Regression (PR #673): a candidate launched but whose audit insert was
    lost must be counted on their live Pair Bot status, not dropped and not
    parked in Pending. The shared population includes them via the JSONB
    interview id."""
    result = _run(
        mock_launched, mock_fetch_all_outreach,
        [
            _launched("c1", "int_1", status="pending", phase="phase1", audit_status="Initiated"),
            _launched("c2", "int_2", status="pending", phase="phase1"),   # no audit side at all
        ],
        {
            "int_1": {"outreach_status": "pending", "outreach_phase": "phase1"},
            "int_2": {"outreach_status": "in_progress", "outreach_phase": "phase1"},
        },
    )
    assert result["launched"] == 2
    assert result["buckets"]["pending"] == 1
    assert result["buckets"]["in_progress"] == 1


def test_buckets_partition_launched_and_launch_time_sent_is_pending(
    mock_db_connection, mock_verify_job_access, mock_fetch_all_outreach, mock_launched
):
    """A just-launched candidate carries engage_status='sent' / audit
    'Initiated' before pair-bot has contacted anyone. The table shows them as
    Pending, so the header must too — and the four buckets sum to launched."""
    result = _run(
        mock_launched, mock_fetch_all_outreach,
        [
            _launched("c1", "int_1", status="sent", audit_status="Initiated"),
            _launched("c2", "int_2", status="in_progress", audit_status="Initiated"),
            _launched("c3", "int_3", status="completed", hf="pass", audit_status="completed"),
            _launched("c4", "int_4", status="sent", audit_status="Initiated"),
        ],
        {"int_4": {"outreach_status": "outreach_incomplete"}},
    )
    b = result["buckets"]
    assert (b["pending"], b["in_progress"], b["completed"], b["partial_complete"]) == (1, 1, 1, 1)
    assert b["pending"] + b["in_progress"] + b["completed"] + b["partial_complete"] == result["launched"] == 4
    assert (b["passed"], b["failed"]) == (1, 0)


def test_terminal_jsonb_status_without_an_interview_id_is_not_in_the_population():
    """A failed launch writes engage_status='failed' with an empty interview id.
    That person was never launched: the shared SQL keeps them out, so the
    header cannot count them — there is no Python-side "uncovered" add-on."""
    from services.launched_candidates import LAUNCHED_CANDIDATES_SQL
    import routers.jobs as jobs_module

    assert "WHERE la.candidate_id IS NOT NULL OR sc.engage_interview_id IS NOT NULL" in LAUNCHED_CANDIDATES_SQL
    assert not hasattr(jobs_module, "apply_uncovered_pass_fail")


def test_pairbot_phase3_counts_as_rankings_phase4(
    mock_db_connection, mock_verify_job_access, mock_fetch_all_outreach, mock_launched
):
    result = _run(
        mock_launched, mock_fetch_all_outreach,
        [_launched("c1", "int_1", status="pending", phase="phase3", audit_status="Initiated")],
        {"int_1": {"outreach_status": "pending", "outreach_phase": "phase3"}},
    )
    assert result["phases"]["phase4"] == 1
    assert result["phases"]["phase3"] == 0


def test_extra2_stays_extra2(
    mock_db_connection, mock_verify_job_access, mock_fetch_all_outreach, mock_launched
):
    result = _run(
        mock_launched, mock_fetch_all_outreach,
        [_launched("c1", "int_1", status="pending", phase="phase1_6hr_extra", audit_status="Initiated")],
        {"int_1": {"outreach_status": "pending", "outreach_phase": "phase1_6hr_extra"}},
    )
    assert result["phases"]["extra2"] == 1
    assert result["phases"]["extra"] == 1
    assert result["phases"]["phase2"] == 0


def test_pending_extra_job_stays_phase1(
    mock_db_connection, mock_verify_job_access, mock_fetch_all_outreach, mock_launched
):
    result = _run(
        mock_launched, mock_fetch_all_outreach,
        [_launched("c1", "int_1", status="pending", phase="phase1", audit_status="Initiated")],
        {
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
        },
    )
    assert result["phases"]["phase1"] == 1
    assert result["phases"]["extra1"] == 0
