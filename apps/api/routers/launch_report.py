"""Daily PAIR launch report.

One row per job whose FIRST PAIR launch landed on the requested calendar
date, evaluated in America/New_York (EDT/EST) — not UTC — so a job launched
at 21:00 EDT belongs to that day and not the next.

Data comes from two places:

  * pair's own Postgres (`monitored_jobs`, `sourced_candidates`,
    `engage_interview_audit`) for sourcing/launch/feedback columns;
  * pair-bot, live, via `GET /api/interviews/{id}/outreach-status` for the
    outreach columns pair never stores — per-candidate status buckets,
    channel counts (call/sms/web), phase distribution, and response times.

The cross-service half is one call per launched interview, not per job. The
cheaper `/api/dashboard/pair-outreach` endpoint filters on `pair_tag = 'pair'`
and pair launches with `source: "Curate"` and no pair_tag (engagement.py:819),
so it returns nothing for our candidates. The per-interview endpoint carries
no such filter. Fan-out is bounded by a semaphore and a whole-report deadline;
anything that times out degrades that job's outreach columns to null rather
than failing the report.
"""
import asyncio
import datetime
import logging
import os
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query

from core.auth import UserIdentity, get_current_user
from routers._helpers import (
    get_db_connection,
    _load_team_scope,
    _mj_filter,
    _parse_posted_date,
    _parse_recruiter_emails,
    _ts,
)

router = APIRouter(prefix="/api/v1", tags=["Launch Report"])
logger = logging.getLogger(__name__)

REPORT_TIMEZONE = ZoneInfo(os.getenv("REPORT_TIMEZONE", "America/New_York"))

# engage_interview_audit.created_at / sourced_candidates.created_at are
# `TIMESTAMP` (no tz) filled by CURRENT_TIMESTAMP, so their wall-clock reading
# is whatever the DB session timezone was — UTC on RDS. Naming it here means a
# DB that is not on UTC is a one-line env fix, not a silent day-boundary bug.
REPORT_DB_TIMEZONE = os.getenv("REPORT_DB_TIMEZONE", "UTC")
_DB_TZ = ZoneInfo(REPORT_DB_TIMEZONE)

EXTERNAL_INTERVIEW_API_URL = os.getenv("EXTERNAL_INTERVIEW_API_URL", "https://pairbotqa.hoonr.ai")

# Per-interview outreach fetch. The concurrency cap mirrors engagement.py's
# _PROVISION_CONCURRENCY rationale: pair-bot is a single app VM behind nginx
# with a per-endpoint rate-limit zone, and a 300-candidate day would otherwise
# open 300 sockets at once.
_OUTREACH_CONCURRENCY = int(os.getenv("LAUNCH_REPORT_OUTREACH_CONCURRENCY", "8"))
_OUTREACH_TIMEOUT_S = float(os.getenv("LAUNCH_REPORT_OUTREACH_TIMEOUT", "10"))
_OUTREACH_BUDGET_S = float(os.getenv("LAUNCH_REPORT_OUTREACH_BUDGET", "120"))

# Status buckets. Both Pending/InProgress/Completed AND Partial Complete are
# read off pair-bot's own `outreach_status` so the four buckets partition the
# same population — mixing pair's engage_status with pair-bot's status would
# let a candidate land in two buckets and break the Percentage denominator.
# Unrecognised values are logged and bucketed as partial (see _bucket_status).
_PENDING_STATUSES = {"pending", "scheduled", "queued", "contact_check", "not_started"}
_IN_PROGRESS_STATUSES = {"in_progress", "phase1", "phase2", "phase3", "active", "sent"}
_COMPLETED_STATUSES = {"completed", "passed", "failed", "pass", "fail", "complete"}
_PARTIAL_STATUSES = {
    "outreach_incomplete", "partial", "partial_complete", "incomplete",
    "expired", "no_response", "unreachable", "abandoned",
}

_CHANNEL_COLUMNS = {"call": "call", "sms": "sms", "email": "web"}


