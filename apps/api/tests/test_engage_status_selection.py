"""select_engage_status: the ONE raw status a candidate is classified by.

The rank list table (candidates.py), the Rankings header and the launch
report (_summarise_outreach) all call it, so these cases are the contract
that keeps one candidate reading the same on every screen. The three
divergences it closes were reproduced on QA / in code:

  A. stored in_progress + live outreach block still `pending`  → In Progress
  C. stored in_progress + live interview_status `active`        → In Progress
  F. launch-time `sent` + live interview_status `in_progress`   → In Progress
"""
from services.engage_status import format_engage_status, select_engage_status, status_candidates


def _label(merged):
    raw = select_engage_status(merged)
    return format_engage_status(raw, None, "") if raw else "Pending"


# --- merged payloads (build_merged_outreach_payload results) -----------------
def test_case_a_live_outreach_block_cannot_demote_a_stored_in_progress():
    merged = {
        "outreach_status": "in_progress", "status": "in_progress",
        "outreach": {"outreach_status": "pending", "outreach_phase": "phase2"},
        "communications": [],
    }
    assert select_engage_status(merged) == "in_progress"
    assert _label(merged) == "In Progress"


def test_case_c_unrecognised_live_interview_status_cannot_displace_anything():
    started = {"outreach_status": "in_progress", "interview_status": "active",
               "outreach": {"outreach_status": "pending"}}
    assert select_engage_status(started) == "in_progress"
    stamped = {"outreach_status": "sent", "interview_status": "active"}
    assert select_engage_status(stamped) == "sent"
    assert _label(stamped) == "Pending"


def test_case_f_live_interview_status_lifts_a_launch_time_sent():
    merged = {"outreach_status": "sent", "interview_status": "in_progress",
              "outreach": {"outreach_status": "pending"}}
    assert select_engage_status(merged) == "in_progress"
    assert _label(merged) == "In Progress"


def test_nested_interview_status_counts_too():
    merged = {"outreach_status": "sent", "outreach": {"outreach_status": "pending", "interview_status": "In Progress"}}
    assert select_engage_status(merged) == "In Progress"


def test_a_decided_outcome_is_never_replaced():
    assert select_engage_status({"outreach_status": "passed", "interview_status": "in_progress"}) == "passed"
    # Fail with a score stays Fail even when pair-bot's interview_status says completed
    merged = {"outreach_status": "failed", "engage_score": "40", "interview_status": "completed"}
    assert select_engage_status(merged) == "failed"
    assert format_engage_status("failed", 40.0, "") == "Fail"


def test_score_less_failed_is_pending_so_in_progress_beats_it():
    merged = {"outreach_status": "failed", "interview_status": "in_progress"}
    assert select_engage_status(merged) == "in_progress"


def test_live_completed_lifts_in_progress_like_the_webhook_would():
    """effective_status_for_webhook maps completed + no hard-filter verdict to
    passed; the live interview_status follows the same rule via format_engage_status."""
    merged = {"outreach_status": "in_progress", "interview_status": "completed"}
    assert select_engage_status(merged) == "completed"
    assert _label(merged) == "Pass"


def test_nothing_known_returns_none():
    assert select_engage_status({}) is None
    assert select_engage_status({"outreach_status": "", "outreach": {}}) is None


# --- raw pair-bot / UI bodies ---------------------------------------------------
def test_raw_body_reads_the_nested_block_and_ignores_the_sourcing_status():
    raw = {"status": "passed", "outreach": {"outreach_status": "pending", "outreach_phase": "phase1"}}
    assert status_candidates(raw) == ["pending"]
    assert select_engage_status(raw) == "pending"


def test_raw_body_interview_status_still_lifts_pending():
    raw = {"outreach": {"outreach_status": "pending", "interview_status": "in_progress"}}
    assert select_engage_status(raw) == "in_progress"
    raw_top = {"interview_status": "in_progress", "outreach": {"outreach_status": "pending"}}
    assert select_engage_status(raw_top) == "in_progress"


def test_candidate_order_is_stored_then_live_then_nested_copy():
    merged = {"outreach_status": "a", "status": "b", "interview_status": "c",
              "outreach": {"interview_status": "d", "outreach_status": "e", "status": "f"}}
    assert status_candidates(merged) == ["a", "b", "c", "d", "e", "f"]
