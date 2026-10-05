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
import logging
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException

from core.db import get_db_connection, get_dict_cursor_connection
from core.auth import UserIdentity, verify_job_access

logger = logging.getLogger(__name__)

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
    "_get_job_draft_sync",
    "_verify_job_access_by_id",
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


# ---------------------------------------------------------------------------
# Job-access guard
#
# Moved here from routers/jobs.py so other routers (candidates.py,
# engagement.py) depend on one shared, public-by-package module instead of
# importing a leading-underscore name out of a sibling router — that import
# worked (no cycle today), but it coupled an unrelated router's internals to
# jobs.py and was one future jobs.py -> that router import away from a real
# circular import. jobs.py re-exports both names for its own call sites.
# ---------------------------------------------------------------------------
def _get_job_draft_sync(job_id: str) -> dict:
    def parse_json(val):
        if not val: return []
        if isinstance(val, (list, dict)): return val
        try: return json.loads(val)
        except: return []

    def _fetch_one_monitored_job(cursor, requested_job_id: str):
        # Avoid `OR` predicates on job_id/jobdiva_id so planner can use single-column
        # indexes more reliably under load.
        cursor.execute(
            """
            SELECT * FROM monitored_jobs
            WHERE job_id = %s
            UNION ALL
            SELECT * FROM monitored_jobs
            WHERE jobdiva_id = %s AND job_id <> %s
            LIMIT 1
            """,
            (requested_job_id, requested_job_id, requested_job_id),
        )
        return cursor.fetchone()

    conn = get_dict_cursor_connection()
    cursor = conn.cursor()
    try:
        normalized_job_id = job_id.lstrip('0') if job_id and '-' not in job_id else job_id
        job_row = _fetch_one_monitored_job(cursor, job_id)
        if not job_row and normalized_job_id != job_id:
            job_row = _fetch_one_monitored_job(cursor, normalized_job_id)
    finally:
        cursor.close()
        conn.close()

    if not job_row:
        return {"status": "error", "message": f"No data found for job {job_id}"}

    return {
        "status": "success",
        "data": {
            "id": job_id,
            "job_id": job_id,
            "jobdiva_id": job_row.get("jobdiva_id") or job_id,
            "title": job_row.get("title") or "",
            "enhanced_title": job_row.get("enhanced_title") or job_row.get("title") or "",
            "customer_name": job_row.get("customer_name") or "",
            "location_type": job_row.get("location_type") or "Onsite",
            "city": job_row.get("city") or "",
            "state": job_row.get("state") or "",
            "zip_code": job_row.get("zip_code") or "",
            "ai_description": job_row.get("ai_description") or "",
            "jobdiva_description": job_row.get("jobdiva_description") or "",
            "recruiter_notes": job_row.get("recruiter_notes") or "",
            "work_authorization": job_row.get("work_authorization") or "",
            "selected_job_boards": parse_json(job_row.get("selected_job_boards")),
            "recruiter_emails": parse_json(job_row.get("recruiter_emails")),
            "selected_employment_types": parse_json(job_row.get("selected_employment_types")),
            "current_step": job_row.get("current_step") or 1,
            "screening_level": job_row.get("screening_level") or "L0.5",
            "bot_introduction": job_row.get("bot_introduction") or "",
            "resume_match_filters": parse_json(job_row.get("resume_match_filters")),
            "sourcing_filters": job_row.get("sourcing_filters") or {},
            "job_details": {
                "id": job_row.get("job_id") or job_id,
                "jobdiva_id": job_row.get("jobdiva_id") or job_id,
                "title": job_row.get("title") or "",
                "customer_name": job_row.get("customer_name") or "",
                "status": job_row.get("status") or "OPEN",
                "city": job_row.get("city") or "",
                "state": job_row.get("state") or "",
                "zip_code": job_row.get("zip_code") or "",
                "location_type": job_row.get("location_type") or "Onsite",
                "description": job_row.get("jobdiva_description") or "",
                "jobdiva_description": job_row.get("jobdiva_description") or "",
                "ai_description": job_row.get("ai_description") or "",
                "recruiter_notes": job_row.get("recruiter_notes") or "",
                "employment_type": job_row.get("employment_type") or "",
                "pay_rate": job_row.get("pay_rate") or "",
                "work_authorization": job_row.get("work_authorization") or ""
            }
        }
    }


def _verify_job_access_by_id(job_id: str, user: UserIdentity, allow_not_found: bool = False, check_duplicate_launch: bool = False) -> None:
    if user.is_admin:
        return
    try:
        job_dict = _get_job_draft_sync(job_id)
        if job_dict.get("status") == "success" and job_dict.get("data"):
            try:
                verify_job_access(job_dict["data"], user)
                return
            except HTTPException as e:
                if e.status_code == 403 and check_duplicate_launch:
                    job_data = job_dict["data"]
                    raw_emails = job_data.get("recruiter_emails", [])
                    if isinstance(raw_emails, str):
                        try:
                            emails = json.loads(raw_emails) if raw_emails.strip().startswith("[") else [raw_emails]
                        except Exception:
                            emails = [raw_emails] if raw_emails else []
                    elif isinstance(raw_emails, list):
                        emails = raw_emails
                    else:
                        emails = []
                    clean_assigned_emails = [str(email).strip().lower() for email in emails if email]
                    if clean_assigned_emails:
                        assigned_email = clean_assigned_emails[0]
                        raise HTTPException(
                            status_code=403,
                            detail={"code": "JOB_ALREADY_LAUNCHED", "recruiter_email": assigned_email, "message": f"Job has already been launched by {assigned_email}."}
                        )
                    else:
                        raise HTTPException(
                            status_code=403,
                            detail={"code": "JOB_LOCKED", "message": "This job cannot be launched."}
                        )
                raise
        elif allow_not_found and job_dict.get("status") == "error" and "No data found" in str(job_dict.get("message", "")):
            return
    except Exception as e:
        if isinstance(e, HTTPException):
            raise
        logger.error(f"Error verifying job access for job_id={job_id}: {e}")

    raise HTTPException(
        status_code=403,
        detail="Access denied. You do not have permission to access or modify this job."
    )