# ---------------------------------------------------------------------------
# Small parsing helpers
# ---------------------------------------------------------------------------
def _parse_iso(value: Any) -> Optional[datetime.datetime]:
    """Parse an ISO-8601 string from JSONB or a pair-bot payload.

    Tolerates a trailing 'Z'; a naive string is assumed UTC, which is what
    both writers emit (`datetime.now(timezone.utc).isoformat()`). Returns None
    on anything unparseable — these strings are written by two different
    services and one bad row must not take out the whole report.
    """
    if isinstance(value, datetime.datetime):
        return _from_db(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)


def _from_db(value: Optional[datetime.datetime]) -> Optional[datetime.datetime]:
    """Attach a timezone to a naive `TIMESTAMP` column read from Postgres.

    These columns are filled by CURRENT_TIMESTAMP/NOW(), so their wall-clock
    reading is the DB session's timezone — REPORT_DB_TIMEZONE, not the
    server's local zone and not necessarily UTC.
    """
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=_DB_TZ)


def _edt(dt: Optional[datetime.datetime]) -> Optional[str]:
    """Render a timestamp as an offset-aware ISO string in Eastern time.

    Offset-aware (…-04:00) rather than a preformatted label so the frontend's
    existing `toLocaleString("en-US", { timeZone: "America/New_York" })`
    convention keeps working unchanged.
    """
    return dt.astimezone(REPORT_TIMEZONE).isoformat() if dt else None


def _minutes_between(start: Optional[datetime.datetime], end: Optional[datetime.datetime]) -> Optional[float]:
    """Elapsed minutes, or None if either end is missing or the span is negative.

    A negative span means the two timestamps came from clocks/services that
    disagree; reporting it as a duration would be worse than reporting nothing.
    """
    if not start or not end:
        return None
    delta = (end - start).total_seconds() / 60.0
    return round(delta, 1) if delta >= 0 else None


def _mean(values: List[float]) -> Optional[float]:
    return round(sum(values) / len(values), 1) if values else None


def _bucket_status(raw: Optional[str]) -> str:
    """Map a pair-bot outreach_status onto one of the four report buckets."""
    status = (raw or "").strip().lower()
    if not status:
        return "pending"
    if status in _PENDING_STATUSES:
        return "pending"
    if status in _IN_PROGRESS_STATUSES:
        return "in_progress"
    if status in _COMPLETED_STATUSES:
        return "completed"
    if status in _PARTIAL_STATUSES:
        return "partial_complete"
    # Deliberately visible: the status vocabulary lives in pair-bot and can
    # grow without pair knowing. Logging the unknown value is how the sets
    # above get corrected.
    logger.warning(f"LAUNCH-REPORT: unrecognised pair-bot outreach_status {status!r} — bucketed as partial_complete")
    return "partial_complete"


# ---------------------------------------------------------------------------
# Postgres side
# ---------------------------------------------------------------------------
def _eastern_date_expr(col: str) -> str:
    """SQL casting a naive DB timestamp to its calendar date in Eastern time."""
    return f"(({col} AT TIME ZONE %s) AT TIME ZONE %s)::date"


