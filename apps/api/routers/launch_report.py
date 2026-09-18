"""Daily PAIR launch report.

One row per job whose FIRST PAIR launch landed on the requested calendar
date, evaluated in America/New_York (EDT/EST) — not UTC — so a job launched
at 21:00 EDT belongs to that day and not the next.

Each row is that job's rank list, summarised. The Rankings page is the
source of truth for a job's candidates, and this report must agree with it:

  * one unit per launched PERSON, never per interview — a candidate who was
    re-launched is one launched candidate whose status is read from their
    latest interview, exactly as the rank list's table shows them;
  * the job's whole lifetime, not just the launch day — the row is indexed
    by the day the job first launched, but its numbers are the job's
    current numbers, the same ones the Rankings header shows;
  * the same population and the same classification code as the Rankings
    header (`services/launched_candidates.py` + `summarise_launched_candidates`),
    so "Launched" here equals "Candidates Launched" there and the four
    status buckets always sum to it.

Data comes from two places:

  * pair's own Postgres (`monitored_jobs`, `sourced_candidates`,
    `engage_interview_audit`) for sourcing/launch/feedback columns and the
    stored status fallbacks;
  * pair-bot, live, via `GET /api/interviews/{id}/outreach-status` for the
    outreach columns pair never stores — per-candidate status, channel counts
    (call/sms/web), phase distribution, and response times.

The cross-service half is one call per launched candidate (their latest
interview), not per job. The cheaper `/api/dashboard/pair-outreach` endpoint
filters on `pair_tag = 'pair'` and pair launches with `source: "Curate"` and no
pair_tag (engagement.py:819), so it returns nothing for our candidates. The
per-interview endpoint carries no such filter. Fan-out is bounded by a
semaphore and a whole-report deadline; anything that times out degrades that
candidate's outreach columns to the stored fallback rather than failing the
report.
"""
import asyncio
import datetime
import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Response

