"""Local mirror of the JobDiva BI feeds behind the PAIR Dashboard.

The dashboard (services/pair_dashboard.py) needs facts PAIR never records
itself:

  * every Pyramid requirement, not only the ones added to PAIR: the
    denominator of "PAIR Volume", the vertical (JobDiva division) filter, and
    the per-recruiter "Reqs Assigned" productivity number;
  * what happened to candidates in JobDiva: external submittals, client
    interviews and starts, for PAIR and non-PAIR candidates alike;
  * who is tagged on each req, and each JobDiva user's email, so a PAIR Team
    (a Recruiting Manager plus their recruiters, managed on the Teams page) can
    be matched to JobDiva's records.

JobDiva's BI API answers all of that, but slowly (a 7-day activity window
takes ~50 s), so no request may ask it live. This module copies the feeds into
local tables on a schedule and the dashboard only reads those.

Feeds, probed read-only on 2026-09-24. BI timestamps are naive US Eastern wall
clock, and fromDate/toDate are read the same way:

  jobs        IssuedJobsList(fromDate, toDate)   filters on ISSUEDATE; backfill.
              NewUpdatedJobRecords(from, to)     filters on DATEUPDATED; incremental.
                                                 Carries PRIORITY and DATESTATUSUPDATED,
                                                 which IssuedJobsList lacks.
              OpenJobsList()                     every open req, whatever its issue
                                                 date (~5.9k rows, ~75 s); daily, so
                                                 long-running reqs are never missing.
              JobsDetail(jobIds[])               per job; fills those two in for
                                                 backfilled jobs, and covers PAIR
                                                 jobs and jobs activity refers to
                                                 that the other feeds have not seen.
  activities  SubmittalInterviewHireActivitiesList(from, to)
                                                 filters on ACTIVITYDATE (the
                                                 record's last update) and returns
                                                 each submittal in its CURRENT state
                                                 (interview / hire flags and dates),
                                                 so one feed serves both the
                                                 backfill and the incremental sweep.
  job users   JobsUsersDetail(jobIds[])          every user tagged on the req.
  users       NewUpdatedUserRecords(from, to)    the user directory (USERID -> EMAIL).

Candidate names and emails in the activity feed are dropped here. Everything
keys on JobDiva's CANDIDATEID, which is what PAIR's own rows store
(`sourced_candidates.candidate_id` for JobDiva-sourced rows,
`data.jobdiva_candidate_id` for the rest).

Scheduling: every uvicorn worker schedules the cycle and a Postgres advisory
lock lets exactly one run it (see main.poll_all_jobs for why). A cycle has a
time budget. The backfill walks NEWEST-FIRST, so the last few weeks reach the
dashboard minutes after the first deploy while older history fills in over the
next cycles. Progress is stored per feed, so a restart resumes instead of
starting over. A failed JobDiva call ends that step without touching what is
already stored, and the next cycle retries from the same cursor.
"""

import asyncio
import datetime
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

JOBDIVA_TIMEZONE = ZoneInfo("America/New_York")

SYNC_ENABLED = os.getenv("JOBDIVA_BI_SYNC_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")
# Nine months reaches back past January 1st, so YTD is complete once the
# backfill finishes. PAIR itself started in 2026.
BACKFILL_DAYS = int(os.getenv("JOBDIVA_BI_BACKFILL_DAYS", "270"))
SYNC_INTERVAL_MINUTES = int(os.getenv("JOBDIVA_BI_SYNC_INTERVAL_MINUTES", "30"))
# Upper bound on one cycle's wall time; the backfill resumes next cycle.
SYNC_BUDGET_SECONDS = int(os.getenv("JOBDIVA_BI_SYNC_BUDGET_SECONDS", "600"))
# Re-read this much before the stored cursor: a record updated while the last
# sweep ran must not fall between two windows.
CURSOR_OVERLAP = datetime.timedelta(minutes=30)
BACKFILL_WINDOW = datetime.timedelta(days=7)
INCREMENTAL_WINDOW = datetime.timedelta(days=1)
USER_DIRECTORY_REFRESH = datetime.timedelta(hours=24)
OPEN_JOBS_REFRESH = datetime.timedelta(hours=24)
# Open reqs are re-checked for who is tagged on them at least this often.
JOB_USERS_REFRESH = datetime.timedelta(days=7)
JOB_BATCH_SIZE = 50
# The sync shares the JobDiva account with live Step 5 searches (whose JobAgent
# call returns nothing on a 429), so the per-job enrichment is paced gently:
# at most this many 50-job batches per endpoint per cycle, a pause between calls.
JOB_BATCHES_PER_CYCLE = int(os.getenv("JOBDIVA_BI_JOB_BATCHES_PER_CYCLE", "20"))
REQUEST_TIMEOUT_SECONDS = 180.0
PAUSE_BETWEEN_CALLS_SECONDS = float(os.getenv("JOBDIVA_BI_PAUSE_SECONDS", "1.0"))
# A per-job endpoint that answers an all-empty batch is retried one job at a
# time; this many empty single-job answers in a row mean the endpoint itself is
# failing, and the step stops instead of burning the account's quota.
EMPTY_SINGLE_CALLS_BEFORE_GIVING_UP = 3

