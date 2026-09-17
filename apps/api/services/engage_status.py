"""Shared PAIR engage Pass/Fail classification.

Rankings, the launch report, and admin analytics must use the same rules:
pass/passed/hired → Pass; fail/failed/rejected with a score → Fail (no score is
an outreach miss, not an interview Fail); completed follows hard-filter status.
"""
from typing import Any, Dict, Optional


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


def hf_display_from_payload(payload: Dict[str, Any]) -> str:
    return str(
        payload.get("engage_hard_filter_status")
        or payload.get("hard_filter_status")
        or ""
    ).strip().lower()


def score_from_payload(payload: Dict[str, Any]) -> Optional[float]:
    return parse_engage_score(
        payload.get("engage_score")
        if payload.get("engage_score") is not None
        else payload.get("candidate_score")
        if payload.get("candidate_score") is not None
        else payload.get("score")
    )


def effective_funnel_status(
    engage_status: Optional[str],
    engage_score: Any,
    hf_status: Optional[str],
    *,
    has_interview: bool,
    sc_status: Optional[str] = None,
) -> str:
    """Python mirror of the admin-analytics funnel CASE.

    Rankings/launch-report Pass/Fail come from format_engage_status; this maps
    that display plus interview presence onto the dashboard buckets.
    """
    display = format_engage_status(
        engage_status,
        parse_engage_score(engage_score),
        (hf_status or "").strip().lower(),
    )
    if display == "Pass":
        return "passed"
    if display == "Fail":
        return "failed"
    if display == "In Progress":
        return "in_progress"
    if has_interview:
        return "launched"
    s = (sc_status or "").strip().lower()
    if s in ("launched", "submitted"):
        return "launched"
    if s in ("pass", "passed", "qualified", "shortlisted"):
        return "passed"
    if s in ("fail", "failed", "rejected") and parse_engage_score(engage_score) is not None:
        return "failed"
    return s or "pending"
