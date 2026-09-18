"""Shared PAIR engage Pass/Fail classification.

Rankings, the launch report, and admin analytics must use the same rules:
pass/passed/hired → Pass; fail/failed/rejected with a score → Fail (no score is
an outreach miss, not an interview Fail); completed follows hard-filter status.
Everything else — including the launch-time `sent` / `Initiated` stamps and the
reminder phases pair-bot reports as a status — is Pending: the candidate has
not started. The Rankings header and launch-report buckets split Pending / In
Progress on this same function (see _summarise_outreach), so a candidate can
never read Pending in the table and In Progress in a count.
"""
from typing import Any, Dict, List, Optional


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
    if s in (
        "in_progress", "in progress", "screening", "interview_completed",
        "interview completed", "contacted",
        # pair-bot: the screening call is happening right now.
        "call_in_progress",
    ):
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


# ---------------------------------------------------------------------------
# One status per candidate, for every screen
# ---------------------------------------------------------------------------
# Display ladder used to pick ONE status per candidate out of several raw
# candidates (stored outreach_status, stored status, live interview_status…).
# Pass and Fail tie on purpose: a decided outcome is never replaced by another
# decided outcome coming from a lower-priority source.
_DISPLAY_RANK = {"Pending": 0, "In Progress": 1, "Fail": 2, "Pass": 2}


def status_candidates(merged: Dict[str, Any]) -> List[str]:
    """Raw status strings for one candidate, most authoritative first.

    ``merged`` is either a ``build_merged_outreach_payload`` result — flat; the
    stored → audit → live monotonic merge sits in ``outreach_status`` /
    ``status`` and a copy of pair-bot's live body may sit under ``outreach`` —
    or a raw pair-bot / UI body: a nested ``outreach`` block and no top-level
    ``outreach_status``. For a raw body the top-level ``status`` is the
    candidate's sourcing status, not an outreach status, and is ignored.
    """
    nested = merged.get("outreach") if isinstance(merged.get("outreach"), dict) else {}
    raw_body = "outreach_status" not in merged and bool(nested)
    if raw_body:
        ordered = [nested.get("outreach_status"), nested.get("status")]
    else:
        ordered = [merged.get("outreach_status"), merged.get("status")]
    # pair-bot's interview state runs ahead of its outreach-sequence state: the
    # interview can be in progress while reminders are still scheduled.
    ordered += [merged.get("interview_status"), nested.get("interview_status")]
    if not raw_body:
        # The live block exactly as fetched — fill-in only, it never outranks
        # the merge it was already folded into.
        ordered += [nested.get("outreach_status"), nested.get("status")]
    return [str(value).strip() for value in ordered if value is not None and str(value).strip()]


def select_engage_status(merged: Dict[str, Any]) -> Optional[str]:
    """The ONE raw status a candidate is classified by, on every screen.

    The first candidate (see ``status_candidates``) whose display label reads
    the furthest along wins. Consequences, all deliberate:

    * a stored ``in_progress`` is never demoted by the live outreach block
      still saying ``pending``;
    * a live ``interview_status: in_progress`` lifts a launch-time ``sent``;
    * a token the display rules do not recognise as progress (``active``,
      ``phase2``) cannot displace anything;
    * a decided Pass or Fail is never replaced by the other.

    The rank list table (candidates.py), the Rankings header and the launch
    report (``_summarise_outreach``) all classify through this function, so a
    candidate reads the same everywhere.
    """
    score = score_from_payload(merged)
    hf = hf_display_from_payload(merged)
    best: Optional[str] = None
    best_rank = -1
    for raw in status_candidates(merged):
        rank = _DISPLAY_RANK[format_engage_status(raw.lower(), score, hf)]
        if rank > best_rank:
            best, best_rank = raw, rank
    return best
