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


# The status vocabularies behind format_engage_status. Module-level so the SQL
# twin (engage_display_sql) is generated from the very same tuples and cannot
# drift from the Python rules.
PASS_STATUSES = ("passed", "hired", "pass", "qualified", "shortlisted", "selected")
FAIL_STATUSES = ("failed", "rejected", "fail", "disqualified", "declined")
IN_PROGRESS_STATUSES = (
    "in_progress", "in progress", "screening", "interview_completed",
    "interview completed", "contacted",
    # pair-bot: the screening call is happening right now.
    "call_in_progress",
)
COMPLETED_STATUSES = ("completed", "complete")
# Hard-filter values that let a bare "completed" read as Pass.
HF_PASS_VALUES = ("", "pass", "passed", "not_hard_filter")


def format_engage_status(
    engage_status: Optional[str],
    engage_score: Optional[float],
    hf_display: str,
) -> str:
    if not engage_status:
        return "Pending"
    s = engage_status.lower()
    if s in PASS_STATUSES:
        return "Pass"
    if s in FAIL_STATUSES:
        if engage_score is None:
            return "Pending"
        return "Fail"
    if s in IN_PROGRESS_STATUSES:
        return "In Progress"
    if s in COMPLETED_STATUSES:
        hf_passed = (hf_display or "").strip().lower() in HF_PASS_VALUES
        return "Pass" if hf_passed else "Fail"
    return "Pending"


def _sql_list(values) -> str:
    return ", ".join("'" + v.replace("'", "''") + "'" for v in values)


# A JSON text value that float() would accept, restricted to plain decimals —
# the only shape pair-bot and the webhook write. Anything else is "no score",
# exactly as parse_engage_score returns None for it.
_NUMERIC_TEXT_RE = r"^\s*[-+]?([0-9]+([.][0-9]*)?|[.][0-9]+)([eE][-+]?[0-9]+)?\s*$"


def engage_display_sql(data_expr: str = "sc.data") -> str:
    """SQL twin of ``format_engage_status`` over a stored candidate blob.

    ``data_expr`` is a ``sourced_candidates.data``-shaped JSONB expression.
    Yields 'Pass' | 'Fail' | 'In Progress' | 'Pending' from the stored
    ``engage_status`` / ``engage_score`` / hard-filter keys — the same inputs
    and rules the rank list uses for a candidate whose status is not being
    lifted by a live pair-bot read. Use it wherever a report has to classify
    candidates in SQL (per-job / per-recruiter counts) so every report's Pass
    matches the rank list's Pass. Drift-tested against the Python rules on a
    real Postgres (tests/test_engage_status_sql.py).

    Contains no ``%`` and no placeholders, so it is safe to splice into a
    psycopg2 statement that also takes parameters.
    """
    status = f"LOWER(TRIM(COALESCE({data_expr}->>'engage_status', '')))"
    score = f"COALESCE({data_expr}->>'engage_score', '')"
    hf = (
        f"LOWER(TRIM(COALESCE(NULLIF(TRIM({data_expr}->>'engage_hard_filter_status'), ''), "
        f"NULLIF(TRIM({data_expr}->>'hard_filter_status'), ''), '')))"
    )
    return (
        "(CASE"
        f" WHEN {status} IN ({_sql_list(PASS_STATUSES)}) THEN 'Pass'"
        f" WHEN {status} IN ({_sql_list(FAIL_STATUSES)}) THEN"
        f" (CASE WHEN {score} ~ '{_NUMERIC_TEXT_RE}' THEN 'Fail' ELSE 'Pending' END)"
        f" WHEN {status} IN ({_sql_list(IN_PROGRESS_STATUSES)}) THEN 'In Progress'"
        f" WHEN {status} IN ({_sql_list(COMPLETED_STATUSES)}) THEN"
        f" (CASE WHEN {hf} IN ({_sql_list(HF_PASS_VALUES)}) THEN 'Pass' ELSE 'Fail' END)"
        " ELSE 'Pending' END)"
    )


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
