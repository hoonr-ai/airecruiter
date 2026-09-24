"""Recruiter Analytics: per-recruiter rollup of jobs, candidates and outcomes.

GET /api/v1/admin/recruiter-analytics

Attribution is BY JOB ASSIGNMENT: each job's numbers are credited to every
email in its `monitored_jobs.recruiter_emails`. A job shared by three
recruiters therefore shows up in three rows, so the Totals block is computed
over the DISTINCT job set and is never the sum of the recruiter rows. (Crediting
by who acted, `sourced_candidates.data.submitted_by`, only exists for feedback
since 2026-09-11 and nothing at all records who sourced or screened, so it
cannot cover history.)

Definitions are imported, never restated here:

  * Pass / Fail / In Progress, feedback kinds, internal vs external PAIR
    submits and "awaiting feedback" come from
    services/job_candidate_metrics.fetch_job_candidate_metrics, which is built
    on the rank list's own rules (engage_display_sql, feedback_metrics).
  * "JobDiva-Confirmed" is `monitored_jobs.pair_external_subs`, the
    JobDiva-verified count, kept apart from "PAIR External" (what PAIR
    recorded) because a gap between the two is a real signal.
  * A job's launch (the range filter, "Launched", "Jobs Launched") is the
    Launch Report's: its first SUCCESSFUL launch, see _fetch_job_rows.
  * Step 5 time comes from services/job_step_time.fetch_step_metrics, the
    read the Launch Report and Admin Analytics use too. It is the one
    exception to crediting by assignment: a recruiter row's Step 5 Active
    Time is the time THAT person spent themselves (the table records who had
    the step open), while Step 5 → Launch is a job property and is credited
    by assignment like everything else. See _step5_active.

Access mirrors Admin Analytics and the Launch Report: admins see everyone
(optional team_id), team leads are pinned to their own team, everyone else
gets 403.
"""

import asyncio
import datetime
import logging
import os
import re
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from core.auth import UserIdentity, get_current_user, get_user_scope_emails
from routers._helpers import (
    _int,
    _load_team_scope,
    _mj_filter,
    _parse_recruiter_emails,
    _ts_utc,
    get_db_connection,
    optional_monitored_jobs_columns,
)
from routers.launch_report import REPORT_DB_TIMEZONE
from services.job_attribution import attribution_fields
from services.job_candidate_metrics import empty_metrics, fetch_job_candidate_metrics
from services.job_step_time import empty_step_metrics, fetch_step_metrics

router = APIRouter(prefix="/api/v1", tags=["Recruiter Analytics"])
logger = logging.getLogger(__name__)

UTC = datetime.timezone.utc
REPORT_TIMEZONE = ZoneInfo(os.getenv("REPORT_TIMEZONE", "America/New_York"))

# A year plus a leap day: wide enough for "last 12 months", narrow enough that
# a typo'd year cannot turn into an unbounded request.
MAX_RANGE_DAYS = 366

# The jobs / Step 5 time / directory reads are small indexed-or-tiny-table
# statements. The candidate-metrics read aggregates sourced_candidates for every
# job in the population, which on an all-time admin view is the heaviest
# statement here.
_STATEMENT_TIMEOUT_MS = int(os.getenv("RECRUITER_ANALYTICS_STATEMENT_TIMEOUT_MS", "5000"))
_METRICS_STATEMENT_TIMEOUT_MS = int(os.getenv("RECRUITER_ANALYTICS_METRICS_TIMEOUT_MS", "20000"))

# Per-worker cache. With 8 uvicorn workers a burst of dashboard opens costs at
# most one computation per worker per scope per minute; ?refresh=true skips it.
_CACHE_TTL_SECONDS = 60.0
# A degraded payload (a section failed and carries a warning) is kept too, but
# briefly. Its likeliest cause is the metrics statement hitting its 20s timeout
# on a big population (an admin's all-time view), and never caching it meant
# every open re-ran a scan already known to time out, holding a pool
# connection for 20s to return the same fallback. The short TTL still lets a
# transient failure clear on its own; the page's Refresh (?refresh=true)
# retries at once, and a healthy result overwrites the entry.
_DEGRADED_CACHE_TTL_SECONDS = 20.0
_CACHE_MAX_ENTRIES = 128
# key -> (expires_at on the monotonic clock, payload)
_cache: Dict[Tuple[Optional[str], Optional[str], str, str], Tuple[float, Dict[str, Any]]] = {}
_cache_lock = threading.Lock()

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# v2+ clones of a job are stored as "<root>-v<N>" (routers/jobs.create_job_version).
_VERSION_SUFFIX_RE = re.compile(r"-v\d+$", re.IGNORECASE)

