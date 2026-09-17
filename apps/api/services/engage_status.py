"""Shared PAIR engage Pass/Fail classification.

Rankings, the launch report, and admin analytics must use the same rules:
pass/passed/hired → Pass; fail/failed/rejected with a score → Fail (no score is
an outreach miss, not an interview Fail); completed follows hard-filter status.
"""
from typing import Any, Optional


def parse_engage_score(raw: Any) -> Optional[float]:
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def format_engage_status(
    engage_status: Optional[str],
    engage_score: Optional[float],
    hf_display: str,
) -> str:
    if not engage_status:
        return "Pending"
    s = engage_status.lower()
    if s in ("passed", "hired", "pass", "qualified", "shortlisted", "selected"):
        return "Pass"
    if s in ("failed", "rejected", "fail", "disqualified", "declined"):
        if engage_score is None:
            return "Pending"
        return "Fail"
    if s in ("in_progress", "in progress", "screening", "interview_completed", "interview completed", "contacted"):
        return "In Progress"
    if s in ("completed", "complete"):
        hf_passed = (hf_display or "").strip().lower() in (
            "",
            "pass",
            "passed",
            "not_hard_filter",
        )
        return "Pass" if hf_passed else "Fail"
    return "Pending"


def hf_display_from_payload(payload: dict) -> str:
    return str(
        payload.get("engage_hard_filter_status")
        or payload.get("hard_filter_status")
        or ""
    ).strip().lower()


def score_from_payload(payload: dict) -> Optional[float]:
    return parse_engage_score(
        payload.get("engage_score")
        if payload.get("engage_score") is not None
        else payload.get("candidate_score")
        if payload.get("candidate_score") is not None
        else payload.get("score")
    )