from core.auth import UserIdentity, get_current_user
from routers._helpers import (
    get_db_connection,
    _load_team_scope,
    _mj_filter,
    _parse_posted_date,
    _parse_recruiter_emails,
)
from services.launched_candidates import (
    fetch_launched_candidates,
    fetch_sourced_candidates,
    interview_id_of,
)
from services.engage_status import (
    format_engage_status,
    hf_display_from_payload,
    parse_engage_score,
    score_from_payload,
)
from services.outreach_normalization import (
    normalize_channel,
    normalize_phase,
    promote_high_score_extra_phase,
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

# Some monitored_jobs rows carry a readable_ist_now() string (".. IST").
_IST = ZoneInfo("Asia/Kolkata")

EXTERNAL_INTERVIEW_API_URL = os.getenv("EXTERNAL_INTERVIEW_API_URL", "https://pairbotqa.hoonr.ai")

# Per-interview outreach fetch. The concurrency cap mirrors engagement.py's
# _PROVISION_CONCURRENCY rationale: pair-bot is a single app VM behind nginx
# with a per-endpoint rate-limit zone, and a 300-candidate day would otherwise
# open 300 sockets at once.
_OUTREACH_CONCURRENCY = int(os.getenv("LAUNCH_REPORT_OUTREACH_CONCURRENCY", "8"))
_OUTREACH_TIMEOUT_S = float(os.getenv("LAUNCH_REPORT_OUTREACH_TIMEOUT", "10"))
_OUTREACH_BUDGET_S = float(os.getenv("LAUNCH_REPORT_OUTREACH_BUDGET", "120"))
MAX_LAUNCH_REPORT_RANGE_DAYS = int(os.getenv("LAUNCH_REPORT_MAX_RANGE_DAYS", "31"))

# Status buckets. Both Pending/InProgress/Completed AND Partial Complete are
# read off pair-bot's own `outreach_status` so the four buckets partition the
# same population — mixing pair's engage_status with pair-bot's status would
# let a candidate land in two buckets and break the Percentage denominator.
# Unrecognised values are logged and bucketed as partial (see _bucket_status).
_PENDING_STATUSES = {"pending", "scheduled", "queued", "contact_check", "not_started", "initiated"}
_IN_PROGRESS_STATUSES = {
    "in_progress",
    "phase1", "phase2", "phase3", "phase4", "active", "sent",
    "call_in_progress", "screening", "interview_completed",
    "contacted",
}
_COMPLETED_STATUSES = {
    "completed", "passed", "failed", "pass", "fail", "complete", "hired",
    "qualified", "shortlisted", "selected", "disqualified", "declined", "rejected",
}
_PARTIAL_STATUSES = {
    "outreach_incomplete", "partial", "partial_complete", "incomplete",
    "expired", "no_response", "unreachable", "abandoned", "outreach_failed",
}

_STATUS_HIERARCHY: Dict[str, int] = {
    # Completed states rank highest
    **{st: 4 for st in _COMPLETED_STATUSES},
    # Partial states
    **{st: 3 for st in _PARTIAL_STATUSES},
    # In progress states
    **{st: 2 for st in _IN_PROGRESS_STATUSES},
    # Pending states
    **{st: 1 for st in _PENDING_STATUSES},
}

_CHANNEL_COLUMNS = {"call": "call", "sms": "sms", "email": "web"}
_CHANNEL_ALIASES = {
    "phone": "call",
    "voice": "call",
    "telephony": "call",
    "text": "sms",
    "whatsapp": "sms",
    "mail": "web",
    "web": "web",
}


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


def _parse_monitored_jobs_timestamp(raw: Optional[str]) -> Optional[datetime.datetime]:
    """Parse a monitored_jobs timestamp column read as ::text.

    The column is written by `readable_ist_now()` in some paths, which emits
    "2026-05-20 20:46:36 IST" — an India wall-clock reading. Casting that to
    ::timestamp drops the suffix, so the value would be read back as if it
    were REPORT_DB_TIMEZONE and land ~5.5h off. Now that this timestamp is
    user-facing ("PAIR Published"), that skew would be visible and wrong, so
    the suffix is honoured explicitly. Everything else is interpreted as
    REPORT_DB_TIMEZONE, matching NOW()/CURRENT_TIMESTAMP writers.
    """
    text = (raw or "").strip()
    if not text:
        return None
    tz = _IST if text.upper().endswith("IST") else _DB_TZ
    # Truncate to "YYYY-MM-DD HH:MM:SS" — the same 19-char window _ts() uses,
    # which covers every shape observed in this column.
    try:
        naive = datetime.datetime.fromisoformat(text[:19].replace(" ", "T"))
    except ValueError:
        # Logged for the same reason _bucket_status logs unknown statuses: a
        # shape this parser does not know would otherwise surface only as a
        # quietly blank PAIR Published / Turn Around Time cell, indistinguishable
        # from a legitimately missing value.
        logger.warning(f"LAUNCH-REPORT: unparseable monitored_jobs timestamp {raw!r} — reported as blank")
        return None
    return naive.replace(tzinfo=tz)


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


def _midnight_eastern(day: Optional[datetime.date]) -> Optional[datetime.datetime]:
    """Start of an Eastern calendar day, for durations anchored on a date-only
    column. JobDiva's posted_date carries no time, so this is the earliest
    instant it could mean — durations from it are day-granular, not exact.
    """
    if day is None:
        return None
    return datetime.datetime.combine(day, datetime.time.min, tzinfo=REPORT_TIMEZONE)


def _mean(values: List[float]) -> Optional[float]:
    return round(sum(values) / len(values), 1) if values else None


def _status_rank(raw: Optional[str]) -> int:
    return _STATUS_HIERARCHY.get((raw or "").strip().lower().replace(" ", "_"), 0)


def _funnel_status_raw(merged: Dict[str, Any], outreach_status: Optional[str]) -> Optional[str]:
    """Status for Pending / In Progress / Completed buckets.

    ``interview_status`` from the live pair-bot API response takes priority when
    it ranks higher than ``outreach_status``. This handles the case where
    ``GET /api/interviews/{id}/outreach-status`` returns ``interview_status:
    in_progress`` at the top level while ``outreach_status`` is still
    ``pending`` (pair-bot has not yet written back the final status for later
    phases). Prefer whichever recognised value is further along the hierarchy.
    """
    # interview_status comes from the live pair-bot API top-level response field.
    interview_status = merged.get("interview_status")
    if _status_rank(interview_status) > _status_rank(outreach_status):
        return interview_status
    return outreach_status


def _bucket_status(raw: Optional[str]) -> str:
    """Map a pair-bot outreach_status onto one of the four report buckets."""
    status = (raw or "").strip().lower().replace(" ", "_")
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
    # above get corrected. Defaults to pending so unknown states do not falsely inflate completion percentage.
    logger.warning(f"LAUNCH-REPORT: unrecognised pair-bot outreach_status {status!r} — bucketed as pending")
    return "pending"


def _normalize_phase(
    raw: Optional[str],
    *,
    allow_pending_aliases: bool = True,
    shift_phases: bool = True,
) -> Optional[str]:
    """Map phase variants onto phase1/phase2/phase3/phase4/extra.

    Caller matrix (single source of truth for phase shifts):
      - Launch Report and Rankings (summarise_launched_candidates):
        shift_phases=True, include_pending_extra=False
      - Raw PairBot (tests/other callers): shift_phases=False

    When shift_phases=True (Launch Report and rankings outreach-stats):
      contact_check / phase1 -> phase1
      phase1_6hr             -> phase2
      phase2                 -> phase3
      phase3                 -> phase4
      phase1_extra           -> extra1
      phase1_6hr_extra       -> extra2
      phase2_extra           -> extra3
      phase3_extra           -> extra3

    When shift_phases=False (raw PairBot vocabulary, tests / other callers):
      contact_check / phase1 -> phase1
      phase1_6hr             -> phase1_6hr
      phase2                 -> phase2
      phase3                 -> phase3
      phase1_extra           -> extra1
      phase1_6hr_extra       -> extra2
      phase2_extra           -> extra3
      phase3_extra           -> extra3
    """
    norm = normalize_phase(raw, allow_pending_aliases=allow_pending_aliases)
    if not norm:
        return None
    if shift_phases:
        if norm in ("contact_check", "phase1"):
            return "phase1"
        if norm == "phase1_6hr":
            return "phase2"
        if norm == "phase2":
            return "phase3"
        if norm == "phase3":
            return "phase4"
        if norm == "phase1_extra":
            return "extra1"
        if norm == "phase1_6hr_extra":
            return "extra2"
        if norm in ("phase2_extra", "phase3_extra"):
            return "extra3"
    else:
        if norm in ("contact_check", "phase1"):
            return "phase1"
        if norm in ("phase1_6hr", "phase2", "phase3"):
            return norm
        if norm == "phase1_extra":
            return "extra1"
        if norm == "phase1_6hr_extra":
            return "extra2"
        if norm in ("phase2_extra", "phase3_extra"):
            return "extra3"
    return None


def _extract_phase(
    outreach: Dict[str, Any],
    *,
    shift_phases: bool = True,
    promote_extra: bool = True,
    include_pending_extra: bool = True,
) -> Optional[str]:
    """Pick phase from known keys, then fall back to status-shaped phase values.

    Rankings and the launch report keep promotion on but set
    include_pending_extra=False so a queued Extra job cannot bump P1→Extra 1.
    Completed/processing Extra jobs and confirmed Extra comms still promote
    when Pair Bot's raw column lags.
    """
    raw = (
        outreach.get("outreach_phase")
        or outreach.get("phase")
        or outreach.get("current_phase")
    )
    if promote_extra:
        raw = promote_high_score_extra_phase(
            outreach, raw, include_pending_extra=include_pending_extra
        )
    phase = _normalize_phase(raw, shift_phases=shift_phases)
    if phase:
        return phase
    return _normalize_phase(
        outreach.get("outreach_status"),
        allow_pending_aliases=False,
        shift_phases=shift_phases,
    )


def _normalize_channel(raw: Optional[str]) -> Optional[str]:
    """Map communication channel/source variants onto call/sms/web columns."""
    return normalize_channel(raw)



def _extract_channel(comm: Dict[str, Any]) -> Optional[str]:
    """Pick communication type from channel/source style keys."""
    return (
        _normalize_channel(comm.get("channel"))
        or _normalize_channel(comm.get("source"))
        or _normalize_channel(comm.get("communication_source"))
        or _normalize_channel(comm.get("type"))
    )


# ---------------------------------------------------------------------------
# Postgres side
# ---------------------------------------------------------------------------
def _eastern_date_expr(col: str) -> str:
    """SQL casting a naive DB timestamp to its calendar date in Eastern time."""
    return f"(({col} AT TIME ZONE %s) AT TIME ZONE %s)::date"


def _fetch_jobs_launched_on(
    conn,
    start_date: datetime.date,
    scope: Optional[Dict[str, Any]] = None,
    end_date: Optional[datetime.date] = None,
) -> List[Dict[str, Any]]:
    """Jobs whose FIRST launch (MIN of engage_interview_audit.created_at) falls
    between `start_date` and `end_date` (inclusive) in Eastern time. A single
    day is just the range where start_date == end_date.

    Keyed on true first launch rather than "any launch or audit activity that day" so a job appears
    exactly once, on the day it went live, however long it keeps launching or receiving updates.
    """
    single_day_query = end_date is None
    if single_day_query:
        end_date = start_date

    mj_cond, mj_params = _mj_filter(scope, "mj")
    launch_date_expr = _eastern_date_expr('l.first_launch_at')
    launch_date_filter = f"{launch_date_expr} = %s" if single_day_query else f"{launch_date_expr} BETWEEN %s AND %s"
    sql = f"""
        WITH launches AS (
            SELECT
                mj.job_id                                     AS job_id,
                MIN(a.created_at)                             AS first_launch_at
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
            -- "Edit Job Setup" after launch clones a job into a new versioned
            -- monitored_jobs row with its own created_at and its own launches.
            -- Grouping by mj.job_id therefore already gives each version its own
            -- report row; version is surfaced so two rows sharing a title are
            -- tellable apart.
            mj.version,
            mj.recruiter_emails,
            mj.posted_date,
            mj.time_to_first_pass,
            -- "PAIR Published" is when the job was brought INTO pair, i.e. the
            -- monitored_jobs row's birth — not monitored_jobs.pair_launched_at,
            -- which is stamped by the publish endpoint. Selected as ::text and
            -- parsed in Python because some rows carry an "… IST" suffix that a
            -- ::timestamp cast silently reads as if it were the DB's own zone.
            mj.created_at::text          AS job_created_at_text,
            l.first_launch_at
        FROM launches l
        JOIN monitored_jobs mj ON mj.job_id = l.job_id
        WHERE {launch_date_filter}
        ORDER BY l.first_launch_at ASC
    """
    params = mj_params + [REPORT_DB_TIMEZONE, str(REPORT_TIMEZONE), start_date]
    if not single_day_query:
        params.append(end_date)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


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
            payload = res.json()
            if isinstance(payload, dict) and payload.get("success") is True and isinstance(payload.get("data"), dict):
                return payload["data"]
            return payload
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


def merge_outreach_payloads(
    cand_fallback: Dict[str, Any],
    audit_fallback: Dict[str, Any],
    live_api: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Merge outreach data from 3 layers:
      Candidate DB (lowest priority) → Audit DB → Live API (highest priority).

    State hierarchy protection: A lower progression status cannot overwrite a
    higher one (e.g. if candidate has completed, an earlier or un-synced pending
    status will not downgrade it).
    """
    merged_payload = {**cand_fallback}
    for source in (audit_fallback, live_api):
        if isinstance(source, dict):
            for k, v in source.items():
                if v is not None:
                    if k in ("outreach_status", "status"):
                        existing_rank = _status_rank(merged_payload.get(k))
                        new_rank = _status_rank(v)
                        # If both statuses are recognized in the hierarchy, enforce monotonic progression.
                        # If either is unrecognised, allow the higher-priority layer to win so genuinely
                        # newer pair-bot statuses are surfaced to logs rather than silently swallowed.
                        if existing_rank and new_rank:
                            if new_rank >= existing_rank:
                                merged_payload[k] = v
                        else:
                            merged_payload[k] = v
                    else:
                        merged_payload[k] = v
    return merged_payload


def build_merged_outreach_payload(
    cand_data: Dict[str, Any],
    audit_response: Any,
    audit_status: Optional[str],
    raw_live_api: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """
    Constructs the merged outreach payload from the 3 fallback layers:
    Candidate DB -> Audit DB -> Live API.
    """
    # Layer 1: Local DB Candidate Row
    cand_fallback = {}
    if cand_data.get("engage_status"):
        cand_fallback["outreach_status"] = cand_data["engage_status"]
    raw_p = cand_data.get("outreach_phase") or cand_data.get("phase")
    if raw_p:
        cand_fallback["outreach_phase"] = raw_p
    raw_c = cand_data.get("outreach_channel") or cand_data.get("channel")
    if raw_c:
        cand_fallback["outreach_channel"] = raw_c
    if cand_data.get("first_attempted_at"):
        cand_fallback["first_attempted_at"] = cand_data["first_attempted_at"]
    raw_comp = cand_data.get("first_completed_at") or cand_data.get("engage_completed_at")
    if raw_comp:
        cand_fallback["first_completed_at"] = raw_comp
    if cand_data.get("engage_completed_at"):
        cand_fallback["engage_completed_at"] = cand_data["engage_completed_at"]
    if cand_data.get("engage_updated_at"):
        cand_fallback["engage_updated_at"] = cand_data["engage_updated_at"]
    if cand_data.get("engage_score") is not None:
        cand_fallback["engage_score"] = cand_data["engage_score"]
    if cand_data.get("engage_hard_filter_status"):
        cand_fallback["engage_hard_filter_status"] = cand_data["engage_hard_filter_status"]

    # Layer 2: Local DB Audit Row Response
    audit_fallback = {}
    if isinstance(audit_response, dict):
        audit_fallback = dict(audit_response)
    elif isinstance(audit_response, str) and audit_response.strip():
        try:
            audit_fallback = json.loads(audit_response)
        except Exception as e:
            logger.warning(f"Failed to decode audit response JSON for candidate: {e}")
            audit_fallback = {}
            
    # Webhook write-backs store pair-bot's interview `status`; older launch
    # responses store `outreach_status`. The merge below ranks each key on
    # its own, so a layer carrying only one of them would leave the other
    # untouched — and a launch-time `sent` stamp in the JSONB would then hide
    # a stored `in_progress`. Mirror the two keys so both take part.
    if audit_fallback.get("status") and not audit_fallback.get("outreach_status"):
        audit_fallback["outreach_status"] = audit_fallback["status"]
    elif audit_fallback.get("outreach_status") and not audit_fallback.get("status"):
        audit_fallback["status"] = audit_fallback["outreach_status"]

    if audit_status:
        curr_st = str(audit_fallback.get("outreach_status") or audit_fallback.get("status") or "").strip()
        audit_status_rank = _status_rank(audit_status)
        current_status_rank = _status_rank(curr_st)
        # Backfill missing keys only; never replace an already-recorded audit
        # response status at equal rank (passed vs completed, fail vs failed).
        if not curr_st:
            audit_fallback["outreach_status"] = audit_status
            audit_fallback["status"] = audit_status
        elif audit_status_rank and current_status_rank:
            if audit_status_rank > current_status_rank:
                audit_fallback["outreach_status"] = audit_status
                audit_fallback["status"] = audit_status
        # Unrecognised overlay must not clobber a status the audit response
        # already stored; missing-key backfill above covers the empty case.

    # Layer 3: Live PairBot HTTP API Response
    # Unwrap nested `outreach` key from live API payload if present
    if isinstance(raw_live_api, dict) and isinstance(raw_live_api.get("outreach"), dict):
        live_api: Optional[Dict[str, Any]] = {**raw_live_api, **raw_live_api["outreach"]}
    else:
        live_api = raw_live_api

    # Merge in priority order: Layer 1 -> Layer 2 -> Layer 3
    return merge_outreach_payloads(cand_fallback, audit_fallback, live_api)


def _summarise_outreach(
    payloads: List[Dict[str, Any]],
    *,
    shift_phases: bool = False,
    promote_extra: bool = True,
    include_pending_extra: bool = True,
) -> Dict[str, Any]:
    """Collapse per-interview outreach payloads into one job's outreach columns.

    Channel counts are per *candidate reached on that channel*, not per message
    sent — a candidate SMS'd three times counts once, which is what a recruiter
    reading "SMS: 12" expects.
    """
    buckets = {"pending": 0, "in_progress": 0, "completed": 0, "partial_complete": 0, "passed": 0, "failed": 0}
    phases = {
        "phase1": 0,
        "phase2": 0,
        "phase3": 0,
        "phase4": 0,
        "extra": 0,
        "extra1": 0,
        "extra2": 0,
        "extra3": 0,
    }
    channels = {"call": 0, "sms": 0, "web": 0}
    first_response_minutes: List[float] = []
    response_timestamps: List[datetime.datetime] = []
    first_contact_timestamps: List[datetime.datetime] = []
    first_attempted_timestamps: List[datetime.datetime] = []
    first_completed_timestamps: List[datetime.datetime] = []
    first_pass_timestamps: List[datetime.datetime] = []

    for payload in payloads:
        outreach_dict = payload.get("outreach") if isinstance(payload.get("outreach"), dict) else {}
        merged = {**payload, **outreach_dict}

        # If the payload came wrapped with an "outreach" dict (like from the UI or candidates API),
        # we strictly avoid falling back to the top-level `payload["status"]` (which is the candidate's
        # resume-screening status). If it's a flat payload, `payload["status"]` IS the outreach status.
        if "outreach" in payload:
            status_raw = merged.get("outreach_status") or outreach_dict.get("status")
        else:
            status_raw = merged.get("outreach_status") or merged.get("status")
        normalized_status = (status_raw or "").strip().lower()

        # Candidates marked as failed/rejected who never actually engaged/attended
        # the interview across phases should be classified as pending.
        # Engagement signals: any recorded score, completion timestamp, a comms
        # response, OR a known phase beyond the initial dispatch (outreach_phase
        # being set means pair-bot already routed this candidate to a call/SMS phase).
        if normalized_status in ("failed", "fail", "rejected"):
            comms = payload.get("communications") or merged.get("communications") or []
            phase_raw = (
                merged.get("outreach_phase")
                or merged.get("phase")
                or merged.get("current_phase")
            )
            has_engaged = (
                merged.get("candidate_score") is not None
                or merged.get("score") is not None
                or merged.get("engage_score") is not None
                or merged.get("first_completed_at") is not None
                or bool(phase_raw)  # phase2/phase3 implies contact was made
                or any(comm.get("response_at") for comm in comms if isinstance(comm, dict))
            )
            if not has_engaged:
                status_raw = "pending"
                normalized_status = "pending"

        funnel_raw = _funnel_status_raw(merged, status_raw)

        # Classify exactly like the rank list's table (`format_engage_status`
        # is what candidates.py stamps on each row), then bucket:
        #   Pass / Fail            -> completed (+ passed / failed)
        #   In Progress            -> in_progress
        #   partial vocabulary     -> partial_complete (the table has no such
        #                             label and shows these as Pending)
        #   anything else          -> pending — including `sent` / `Initiated`
        #                             (stamped at launch, before any contact)
        #                             and reminder phases reported as a status.
        # Bucketing off the raw pair-bot vocabulary instead used to count a
        # just-launched `sent` candidate as In Progress while the table said
        # Pending — the two screens must never disagree on one candidate.
        display = format_engage_status(
            (funnel_raw or "").strip().lower(),
            score_from_payload(merged),
            hf_display_from_payload(merged),
        )
        raw_bucket = _bucket_status(funnel_raw)
        if raw_bucket == "partial_complete":
            bucket = "partial_complete"
        elif display in ("Pass", "Fail") or raw_bucket == "completed":
            # `raw_bucket == "completed"` keeps a terminal pair-bot status the
            # table cannot call Pass or Fail (e.g. `disqualified` with no
            # score) out of Pending — the interview is over either way.
            bucket = "completed"
        elif display == "In Progress":
            bucket = "in_progress"
        else:
            bucket = "pending"
        buckets[bucket] += 1

        if display == "Pass":
            buckets["passed"] += 1
            passed_at = (
                _parse_iso(merged.get("first_pass_at"))
                or _parse_iso(merged.get("first_completed_at"))
                or _parse_iso(merged.get("engage_completed_at"))
                or _parse_iso(merged.get("engage_updated_at"))
                or _parse_iso(merged.get("completed_at"))
                or _parse_iso(merged.get("updated_at"))
            )
            if passed_at:
                first_pass_timestamps.append(passed_at)
        elif display == "Fail":
            buckets["failed"] += 1

        phase = _extract_phase(
            merged,
            shift_phases=shift_phases,
            promote_extra=promote_extra,
            include_pending_extra=include_pending_extra,
        )
        if phase:
            phases[phase] = phases.get(phase, 0) + 1
            if phase in ("extra1", "extra2", "extra3"):
                phases["extra"] = phases.get("extra", 0) + 1

        comms = payload.get("communications") or merged.get("communications") or []
        seen_channels = set()
        sent_times: List[datetime.datetime] = []
        responded_times: List[datetime.datetime] = []
        for comm in comms:
            column = _extract_channel(comm)
            if column:
                seen_channels.add(column)
            sent = _parse_iso(comm.get("sent_at"))
            if sent:
                sent_times.append(sent)
            responded = _parse_iso(comm.get("response_at"))
            if responded:
                responded_times.append(responded)

        # Some pair-bot payloads expose a single channel/source at outreach level.
        if not seen_channels:
            fallback_channel = (
                _normalize_channel(merged.get("outreach_channel"))
                or _normalize_channel(merged.get("channel"))
                or _normalize_channel(merged.get("source"))
                or _normalize_channel(merged.get("communication_source"))
            )
            if fallback_channel:
                seen_channels.add(fallback_channel)

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

        # Pair-bot is the current source of truth for these lifecycle stamps.
        # The local candidate record is retained as a fallback below because
        # older pair-bot responses may omit them.
        attempted = _parse_iso(
            merged.get("first_attempted_at") or merged.get("attempted_at")
        )
        if attempted:
            first_attempted_timestamps.append(attempted)
        completed = _parse_iso(
            merged.get("first_completed_at")
            or merged.get("engage_completed_at")
            or merged.get("completed_at")
        )
        if completed:
            first_completed_timestamps.append(completed)

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
        "first_attempted_at": min(first_attempted_timestamps) if first_attempted_timestamps else None,
        "first_completed_at": min(first_completed_timestamps) if first_completed_timestamps else None,
        "first_pass_at": min(first_pass_timestamps) if first_pass_timestamps else None,
    }


# ---------------------------------------------------------------------------
# Row assembly
# ---------------------------------------------------------------------------
def _summarise_candidates(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Sourcing + recruiter-feedback columns, all from pair's own tables."""
    submitted = rejected = 0
    passed = failed = 0
    time_to_feedback: List[float] = []
    sourced_at: List[datetime.datetime] = []
    first_attempted_at: List[datetime.datetime] = []
    first_completed_at: List[datetime.datetime] = []
    first_pass_at: List[datetime.datetime] = []

    for row in rows:
        feedback_type = (row.get("feedback_type") or "").strip().lower()
        has_reason = bool((row.get("feedback_reason") or "").strip())
        if feedback_type == "submit":
            submitted += 1
        elif feedback_type == "reject":
            rejected += 1

        engage_status = (row.get("engage_status") or "").strip().lower()
        engage_score = parse_engage_score(row.get("engage_score"))
        hf_status = (row.get("engage_hard_filter_status") or "").strip().lower()
        display = format_engage_status(engage_status, engage_score, hf_status)
        if display == "Pass":
            passed += 1
            completed_time = (
                _parse_iso(row.get("first_completed_at"))
                or _parse_iso(row.get("engage_completed_at"))
                or _parse_iso(row.get("engage_updated_at"))
            )
            if completed_time:
                first_pass_at.append(completed_time)
        elif display == "Fail":
            failed += 1

        created = _parse_iso(row.get("created_at"))
        if created:
            sourced_at.append(created)

        attempted = _parse_iso(row.get("first_attempted_at"))
        if attempted:
            first_attempted_at.append(attempted)

        completed = _parse_iso(row.get("first_completed_at")) or _parse_iso(row.get("engage_completed_at"))
        if completed:
            first_completed_at.append(completed)

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
        "first_attempted_at": min(first_attempted_at) if first_attempted_at else None,
        "first_completed_at": min(first_completed_at) if first_completed_at else None,
        "submitted_candidates": submitted,
        "rejected_candidates": rejected,
        "passed_candidates": passed,
        "failed_candidates": failed,
        "first_pass_at": min(first_pass_at) if first_pass_at else None,
        "time_to_feedback_minutes": _mean(time_to_feedback),
        "first_feedback_at": min(
            (_parse_iso(r.get("feedback_at")) for r in rows if r.get("feedback_at")),
            default=None,
        ),
    }


# ---------------------------------------------------------------------------
# Launched candidates → outreach columns
# ---------------------------------------------------------------------------
# JSONB engage fields a launched-candidate row carries (services/launched_candidates.py).
_CANDIDATE_PAYLOAD_FIELDS = (
    "engage_status",
    "engage_score",
    "engage_hard_filter_status",
    "engage_completed_at",
    "engage_updated_at",
    "first_attempted_at",
    "first_completed_at",
    "phase",
    "outreach_phase",
    "channel",
    "outreach_channel",
)


def candidate_outreach_payload(
    row: Dict[str, Any], live_api: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """Merged outreach payload for ONE launched candidate.

    Same three layers, in the same order, as the rank list builds for each of
    its table rows (candidates.py get_job_candidates): the candidate's JSONB
    engage fields, then their latest audit row, then pair-bot's live answer for
    the interview the row points at. A candidate we know nothing about beyond
    "launched" is pending, so the four buckets always sum to launched.
    """
    cand_data = {
        key: row.get(key)
        for key in _CANDIDATE_PAYLOAD_FIELDS
        if row.get(key) not in (None, "")
    }
    # The rank list falls back to engage_candidate_score when engage_score is
    # unset — and a Fail only counts as Fail with a score (format_engage_status).
    if cand_data.get("engage_score") is None and row.get("engage_candidate_score") not in (None, ""):
        cand_data["engage_score"] = row["engage_candidate_score"]
    merged = build_merged_outreach_payload(
        cand_data, row.get("audit_response"), row.get("audit_status"), live_api
    )
    if not (merged.get("outreach_status") or merged.get("status")):
        merged = {**merged, "outreach_status": "pending"}
    return merged


def summarise_launched_candidates(
    launched_rows: List[Dict[str, Any]],
    live_by_interview: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """Outreach columns for one job over its launched candidates.

    The launch report row and the rank list's header stats both call this, so
    Pending / In Progress / Completed / Partial Complete / Passed / Failed, the
    phase and channel columns and the lifecycle timestamps are one computation
    over one population (one unit per launched person, latest interview,
    lifetime of the job). Phases follow the rank list rule: a still-pending
    Extra job stays Phase 1.

    ``resolved`` counts candidates with any status evidence (live pair-bot
    answer or a stored audit / JSONB status); ``launched`` is the population.
    """
    payloads: List[Dict[str, Any]] = []
    resolved = 0
    for row in launched_rows:
        iid = interview_id_of(row)
        live_api = live_by_interview.get(iid) if iid else None
        if (
            live_api is not None
            or row.get("audit_status")
            or row.get("audit_response")
            or row.get("engage_status")
        ):
            resolved += 1
        payloads.append(candidate_outreach_payload(row, live_api))

    summary = _summarise_outreach(payloads, shift_phases=True, include_pending_extra=False)
    summary["launched"] = len(launched_rows)
    summary["resolved"] = resolved
    return summary


# ---------------------------------------------------------------------------
# Row assembly
# ---------------------------------------------------------------------------
def _build_row(
    job: Dict[str, Any],
    launched_rows: List[Dict[str, Any]],
    sourced_rows: List[Dict[str, Any]],
    live_by_interview: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """One report row: the job's rank list, summarised.

    ``launched_rows`` / ``sourced_rows`` are the rank list's populations for
    this job (services/launched_candidates.py) over its whole lifetime — the
    row is indexed by the day the job first launched, but its numbers are the
    job's current numbers, the same ones the Rankings page shows.
    """
    cand = _summarise_candidates(sourced_rows)
    outreach = summarise_launched_candidates(launched_rows, live_by_interview)

    # PAIR Published = the job arriving in pair. PAIR Launch = "Launch PAIR"
    # clicked, i.e. the first call out to pair-bot, which is exactly when the
    # first engage_interview_audit row is written.
    pair_published_at = _parse_monitored_jobs_timestamp(job.get("job_created_at_text"))
    launch_at = _parse_iso(job.get("first_launch_at"))
    jobdiva_published = _parse_posted_date(job.get("posted_date"))
    total_launched = outreach["launched"]
    num_resolved = outreach["resolved"]

    buckets = outreach["buckets"]
    pass_times = [t for t in (outreach.get("first_pass_at"), cand.get("first_pass_at")) if t]
    merged_first_pass = min(pass_times) if pass_times else None
    live_ttp = _minutes_between(launch_at, merged_first_pass)
    # Percentage = (Completed + Partial Complete) / Total Launched * 100.
    #
    # Undefined rather than 0 when nothing launched, and undefined when no
    # candidate has any status evidence — a 0% that only means "pair-bot did
    # not answer" would read as a real result.
    percentage = (
        round((buckets["completed"] + buckets["partial_complete"]) / total_launched * 100, 1)
        if total_launched and num_resolved
        else None
    )

    return {
        "job_id": str(job.get("job_id") or ""),
        "jobdiva_id": (job.get("jobdiva_id") or "").strip(),
        "recruiter_emails": _parse_recruiter_emails(job.get("recruiter_emails")),
        "job_title": (job.get("enhanced_title") or job.get("title") or "").strip(),
        "customer_name": (job.get("customer_name") or "").strip(),
        # Eastern calendar day of first launch — only meaningful once a
        # multi-day range report can mix rows from different days.
        "launch_date": launch_at.astimezone(REPORT_TIMEZONE).date().isoformat() if launch_at else None,
        # Only a missing version defaults to 1 — `or 1` would rewrite a real 0.
        "version": 1 if job.get("version") is None else int(job["version"]),

        # JobDiva only ever gives a date here, never a time of day.
        "jobdiva_published_date": jobdiva_published.isoformat() if jobdiva_published else None,
        "pair_published_at": _edt(pair_published_at),
        "time_to_source_minutes": _minutes_between(pair_published_at, cand["first_sourced_at"]),
        # The rank list's candidate total for this job.
        "total_candidates_sourced": cand["total_sourced"],
        "pair_launch_at": _edt(launch_at),
        # The rank list's "Candidates Launched": people, not interviews.
        "total_candidates_launched": total_launched,
        # Time to Launch spans the whole pipeline: JobDiva posting → PAIR
        # launch. Anchored on posted_date (day granularity — JobDiva gives no
        # time of day) so it stays distinct from Turn Around Time below, which
        # measures only the stretch pair itself owns.
        "time_to_launch_minutes": _minutes_between(_midnight_eastern(jobdiva_published), launch_at),
        # Turn Around Time = PAIR Launch − PAIR Published: how long the job sat
        # in pair before going out.
        "turn_around_time_minutes": _minutes_between(pair_published_at, launch_at),

        # Pending + In Progress + Completed + Partial Complete == Launched.
        "pending": buckets["pending"],
        "in_progress": buckets["in_progress"],
        "completed": buckets["completed"],
        "partial_complete": buckets["partial_complete"],

        # Prefer Pair-bot's live lifecycle timestamps.  `sourced_candidates`
        # is only a fallback when the live response does not expose a stamp.
        "first_attempted_at": _edt(outreach["first_attempted_at"] or cand["first_attempted_at"]),
        "first_completed_at": _edt(outreach["first_completed_at"] or cand["first_completed_at"]),

        "time_to_first_response_minutes": outreach["time_to_first_response_minutes"],
        "launch_to_response_minutes": _minutes_between(launch_at, outreach["earliest_response_at"]),
        "overall_response_time_minutes": outreach["overall_response_time_minutes"],

        "submitted_candidates": cand["submitted_candidates"],
        "rejected_candidates": cand["rejected_candidates"],
        # Pass / Fail per launched candidate, classified exactly like the rank
        # list's table (format_engage_status over the merged payload).
        "passed_candidates": buckets["passed"],
        "failed_candidates": buckets["failed"],
        # Completed candidates the recruiter has not yet actioned either way.
        "outstanding_feedback": max(
            buckets["completed"] - cand["submitted_candidates"] - cand["rejected_candidates"], 0
        ),
        "time_to_feedback_minutes": cand["time_to_feedback_minutes"],
        "first_feedback_at": _edt(cand["first_feedback_at"]),
        "first_pass_at": _edt(merged_first_pass),
        "time_to_first_pass_minutes": (
            live_ttp
            if live_ttp is not None
            else (
                round(float(job["time_to_first_pass"]), 1)
                if job.get("time_to_first_pass") is not None
                else None
            )
        ),

        "call": outreach["channels"]["call"],
        "sms": outreach["channels"]["sms"],
        "web": outreach["channels"]["web"],
        "phase1": outreach["phases"]["phase1"],
        "phase2": outreach["phases"]["phase2"],
        "phase3": outreach["phases"]["phase3"],
        "phase4": outreach["phases"]["phase4"],
        "extra": outreach["phases"]["extra"],
        "extra1": outreach["phases"]["extra1"],
        "extra2": outreach["phases"]["extra2"],
        "extra3": outreach["phases"]["extra3"],
        "percentage": percentage,

        # Lets the UI mark a row whose outreach columns are partial rather
        # than showing dashes that look like real zeros.
        "outreach_detail_resolved": num_resolved,
        "outreach_detail_expected": total_launched,
    }


def _keys_for(job: Dict[str, Any]) -> List[str]:
    keys = []
    if (jobdiva_id := (job.get("jobdiva_id") or "").strip()):
        keys.append(jobdiva_id)
    if job.get("job_id") is not None:
        keys.append(str(job["job_id"]))
    return keys


def _load_report_inputs(
    start_date: datetime.date, end_date: datetime.date, scope_team_id: Optional[str]
) -> Tuple[List[Dict[str, Any]], Dict[str, List[Dict[str, Any]]], Dict[str, List[Dict[str, Any]]]]:
    """All Postgres reads for the report, on a worker thread (psycopg2 is sync).

    Returns (jobs, launched_by_job, sourced_by_job), the last two keyed by
    str(job_id). Both populations are the rank list's, for the job's whole
    lifetime — see services/launched_candidates.py.
    """
    conn = get_db_connection()
    try:
        scope = _load_team_scope(conn, scope_team_id) if scope_team_id else None
        jobs = _fetch_jobs_launched_on(conn, start_date, scope, end_date)
        launched_by_job: Dict[str, List[Dict[str, Any]]] = {}
        sourced_by_job: Dict[str, List[Dict[str, Any]]] = {}
        for job in jobs:
            # sourced_candidates / engage_interview_audit rows were written
            # under either key, so every lookup takes both.
            keys = _keys_for(job)
            job_key = str(job["job_id"])
            launched_by_job[job_key] = fetch_launched_candidates(conn, keys) if keys else []
            sourced_by_job[job_key] = fetch_sourced_candidates(conn, keys) if keys else []
        return jobs, launched_by_job, sourced_by_job
    finally:
        conn.close()


@router.get("/launch-report")
async def get_launch_report(
    date: Optional[str] = Query(default=None, description="YYYY-MM-DD in Eastern time; defaults to yesterday"),
    start_date: Optional[str] = Query(default=None, description="YYYY-MM-DD in Eastern time; range mode, use with end_date"),
    end_date: Optional[str] = Query(default=None, description="YYYY-MM-DD in Eastern time; range mode, use with start_date"),
    team_id: Optional[str] = Query(default=None),
    user: UserIdentity = Depends(get_current_user),
    response: Response = Response(),
):
    """PAIR launch report for jobs first launched on `date`, or between
    `start_date` and `end_date` inclusive (Eastern time). A single day is
    just the range where start_date == end_date; `date` is kept for callers
    that only ever want one day.

    - Admins: system-wide by default; pass ?team_id=... to scope to one team.
    - Team leads: always scoped to their own team (team_id is ignored).
    - Recruiters: 403.
    """
    # A report contains live Pair-bot status. Never let a browser, CDN, or
    # reverse proxy serve an earlier generation for an identical date range.
    if response is not None:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
    if user.is_admin:
        scope_team_id = (team_id or "").strip() or None
    elif user.is_team_lead and user.team_id:
        scope_team_id = user.team_id
    else:
        raise HTTPException(
            status_code=403,
            detail="Access denied. Admin or team lead access required to view the launch report.",
        )

    yesterday = datetime.datetime.now(REPORT_TIMEZONE).date() - datetime.timedelta(days=1)

    if start_date or end_date:
        if not (start_date and end_date):
            raise HTTPException(status_code=400, detail="Both start_date and end_date are required for a range.")
        try:
            report_start_date = datetime.date.fromisoformat(start_date.strip())
            report_end_date = datetime.date.fromisoformat(end_date.strip())
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid start_date/end_date — expected YYYY-MM-DD.")
        if report_start_date > report_end_date:
            raise HTTPException(status_code=400, detail="start_date must not be after end_date.")
        if report_end_date > yesterday:
            raise HTTPException(status_code=400, detail="Today's report is not available yet — end_date must be yesterday or earlier.")
        range_days = (report_end_date - report_start_date).days + 1
        if range_days > MAX_LAUNCH_REPORT_RANGE_DAYS:
            raise HTTPException(
                status_code=400,
                detail=f"Date range cannot exceed {MAX_LAUNCH_REPORT_RANGE_DAYS} days.",
            )
    elif date:
        try:
            report_start_date = report_end_date = datetime.date.fromisoformat(date.strip())
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid date {date!r} — expected YYYY-MM-DD.")
    else:
        report_start_date = report_end_date = yesterday

    try:
        jobs, launched_by_job, sourced_by_job = await asyncio.to_thread(
            _load_report_inputs, report_start_date, report_end_date, scope_team_id
        )
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        # Full detail to the server log; the client gets a generic message so a
        # DB error string never reaches the browser.
        logger.error(f"LAUNCH-REPORT: failed to load {report_start_date}..{report_end_date}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to build the launch report.")

    # One live pair-bot call per launched candidate (their latest interview),
    # fanned out across every job in one pass so the concurrency cap applies
    # to the whole report rather than per job.
    interview_ids = sorted({
        iid
        for launched_rows in launched_by_job.values()
        for row in launched_rows
        if (iid := interview_id_of(row))
    })
    live_by_interview = await _fetch_all_outreach(interview_ids)

    rows = [
        _build_row(
            job,
            launched_by_job.get(str(job["job_id"]), []),
            sourced_by_job.get(str(job["job_id"]), []),
            live_by_interview,
        )
        for job in jobs
    ]

    return {
        "status": "success",
        "data": {
            # report_date kept for callers that only ever requested one day.
            "report_date": report_start_date.isoformat(),
            "start_date": report_start_date.isoformat(),
            "end_date": report_end_date.isoformat(),
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