# Column order of the monitored_jobs read below.
_JOB_COLUMNS: Tuple[str, ...] = (
    "job_id",
    "jobdiva_id",
    "title",
    "customer_name",
    "recruiter_emails",
    "is_archived",
    "created_at",
    "launched_at",
    "candidates_sourced",
    "candidates_launched",
    "pair_external_subs",
    "jobdiva_total_subs",
    "time_to_first_pass",
    "pair_posted_by",
    "pair_launched_by",
    "parent_job_id",
    "version",
)

_RANGE_DESCRIPTION = (
    "Jobs launched in range: jobs whose first successful PAIR launch falls between "
    "start_date 00:00 and end_date 24:00 US Eastern time."
)
_ALL_TIME_DESCRIPTION = "All jobs, launched or not."


# ---------------------------------------------------------------------------
# Request parsing
# ---------------------------------------------------------------------------

def _resolve_scope_team_id(user: UserIdentity, team_id: Optional[str]) -> Optional[str]:
    """Same rule as Admin Analytics and the Launch Report."""
    if user.is_admin:
        return (team_id or "").strip() or None
    if user.is_team_lead and user.team_id:
        # A team lead's team_id param is ignored, never honoured.
        return user.team_id
    raise HTTPException(
        status_code=403,
        detail="Access denied. Admin or team lead access required to view recruiter analytics.",
    )