# Distinct from main.POLL_LOCK_KEY (728193).
SYNC_LOCK_KEY = 728194

FEED_JOBS = "jobs"
FEED_ACTIVITIES = "activities"
FEED_USERS = "users"
FEED_OPEN_JOBS = "open_jobs"
FEED_JOB_DETAILS = "job_details"
FEED_JOB_USERS = "job_users"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_STATEMENTS: Tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS jobdiva_jobs (
        job_id TEXT PRIMARY KEY,
        jobdiva_ref TEXT,
        title TEXT,
        company_id TEXT,
        company_name TEXT,
        division_id TEXT,
        division_name TEXT,
        job_status TEXT,
        status_updated_at TIMESTAMP,
        priority TEXT,
        position_type TEXT,
        openings INTEGER,
        fills INTEGER,
        issue_date TIMESTAMP,
        primary_recruiter_id TEXT,
        primary_sales_id TEXT,
        primary_owner_id TEXT,
        jobdiva_updated_at TIMESTAMP,
        detail_synced_at TIMESTAMPTZ,
        users_synced_at TIMESTAMPTZ,
        synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_jobdiva_jobs_issue_date ON jobdiva_jobs (issue_date)",
    "CREATE INDEX IF NOT EXISTS idx_jobdiva_jobs_ref ON jobdiva_jobs (LOWER(jobdiva_ref))",
    """
    CREATE TABLE IF NOT EXISTS jobdiva_activities (
        activity_id TEXT PRIMARY KEY,
        job_id TEXT,
        jobdiva_ref TEXT,
        candidate_id TEXT,
        user_id TEXT,
        is_submittal BOOLEAN NOT NULL DEFAULT FALSE,
        is_internal BOOLEAN NOT NULL DEFAULT FALSE,
        submittal_date TIMESTAMP,
        interview_flag BOOLEAN NOT NULL DEFAULT FALSE,
        interview_date TIMESTAMP,
        interview_type TEXT,
        hire_flag BOOLEAN NOT NULL DEFAULT FALSE,
        start_date TIMESTAMP,
        start_status TEXT,
        external_reject_date TIMESTAMP,
        internal_reject_date TIMESTAMP,
        termination_date TIMESTAMP,
        activity_date TIMESTAMP,
        synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_jobdiva_activities_job ON jobdiva_activities (job_id)",
    "CREATE INDEX IF NOT EXISTS idx_jobdiva_activities_candidate ON jobdiva_activities (candidate_id)",
    "CREATE INDEX IF NOT EXISTS idx_jobdiva_activities_submittal ON jobdiva_activities (submittal_date)",
    "CREATE INDEX IF NOT EXISTS idx_jobdiva_activities_interview ON jobdiva_activities (interview_date)",
    "CREATE INDEX IF NOT EXISTS idx_jobdiva_activities_start ON jobdiva_activities (start_date)",
    """
    CREATE TABLE IF NOT EXISTS jobdiva_job_users (
        job_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        roles TEXT,
        date_last_assigned TIMESTAMP,
        PRIMARY KEY (job_id, user_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_jobdiva_job_users_user ON jobdiva_job_users (user_id)",
    """
    CREATE TABLE IF NOT EXISTS jobdiva_users (
        user_id TEXT PRIMARY KEY,
        email TEXT,
        first_name TEXT,
        last_name TEXT,
        title TEXT,
        division TEXT,
        is_active BOOLEAN,
        is_recruiter BOOLEAN,
        is_recruiting_manager BOOLEAN,
        is_team_leader BOOLEAN,
        synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_jobdiva_users_email ON jobdiva_users (LOWER(email))",
    """
    CREATE TABLE IF NOT EXISTS jobdiva_bi_sync_state (
        feed TEXT PRIMARY KEY,
        cursor_at TIMESTAMP,
        backfill_until TIMESTAMP,
        backfill_target TIMESTAMP,
        backfill_done BOOLEAN NOT NULL DEFAULT FALSE,
        last_success_at TIMESTAMPTZ,
        last_error TEXT,
        last_error_at TIMESTAMPTZ,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
)


def ensure_schema(conn) -> None:
    """Idempotent DDL. Serialised across workers so eight simultaneous boots
    don't race on CREATE INDEX (an advisory xact lock, released at commit)."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtext('jobdiva_bi_schema'))")
        for statement in SCHEMA_STATEMENTS:
            cur.execute(statement)
    conn.commit()


async def init_jobdiva_bi_schema() -> None:
    from core.db import get_db_connection

    def _run():
        conn = get_db_connection()
        try:
            ensure_schema(conn)
        finally:
            conn.close()

    await asyncio.to_thread(_run)


# ---------------------------------------------------------------------------
# Record normalisation (pure; see tests/test_jobdiva_bi_sync.py)
# ---------------------------------------------------------------------------

_TIMESTAMP_FORMATS = ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y")


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _flag(value: Any) -> bool:
    return str(value if value is not None else "").strip().lower() in ("1", "true", "y", "yes")


def _int(value: Any) -> Optional[int]:
    text = _text(value)
    if text is None:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _timestamp(value: Any) -> Optional[datetime.datetime]:
    """JobDiva wall clock (naive, Eastern). ISO ("2026-09-17T19:30:48") is what
    the BI feeds return; the US formats are what the API accepts back."""
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        return value.replace(tzinfo=None)
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.datetime.fromisoformat(text[:19])
    except ValueError:
        pass
    for fmt in _TIMESTAMP_FORMATS:
        try:
            return datetime.datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _first(row: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip() != "":
            return value
    return None


JOB_COLUMNS: Tuple[str, ...] = (
    "job_id",
    "jobdiva_ref",
    "title",
    "company_id",
    "company_name",
    "division_id",
    "division_name",
    "job_status",
    "status_updated_at",
    "priority",
    "position_type",
    "openings",
    "fills",
    "issue_date",
    "primary_recruiter_id",
    "primary_sales_id",
    "primary_owner_id",
    "jobdiva_updated_at",
)


def normalize_job(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """One jobdiva_jobs row from any of the three job feeds, or None.

    JobsDetail names the job id ID (the list feeds say JOBID) and the issue
    date DATEISSUED. JobDiva pads some division names with trailing spaces
    ("Pharma                 "), so every text value is trimmed.
    """
    job_id = _text(_first(row, "JOBID", "ID"))
    if not job_id:
        return None
    status = _text(row.get("JOBSTATUS"))
    return {
        "job_id": job_id,
        "jobdiva_ref": _text(row.get("JOBDIVANO")),
        "title": _text(_first(row, "TITLE", "JOBTITLE")),
        "company_id": _text(row.get("COMPANYID")),
        "company_name": _text(row.get("COMPANYNAME")),
        "division_id": _text(row.get("DIVISIONID")),
        "division_name": _text(_first(row, "DIVISIONNAME", "DIVISION")),
        "job_status": status.upper() if status else None,
        "status_updated_at": _timestamp(row.get("DATESTATUSUPDATED")),
        "priority": _text(row.get("PRIORITY")),
        "position_type": _text(row.get("POSITIONTYPE")),
        "openings": _int(row.get("OPENINGS")),
        "fills": _int(row.get("FILLS")),
        "issue_date": _timestamp(_first(row, "ISSUEDATE", "DATEISSUED")),
        "primary_recruiter_id": _text(row.get("PRIMARYRECRUITERID")),
        "primary_sales_id": _text(row.get("PRIMARYSALESID")),
        "primary_owner_id": _text(row.get("PRIMARYOWNERID")),
        "jobdiva_updated_at": _timestamp(row.get("DATEUPDATED")),
    }


ACTIVITY_COLUMNS: Tuple[str, ...] = (
    "activity_id",
    "job_id",
    "jobdiva_ref",
    "candidate_id",
    "user_id",
    "is_submittal",
    "is_internal",
    "submittal_date",
    "interview_flag",
    "interview_date",
    "interview_type",
    "hire_flag",
    "start_date",
    "start_status",
    "external_reject_date",
    "internal_reject_date",
    "termination_date",
    "activity_date",
)


def normalize_activity(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """One jobdiva_activities row, or None. Candidate name / email are not kept.

    External vs internal: INTERNALSUBMITTALFLAG. Cross-checked against
    NewUpdatedSubmittalInterviewHireActivityRecords' EXTERNALSUBMITTALFLAG on
    1,703 shared records: external == (INTERNALSUBMITTALFLAG = 0) in every one.
    """
    activity_id = _text(row.get("ACTIVITYID"))
    if not activity_id:
        return None
    return {
        "activity_id": activity_id,
        "job_id": _text(row.get("JOBID")),
        "jobdiva_ref": _text(row.get("JOBREFERENCENUMBER")),
        "candidate_id": _text(row.get("CANDIDATEID")),
        "user_id": _text(row.get("USERID")),
        "is_submittal": _flag(row.get("SUBMITTALFLAG")),
        "is_internal": _flag(row.get("INTERNALSUBMITTALFLAG")),
        "submittal_date": _timestamp(row.get("SUBMITTALDATE")),
        "interview_flag": _flag(row.get("INTERVIEWFLAG")),
        "interview_date": _timestamp(_first(row, "INTERVIEWDATE", "INTERVIEWSCHEDULEDATE")),
        "interview_type": _text(row.get("INTERVIEW_TYPE")),
        "hire_flag": _flag(row.get("HIREFLAG")),
        "start_date": _timestamp(row.get("STARTDATE")),
        "start_status": _text(row.get("START_STATUS")),
        "external_reject_date": _timestamp(row.get("EXTERNALREJECTDATE")),
        "internal_reject_date": _timestamp(row.get("INTERNALREJECTDATE")),
        "termination_date": _timestamp(row.get("TERMINATIONDATE")),
        "activity_date": _timestamp(_first(row, "ACTIVITYDATE", "DATEUPDATED")),
    }


USER_COLUMNS: Tuple[str, ...] = (
    "user_id",
    "email",
    "first_name",
    "last_name",
    "title",
    "division",
    "is_active",
    "is_recruiter",
    "is_recruiting_manager",
    "is_team_leader",
)


def normalize_user(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    user_id = _text(row.get("USERID"))
    if not user_id:
        return None
    email = _text(row.get("EMAIL"))
    return {
        "user_id": user_id,
        "email": email.lower() if email else None,
        "first_name": _text(row.get("FIRSTNAME")),
        "last_name": _text(row.get("LASTNAME")),
        "title": _text(row.get("TITLE")),
        "division": _text(row.get("DIVISION")),
        "is_active": _flag(row.get("ACTIVEFLAG")),
        "is_recruiter": _flag(row.get("RECRUITERFLAG")),
        "is_recruiting_manager": _flag(row.get("RECRUITINGMANAGERFLAG")),
        "is_team_leader": _flag(row.get("TEAMLEADERFLAG")),
    }


# JobsUsersDetail role columns -> the label stored in jobdiva_job_users.roles.
_JOB_USER_ROLES: Tuple[Tuple[str, str], ...] = (
    ("PRIMARYRECRUITER", "primary_recruiter"),
    ("RECRUITER", "recruiter"),
    ("Secondary Recruiter", "secondary_recruiter"),
    ("Tertiary Recruiter", "tertiary_recruiter"),
    ("PRIMARYSALES", "primary_sales"),
    ("SALES", "sales"),
    ("Secondary Sales", "secondary_sales"),
    ("Tertiary Sales", "tertiary_sales"),
)


def normalize_job_user(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    job_id = _text(row.get("JOBID"))
    user_id = _text(row.get("USERID"))
    if not job_id or not user_id:
        return None
    roles = [label for column, label in _JOB_USER_ROLES if _flag(row.get(column))]
    return {
        "job_id": job_id,
        "user_id": user_id,
        "roles": ",".join(roles) or None,
        "date_last_assigned": _timestamp(row.get("DATELASTASSIGNED")),
    }


# ---------------------------------------------------------------------------
# Windows (pure)
# ---------------------------------------------------------------------------

def jobdiva_now() -> datetime.datetime:
    """Now on JobDiva's clock: naive US Eastern wall time."""
    return datetime.datetime.now(JOBDIVA_TIMEZONE).replace(tzinfo=None)


def format_jobdiva_datetime(value: datetime.datetime) -> str:
    return value.strftime("%m/%d/%Y %H:%M:%S")


def forward_windows(
    start: datetime.datetime, end: datetime.datetime, span: datetime.timedelta
) -> List[Tuple[datetime.datetime, datetime.datetime]]:
    """[start, end) in consecutive windows of at most `span`, oldest first."""
    windows = []
    cursor = start
    while cursor < end:
        upper = min(cursor + span, end)
        windows.append((cursor, upper))
        cursor = upper
    return windows


def backfill_windows(
    newest: datetime.datetime, oldest: datetime.datetime, span: datetime.timedelta
) -> List[Tuple[datetime.datetime, datetime.datetime]]:
    """[oldest, newest) in windows of at most `span`, NEWEST first."""
    windows = []
    cursor = newest
    while cursor > oldest:
        lower = max(cursor - span, oldest)
        windows.append((lower, cursor))
        cursor = lower
    return windows


def batched(values: Sequence[Any], size: int) -> List[List[Any]]:
    return [list(values[i:i + size]) for i in range(0, len(values), size)]


# ---------------------------------------------------------------------------
# JobDiva BI client
# ---------------------------------------------------------------------------

class JobDivaBIError(Exception):
    """A BI call failed; the step stops and keeps its cursor."""


class JobDivaRateLimited(JobDivaBIError):
    """HTTP 429: the whole cycle stops, the next one retries."""


BiFetch = Callable[[str, Dict[str, Any]], Awaitable[List[Dict[str, Any]]]]


def _make_http_fetch(client) -> BiFetch:
    async def fetch(path: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        from services.jobdiva import jobdiva_service

        for attempt in (1, 2):
            token = await jobdiva_service.authenticate(force_refresh=attempt == 2)
            if not token:
                raise JobDivaBIError("JobDiva authentication failed")
            response = await client.get(
                f"{jobdiva_service.api_url}{path}",
                params=params,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
            if response.status_code == 401 and attempt == 1:
                continue
            if response.status_code == 429:
                raise JobDivaRateLimited(f"{path}: HTTP 429")
            if response.status_code != 200:
                raise JobDivaBIError(f"{path}: HTTP {response.status_code} {response.text[:200]}")
            try:
                payload = response.json()
            except ValueError as exc:
                raise JobDivaBIError(f"{path}: response is not JSON") from exc
            if isinstance(payload, dict):
                if "data" not in payload:
                    # An error shaped as a 200 (e.g. {"message": ...}) is not "no rows".
                    raise JobDivaBIError(f"{path}: unexpected payload {str(payload)[:200]}")
                rows = payload["data"]
            else:
                rows = payload
            if rows is None:
                return []
            if isinstance(rows, dict):
                rows = [rows]  # a single record comes back unwrapped
            if not isinstance(rows, list):
                raise JobDivaBIError(f"{path}: unexpected payload {str(payload)[:200]}")
            return [r for r in rows if isinstance(r, dict)]
        raise JobDivaBIError(f"{path}: unauthorised after a token refresh")

    return fetch


async def _fetch_range(fetch: BiFetch, path: str, lower: datetime.datetime, upper: datetime.datetime):
    rows = await fetch(path, {
        "fromDate": format_jobdiva_datetime(lower),
        "toDate": format_jobdiva_datetime(upper),
    })
    await asyncio.sleep(PAUSE_BETWEEN_CALLS_SECONDS)
    return rows


async def _fetch_jobs_by_id(fetch: BiFetch, path: str, job_ids: Sequence[str]):
    numeric = [int(j) for j in job_ids if str(j).isdigit()]
    if not numeric:
        return []
    rows = await fetch(path, {"jobIds": numeric})
    await asyncio.sleep(PAUSE_BETWEEN_CALLS_SECONDS)
    return rows


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _upsert(conn, table: str, columns: Sequence[str], key: Sequence[str], rows: Iterable[Dict[str, Any]], coalesce: bool) -> int:
    """INSERT ... ON CONFLICT (key) DO UPDATE.

    coalesce=True keeps a stored value when the incoming one is NULL: the job
    feeds each carry a different subset of fields (IssuedJobsList has no
    PRIORITY; NewUpdatedJobRecords has division and client but no recruiter
    names; JobsDetail no JOBID), so one must not blank what another filled in. Activities arrive whole, in their current state, and
    overwrite (a hire flag can legitimately go back to 0).
    """
    from psycopg2.extras import execute_values

    unique: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
    for row in rows:
        unique[tuple(row[k] for k in key)] = row  # last one wins within a batch
    if not unique:
        return 0
    updates = [c for c in columns if c not in key]
    if coalesce:
        assignments = ", ".join(f"{c} = COALESCE(EXCLUDED.{c}, {table}.{c})" for c in updates)
    else:
        assignments = ", ".join(f"{c} = EXCLUDED.{c}" for c in updates)
    sql = (
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES %s "
        f"ON CONFLICT ({', '.join(key)}) DO UPDATE SET {assignments}, synced_at = NOW()"
    )
    with conn.cursor() as cur:
        execute_values(cur, sql, [tuple(r[c] for c in columns) for r in unique.values()], page_size=500)
    return len(unique)


def store_jobs(conn, rows: Iterable[Dict[str, Any]], *, mark_detailed: bool = False, mark_users_stale: bool = False) -> int:
    jobs = [j for j in (normalize_job(r) for r in rows) if j]
    count = _upsert(conn, "jobdiva_jobs", JOB_COLUMNS, ("job_id",), jobs, coalesce=True)
    ids = [j["job_id"] for j in jobs]
    if ids and (mark_detailed or mark_users_stale):
        sets = []
        if mark_detailed:
            sets.append("detail_synced_at = NOW()")
        if mark_users_stale:
            # An update may be a reassignment; re-read who is tagged.
            sets.append("users_synced_at = NULL")
        with conn.cursor() as cur:
            cur.execute(f"UPDATE jobdiva_jobs SET {', '.join(sets)} WHERE job_id = ANY(%s)", (ids,))
    return count


def store_activities(conn, rows: Iterable[Dict[str, Any]]) -> int:
    activities = [a for a in (normalize_activity(r) for r in rows) if a]
    return _upsert(conn, "jobdiva_activities", ACTIVITY_COLUMNS, ("activity_id",), activities, coalesce=False)


def store_users(conn, rows: Iterable[Dict[str, Any]]) -> int:
    users = [u for u in (normalize_user(r) for r in rows) if u]
    return _upsert(conn, "jobdiva_users", USER_COLUMNS, ("user_id",), users, coalesce=False)


def replace_job_users(conn, job_ids: Sequence[str], rows: Iterable[Dict[str, Any]]) -> int:
    """Swap in the current assignment of each job JobDiva returned users for,
    then stamp every requested job as read.

    A requested job absent from the answer keeps the tags it had: an empty or
    partial answer must never erase what an earlier read stored (a job with
    nobody tagged at all is not something JobDiva has; every req has at least
    its primary recruiter).
    """
    from psycopg2.extras import execute_values

    requested = [str(j) for j in job_ids]
    assigned = {}
    for row in rows:
        normalized = normalize_job_user(row)
        if normalized and normalized["job_id"] in requested:
            assigned[(normalized["job_id"], normalized["user_id"])] = normalized
    answered = sorted({job_id for job_id, _ in assigned})
    with conn.cursor() as cur:
        if answered:
            cur.execute("DELETE FROM jobdiva_job_users WHERE job_id = ANY(%s)", (answered,))
        if assigned:
            execute_values(
                cur,
                "INSERT INTO jobdiva_job_users (job_id, user_id, roles, date_last_assigned) VALUES %s",
                [(r["job_id"], r["user_id"], r["roles"], r["date_last_assigned"]) for r in assigned.values()],
            )
        cur.execute(
            """
            INSERT INTO jobdiva_jobs (job_id, users_synced_at) SELECT unnest(%s::text[]), NOW()
            ON CONFLICT (job_id) DO UPDATE SET users_synced_at = NOW()
            """,
            (requested,),
        )
    return len(assigned)


@dataclass
class FeedState:
    feed: str
    cursor_at: Optional[datetime.datetime] = None
    backfill_until: Optional[datetime.datetime] = None
    backfill_target: Optional[datetime.datetime] = None
    backfill_done: bool = False
    last_success_at: Optional[datetime.datetime] = None


def load_state(conn, feed: str) -> FeedState:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT cursor_at, backfill_until, backfill_target, backfill_done, last_success_at "
            "FROM jobdiva_bi_sync_state WHERE feed = %s",
            (feed,),
        )
        row = cur.fetchone()
    if not row:
        return FeedState(feed)
    return FeedState(feed, row[0], row[1], row[2], bool(row[3]), row[4])


def save_state(conn, state: FeedState, *, success: bool = True) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO jobdiva_bi_sync_state
                (feed, cursor_at, backfill_until, backfill_target, backfill_done, last_success_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, CASE WHEN %s THEN NOW() END, NOW())
            ON CONFLICT (feed) DO UPDATE SET
                cursor_at = EXCLUDED.cursor_at,
                backfill_until = EXCLUDED.backfill_until,
                backfill_target = EXCLUDED.backfill_target,
                backfill_done = EXCLUDED.backfill_done,
                last_success_at = COALESCE(EXCLUDED.last_success_at, jobdiva_bi_sync_state.last_success_at),
                updated_at = NOW()
            """,
            (state.feed, state.cursor_at, state.backfill_until, state.backfill_target, state.backfill_done, success),
        )


def record_error(conn, feed: str, error: str) -> None:
    try:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO jobdiva_bi_sync_state (feed, last_error, last_error_at, updated_at)
                VALUES (%s, %s, NOW(), NOW())
                ON CONFLICT (feed) DO UPDATE SET last_error = EXCLUDED.last_error,
                    last_error_at = NOW(), updated_at = NOW()
                """,
                (feed, error[:1000]),
            )
        conn.commit()
    except Exception as exc:  # noqa: BLE001 - the error log must not raise
        logger.warning(f"[JobDivaBI] could not record {feed} error: {exc}")


def sync_status(conn) -> Dict[str, Any]:
    """Per-feed progress for the dashboard's "data as of" note."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT feed, cursor_at, backfill_until, backfill_target, backfill_done, "
            "last_success_at, last_error, last_error_at FROM jobdiva_bi_sync_state"
        )
        rows = cur.fetchall()
    return {
        r[0]: {
            "cursor_at": r[1],
            "backfill_until": r[2],
            "backfill_target": r[3],
            "backfill_done": bool(r[4]),
            "last_success_at": r[5],
            "last_error": r[6],
            "last_error_at": r[7],
        }
        for r in rows
    }


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

class _Budget:
    def __init__(self, seconds: float):
        self.deadline = time.monotonic() + seconds

    def left(self) -> bool:
        return time.monotonic() < self.deadline


async def _db(fn, *args, **kwargs):
    # psycopg2 blocks; keep the event loop serving requests while it writes.
    return await asyncio.to_thread(fn, *args, **kwargs)


def _commit(conn) -> None:
    conn.commit()


def _start_feed(state: FeedState, now: datetime.datetime) -> None:
    """First ever run: the incremental sweep starts now and the backfill walks
    back from here, so nothing between the two is ever skipped."""
    if state.cursor_at is None:
        state.cursor_at = now
    if state.backfill_until is None:
        state.backfill_until = state.cursor_at
    if state.backfill_target is None:
        state.backfill_target = state.backfill_until - datetime.timedelta(days=BACKFILL_DAYS)


async def _sweep_forward(conn, fetch: BiFetch, budget: _Budget, state: FeedState, now, path: str, store) -> int:
    stored = 0
    for lower, upper in forward_windows(state.cursor_at - CURSOR_OVERLAP, now, INCREMENTAL_WINDOW):
        if not budget.left():
            break
        rows = await _fetch_range(fetch, path, lower, upper)
        stored += await _db(store, conn, rows)
        state.cursor_at = upper
        await _db(save_state, conn, state)
        await _db(_commit, conn)
    return stored


async def _sweep_backfill(conn, fetch: BiFetch, budget: _Budget, state: FeedState, path: str, store) -> int:
    stored = 0
    if state.backfill_done:
        return stored
    for lower, upper in backfill_windows(state.backfill_until, state.backfill_target, BACKFILL_WINDOW):
        if not budget.left():
            return stored
        rows = await _fetch_range(fetch, path, lower, upper)
        stored += await _db(store, conn, rows)
        state.backfill_until = lower
        await _db(save_state, conn, state)
        await _db(_commit, conn)
    state.backfill_done = True
    await _db(save_state, conn, state)
    await _db(_commit, conn)
    return stored


def _due(state: FeedState, every: datetime.timedelta) -> bool:
    if state.last_success_at is None:
        return True
    last = state.last_success_at if state.last_success_at.tzinfo else state.last_success_at.replace(tzinfo=datetime.timezone.utc)
    return datetime.datetime.now(datetime.timezone.utc) - last >= every


async def sync_users(conn, fetch: BiFetch, budget: _Budget, now: datetime.datetime) -> int:
    state = await _db(load_state, conn, FEED_USERS)
    if not _due(state, USER_DIRECTORY_REFRESH):
        return 0
    # The whole directory: every user record since JobDiva's early days. ~45 s.
    rows = await _fetch_range(fetch, "/apiv2/bi/NewUpdatedUserRecords", datetime.datetime(2000, 1, 1), now)
    stored = await _db(store_users, conn, rows)
    state.cursor_at = now
    await _db(save_state, conn, state)
    await _db(_commit, conn)
    return stored


async def sync_open_jobs(conn, fetch: BiFetch, budget: _Budget, now: datetime.datetime) -> int:
    """Every open req, daily. It carries priority and division, so the rows
    need no JobsDetail round-trip."""
    state = await _db(load_state, conn, FEED_OPEN_JOBS)
    if not _due(state, OPEN_JOBS_REFRESH):
        return 0
    rows = await fetch("/apiv2/bi/OpenJobsList", {})
    await asyncio.sleep(PAUSE_BETWEEN_CALLS_SECONDS)
    stored = await _db(store_jobs, conn, rows, mark_detailed=True)
    state.cursor_at = now
    await _db(save_state, conn, state)
    await _db(_commit, conn)
    return stored


async def sync_jobs(conn, fetch: BiFetch, budget: _Budget, now: datetime.datetime) -> int:
    state = await _db(load_state, conn, FEED_JOBS)
    _start_feed(state, now)

    def store_updated(c, rows):
        # NewUpdatedJobRecords carries priority and the status-change date, so
        # these rows need no JobsDetail round-trip.
        return store_jobs(c, rows, mark_detailed=True, mark_users_stale=True)

    stored = await _sweep_forward(conn, fetch, budget, state, now, "/apiv2/bi/NewUpdatedJobRecords", store_updated)
    stored += await _sweep_backfill(conn, fetch, budget, state, "/apiv2/bi/IssuedJobsList", store_jobs)
    return stored


async def sync_activities(conn, fetch: BiFetch, budget: _Budget, now: datetime.datetime) -> int:
    state = await _db(load_state, conn, FEED_ACTIVITIES)
    _start_feed(state, now)
    path = "/apiv2/bi/SubmittalInterviewHireActivitiesList"
    stored = await _sweep_forward(conn, fetch, budget, state, now, path, store_activities)
    stored += await _sweep_backfill(conn, fetch, budget, state, path, store_activities)
    return stored


_JOBS_NEEDING_DETAIL_SQL = """
    SELECT job_id FROM (
        SELECT job_id, issue_date FROM jobdiva_jobs WHERE detail_synced_at IS NULL
        UNION
        -- PAIR jobs issued before the backfill window: the dashboard still
        -- needs their division / priority / status.
        SELECT mj.job_id::text, NULL::timestamp
        FROM monitored_jobs mj
        WHERE mj.job_id::text ~ '^[0-9]+$'
          AND NOT EXISTS (SELECT 1 FROM jobdiva_jobs j WHERE j.job_id = mj.job_id::text)
        UNION
        -- Jobs that submittals point at but no job feed has returned (closed
        -- before the backfill window, or not updated since).
        SELECT DISTINCT a.job_id, NULL::timestamp
        FROM jobdiva_activities a
        WHERE a.job_id ~ '^[0-9]+$'
          AND NOT EXISTS (SELECT 1 FROM jobdiva_jobs j WHERE j.job_id = a.job_id)
    ) pending
    WHERE job_id ~ '^[0-9]+$'
    ORDER BY issue_date DESC NULLS FIRST
    LIMIT %s
"""


def _jobs_needing_detail(conn, limit: int) -> List[str]:
    with conn.cursor() as cur:
        cur.execute(_JOBS_NEEDING_DETAIL_SQL, (limit,))
        return [str(r[0]) for r in cur.fetchall()]


_JOBS_NEEDING_USERS_SQL = """
    SELECT job_id FROM jobdiva_jobs
    WHERE job_id ~ '^[0-9]+$'
      AND (users_synced_at IS NULL
           OR (COALESCE(job_status, 'OPEN') IN ('OPEN', 'ON HOLD') AND users_synced_at < NOW() - %s::interval))
    ORDER BY users_synced_at NULLS FIRST, issue_date DESC NULLS LAST
    LIMIT %s
"""


def _jobs_needing_users(conn, limit: int) -> List[str]:
    with conn.cursor() as cur:
        cur.execute(_JOBS_NEEDING_USERS_SQL, (f"{int(JOB_USERS_REFRESH.total_seconds())} seconds", limit))
        return [str(r[0]) for r in cur.fetchall()]


async def _fetch_batch(fetch: BiFetch, path: str, batch: Sequence[str], budget: _Budget):
    """One batch of a per-job endpoint -> (rows, the job ids those rows settle).

    An all-empty answer for several jobs is not believed: the jobs are re-asked
    one at a time, so a batch of genuinely gone jobs still settles, while a few
    empty single answers in a row stop the step (JobDivaBIError) instead of
    stamping good jobs as read with nothing. Jobs the budget left un-asked are
    not settled, so the next cycle picks them up.
    """
    rows = await _fetch_jobs_by_id(fetch, path, batch)
    if rows or len(batch) <= 1:
        return rows, list(batch)
    rows, settled = [], []
    empty_streak = 0
    for job_id in batch:
        if not budget.left():
            break
        single = await _fetch_jobs_by_id(fetch, path, [job_id])
        settled.append(job_id)
        if single:
            rows.extend(single)
            empty_streak = 0
            continue
        empty_streak += 1
        if empty_streak >= EMPTY_SINGLE_CALLS_BEFORE_GIVING_UP and not rows:
            raise JobDivaBIError(f"{path} answered nothing for {len(batch)} jobs, one by one too")
    return rows, settled


async def sync_job_details(conn, fetch: BiFetch, budget: _Budget) -> int:
    job_ids = await _db(_jobs_needing_detail, conn, JOB_BATCH_SIZE * JOB_BATCHES_PER_CYCLE)
    stored = 0
    for batch in batched(job_ids, JOB_BATCH_SIZE):
        if not budget.left():
            break
        rows, settled = await _fetch_batch(fetch, "/apiv2/bi/JobsDetail", batch, budget)

        def store(c, fetched=rows, requested=settled):
            count = store_jobs(c, fetched, mark_detailed=True)
            # Mark what JobDiva did not return too (deleted / merged jobs), or
            # they would be asked for again every cycle.
            with c.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO jobdiva_jobs (job_id, detail_synced_at) SELECT unnest(%s::text[]), NOW()
                    ON CONFLICT (job_id) DO UPDATE SET detail_synced_at = NOW()
                    """,
                    (requested,),
                )
            c.commit()
            return count

        stored += await _db(store, conn)
    return stored


async def sync_job_users(conn, fetch: BiFetch, budget: _Budget) -> int:
    job_ids = await _db(_jobs_needing_users, conn, JOB_BATCH_SIZE * JOB_BATCHES_PER_CYCLE)
    stored = 0
    for batch in batched(job_ids, JOB_BATCH_SIZE):
        if not budget.left():
            break
        rows, settled = await _fetch_batch(fetch, "/apiv2/bi/JobsUsersDetail", batch, budget)

        def store(c, fetched=rows, requested=settled):
            count = replace_job_users(c, requested, fetched)
            c.commit()
            return count

        stored += await _db(store, conn)
    return stored


# ---------------------------------------------------------------------------
# Cycle
# ---------------------------------------------------------------------------

async def run_sync_cycle(
    *,
    conn_factory: Optional[Callable[[], Any]] = None,
    fetch: Optional[BiFetch] = None,
    budget_seconds: Optional[float] = None,
    now: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    """One lock-guarded pass over every feed. Returns a per-step summary.

    The incremental sweeps go first (they keep "this week" current), then the
    backfill, then the per-job enrichment. A step that fails records its error
    and the cycle moves on; a 429 ends the cycle.
    """
    if conn_factory is None:
        from core.db import get_db_connection as conn_factory  # noqa: N813
    summary: Dict[str, Any] = {}
    conn = await _db(conn_factory)
    got_lock = False
    try:
        got_lock = await _db(_try_lock, conn)
        if not got_lock:
            return {"skipped": "another worker holds the JobDiva BI sync lock"}
        budget = _Budget(budget_seconds if budget_seconds is not None else SYNC_BUDGET_SECONDS)
        clock = now or jobdiva_now()
        client = None
        if fetch is None:
            import httpx

            client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS)
            fetch = _make_http_fetch(client)
        try:
            steps: List[Tuple[str, Callable[[], Awaitable[int]]]] = [
                (FEED_USERS, lambda: sync_users(conn, fetch, budget, clock)),
                (FEED_JOBS, lambda: sync_jobs(conn, fetch, budget, clock)),
                (FEED_OPEN_JOBS, lambda: sync_open_jobs(conn, fetch, budget, clock)),
                (FEED_ACTIVITIES, lambda: sync_activities(conn, fetch, budget, clock)),
                (FEED_JOB_DETAILS, lambda: sync_job_details(conn, fetch, budget)),
                (FEED_JOB_USERS, lambda: sync_job_users(conn, fetch, budget)),
            ]
            for name, step in steps:
                if not budget.left():
                    summary[name] = "deferred (cycle budget spent)"
                    continue
                try:
                    summary[name] = await step()
                except JobDivaRateLimited as exc:
                    summary[name] = f"rate limited: {exc}"
                    await _db(record_error, conn, name, str(exc))
                    break
                except Exception as exc:  # noqa: BLE001 - one feed must not stop the others
                    logger.warning(f"[JobDivaBI] {name} step failed: {exc}", exc_info=True)
                    summary[name] = f"failed: {exc}"
                    await _db(record_error, conn, name, str(exc))
        finally:
            if client is not None:
                await client.aclose()
        return summary
    finally:
        if got_lock:
            await _db(_unlock, conn)
        await _db(conn.close)


def _try_lock(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (SYNC_LOCK_KEY,))
        got = bool(cur.fetchone()[0])
    conn.commit()
    return got


def _unlock(conn) -> None:
    # A pooled connection outlives close(); a session lock left on it would
    # block every later cycle on every worker.
    try:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (SYNC_LOCK_KEY,))
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[JobDivaBI] advisory unlock failed: {exc}")


async def scheduled_sync() -> None:
    """Scheduler entry point: never raises."""
    if not SYNC_ENABLED:
        return
    started = time.monotonic()
    try:
        summary = await run_sync_cycle()
        logger.info(f"[JobDivaBI] cycle done in {time.monotonic() - started:.0f}s: {summary}")
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[JobDivaBI] cycle crashed: {exc}", exc_info=True)
