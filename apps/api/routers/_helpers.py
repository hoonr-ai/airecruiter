"""Shared helpers for route modules.

Re-exports the canonical DB connection from core.db so router modules
have one import path.

Also holds the monitored_jobs reading helpers — type coercion for its
mixed-type columns, and team scoping. These started life in
admin_analytics.py; they live here now that more than one router reads
monitored_jobs, so the second consumer isn't coupled to admin_analytics'
internals.
"""

import datetime
import json
from typing import Any, Dict, List, Optional, Tuple

from core.db import get_db_connection, get_dict_cursor_connection

__all__ = [
    "get_db_connection",
    "get_dict_cursor_connection",
    "_parse_posted_date",
    "_parse_recruiter_emails",
    "_ts",
    "_int",
    "_load_team_scope",
    "_mj_filter",
    "_sc_filter",
]


def _parse_posted_date(raw: str) -> Any:
    """Parse monitored_jobs.posted_date, a free-TEXT column.

    Shapes seen in the wild: "%b %d, %Y" from normalize_jobdiva_date
    ("Feb 24, 2026"), "YYYY-MM-DD HH:MM:SS IST" from readable_ist_now(),
    and ""/garbage. Parsing happens in Python (not SQL to_date) because
    to_date raises on shape-valid-but-impossible values like "Feb 31, 2026",
    which would abort the whole analytics section for every job.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return datetime.datetime.strptime(raw, "%b %d, %Y").date()
    except ValueError:
        pass
    try:
        return datetime.date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _ts(col: str) -> str:
    """Type-agnostic timestamp expression for monitored_jobs date columns.

    monitored_jobs mixes TIMESTAMP columns (prod) with TEXT columns holding
    either NOW()-style strings or readable_ist_now() strings like
    "2026-05-20 20:46:36 IST" (dev / legacy rows). Truncating the ::text form
    to 19 chars ("YYYY-MM-DD HH:MM:SS") parses every observed shape.
    """
    return f"NULLIF(substring({col}::text from 1 for 19), '')::timestamp"


def _ts_utc(col: str) -> str:
    """timestamptz expression for a monitored_jobs date column, zone-correct.

    `readable_ist_now()` writers store an India wall-clock reading with an
    " IST" suffix ("2026-09-21 19:16:55 IST"); NOW()-style writers store UTC.
    `_ts()` truncates the suffix away, so `_ts(col) AT TIME ZONE 'UTC'` read
    every IST row 5h30m late — the "Added on PAIR is +5:30" bug in Admin
    Analytics. This honours the suffix exactly like the launch report's
    `_parse_monitored_jobs_timestamp` (IST when the text ends in "IST",
    otherwise UTC), so both reports agree on the same row.

    No '%' and no placeholders: safe inside parameterised statements.
    """
    return (
        f"(CASE WHEN RIGHT(UPPER(TRIM({col}::text)), 3) = 'IST' "
        f"THEN ({_ts(col)}) AT TIME ZONE 'Asia/Kolkata' "
        f"ELSE ({_ts(col)}) AT TIME ZONE 'UTC' END)"
    )


# monitored_jobs columns added after the base table by a startup ALTER
# (routers/jobs._ensure_monitored_jobs_schema). That ALTER needs an ACCESS
# EXCLUSIVE lock and runs inside main.py's 10s startup schema budget, swallowing
# its own errors, so behind a long query it can be cancelled and leave a column
# missing until a later boot. A report that selects a missing column would 500
# as a whole; selecting NULL just renders "—" in that column.
OPTIONAL_MONITORED_JOBS_COLUMNS = ("pair_posted_by", "pair_launched_by")

# to_regclass resolves through search_path like the reports' bare
# `monitored_jobs`, and yields NULL (no rows) instead of raising if the table
# is absent. No '%': it runs with an empty params tuple.
_PRESENT_MONITORED_JOBS_COLUMNS_SQL = (
    "SELECT attname FROM pg_attribute "
    "WHERE attrelid = to_regclass('monitored_jobs') AND attnum > 0 AND NOT attisdropped"
)


def optional_monitored_jobs_columns(cur, alias: str = "mj.") -> Dict[str, str]:
    """``{column: select expression}`` for OPTIONAL_MONITORED_JOBS_COLUMNS:
    ``<alias><column>`` when monitored_jobs has it, else ``NULL::text``.

    One sub-millisecond catalog read per request, deliberately not cached: a
    cache would have to notice the ALTER landing on a later boot.
    """
    cur.execute(_PRESENT_MONITORED_JOBS_COLUMNS_SQL, ())
    present = {str(r[0]) for r in cur.fetchall() or []}
    return {
        col: (f"{alias}{col}" if col in present else "NULL::text")
        for col in OPTIONAL_MONITORED_JOBS_COLUMNS
    }


def _int(col: str) -> str:
    """Type-agnostic integer expression for counter columns (INT or TEXT)."""
    return f"COALESCE(NULLIF(TRIM({col}::text), '')::numeric, 0)::int"


def _parse_recruiter_emails(raw_emails: Any) -> List[str]:
    if isinstance(raw_emails, str):
        try:
            emails = json.loads(raw_emails) if raw_emails.strip().startswith("[") else [raw_emails]
        except Exception:
            emails = [raw_emails] if raw_emails else []
    elif isinstance(raw_emails, list):
        emails = raw_emails
    else:
        emails = []
    return [str(e).strip().lower() for e in emails if e and str(e).strip()]


# ---------------------------------------------------------------------------
# Team scoping
# ---------------------------------------------------------------------------
# When a team scope is active (admin clicked a team tab, or the caller is a
# team lead), every section is restricted to the jobs assigned to that team's
# emails via monitored_jobs.recruiter_emails. sourced_candidates /
# engage_interview_audit rows key on either the alphanumeric JobDiva ref or
# the job_id uuid text, so the scope carries both key sets.

def _load_team_scope(conn, team_id: str) -> Dict[str, Any]:
    """Resolve a team into its email list + scoped job key sets.

    Raises LookupError for an unknown team — callers translate to 404.
    """
    from services import teams_db

    team = teams_db.get_team(team_id)
    if not team:
        raise LookupError(f"Team '{team_id}' not found.")
    emails = set(
        e.strip().lower()
        for e in (team.get("lead_emails") or []) + (team.get("member_emails") or [])
        if e and str(e).strip()
    )

    job_ids: List[str] = []
    jobdiva_ids: List[str] = []
    with conn.cursor() as cur:
        cur.execute("SELECT job_id, jobdiva_id, recruiter_emails FROM monitored_jobs")
        for job_id, jobdiva_id, raw_emails in cur.fetchall():
            assigned = _parse_recruiter_emails(raw_emails)
            if assigned and not emails.isdisjoint(assigned):
                if job_id is not None and str(job_id):
                    job_ids.append(str(job_id))
                if jobdiva_id is not None and str(jobdiva_id).strip():
                    jobdiva_ids.append(str(jobdiva_id).strip())

    return {
        "team_id": team["id"],
        "team_name": team["name"],
        "emails": sorted(emails),
        # monitored_jobs rows are matched on job_id::text
        "job_ids": sorted(set(job_ids)),
        # sourced_candidates / engage_interview_audit rows were written under
        # either key — match both (mirrors _sum_metrics_for_job in jobs.py)
        "sc_keys": sorted(set(job_ids) | set(jobdiva_ids)),
    }


def _mj_filter(scope: Optional[Dict[str, Any]], alias: str = "") -> Tuple[str, List[Any]]:
    """(SQL condition, params) restricting monitored_jobs rows to the scope.

    Returns ("TRUE", []) when unscoped so callers can embed it uniformly.
    """
    if scope is None:
        return "TRUE", []
    col = f"{alias}.job_id" if alias else "job_id"
    return f"{col}::text = ANY(%s)", [scope["job_ids"]]


def _sc_filter(scope: Optional[Dict[str, Any]], col: str) -> Tuple[str, List[Any]]:
    """(SQL condition, params) restricting candidate-keyed tables to the scope."""
    if scope is None:
        return "TRUE", []
    return f"{col} = ANY(%s)", [scope["sc_keys"]]