def _parse_range(
    start_date: Optional[str], end_date: Optional[str]
) -> Optional[Tuple[datetime.date, datetime.date]]:
    start_raw = (start_date or "").strip()
    end_raw = (end_date or "").strip()
    if not start_raw and not end_raw:
        return None
    if not (start_raw and end_raw):
        raise HTTPException(status_code=400, detail="Both start_date and end_date are required for a range.")
    try:
        if not (_DATE_RE.match(start_raw) and _DATE_RE.match(end_raw)):
            raise ValueError
        start = datetime.date.fromisoformat(start_raw)
        end = datetime.date.fromisoformat(end_raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid start_date/end_date: expected YYYY-MM-DD.")
    if start > end:
        raise HTTPException(status_code=400, detail="start_date must not be after end_date.")
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        raise HTTPException(status_code=400, detail=f"Date range cannot exceed {MAX_RANGE_DAYS} days.")
    # Today is allowed on purpose: nothing here waits on pair-bot, and every
    # number is read from the database as of this request.
    return start, end


def _range_bounds_utc(start: datetime.date, end: datetime.date) -> Tuple[datetime.datetime, datetime.datetime]:
    """[start 00:00 ET, end+1 00:00 ET) as UTC instants.

    Midnight is never skipped or repeated by a US DST change (those happen at
    02:00), so combining with the zone is unambiguous.
    """
    lo = datetime.datetime.combine(start, datetime.time.min, tzinfo=REPORT_TIMEZONE)
    hi = datetime.datetime.combine(end + datetime.timedelta(days=1), datetime.time.min, tzinfo=REPORT_TIMEZONE)
    return lo.astimezone(UTC), hi.astimezone(UTC)


def _normalize_recruiter(recruiter: Optional[str]) -> Optional[str]:
    value = (recruiter or "").strip().lower()
    return value[:320] or None


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_CacheKey = Tuple[Optional[str], Optional[str], str, str]


def _cache_key(
    scope_team_id: Optional[str],
    recruiter: Optional[str],
    date_range: Optional[Tuple[datetime.date, datetime.date]],
) -> _CacheKey:
    start, end = (date_range[0].isoformat(), date_range[1].isoformat()) if date_range else ("", "")
    # "No filter" stays None: a placeholder string would be a key a request
    # can also produce (?recruiter=*), sharing the unfiltered view's entry.
    return (scope_team_id, recruiter, start, end)


def _cache_get(key: _CacheKey) -> Optional[Dict[str, Any]]:
    with _cache_lock:
        hit = _cache.get(key)
        if hit is None:
            return None
        expires_at, payload = hit
        if time.monotonic() >= expires_at:
            _cache.pop(key, None)
            return None
        return payload


def _cache_put(
    key: _CacheKey, payload: Dict[str, Any], ttl_seconds: float = _CACHE_TTL_SECONDS
) -> None:
    with _cache_lock:
        now = time.monotonic()
        for stale in [k for k, (exp, _) in _cache.items() if now >= exp]:
            del _cache[stale]
        # Arbitrary custom ranges would otherwise grow this without bound;
        # drop whatever expires soonest.
        while len(_cache) >= _CACHE_MAX_ENTRIES:
            del _cache[min(_cache, key=lambda k: _cache[k][0])]
        _cache[key] = (now + ttl_seconds, payload)


def clear_recruiter_analytics_cache() -> None:
    with _cache_lock:
        _cache.clear()


# ---------------------------------------------------------------------------
# Small value helpers
# ---------------------------------------------------------------------------

def _as_utc(value: Any) -> Optional[datetime.datetime]:
    if not isinstance(value, datetime.datetime):
        return None
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def _iso_et(value: Optional[datetime.datetime]) -> Optional[str]:
    """Offset-aware ISO string in Eastern time (…-04:00), like the Launch Report."""
    return value.astimezone(REPORT_TIMEZONE).isoformat() if value else None


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_minutes(value: Any) -> Optional[float]:
    try:
        minutes = float(value)
    except (TypeError, ValueError):
        return None
    # NaN fails the comparison; a negative span means two clocks disagreed.
    return minutes if minutes >= 0 else None


def _ms_to_minutes(ms: float) -> float:
    # The same rounding fetch_step_metrics applies to a job's own total.
    return round(ms / 60000.0, 1)


def _actor_ms(by_user: Any) -> Dict[str, int]:
    """fetch_step_metrics' by_user, keyed like recruiter_emails (stripped,
    lowercased) so it can be matched against the rows, keeping only people
    with time. fetch_step_metrics already normalises and sums colliding keys
    (and the step-time endpoint stores normalize_actor_email's form), so this
    is a defensive re-normalisation, not the place spellings get merged."""
    out: Dict[str, int] = {}
    for email, ms in (by_user or {}).items():
        key = str(email or "").strip().lower()
        value = _to_int(ms)
        if key and value > 0:
            out[key] = out.get(key, 0) + value
    return out


def _unique(values: Iterable[str]) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _family_key(parent_job_id: Any, jobdiva_id: Any, job_id: Any) -> str:
    """The JobDiva job a version belongs to.

    v2+ clones copy the job and re-resolve to the SAME JobDiva job (the "-vN"
    suffix is stripped before every JobDiva call), so the JobDiva-derived
    counters of v1 and v2 describe the same submittals. They are MAX-ed within
    a family instead of summed. The clone stores the root ref in parent_job_id;
    the original has none, so its own ref (or job_id) is the root.
    """
    parent = str(parent_job_id or "").strip()
    if parent:
        return parent.lower()
    ref = str(jobdiva_id or "").strip() or str(job_id or "").strip()
    return _VERSION_SUFFIX_RE.sub("", ref).lower()


def _admin_env_emails() -> Set[str]:
    return {e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()}


# ---------------------------------------------------------------------------
# Database reads
# ---------------------------------------------------------------------------

def _set_statement_timeout(cur, ms: int) -> None:
    cur.execute(f"SET LOCAL statement_timeout = '{int(ms)}ms'")


def _fetch_job_rows(
    conn,
    scope: Optional[Dict[str, Any]],
    date_range: Optional[Tuple[datetime.date, datetime.date]],
) -> List[Tuple[Any, ...]]:
    """monitored_jobs rows in scope, optionally only those launched in range.

    A job's launch is the Launch Report's: its FIRST SUCCESSFUL launch, the
    earliest engage_interview_audit row that got an interview id
    (routers/launch_report._fetch_jobs_launched_on; the `launches` CTE below
    restates that one and a real-Postgres test pins the two together). Not
    monitored_jobs.pair_launched_at: /candidates/save stamps that before
    /engage/launch runs and whatever pair-bot answers, so a job whose every
    attempt failed read as launched here while the Launch Report never
    listed it, and a failed first attempt dated the job by that attempt.
    """
    mj_cond, mj_params = _mj_filter(scope, "mj")
    # In placeholder order: the CTE's AT TIME ZONE (the audit column is a
    # naive reading in the DB session zone), its scope, the job read's scope.
    params: List[Any] = [REPORT_DB_TIMEZONE, *mj_params, *mj_params]
    launched_expr = "l.first_launch_at"
    range_cond = ""
    if date_range:
        lo, hi = _range_bounds_utc(*date_range)
        # timestamptz on both sides, so the comparison does not depend on the
        # DB session's TimeZone setting.
        range_cond = f" AND {launched_expr} >= %s AND {launched_expr} < %s"
        params = [*params, lo, hi]

    with conn.cursor() as cur:
        _set_statement_timeout(cur, _STATEMENT_TIMEOUT_MS)
        # NULL for an attribution column the startup ALTER has not added yet.
        optional = optional_monitored_jobs_columns(cur, "mj.")
        cur.execute(
            f"""
            WITH launches AS (
                SELECT
                    mj.job_id AS job_id,
                    MIN(a.created_at) AT TIME ZONE %s AS first_launch_at
                FROM monitored_jobs mj
                JOIN engage_interview_audit a
                  -- A job with no JobDiva ref stores '' (not NULL); without
                  -- this guard an audit row with '' matches every such job.
                  ON NULLIF(a.jobdiva_id, '') IS NOT NULL
                 AND (a.jobdiva_id = NULLIF(mj.jobdiva_id, '') OR a.jobdiva_id = mj.job_id::text)
                WHERE {mj_cond}
                  -- Successful launches only: a failed attempt writes an audit
                  -- row with an empty or NULL interview_id.
                  AND COALESCE(NULLIF(a.interview_id, ''), '') <> ''
                  AND COALESCE(NULLIF(a.candidate_id, ''), '') <> ''
                GROUP BY mj.job_id
            )
            SELECT
                mj.job_id::text,
                mj.jobdiva_id::text,
                COALESCE(NULLIF(TRIM(mj.enhanced_title), ''), NULLIF(TRIM(mj.title), ''), 'Untitled'),
                COALESCE(NULLIF(TRIM(mj.customer_name), ''), ''),
                mj.recruiter_emails,
                COALESCE(mj.is_archived, FALSE),
                {_ts_utc("mj.created_at")},
                {launched_expr},
                {_int("mj.candidates_sourced")},
                {_int("mj.candidates_launched")},
                {_int("mj.pair_external_subs")},
                {_int("mj.jobdiva_total_subs")},
                NULLIF(TRIM(mj.time_to_first_pass::text), '')::double precision,
                {optional["pair_posted_by"]},
                {optional["pair_launched_by"]},
                mj.parent_job_id::text,
                mj.version
            FROM monitored_jobs mj
            LEFT JOIN launches l ON l.job_id = mj.job_id
            WHERE {mj_cond}{range_cond}
            ORDER BY {launched_expr} DESC NULLS LAST, mj.job_id::text
            """,
            params,
        )
        return list(cur.fetchall() or [])


def _load_directory(conn) -> Dict[str, Any]:
    """Who has a PAIR login, and which team each email is on.

    There is no users table. A "PAIR account" is anyone an admin put on a team
    or in user_roles, plus ADMIN_EMAILS (added by the caller). JobDiva fills
    recruiter_emails from recruiter / owner / account-manager / contact fields,
    so some assigned emails will never sign in to PAIR; the UI can hide them.
    """
    team_by_email: Dict[str, Dict[str, Any]] = {}
    accounts: Set[str] = set()
    with conn.cursor() as cur:
        _set_statement_timeout(cur, _STATEMENT_TIMEOUT_MS)
        cur.execute(
            """
            SELECT LOWER(TRIM(tm.email)), t.name, LOWER(TRIM(COALESCE(tm.member_role, 'member')))
            FROM team_members tm
            LEFT JOIN teams t ON t.id = tm.team_id
            """
        )
        for email, team_name, member_role in cur.fetchall() or []:
            if not email:
                continue
            accounts.add(email)
            team_by_email[email] = {"team_name": team_name, "is_team_lead": member_role == "lead"}
        cur.execute("SELECT LOWER(TRIM(email)) FROM user_roles")
        accounts.update(r[0] for r in cur.fetchall() or [] if r[0])
    return {"team_by_email": team_by_email, "accounts": accounts}


def _section(conn, label: str, compute, default) -> Tuple[Any, bool]:
    """Run one optional read → (value, ok); on failure roll back to a default.

    A failed statement aborts the whole Postgres transaction, so the rollback
    is what lets the next section run at all (same pattern as Admin
    Analytics). The DB error goes to the log, not to the browser.
    """
    try:
        return compute(), True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"RECRUITER-ANALYTICS: {label} unavailable: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return default, False


# ---------------------------------------------------------------------------
# Assembly (pure; unit-tested without a database)
# ---------------------------------------------------------------------------

def _rows_to_jobs(
    rows: Iterable[Tuple[Any, ...]],
    scope_emails: Optional[Set[str]],
    recruiter: Optional[str],
) -> List[Dict[str, Any]]:
    """Typed job dicts. recruiter_emails is narrowed to the team scope, so a job
    shared with someone outside the team never shows that person in the team's
    view (the same guard as Admin Analytics' top recruiters)."""
    jobs: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for row in rows:
        rec = dict(zip(_JOB_COLUMNS, row))
        job_id = str(rec["job_id"] or "").strip()
        if not job_id or job_id in seen:
            continue
        seen.add(job_id)
        all_emails = _unique(_parse_recruiter_emails(rec["recruiter_emails"]))
        assigned = [e for e in all_emails if scope_emails is None or e in scope_emails]
        # The team scope's job list and this read are separate statements; a
        # job reassigned in between must not sit in the team's totals uncredited.
        if scope_emails is not None and not assigned:
            continue
        if recruiter is not None and recruiter not in assigned:
            continue
        jobdiva_id = str(rec["jobdiva_id"] or "").strip() or None
        jobs.append({
            "job_id": job_id,
            "jobdiva_id": jobdiva_id,
            "title": rec["title"] or "Untitled",
            "customer_name": rec["customer_name"] or "",
            "recruiter_emails": assigned,
            "unassigned": not all_emails,
            "is_archived": bool(rec["is_archived"]),
            "created_at": _as_utc(rec["created_at"]),
            "launched_at": _as_utc(rec["launched_at"]),
            "candidates_sourced": _to_int(rec["candidates_sourced"]),
            "candidates_launched": _to_int(rec["candidates_launched"]),
            "jobdiva_confirmed_subs": _to_int(rec["pair_external_subs"]),
            "jobdiva_total_subs": _to_int(rec["jobdiva_total_subs"]),
            "time_to_first_pass_minutes": _to_minutes(rec["time_to_first_pass"]),
            **attribution_fields(rec["pair_posted_by"], rec["pair_launched_by"]),
            "version": _to_int(rec["version"], 1) or 1,
            "family": _family_key(rec["parent_job_id"], jobdiva_id, job_id),
        })
    return jobs


def _pass_rate(passed: int, failed: int) -> Optional[float]:
    decided = passed + failed
    return round(passed / decided, 4) if decided else None


def _aggregate(jobs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Roll a DISTINCT list of jobs up into one block.

    Used for each recruiter's own jobs and, separately, for the whole
    population — so Totals are never derived from recruiter rows.
    """
    def total(key: str) -> int:
        return sum(int(j.get(key) or 0) for j in jobs)

    # JobDiva-derived counters: MAX within a version family, then summed.
    confirmed_by_family: Dict[str, int] = {}
    jobdiva_total_by_family: Dict[str, int] = {}
    for j in jobs:
        fam = j["family"]
        confirmed_by_family[fam] = max(confirmed_by_family.get(fam, 0), j["jobdiva_confirmed_subs"])
        jobdiva_total_by_family[fam] = max(jobdiva_total_by_family.get(fam, 0), j["jobdiva_total_subs"])

    first_external = [j["first_external_submit_at"] for j in jobs if j.get("first_external_submit_at")]
    ttfp = [j["time_to_first_pass_minutes"] for j in jobs if j.get("time_to_first_pass_minutes") is not None]
    # Jobs with no Step 5 → Launch (untracked, unlaunched, or launched before
    # Step 5 was first timed) are left out of the mean, never counted as 0.
    to_launch = [j["step5_to_launch_minutes"] for j in jobs if j.get("step5_to_launch_minutes") is not None]
    passed, failed = total("passed"), total("failed")
    archived = sum(1 for j in jobs if j["is_archived"])

    return {
        "jobs": {
            "assigned": len(jobs),
            "active": len(jobs) - archived,
            "archived": archived,
            "launched": sum(1 for j in jobs if j["launched_at"]),
        },
        "candidates": {
            "sourced": total("candidates_sourced"),
            "launched": total("candidates_launched"),
        },
        "outcomes": {
            "passed": passed,
            "failed": failed,
            "in_progress": total("in_progress"),
        },
        "feedback": {
            "total": total("feedback_total"),
            "submits": total("pair_submits"),
            "internal": total("pair_internal_submits"),
            "external": total("pair_external_submits"),
            "rejects": total("rejects"),
            "unreachable": total("unreachable"),
            "awaiting": total("awaiting_feedback"),
        },
        "submittals": {
            "pair_internal": total("pair_internal_submits"),
            "pair_external": total("pair_external_submits"),
            "jobdiva_confirmed": sum(confirmed_by_family.values()),
            "jobdiva_total": sum(jobdiva_total_by_family.values()),
        },
        "first_external_submit_at": _iso_et(min(first_external)) if first_external else None,
        "avg_time_to_first_pass_minutes": round(sum(ttfp) / len(ttfp), 1) if ttfp else None,
        "step5_to_launch_minutes_avg": round(sum(to_launch) / len(to_launch), 1) if to_launch else None,
        "pass_rate": _pass_rate(passed, failed),
    }


def _step5_active(ms_per_job: Iterable[Optional[int]]) -> Dict[str, Any]:
    """Step 5 Active Time over one set of per-job values, in ms.

    Only jobs with time > 0 count as timed: an untracked job (None, worked
    before the tracking existed) or an entry ping with no active time must not
    drag the per-job average towards zero. Nothing timed → None, not 0, so the
    page shows a dash rather than a real-looking "0m".
    """
    timed = [int(ms) for ms in ms_per_job if ms and ms > 0]
    total_ms = sum(timed)
    return {
        "step5_active_minutes_total": _ms_to_minutes(total_ms) if timed else None,
        "step5_active_minutes_avg": _ms_to_minutes(total_ms / len(timed)) if timed else None,
        "step5_jobs_timed": len(timed),
    }


def _serialize_job(job: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "job_id": job["job_id"],
        "jobdiva_id": job["jobdiva_id"],
        "title": job["title"],
        "customer_name": job["customer_name"],
        "recruiter_emails": job["recruiter_emails"],
        "is_archived": job["is_archived"],
        "version": job["version"],
        "created_at": _iso_et(job["created_at"]),
        "launched_at": _iso_et(job["launched_at"]),
        "posted_by": job["posted_by"],
        "launched_by": job["launched_by"],
        "candidates_sourced": job["candidates_sourced"],
        "candidates_launched": job["candidates_launched"],
        "passed": job["passed"],
        "failed": job["failed"],
        "in_progress": job["in_progress"],
        "pass_rate": _pass_rate(job["passed"], job["failed"]),
        "feedback_total": job["feedback_total"],
        "pair_submits": job["pair_submits"],
        "pair_internal_submits": job["pair_internal_submits"],
        "pair_external_submits": job["pair_external_submits"],
        "rejects": job["rejects"],
        "unreachable": job["unreachable"],
        "awaiting_feedback": job["awaiting_feedback"],
        "first_feedback_at": _iso_et(job["first_feedback_at"]),
        "first_external_submit_at": _iso_et(job["first_external_submit_at"]),
        "jobdiva_confirmed_subs": job["jobdiva_confirmed_subs"],
        "jobdiva_total_subs": job["jobdiva_total_subs"],
        "time_to_first_pass_minutes": job["time_to_first_pass_minutes"],
        # The job's own numbers: everyone's active time on it (admins' and
        # other teams' included) and its first Step 5 entry → first launch.
        "step5_active_minutes": job["step5_active_minutes"],
        "step5_first_entered_at": _iso_et(job["step5_first_entered_at"]),
        "step5_first_launch_at": _iso_et(job["step5_first_launch_at"]),
        "step5_to_launch_minutes": job["step5_to_launch_minutes"],
        # Each assigned recruiter's own share, for the drill-down. Limited to
        # recruiter_emails, which is already narrowed to the team scope, so a
        # team's view never names an outsider or an admin who worked the job.
        "step5_active_minutes_by_recruiter": {
            email: _ms_to_minutes(job["step5_by_user"][email])
            for email in job["recruiter_emails"]
            if email in job["step5_by_user"]
        },
    }


def _build_payload(
    jobs: List[Dict[str, Any]],
    metrics_by_job: Dict[str, Dict[str, Any]],
    directory: Dict[str, Any],
    *,
    scope: Optional[Dict[str, Any]],
    team_scope_out: Optional[Dict[str, Any]],
    date_range: Optional[Tuple[datetime.date, datetime.date]],
    recruiter: Optional[str],
    warnings: List[str],
    metrics_available: bool = True,
    directory_available: bool = True,
    step_metrics_by_job: Optional[Dict[str, Dict[str, Any]]] = None,
    step_time_available: bool = True,
    now: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    for job in jobs:
        metrics = metrics_by_job.get(job["job_id"]) or empty_metrics()
        job.update({k: metrics.get(k, v) for k, v in empty_metrics().items()})
        step = (step_metrics_by_job or {}).get(job["job_id"]) or empty_step_metrics()
        job.update({
            # None = nothing tracked (jobs worked before 2026-09-23), not 0.
            "step5_active_ms": step.get("active_ms"),
            "step5_active_minutes": step.get("active_minutes"),
            "step5_first_entered_at": _as_utc(step.get("first_entered_at")),
            # The launch end the → Launch number was computed from. It follows
            # the same rule as launched_at but is read separately, so the
            # drill-down's hover shows this one to always agree with the number.
            "step5_first_launch_at": _as_utc(step.get("first_launch_at")),
            "step5_to_launch_minutes": step.get("to_launch_minutes"),
            "step5_by_user": _actor_ms(step.get("by_user")),
        })

    team_by_email: Dict[str, Dict[str, Any]] = directory.get("team_by_email") or {}
    accounts: Set[str] = set(directory.get("accounts") or ()) | _admin_env_emails()
    # Anyone PAIR recorded posting or launching a job signed in to do it.
    accounts.update(j["posted_by"] for j in jobs if j["posted_by"])
    accounts.update(j["launched_by"] for j in jobs if j["launched_by"])

    jobs_by_recruiter: Dict[str, List[Dict[str, Any]]] = {}
    for job in jobs:
        for email in job["recruiter_emails"]:
            # With a recruiter filter, co-assigned recruiters must not get a
            # row holding only the shared jobs.
            if recruiter is not None and email != recruiter:
                continue
            jobs_by_recruiter.setdefault(email, []).append(job)

    # Step 5 Active Time is credited to WHO SPENT IT, not by assignment: each
    # row gets that person's own time on every job in the population, whether
    # or not they are assigned to it (a recruiter helping on a colleague's job
    # did that work). Rows still come only from assignment, so time only lands
    # on a row that already exists:
    #   * an admin, or anyone else who opens Step 5 without being assigned to
    #     any job in view, gets no row; their time still counts in the job's
    #     own total and in Totals;
    #   * in a team's view the rows are the team's emails, so an outsider's
    #     time on a shared job never becomes a row either;
    #   * with a recruiter filter the population is that recruiter's assigned
    #     jobs, so time they spent on other people's jobs is outside the view.
    # Step 5 → Launch is a property of the job and stays with _aggregate's
    # assignment credit.
    actor_ms: Dict[str, List[int]] = {email: [] for email in jobs_by_recruiter}
    for job in jobs:
        for email, ms in job["step5_by_user"].items():
            if email in actor_ms:
                actor_ms[email].append(ms)

    recruiters: List[Dict[str, Any]] = []
    for email, their_jobs in jobs_by_recruiter.items():
        team = team_by_email.get(email) or {}
        recruiters.append({
            "email": email,
            "team_name": (scope or {}).get("team_name") or team.get("team_name"),
            "is_team_lead": bool(team.get("is_team_lead")),
            # None = unknown: without the directory most real recruiters would
            # read as "no PAIR login" and the UI's hide toggle would drop them.
            "has_pair_account": True if email in accounts else (False if directory_available else None),
            **_aggregate(their_jobs),
            **_step5_active(actor_ms[email]),
            "job_ids": [j["job_id"] for j in their_jobs],
        })
    recruiters.sort(key=lambda r: (-r["jobs"]["launched"], r["email"]))

    unassigned = sum(1 for j in jobs if j["unassigned"])
    totals = _aggregate(jobs)
    # Everyone's time on the DISTINCT jobs, admins and people without a row
    # included — so it is not the sum of the recruiter rows either.
    totals.update(_step5_active(j["step5_active_ms"] for j in jobs))
    totals["jobs"]["unassigned"] = unassigned
    totals["recruiters"] = len(recruiters)

    ordered_jobs = sorted(
        jobs,
        key=lambda j: (j["launched_at"] is None, -(j["launched_at"].timestamp() if j["launched_at"] else 0), j["title"].lower()),
    )
    generated = now or datetime.datetime.now(UTC)
    return {
        "range": {
            "start_date": date_range[0].isoformat() if date_range else None,
            "end_date": date_range[1].isoformat() if date_range else None,
            "timezone": str(REPORT_TIMEZONE),
            "mode": "launched_in_range" if date_range else "all_time",
            "description": _RANGE_DESCRIPTION if date_range else _ALL_TIME_DESCRIPTION,
        },
        "generated_at": _iso_et(generated),
        "team_scope": team_scope_out,
        "recruiter": recruiter,
        "attribution": "assignment",
        "totals": totals,
        "unassigned_jobs": unassigned,
        "recruiters": recruiters,
        "jobs": [_serialize_job(j) for j in ordered_jobs],
        "metrics_available": metrics_available,
        "step_time_available": step_time_available,
        "warnings": warnings,
    }


def _compute_recruiter_analytics_sync(
    scope_team_id: Optional[str],
    recruiter: Optional[str],
    date_range: Optional[Tuple[datetime.date, datetime.date]],
) -> Dict[str, Any]:
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # Covers the team-scope scan below as well as the job read.
            _set_statement_timeout(cur, _STATEMENT_TIMEOUT_MS)
        scope: Optional[Dict[str, Any]] = None
        team_scope_out: Optional[Dict[str, Any]] = None
        if scope_team_id:
            # LookupError (unknown team) propagates → the endpoint answers 404.
            scope = _load_team_scope(conn, scope_team_id)
            team_scope_out = {
                "team_id": scope["team_id"],
                "team_name": scope["team_name"],
                "member_count": len(scope["emails"]),
            }
        scope_emails = set(scope["emails"]) if scope else None

        # The job list is the report; without it there is nothing to show, so
        # a failure here is a 500 rather than a quietly empty dashboard.
        jobs = _rows_to_jobs(_fetch_job_rows(conn, scope, date_range), scope_emails, recruiter)

        def candidate_metrics() -> Dict[str, Dict[str, Any]]:
            with conn.cursor() as cur:
                _set_statement_timeout(cur, _METRICS_STATEMENT_TIMEOUT_MS)
            return fetch_job_candidate_metrics(conn, [(j["job_id"], j["jobdiva_id"]) for j in jobs])

        warnings: List[str] = []
        metrics_by_job, metrics_ok = _section(conn, "candidate metrics", candidate_metrics, {})
        if not metrics_ok:
            # Zeros that look real would be worse than a visible gap.
            warnings.append(
                "Candidate outcome counts (pass, feedback, PAIR submittals) are temporarily "
                "unavailable; only the job counters are current."
            )

        def step_time() -> Dict[str, Dict[str, Any]]:
            # One statement for the whole population, like the metrics read.
            # It reads job_step_time (one row per job and person) and the
            # audit's first successful launch on the indexed jobdiva_id — the
            # same lookup _fetch_job_rows makes under this timeout.
            with conn.cursor() as cur:
                _set_statement_timeout(cur, _STATEMENT_TIMEOUT_MS)
            return fetch_step_metrics(conn, [(j["job_id"], j["jobdiva_id"]) for j in jobs])

        step_by_job, step_ok = _section(conn, "step 5 time", step_time, {})
        if not step_ok:
            # Its fallback is None everywhere, which the page already shows as
            # a dash; the warning says the dash means "unavailable", not
            # "never tracked". It also puts the payload on the short TTL.
            warnings.append(
                "Step 5 time (Step 5 Active Time and Step 5 → Launch) is temporarily unavailable."
            )
        directory, directory_ok = _section(conn, "team/login directory", lambda: _load_directory(conn), {})
        if not directory_ok:
            warnings.append(
                "Team and login lookups are unavailable; team names and the PAIR-login flag may be incomplete."
            )
        return _build_payload(
            jobs,
            metrics_by_job,
            directory,
            scope=scope,
            team_scope_out=team_scope_out,
            date_range=date_range,
            recruiter=recruiter,
            warnings=warnings,
            metrics_available=metrics_ok,
            directory_available=directory_ok,
            step_metrics_by_job=step_by_job,
            step_time_available=step_ok,
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.get("/admin/recruiter-analytics")
async def get_recruiter_analytics(
    start_date: Optional[str] = Query(default=None, description="YYYY-MM-DD, Eastern; use with end_date"),
    end_date: Optional[str] = Query(default=None, description="YYYY-MM-DD, Eastern; use with start_date"),
    team_id: Optional[str] = Query(default=None, description="Admins only; team leads are pinned to their team"),
    recruiter: Optional[str] = Query(default=None, description="Only this recruiter's row and jobs"),
    refresh: bool = Query(default=False, description="Bypass the per-worker cache (60s; 20s for a degraded result)"),
    user: UserIdentity = Depends(get_current_user),
    response: Response = Response(),
):
    """Per-recruiter jobs, candidates, pass, feedback, submittal and Step 5 time numbers.

    - Admins: everyone by default; ?team_id=... scopes to one team.
    - Team leads: always their own team (team_id is ignored); a recruiter
      filter outside the team is 403.
    - Recruiters: 403.

    With start_date/end_date, the population is the jobs whose first
    successful PAIR launch falls in that Eastern-time range; without, every
    job in scope.
    """
    if response is not None:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    scope_team_id = _resolve_scope_team_id(user, team_id)
    date_range = _parse_range(start_date, end_date)
    recruiter_email = _normalize_recruiter(recruiter)

    if recruiter_email and not user.is_admin:
        allowed = {e.strip().lower() for e in await asyncio.to_thread(get_user_scope_emails, user) if e}
        if recruiter_email not in allowed:
            raise HTTPException(status_code=403, detail="Access denied. That recruiter is not on your team.")

    key = _cache_key(scope_team_id, recruiter_email, date_range)
    if not refresh:
        cached = _cache_get(key)
        if cached is not None:
            return {"status": "success", "data": {**cached, "cached": True}}

    try:
        payload = await asyncio.to_thread(
            _compute_recruiter_analytics_sync, scope_team_id, recruiter_email, date_range
        )
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"RECRUITER-ANALYTICS: failed ({scope_team_id or 'all'}, {date_range}): {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to build recruiter analytics.")

    # A degraded result gets the short TTL (see _DEGRADED_CACHE_TTL_SECONDS):
    # long enough that repeat opens don't re-run a statement that just timed
    # out, short enough that its fallback isn't served for a full minute.
    _cache_put(key, payload, _DEGRADED_CACHE_TTL_SECONDS if payload["warnings"] else _CACHE_TTL_SECONDS)
    return {"status": "success", "data": {**payload, "cached": False}}