def _fetch_jobs_launched_on(conn, report_date: datetime.date, scope: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Jobs whose FIRST launch (MIN of engage_interview_audit.created_at) falls
    on `report_date` in Eastern time.

    Keyed on first launch rather than "any launch that day" so a job appears
    exactly once, on the day it went live, however long it keeps launching.
    """
    mj_cond, mj_params = _mj_filter(scope, "mj")
    sql = f"""
        WITH launches AS (
            SELECT
                mj.job_id                                     AS job_id,
                MIN(a.created_at)                             AS first_launch_at,
                COUNT(DISTINCT NULLIF(a.interview_id, ''))    AS total_launched
            FROM monitored_jobs mj
            JOIN engage_interview_audit a
              -- monitor_job_locally writes `data.get("jobdiva_id") or ""`, so a
              -- job with no JobDiva reference stores '' rather than NULL. Without
              -- this guard an audit row that also has '' matches EVERY such job at
              -- once, silently pooling their launch counts together.
              ON NULLIF(a.jobdiva_id, '') IS NOT NULL
             AND (a.jobdiva_id = NULLIF(mj.jobdiva_id, '') OR a.jobdiva_id = mj.job_id::text)
            WHERE {mj_cond}
            GROUP BY mj.job_id
        )
        SELECT
            mj.job_id,
            mj.jobdiva_id,
            mj.title,
            mj.enhanced_title,
            mj.customer_name,
            mj.recruiter_emails,
            mj.posted_date,
            mj.time_to_first_pass,
            {_ts('mj.pair_launched_at')} AS pair_published_at,
            {_ts('mj.created_at')}       AS job_created_at,
            l.first_launch_at,
            l.total_launched
        FROM launches l
        JOIN monitored_jobs mj ON mj.job_id = l.job_id
        WHERE {_eastern_date_expr('l.first_launch_at')} = %s
        ORDER BY l.first_launch_at ASC
    """
    params = mj_params + [REPORT_DB_TIMEZONE, str(REPORT_TIMEZONE), report_date]
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def _fetch_candidate_rows(conn, job_keys: List[str]) -> Dict[str, List[Dict[str, Any]]]:
    """Per-job candidate rows, keyed by the job key the row was written under.

    Only the JSONB fields the report needs are pulled out in SQL; the
    timestamp strings inside them are parsed in Python rather than cast in
    SQL, because `->>` values are written by two services and a single
    malformed one would abort the whole statement on a ::timestamptz cast.
    """
    if not job_keys:
        return {}
    sql = """
        SELECT
            jobdiva_id,
            candidate_id,
            created_at,
            data->>'feedback_type'        AS feedback_type,
            data->>'feedback_reason'      AS feedback_reason,
            data->>'feedback_at'          AS feedback_at,
            data->>'engage_completed_at'  AS engage_completed_at,
            data->>'engage_status'        AS engage_status,
            data->>'engage_interview_id'  AS engage_interview_id
        FROM sourced_candidates
        WHERE jobdiva_id = ANY(%s)
    """
    out: Dict[str, List[Dict[str, Any]]] = {}
    with conn.cursor() as cur:
        cur.execute(sql, (job_keys,))
        cols = [d[0] for d in cur.description]
        for row in cur.fetchall():
            record = dict(zip(cols, row))
            out.setdefault(str(record["jobdiva_id"]), []).append(record)
    return out


def _fetch_audit_rows(conn, job_keys: List[str]) -> Dict[str, List[Dict[str, Any]]]:
    """Per-job launched-interview rows (interview_id + launch time)."""
    if not job_keys:
        return {}
    sql = """
        SELECT jobdiva_id, interview_id, candidate_id, created_at
        FROM engage_interview_audit
        WHERE jobdiva_id = ANY(%s)
          AND NULLIF(interview_id, '') IS NOT NULL
    """
    out: Dict[str, List[Dict[str, Any]]] = {}
    with conn.cursor() as cur:
        cur.execute(sql, (job_keys,))
        cols = [d[0] for d in cur.description]
        for row in cur.fetchall():
            record = dict(zip(cols, row))
            out.setdefault(str(record["jobdiva_id"]), []).append(record)
    return out


# ---------------------------------------------------------------------------
# pair-bot side
# ---------------------------------------------------------------------------
async def _fetch_outreach_status(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    deadline: float,
    interview_id: str,
) -> Optional[Dict[str, Any]]:
    """One `GET /api/interviews/{id}/outreach-status`, or None if it fails.

    None is a first-class outcome, not an error: a slow or missing interview
    blanks that candidate's outreach columns and leaves every Postgres-sourced
    column on the report intact.
    """
    if asyncio.get_running_loop().time() >= deadline:
        return None
    async with semaphore:
        if asyncio.get_running_loop().time() >= deadline:
            return None
        try:
            res = await client.get(f"/api/interviews/{interview_id}/outreach-status")
            res.raise_for_status()
            return res.json()
        except Exception as exc:
            logger.warning(f"LAUNCH-REPORT: outreach-status failed for interview {interview_id}: {exc}")
            return None


async def _fetch_all_outreach(interview_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Fan out over every launched interview, bounded by concurrency + deadline."""
    if not interview_ids:
        return {}

    headers = {}
    pair_api_key = os.getenv("PAIR_API_KEY", "").strip()
    if pair_api_key:
        headers["Authorization"] = f"Bearer {pair_api_key}"

    semaphore = asyncio.Semaphore(_OUTREACH_CONCURRENCY)
    deadline = asyncio.get_running_loop().time() + _OUTREACH_BUDGET_S

    async with httpx.AsyncClient(
        base_url=EXTERNAL_INTERVIEW_API_URL,
        headers=headers,
        timeout=_OUTREACH_TIMEOUT_S,
    ) as client:
        results = await asyncio.gather(
            *(_fetch_outreach_status(client, semaphore, deadline, iid) for iid in interview_ids)
        )

    fetched = {iid: res for iid, res in zip(interview_ids, results) if res is not None}
    if len(fetched) < len(interview_ids):
        logger.warning(
            f"LAUNCH-REPORT: outreach detail incomplete — {len(fetched)}/{len(interview_ids)} interviews resolved"
        )
    return fetched


def _summarise_outreach(payloads: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Collapse per-interview outreach payloads into one job's outreach columns.

    Channel counts are per *candidate reached on that channel*, not per message
    sent — a candidate SMS'd three times counts once, which is what a recruiter
    reading "SMS: 12" expects.
    """
    buckets = {"pending": 0, "in_progress": 0, "completed": 0, "partial_complete": 0}
    phases = {"phase1": 0, "phase2": 0, "phase3": 0}
    channels = {"call": 0, "sms": 0, "web": 0}
    first_response_minutes: List[float] = []
    response_timestamps: List[datetime.datetime] = []
    first_contact_timestamps: List[datetime.datetime] = []

    for payload in payloads:
        outreach = payload.get("outreach") or {}
        buckets[_bucket_status(outreach.get("outreach_status"))] += 1

        phase = (outreach.get("outreach_phase") or "").strip().lower()
        if phase in phases:
            phases[phase] += 1

        comms = payload.get("communications") or []
        seen_channels = set()
        sent_times: List[datetime.datetime] = []
        responded_times: List[datetime.datetime] = []
        for comm in comms:
            column = _CHANNEL_COLUMNS.get((comm.get("channel") or "").strip().lower())
            if column:
                seen_channels.add(column)
            sent = _parse_iso(comm.get("sent_at"))
            if sent:
                sent_times.append(sent)
            responded = _parse_iso(comm.get("response_at"))
            if responded:
                responded_times.append(responded)

        for column in seen_channels:
            channels[column] += 1

        if sent_times:
            first_contact_timestamps.append(min(sent_times))
        if responded_times:
            response_timestamps.append(min(responded_times))
            if sent_times:
                elapsed = _minutes_between(min(sent_times), min(responded_times))
                if elapsed is not None:
                    first_response_minutes.append(elapsed)

    return {
        "buckets": buckets,
        "phases": phases,
        "channels": channels,
        # Fastest single candidate to respond after being contacted.
        "time_to_first_response_minutes": min(first_response_minutes) if first_response_minutes else None,
        # Mean contact→response across every candidate who responded.
        "overall_response_time_minutes": _mean(first_response_minutes),
        "earliest_response_at": min(response_timestamps) if response_timestamps else None,
        "responded_count": len(response_timestamps),
        "first_contact_at": min(first_contact_timestamps) if first_contact_timestamps else None,
    }


# ---------------------------------------------------------------------------
# Row assembly
# ---------------------------------------------------------------------------
def _summarise_candidates(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Sourcing + recruiter-feedback columns, all from pair's own tables."""
    submitted = rejected = 0
    time_to_feedback: List[float] = []
    sourced_at: List[datetime.datetime] = []

    for row in rows:
        feedback_type = (row.get("feedback_type") or "").strip().lower()
        has_reason = bool((row.get("feedback_reason") or "").strip())
        if feedback_type == "submit":
            submitted += 1
        elif feedback_type == "reject":
            rejected += 1

        created = _parse_iso(row.get("created_at"))
        if created:
            sourced_at.append(created)

        if feedback_type and has_reason:
            elapsed = _minutes_between(
                _parse_iso(row.get("engage_completed_at")),
                _parse_iso(row.get("feedback_at")),
            )
            if elapsed is not None:
                time_to_feedback.append(elapsed)

    return {
        "total_sourced": len({r["candidate_id"] for r in rows}),
        "first_sourced_at": min(sourced_at) if sourced_at else None,
        "submitted_candidates": submitted,
        "rejected_candidates": rejected,
        "time_to_feedback_minutes": _mean(time_to_feedback),
    }


def _build_row(
    job: Dict[str, Any],
    candidate_rows: List[Dict[str, Any]],
    audit_rows: List[Dict[str, Any]],
    outreach_by_interview: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    cand = _summarise_candidates(candidate_rows)
    payloads = [
        outreach_by_interview[str(a["interview_id"])]
        for a in audit_rows
        if str(a["interview_id"]) in outreach_by_interview
    ]
    outreach = _summarise_outreach(payloads)

    pair_published_at = _parse_iso(job.get("pair_published_at"))
    job_created_at = _parse_iso(job.get("job_created_at"))
    launch_at = _parse_iso(job.get("first_launch_at"))
    total_launched = int(job.get("total_launched") or 0)

    buckets = outreach["buckets"]
    # Percentage = (Completed + Partial Complete) / Total Launched * 100.
    #
    # Undefined rather than 0 when nothing launched, and undefined when the
    # outreach fan-out resolved nothing — a 0% that only means "pair-bot did
    # not answer" would read as a real result.
    #
    # Note the asymmetry when the fan-out only PARTIALLY resolves: the
    # numerator counts just the interviews pair-bot answered for, while the
    # denominator stays the full launched count, so the figure reads low. That
    # is deliberate — inferring the unanswered ones would invent data — and the
    # row carries outreach_detail_resolved/_expected so the UI can mark it.
    resolved = sum(buckets.values())
    percentage = (
        round((buckets["completed"] + buckets["partial_complete"]) / total_launched * 100, 1)
        if total_launched and resolved
        else None
    )

    return {
        "job_id": str(job.get("job_id") or ""),
        "jobdiva_id": (job.get("jobdiva_id") or "").strip(),
        "recruiter_emails": _parse_recruiter_emails(job.get("recruiter_emails")),
        "job_title": (job.get("enhanced_title") or job.get("title") or "").strip(),
        "customer_name": (job.get("customer_name") or "").strip(),

        # JobDiva only ever gives a date here, never a time of day.
        "jobdiva_published_date": (
            d.isoformat() if (d := _parse_posted_date(job.get("posted_date"))) else None
        ),
        "pair_published_at": _edt(pair_published_at),
        "time_to_source_minutes": _minutes_between(job_created_at, cand["first_sourced_at"]),
        "total_candidates_sourced": cand["total_sourced"],
        "pair_launch_at": _edt(launch_at),
        "total_candidates_launched": total_launched,
        "time_to_launch_minutes": _minutes_between(pair_published_at, launch_at),

        "pending": buckets["pending"],
        "in_progress": buckets["in_progress"],
        "completed": buckets["completed"],
        "partial_complete": buckets["partial_complete"],

        "time_to_first_response_minutes": outreach["time_to_first_response_minutes"],
        "launch_to_response_minutes": _minutes_between(launch_at, outreach["earliest_response_at"]),
        "overall_response_time_minutes": outreach["overall_response_time_minutes"],

        "submitted_candidates": cand["submitted_candidates"],
        "rejected_candidates": cand["rejected_candidates"],
        # Completed candidates the recruiter has not yet actioned either way.
        "outstanding_feedback": max(
            buckets["completed"] - cand["submitted_candidates"] - cand["rejected_candidates"], 0
        ),
        "time_to_feedback_minutes": cand["time_to_feedback_minutes"],
        "time_to_first_pass_minutes": (
            round(float(job["time_to_first_pass"]), 1)
            if job.get("time_to_first_pass") is not None
            else None
        ),

        "call": outreach["channels"]["call"],
        "sms": outreach["channels"]["sms"],
        "web": outreach["channels"]["web"],
        "phase1": outreach["phases"]["phase1"],
        "phase2": outreach["phases"]["phase2"],
        "phase3": outreach["phases"]["phase3"],
        "percentage": percentage,

        # Lets the UI mark a row whose outreach columns are partial rather
        # than showing dashes that look like real zeros.
        "outreach_detail_resolved": len(payloads),
        "outreach_detail_expected": len(audit_rows),
    }


def _load_report_inputs(
    report_date: datetime.date, scope_team_id: Optional[str]
) -> Tuple[List[Dict[str, Any]], Dict[str, List[Dict[str, Any]]], Dict[str, List[Dict[str, Any]]]]:
    """All Postgres reads for the report, on a worker thread (psycopg2 is sync)."""
    conn = get_db_connection()
    try:
        scope = _load_team_scope(conn, scope_team_id) if scope_team_id else None
        jobs = _fetch_jobs_launched_on(conn, report_date, scope)
        if not jobs:
            return [], {}, {}

        # sourced_candidates / engage_interview_audit rows were written under
        # either key, so look up both and merge (mirrors _compute_candidate_counters).
        keys = sorted({key for job in jobs for key in _keys_for(job)})

        return jobs, _fetch_candidate_rows(conn, keys), _fetch_audit_rows(conn, keys)
    finally:
        conn.close()


def _keys_for(job: Dict[str, Any]) -> List[str]:
    keys = []
    if (jobdiva_id := (job.get("jobdiva_id") or "").strip()):
        keys.append(jobdiva_id)
    if job.get("job_id") is not None:
        keys.append(str(job["job_id"]))
    return keys


@router.get("/launch-report")
async def get_launch_report(
    date: Optional[str] = Query(default=None, description="YYYY-MM-DD in Eastern time; defaults to yesterday"),
    team_id: Optional[str] = Query(default=None),
    user: UserIdentity = Depends(get_current_user),
):
    """Daily PAIR launch report for jobs first launched on `date` (Eastern time).

    - Admins: system-wide by default; pass ?team_id=... to scope to one team.
    - Team leads: always scoped to their own team (team_id is ignored).
    - Recruiters: 403.
    """
    if user.is_admin:
        scope_team_id = (team_id or "").strip() or None
    elif user.is_team_lead and user.team_id:
        scope_team_id = user.team_id
    else:
        raise HTTPException(
            status_code=403,
            detail="Access denied. Admin or team lead access required to view the launch report.",
        )

    if date:
        try:
            report_date = datetime.date.fromisoformat(date.strip())
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid date {date!r} — expected YYYY-MM-DD.")
    else:
        report_date = datetime.datetime.now(REPORT_TIMEZONE).date() - datetime.timedelta(days=1)

    try:
        jobs, candidates_by_key, audit_by_key = await asyncio.to_thread(
            _load_report_inputs, report_date, scope_team_id
        )
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        # Full detail to the server log; the client gets a generic message so a
        # DB error string never reaches the browser.
        logger.error(f"LAUNCH-REPORT: failed to load {report_date}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to build the launch report.")

    # Fan out over every launched interview across every job in one pass, so
    # the concurrency cap applies to the whole report rather than per job.
    audit_by_job: Dict[str, List[Dict[str, Any]]] = {}
    interview_ids: List[str] = []
    for job in jobs:
        rows = [row for key in _keys_for(job) for row in audit_by_key.get(key, [])]
        # A job matched under both keys yields the same interview twice.
        deduped = {str(r["interview_id"]): r for r in rows}
        audit_by_job[str(job["job_id"])] = list(deduped.values())
        interview_ids.extend(deduped.keys())

    outreach_by_interview = await _fetch_all_outreach(sorted(set(interview_ids)))

    rows = []
    for job in jobs:
        candidate_rows = {
            row["candidate_id"]: row
            for key in _keys_for(job)
            for row in candidates_by_key.get(key, [])
        }
        rows.append(
            _build_row(
                job,
                list(candidate_rows.values()),
                audit_by_job[str(job["job_id"])],
                outreach_by_interview,
            )
        )

    return {
        "status": "success",
        "data": {
            "report_date": report_date.isoformat(),
            "timezone": str(REPORT_TIMEZONE),
            "generated_at": _edt(datetime.datetime.now(datetime.timezone.utc)),
            "team_id": scope_team_id,
            "jobs": rows,
            "totals": {
                "jobs": len(rows),
                "candidates_sourced": sum(r["total_candidates_sourced"] for r in rows),
                "candidates_launched": sum(r["total_candidates_launched"] for r in rows),
                "outreach_detail_resolved": sum(r["outreach_detail_resolved"] for r in rows),
                "outreach_detail_expected": sum(r["outreach_detail_expected"] for r in rows),
            },
        },
    }
