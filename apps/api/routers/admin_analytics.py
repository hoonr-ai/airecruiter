import asyncio
import datetime
import json
import logging
import os
import statistics
from typing import Dict, Any, List, Optional, Tuple
from fastapi import APIRouter, HTTPException, Depends, Query

from core.auth import get_current_user, UserIdentity
from routers._helpers import (
    get_db_connection,
    _int,
    _load_team_scope,
    _mj_filter,
    _parse_posted_date,
    _parse_recruiter_emails,
    _sc_filter,
    _ts,
    _ts_utc,
    optional_monitored_jobs_columns,
)
from services.job_attribution import attribution_fields
from services.job_candidate_metrics import empty_metrics, fetch_job_candidate_metrics
from services.job_step_time import STEP_SOURCE, fetch_step_metrics

router = APIRouter(prefix="/api/v1", tags=["Admin Analytics"])
logger = logging.getLogger(__name__)

def _iso(value) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else (value or None)


def _utc(col: str) -> str:
    """timestamptz for a real TIMESTAMP column written with NOW() (UTC).

    pair_launched_at / outreach_stopped_at are only ever written by the DB
    clock, so declaring them UTC is exact. The TEXT-ish columns that
    readable_ist_now() writes (created_at) go through `_ts_utc` instead —
    `_ts(created_at) AT TIME ZONE 'UTC'` read every " IST" row 5h30m late,
    the "Added on PAIR is +5:30" bug.
    """
    return f"({_ts(col)}) AT TIME ZONE 'UTC'"


# Output columns of `_jobs_timeline_sql`, in SELECT order. The SQL aliases
# every column with exactly these names, and a real-Postgres test compares
# them with cursor.description, so the two can't drift apart silently.
_TIMELINE_COLUMNS = (
    "job_id",
    "jobdiva_id",
    "title",
    "customer_name",
    "posted_date",
    "created_at",
    "pair_launched_at",
    "outreach_stopped_at",
    "is_archived",
    "archive_reason",
    "status",
    "candidates_sourced",
    "candidates_launched",
    "jobdiva_total_subs",
    "pair_external_subs",
    "campaign_id",
    "recruiter_emails",
    "pair_posted_by",
    "pair_launched_by",
)


# The unscoped view aggregates every job's candidates (all of
# sourced_candidates), the heaviest statement on this page and not yet
# measured on production-sized data. It gets its own bound below the pool's
# 30s default, so a slow run costs at most this long before the page shows the
# candidate columns as unavailable (jobs_timeline_metrics_available=false).
_METRICS_STATEMENT_TIMEOUT_MS = int(os.getenv("ADMIN_ANALYTICS_METRICS_TIMEOUT_MS", "20000"))
# Step 5 time is a lighter read (job_step_time holds one row per job and
# recruiter, plus each scoped job's launch audit rows) with its own, lower
# bound: the sections run one after another, so a slow run here must not add a
# second full metrics budget before the Step 5 columns show as unavailable.
_STEP_TIME_STATEMENT_TIMEOUT_MS = int(os.getenv("ADMIN_ANALYTICS_STEP_TIME_TIMEOUT_MS", "10000"))


def _missing_optional_columns(conn) -> frozenset:
    """Which OPTIONAL_MONITORED_JOBS_COLUMNS monitored_jobs lacks right now;
    the timeline selects NULL for those (see routers/_helpers)."""
    with conn.cursor() as cur:
        exprs = optional_monitored_jobs_columns(cur, "")
    return frozenset(col for col, expr in exprs.items() if expr == "NULL::text")


def _jobs_timeline_sql(cond: str, missing_columns: frozenset = frozenset()) -> str:
    # No candidate-table join here. The old `feedback_times` step joined
    # sourced_candidates on EITHER job key (`= jobdiva_id OR = job_id::text`),
    # so a job whose feedback sat under both keys came back as two timeline
    # rows. Candidate numbers now come from `_compute_scoped_job_metrics`
    # (one grouped query, DISTINCT candidates across both keys) and are
    # merged in Python by job_id, which can't multiply rows.
    dedup_key = "job_id::text"
    order_ts = f"COALESCE({_utc('pair_launched_at')}, {_ts_utc('created_at')})"

    def optional(col: str) -> str:
        return f"NULL::text AS {col}" if col in missing_columns else col

    return f"""
        WITH deduped AS (
            SELECT
                job_id,
                jobdiva_id,
                enhanced_title,
                title,
                customer_name,
                posted_date,
                created_at,
                pair_launched_at,
                outreach_stopped_at,
                is_archived,
                archive_reason,
                status,
                candidates_sourced,
                candidates_launched,
                jobdiva_total_subs,
                pair_external_subs,
                campaign_id,
                recruiter_emails,
                {optional('pair_posted_by')},
                {optional('pair_launched_by')},
                {order_ts} AS sort_ts,
                ROW_NUMBER() OVER(
                    PARTITION BY {dedup_key}
                    ORDER BY {order_ts} DESC NULLS LAST
                ) as rn
            FROM monitored_jobs
            WHERE {cond}
        )
        SELECT
            d.job_id AS job_id,
            d.jobdiva_id AS jobdiva_id,
            COALESCE(NULLIF(TRIM(d.enhanced_title), ''), NULLIF(TRIM(d.title), ''), 'Untitled') AS title,
            COALESCE(NULLIF(TRIM(d.customer_name), ''), 'Unknown') AS customer_name,
            d.posted_date AS posted_date,
            {_ts_utc('d.created_at')} AS created_at,
            {_utc('d.pair_launched_at')} AS pair_launched_at,
            {_utc('d.outreach_stopped_at')} AS outreach_stopped_at,
            COALESCE(d.is_archived, FALSE) AS is_archived,
            d.archive_reason AS archive_reason,
            d.status AS status,
            {_int('d.candidates_sourced')} AS candidates_sourced,
            {_int('d.candidates_launched')} AS candidates_launched,
            {_int('d.jobdiva_total_subs')} AS jobdiva_total_subs,
            {_int('d.pair_external_subs')} AS pair_external_subs,
            d.campaign_id AS campaign_id,
            d.recruiter_emails AS recruiter_emails,
            d.pair_posted_by AS pair_posted_by,
            d.pair_launched_by AS pair_launched_by
        FROM deduped d
        WHERE d.rn = 1
        ORDER BY d.is_archived ASC, d.sort_ts DESC NULLS LAST
        LIMIT 2000
    """


def _for_scoped_jobs(conn, scope: Optional[Dict[str, Any]], timeout_ms: int, fetch):
    """``fetch(conn, [(job_id, jobdiva_id), ...])`` over EVERY job in scope,
    under its own statement_timeout (the scoped-job read included)."""
    cond, params = _mj_filter(scope)
    with conn.cursor() as cur:
        cur.execute("SELECT current_setting('statement_timeout')")
        previous_timeout = cur.fetchone()[0]
        cur.execute(f"SET LOCAL statement_timeout = '{int(timeout_ms)}ms'")
        cur.execute(f"SELECT job_id, jobdiva_id FROM monitored_jobs WHERE {cond}", params)
        jobs = cur.fetchall()
    result = fetch(conn, jobs)
    # SET LOCAL lasts until the transaction ends and the later sections share
    # this transaction, so hand them back the timeout they would have had. On
    # failure there is nothing to restore: _section's rollback discards it.
    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = %s", (previous_timeout,))
    return result


def _compute_scoped_job_metrics(conn, scope: Optional[Dict[str, Any]] = None) -> Dict[str, Dict[str, Any]]:
    """Rank-list candidate outcomes for EVERY job in scope, in one query.

    Computed once per request and shared: the timeline merges each row's
    entry, and the submission cards sum the internal/external split over all
    scoped jobs — not just the 2000 rows the timeline loads. Pass / feedback /
    submit definitions live in services/job_candidate_metrics.py.
    """
    return _for_scoped_jobs(conn, scope, _METRICS_STATEMENT_TIMEOUT_MS, fetch_job_candidate_metrics)


def _compute_scoped_step_time(conn, scope: Optional[Dict[str, Any]] = None) -> Dict[str, Dict[str, Any]]:
    """Step 5 ("Source") time for EVERY job in scope, in one query: active
    minutes, and first Step 5 entry → first successful launch.

    The definitions live in services/job_step_time.py, shared with the Launch
    Report and Recruiter Analytics so the three reports can't disagree. Its
    own section: if it fails (job_step_time not created yet because startup
    schema init was cancelled, or its statement timeout), only the two Step 5
    columns go blank.
    """
    return _for_scoped_jobs(
        conn, scope, _STEP_TIME_STATEMENT_TIMEOUT_MS,
        lambda c, jobs: fetch_step_metrics(c, jobs, STEP_SOURCE),
    )


def _compute_jobs_timeline(
    conn,
    scope: Optional[Dict[str, Any]] = None,
    job_metrics: Optional[Dict[str, Dict[str, Any]]] = None,
    step_metrics: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Per-job lifecycle: when it was posted on JobDiva vs launched on Curate.

    Every timestamp is a timestamptz, so the serialized values carry an
    explicit offset — otherwise browsers parse naive strings in the viewer's
    local timezone and dates can shift by a day.

    `job_metrics` is `_compute_scoped_job_metrics` output. None means the
    metrics were unavailable: rows then carry None (rendered "—") for every
    candidate column rather than the whole timeline failing. Not zeros — a
    0 Pass / 0 Feedback row reads as real data, and the CSV travels without
    the page's "unavailable" notice.

    `step_metrics` is `_compute_scoped_step_time` output, None when that
    section failed. The Step 5 columns are None then, and also for any job
    nobody timed (everything worked before the tracking shipped), so a job
    reads "—", never "0m".
    """
    cond, params = _mj_filter(scope)
    missing_columns = _missing_optional_columns(conn)
    if missing_columns:
        logger.warning(
            f"Admin analytics timeline: monitored_jobs lacks {sorted(missing_columns)}; "
            "selecting NULL (schema init has not added them yet)"
        )
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(DISTINCT job_id::text) FROM monitored_jobs WHERE {cond}", params)
        total_jobs = int(cur.fetchone()[0] or 0)

        cur.execute(_jobs_timeline_sql(cond, missing_columns), params)
        rows = cur.fetchall()

    scope_emails = set(scope["emails"]) if scope and scope.get("emails") else None
    # A job absent from an available dict was created after the metrics read
    # (it has no candidates yet), so zeros are right for it.
    unavailable_metrics = dict.fromkeys(empty_metrics())

    timeline = []
    for raw in rows:
        r = dict(zip(_TIMELINE_COLUMNS, raw))
        job_id = r["job_id"]
        posted_raw = r["posted_date"]
        launched_at = r["pair_launched_at"]
        stopped_at = r["outreach_stopped_at"]
        is_archived = r["is_archived"]
        status = r["status"]
        raw_recruiter_emails = r["recruiter_emails"]
        posted_on = _parse_posted_date(posted_raw)
        lag_days = None
        if launched_at is not None and posted_on is not None:
            try:
                posted_ts = datetime.datetime.combine(
                    posted_on, datetime.time.min, tzinfo=datetime.timezone.utc
                )
                lag_days = round((launched_at - posted_ts).total_seconds() / 86400.0, 1)
            except Exception:
                lag_days = None

        # Mirror the canonical pair_status derivation (routers/jobs.py):
        # a launched job whose JobDiva status is no longer OPEN is Inactive.
        raw_status = str(status or "OPEN").strip().upper()
        if launched_at is None:
            pair_status = "Unpublished"
        elif stopped_at is not None or is_archived or raw_status != "OPEN":
            pair_status = "Inactive"
        else:
            pair_status = "Active"

        # Intersect with scope emails if present to avoid leaking out-of-team recruiters
        emails = []
        if raw_recruiter_emails:
            try:
                raw_emails = json.loads(raw_recruiter_emails)
                if isinstance(raw_emails, list):
                    emails = [e.lower().strip() for e in raw_emails if e]
                    if scope_emails is not None:
                        emails = [e for e in emails if e in scope_emails]
            except Exception:
                pass

        job_key = str(job_id or "").strip()
        if job_metrics is None:
            m = unavailable_metrics
        else:
            m = job_metrics.get(job_key) or empty_metrics()
        # Unavailable and untracked both read None here; the response flag
        # jobs_timeline_step_time_available tells the page which it was.
        step = (step_metrics or {}).get(job_key) or {}

        timeline.append({
            "job_id": str(job_id or ""),
            "jobdiva_id": str(r["jobdiva_id"] or ""),
            "title": r["title"],
            "customer_name": r["customer_name"],
            "posted_date_raw": str(posted_raw or ""),
            "jobdiva_posted_on": _iso(posted_on),
            "added_to_curate_at": _iso(r["created_at"]),
            "curate_launched_at": _iso(launched_at),
            "outreach_stopped_at": _iso(stopped_at),
            "posted_to_launch_days": lag_days,
            # Step 5 ("Source"), in minutes: active time summed over every
            # visit and recruiter, and wall clock from the first Step 5 entry
            # to the first SUCCESSFUL launch — which can be later than
            # curate_launched_at, the first launch click.
            "step5_active_minutes": step.get("active_minutes"),
            "step5_to_launch_minutes": step.get("to_launch_minutes"),
            "is_archived": bool(is_archived),
            "archive_reason": r["archive_reason"],
            "jobdiva_status": str(status or ""),
            "pair_status": pair_status,
            "candidates_sourced": int(r["candidates_sourced"] or 0),
            "candidates_launched": int(r["candidates_launched"] or 0),
            # Every JobDiva submittal on the job, PAIR or not.
            "jobdiva_submittals": int(r["jobdiva_total_subs"] or 0),
            "campaign_id": str(r["campaign_id"] or "") or None,
            "recruiter_emails": emails,
            # posted_by / launched_by: NULL for jobs that predate the columns.
            **attribution_fields(r["pair_posted_by"], r["pair_launched_by"]),
            # What PAIR recorded, per candidate, on the rank list's rules.
            "pass_candidates": m["passed"],
            "fail_candidates": m["failed"],
            "feedback_total": m["feedback_total"],
            "feedback_submits": m["pair_submits"],
            "feedback_rejects": m["rejects"],
            "feedback_unreachable": m["unreachable"],
            "pair_internal_submits": m["pair_internal_submits"],
            "pair_external_submits": m["pair_external_submits"],
            # ...next to what JobDiva confirms: a JobDiva submittal to the job
            # contact for a PAIR-passed candidate (auto-sync's
            # _count_external_curate_submittals). A gap is a real signal.
            "jobdiva_confirmed_subs": int(r["pair_external_subs"] or 0),
            "first_feedback_at": _iso(m["first_feedback_at"]),
            "first_pair_external_submit_at": _iso(m["first_external_submit_at"]),
        })

    return {"rows": timeline, "total": total_jobs}


def _compute_launch_speed(conn, scope: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Posted→launched velocity across ALL jobs (not just the timeline page)."""
    cond, params = _mj_filter(scope)
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT
                COUNT(*) FILTER (WHERE pair_launched_at IS NOT NULL) AS launched_jobs,
                COUNT(*) FILTER (WHERE pair_launched_at IS NULL
                                 AND COALESCE(is_archived, FALSE) = FALSE) AS unlaunched_active_jobs,
                COUNT(*) FILTER (WHERE pair_launched_at IS NULL
                                 AND COALESCE(is_archived, FALSE) = FALSE
                                 AND {_ts_utc('created_at')} < NOW() - INTERVAL '7 days') AS aged_unlaunched_jobs
            FROM monitored_jobs
            WHERE {cond}
        """, params)
        launched_jobs, unlaunched_active, aged_unlaunched = cur.fetchone()

        # Lag stats computed in Python — posted_date is unparseable-in-SQL
        # free text (see _parse_posted_date; SQL to_date raises on values
        # like "Feb 31, 2026" and would abort the whole section). Sane-window
        # filter because posted_date silently falls back to the fetch date
        # when JobDiva has nothing: admit [-1, 365] days, clamping the small
        # timezone-skew negatives to 0 so they count as same-day launches.
        cur.execute(f"""
            SELECT posted_date, {_ts('pair_launched_at')} AS launched_ts
            FROM monitored_jobs
            WHERE pair_launched_at IS NOT NULL AND {cond}
        """, params)
        lags = []
        for posted_raw, launched_ts in cur.fetchall():
            posted_on = _parse_posted_date(posted_raw)
            if posted_on is None or launched_ts is None:
                continue
            posted_ts = datetime.datetime.combine(posted_on, datetime.time.min)
            lag = (launched_ts - posted_ts).total_seconds() / 86400.0
            if -1.0 <= lag <= 365.0:
                lags.append(max(lag, 0.0))

    avg_lag = (sum(lags) / len(lags)) if lags else None
    median_lag = statistics.median(lags) if lags else None

    return {
        "launched_jobs": int(launched_jobs or 0),
        "unlaunched_active_jobs": int(unlaunched_active or 0),
        "aged_unlaunched_jobs": int(aged_unlaunched or 0),
        "avg_days_posted_to_launch": round(float(avg_lag), 1) if avg_lag is not None else None,
        "median_days_posted_to_launch": round(float(median_lag), 1) if median_lag is not None else None,
    }


def _compute_weekly_trends(conn, scope: Optional[Dict[str, Any]] = None, weeks: int = 8) -> Dict[str, Any]:
    """Aligned weekly (Monday-anchored) series for the trends card.

    The anchor Monday comes from the DATABASE clock — the same clock the
    per-series date_trunc grouping uses — so the Python-built labels can
    never disagree with the SQL buckets when host and DB timezones differ.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT date_trunc('week', NOW())::date")
        anchor_monday = cur.fetchone()[0]
    week_starts = [anchor_monday - datetime.timedelta(weeks=w)
                   for w in range(weeks - 1, -1, -1)]
    labels = [d.isoformat() for d in week_starts]

    def series(sql: str, extra_params: List[Any]) -> List[int]:
        with conn.cursor() as cur:
            cur.execute(sql, [weeks - 1] + extra_params)
            counts = {row[0].isoformat(): int(row[1]) for row in cur.fetchall() if row[0]}
        return [counts.get(label, 0) for label in labels]

    mj_cond, mj_params = _mj_filter(scope)
    sc_cond, sc_params = _sc_filter(scope, "jobdiva_id")
    sub_cond, sub_params = ("TRUE", []) if scope is None else ("job_id = ANY(%s)", [scope["job_ids"]])
    created_utc = f"({_ts_utc('created_at')} AT TIME ZONE 'UTC')"

    return {
        "weeks": labels,
        # created_at is converted to the true instant (IST-suffix aware) and
        # then to a UTC wall clock, the same basis as the naive-UTC columns
        # the other series bucket on. Read with the bare _ts(), India-written
        # rows landed 5h30m late, so a job added late on a Sunday (UTC) was
        # counted in the following week.
        "jobs_added": series(f"""
            SELECT date_trunc('week', {created_utc})::date, COUNT(*)
            FROM monitored_jobs
            WHERE {created_utc} >= date_trunc('week', NOW()) - make_interval(weeks => %s)
              AND {mj_cond}
            GROUP BY 1
        """, mj_params),
        "jobs_launched": series(f"""
            SELECT date_trunc('week', {_ts('pair_launched_at')})::date, COUNT(*)
            FROM monitored_jobs
            WHERE {_ts('pair_launched_at')} >= date_trunc('week', NOW()) - make_interval(weeks => %s)
              AND {mj_cond}
            GROUP BY 1
        """, mj_params),
        "candidates_sourced": series(f"""
            SELECT date_trunc('week', created_at)::date, COUNT(*)
            FROM sourced_candidates
            WHERE created_at >= date_trunc('week', NOW()) - make_interval(weeks => %s)
              AND {sc_cond}
            GROUP BY 1
        """, sc_params),
        "candidates_launched": series(f"""
            SELECT date_trunc('week', created_at)::date, COUNT(DISTINCT NULLIF(interview_id, ''))
            FROM engage_interview_audit
            WHERE created_at >= date_trunc('week', NOW()) - make_interval(weeks => %s)
              AND {sc_cond}
            GROUP BY 1
        """, sc_params),
        # JobDiva-reported submittals, bucketed by the submittal date JobDiva
        # returned (not our sync time) — mirrored locally by auto-sync into
        # jobdiva_submittals.
        "jobdiva_submittals": series(f"""
            SELECT date_trunc('week', submit_date)::date, COUNT(*)
            FROM jobdiva_submittals
            WHERE submit_date >= date_trunc('week', NOW()) - make_interval(weeks => %s)
              AND {sub_cond}
            GROUP BY 1
        """, sub_params),
    }


# The JobDiva job a monitored_jobs row belongs to: the SQL twin of
# routers.recruiter_analytics._family_key (a test pins the two together).
# parent_job_id when set (clones store the root ref there), else the row's own
# ref with the "-vN" version suffix stripped.
_JOB_FAMILY_SQL = (
    "COALESCE(NULLIF(LOWER(TRIM(parent_job_id::text)), ''), "
    "LOWER(REGEXP_REPLACE(COALESCE(NULLIF(TRIM(jobdiva_id::text), ''), TRIM(job_id::text), ''), "
    "'-v[0-9]+$', '', 'i')))"
)


def _compute_submission_metrics(
    conn,
    scope: Optional[Dict[str, Any]] = None,
    job_metrics: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Submission funnel for the admin / team-lead dashboards.

    Three sources:
      - monitored_jobs counters (refreshed each auto-sync cycle):
        complete/pass submissions (local PAIR funnel), pair_submits
        (recruiter pressed Submit in PAIR), pair_external_subs (the
        JobDiva-CONFIRMED count: JobDiva submittals to the job contact for a
        PAIR-passed candidate) and jobdiva_total_subs (raw JobDiva submittal
        count per job). v2+ clones re-resolve to the SAME JobDiva job, so the
        two JobDiva-derived counters are MAX-ed within a version family and
        then summed (v1 and v2 count once, as on Recruiter Analytics); the
        local counters are per version and summed per row.
      - jobdiva_submittals raw records (BI JobSubmittalsDetail mirror) for
        distinct-candidate and last-30-days cuts plus the top-jobs table.
      - `job_metrics` (`_compute_scoped_job_metrics`, every scoped job) for
        the internal / external split of PAIR submits. None when that
        section failed: the split is then None ("unavailable"), not 0.

    All-time across active AND archived jobs — a submittal on a since-closed
    job still happened.
    """
    cond, params = _mj_filter(scope)
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT
                COALESCE(SUM(complete_subs), 0),
                COALESCE(SUM(pass_subs), 0),
                COALESCE(SUM(pair_external), 0),
                COALESCE(SUM(pair_submits), 0),
                COALESCE(SUM(jobdiva_total), 0)
            FROM (
                SELECT
                    SUM({_int('complete_submissions')}) AS complete_subs,
                    SUM({_int('pass_submissions')}) AS pass_subs,
                    MAX({_int('pair_external_subs')}) AS pair_external,
                    SUM({_int('pair_submits')}) AS pair_submits,
                    MAX({_int('jobdiva_total_subs')}) AS jobdiva_total
                FROM monitored_jobs
                WHERE {cond}
                GROUP BY {_JOB_FAMILY_SQL}
            ) families
        """, params)
        complete_subs, pass_subs, pair_external, pair_submits, jobdiva_total = cur.fetchone()

        sub_cond, sub_params = ("TRUE", []) if scope is None else ("job_id = ANY(%s)", [scope["job_ids"]])
        cur.execute(f"""
            SELECT
                COUNT(*),
                COUNT(DISTINCT NULLIF(candidate_id, '')),
                COUNT(*) FILTER (WHERE submit_date >= NOW() - INTERVAL '30 days')
            FROM jobdiva_submittals
            WHERE {sub_cond}
        """, sub_params)
        recorded_total, distinct_candidates, last_30_days = cur.fetchone()

        top_cond, top_params = ("TRUE", []) if scope is None else ("s.job_id = ANY(%s)", [scope["job_ids"]])
        cur.execute(f"""
            SELECT
                s.job_id,
                MAX(s.jobdiva_ref) AS jobdiva_ref,
                COALESCE(NULLIF(TRIM(MAX(mj.enhanced_title)), ''), NULLIF(TRIM(MAX(mj.title)), ''), 'Untitled') AS title,
                COALESCE(NULLIF(TRIM(MAX(mj.customer_name)), ''), 'Unknown') AS customer_name,
                COUNT(*) AS submittals,
                MAX(s.submit_date) AS last_submit_date
            FROM jobdiva_submittals s
            LEFT JOIN monitored_jobs mj ON mj.job_id::text = s.job_id
            WHERE {top_cond}
            GROUP BY s.job_id
            ORDER BY COUNT(*) DESC, MAX(s.submit_date) DESC NULLS LAST
            LIMIT 10
        """, top_params)
        top_jobs = [
            {
                "job_id": str(r[0] or ""),
                "jobdiva_id": str(r[1] or ""),
                "title": r[2],
                "customer_name": r[3],
                "submittals": int(r[4] or 0),
                "last_submit_date": _iso(r[5]),
            }
            for r in cur.fetchall()
        ]

    def split_total(key: str) -> Optional[int]:
        if job_metrics is None:
            return None
        return sum(int(m.get(key) or 0) for m in job_metrics.values())

    return {
        # Raw JobDiva v2 (BI JobSubmittalsDetail) submittal volume
        "jobdiva_total_submittals": int(jobdiva_total or 0),
        "jobdiva_recorded_submittals": int(recorded_total or 0),
        "jobdiva_distinct_candidates": int(distinct_candidates or 0),
        "jobdiva_submittals_last_30_days": int(last_30_days or 0),
        # Local PAIR funnel counters (per-job denormalized sums)
        "complete_submissions": int(complete_subs or 0),
        "pass_submissions": int(pass_subs or 0),
        "pair_external_subs": int(pair_external or 0),
        # What PAIR recorded (recruiter pressed Submit) vs what JobDiva
        # confirms above. Both are reported; a gap is a real signal.
        "pair_submits": int(pair_submits or 0),
        # PAIR submits split by where they went: internal = to a hiring
        # manager for review, external = to the client (a Submit with no
        # recorded type predates the split and counts as external). Read
        # live from the candidate rows, not from the pair_submits counter,
        # so internal + external can differ from pair_submits while that
        # counter is stale (it is refreshed on each click and each sync).
        "pair_internal_submits": split_total("pair_internal_submits"),
        "pair_external_submits": split_total("pair_external_submits"),
        "top_jobs_by_submittals": top_jobs,
    }


def _compute_linkedin_accounts(conn) -> List[Dict[str, Any]]:
    """Unipile round-robin rotation state (table created lazily by UnipileService)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT account_id, account_name, use_count, last_used_at,
                   cooldown_until, last_error
            FROM unipile_account_usage
            ORDER BY use_count DESC, account_id
        """)
        rows = cur.fetchall()
    return [
        {
            "account_id": r[0],
            "account_name": r[1] or "",
            "use_count": int(r[2] or 0),
            "last_used_at": _iso(r[3]),
            "cooldown_until": _iso(r[4]),
            "last_error": r[5] or "",
        }
        for r in rows
    ]


def _compute_analytics_sync(scope_team_id: Optional[str] = None) -> Dict[str, Any]:
    conn = get_db_connection()
    scope: Optional[Dict[str, Any]] = None
    team_scope_out: Optional[Dict[str, Any]] = None
    try:
        if scope_team_id:
            # LookupError (unknown team) is re-raised below → endpoint 404s
            # instead of returning a zeroed dashboard.
            scope = _load_team_scope(conn, scope_team_id)
            team_scope_out = {
                "team_id": scope["team_id"],
                "team_name": scope["team_name"],
                "member_count": len(scope["emails"]),
            }
        mj_cond, mj_params = _mj_filter(scope)
        sc_cond, sc_params = _sc_filter(scope, "sc.jobdiva_id")
        with conn.cursor() as cur:
            # 1. Overview: Monitored vs Archived Jobs
            cur.execute(f"""
                SELECT COALESCE(is_archived, FALSE), COUNT(DISTINCT COALESCE(jobdiva_id, job_id::text))
                FROM monitored_jobs
                WHERE {mj_cond}
                GROUP BY COALESCE(is_archived, FALSE)
            """, mj_params)
            job_rows = cur.fetchall()
            active_jobs = 0
            archived_jobs = 0
            for is_archived, count in job_rows:
                if is_archived:
                    archived_jobs = count
                else:
                    active_jobs = count

            # 2. Sourced candidates by effective funnel status
            cur.execute(f"""
                SELECT
                    CASE
                        WHEN LOWER(COALESCE(sc.data->>'engage_status', '')) IN ('pass', 'passed', 'qualified', 'shortlisted', 'hired', 'selected') THEN 'passed'
                        WHEN LOWER(COALESCE(sc.data->>'engage_status', '')) IN ('fail', 'failed', 'rejected', 'disqualified', 'declined')
                             AND NULLIF(TRIM(COALESCE(sc.data->>'engage_score', '')), '') IS NOT NULL THEN 'failed'
                        WHEN LOWER(COALESCE(sc.data->>'engage_status', '')) IN ('completed', 'complete')
                             AND LOWER(COALESCE(sc.data->>'engage_hard_filter_status', '')) IN ('', 'pass', 'passed', 'not_hard_filter') THEN 'passed'
                        WHEN LOWER(COALESCE(sc.data->>'engage_status', '')) IN ('completed', 'complete') THEN 'failed'
                        WHEN LOWER(COALESCE(sc.data->>'engage_status', '')) IN ('in_progress', 'in progress', 'screening', 'interview_completed', 'interview completed', 'contacted') THEN 'in_progress'
                        WHEN COALESCE(NULLIF(sc.data->>'engage_interview_id', ''), '') <> ''
                             OR EXISTS (
                                 SELECT 1 FROM engage_interview_audit ea
                                 WHERE ea.candidate_id = sc.candidate_id
                                   AND COALESCE(NULLIF(ea.interview_id, ''), '') <> ''
                             ) THEN 'launched'
                        WHEN LOWER(COALESCE(sc.status, '')) IN ('launched', 'submitted') THEN 'launched'
                        WHEN LOWER(COALESCE(sc.status, '')) IN ('pass', 'passed', 'qualified', 'shortlisted') THEN 'passed'
                        WHEN LOWER(COALESCE(sc.status, '')) IN ('fail', 'failed', 'rejected')
                             AND NULLIF(TRIM(COALESCE(sc.data->>'engage_score', '')), '') IS NOT NULL THEN 'failed'
                        ELSE COALESCE(NULLIF(TRIM(sc.status), ''), 'pending')
                    END AS effective_status,
                    COUNT(*)
                FROM sourced_candidates sc
                WHERE {sc_cond}
                GROUP BY effective_status
            """, sc_params)
            status_rows = cur.fetchall()
            candidates_by_status = {}
            total_candidates = 0
            for status, count in status_rows:
                candidates_by_status[status] = count
                total_candidates += count

            # 3. Jobs by Customer
            cur.execute(f"""
                SELECT COALESCE(NULLIF(TRIM(customer_name), ''), 'Unknown') AS cust, COUNT(*)
                FROM monitored_jobs
                WHERE COALESCE(is_archived, FALSE) = FALSE AND {mj_cond}
                GROUP BY cust
                ORDER BY COUNT(*) DESC
                LIMIT 10
            """, mj_params)
            customer_rows = cur.fetchall()
            jobs_by_customer = [
                {"customer_name": cust, "job_count": count}
                for cust, count in customer_rows
            ]

            # 4. Top Recruiters
            # First map candidate counts by jobdiva_id
            sc_plain_cond, sc_plain_params = _sc_filter(scope, "jobdiva_id")
            cur.execute(f"""
                SELECT CAST(jobdiva_id AS TEXT), COUNT(*)
                FROM sourced_candidates
                WHERE jobdiva_id IS NOT NULL AND {sc_plain_cond}
                GROUP BY jobdiva_id
            """, sc_plain_params)
            cand_count_map = {str(row[0]): row[1] for row in cur.fetchall()}

            # Get recruiter emails, jobdiva_id, and job_id for active jobs
            cur.execute(f"""
                SELECT jobdiva_id, job_id, recruiter_emails
                FROM monitored_jobs
                WHERE COALESCE(is_archived, FALSE) = FALSE AND {mj_cond}
            """, mj_params)
            # Team scope: the leaderboard only ranks the team's own emails —
            # a shared job also assigned to an outside recruiter must not
            # leak that recruiter into the team's view.
            scope_emails = set(scope["emails"]) if scope else None
            recruiter_stats = {}
            for jobdiva_id, job_id, raw_emails in cur.fetchall():
                clean_emails = set(_parse_recruiter_emails(raw_emails))
                if scope_emails is not None:
                    clean_emails &= scope_emails
                cand_count = cand_count_map.get(str(jobdiva_id), 0)
                if cand_count == 0 and job_id:
                    cand_count = cand_count_map.get(str(job_id), 0)
                for em in clean_emails:
                    if em not in recruiter_stats:
                        recruiter_stats[em] = {"email": em, "active_jobs": 0, "total_candidates": 0}
                    recruiter_stats[em]["active_jobs"] += 1
                    recruiter_stats[em]["total_candidates"] += cand_count

            top_recruiters = sorted(
                recruiter_stats.values(),
                key=lambda x: (x["active_jobs"], x["total_candidates"]),
                reverse=True
            )[:10]

            # 5. Candidate Sources (grouped by Step 5 channels: JobDiva Talent, LinkedIn, Dice, Exa)
            cur.execute(f"""
                SELECT source, COUNT(*)
                FROM sourced_candidates
                WHERE {sc_plain_cond}
                GROUP BY source
            """, sc_plain_params)
            source_rows = cur.fetchall()
            source_buckets = {
                "JobDiva Talent": 0,
                "LinkedIn": 0,
                "Dice": 0,
                "Exa": 0,
            }
            for src, count in source_rows:
                src_str = str(src or "").lower().strip()
                if "jobdiva" in src_str:
                    source_buckets["JobDiva Talent"] += count
                elif "exa" in src_str:
                    source_buckets["Exa"] += count
                elif "linkedin" in src_str or "unipile" in src_str:
                    source_buckets["LinkedIn"] += count
                elif "dice" in src_str:
                    source_buckets["Dice"] += count
                else:
                    source_buckets["JobDiva Talent"] += count

            candidates_by_source = [
                {"source": name, "count": count}
                for name, count in source_buckets.items()
            ]

        # New sections run outside the shared cursor block, each individually
        # guarded: a failed statement aborts the whole Postgres transaction,
        # so roll back before falling through to the next section — one
        # missing table (e.g. unipile_account_usage before first rotation)
        # must not zero out the rest of the dashboard.
        def _section(compute, default, *args):
            try:
                return compute(conn, *args)
            except Exception as e:
                logger.warning(f"Admin analytics section {compute.__name__} unavailable: {e}")
                try:
                    conn.rollback()
                except Exception:
                    pass
                return default

        # Candidate outcomes for every scoped job, computed once and shared by
        # the timeline rows and the submission split. None on failure (e.g. its
        # own statement timeout): both consumers report the numbers as
        # unavailable instead of failing or showing zeros.
        job_metrics = _section(_compute_scoped_job_metrics, None, scope)
        # Step 5 time, the same way: None on failure blanks just its columns.
        step_metrics = _section(_compute_scoped_step_time, None, scope)
        jobs_timeline = _section(
            _compute_jobs_timeline, {"rows": [], "total": 0}, scope, job_metrics, step_metrics
        )
        launch_speed = _section(_compute_launch_speed, {}, scope)
        weekly_trends = _section(_compute_weekly_trends, {}, scope)
        submission_metrics = _section(_compute_submission_metrics, {}, scope, job_metrics)
        # LinkedIn accounts are global sourcing infrastructure — only shown
        # on the unscoped (all-teams admin) view.
        linkedin_accounts = _section(_compute_linkedin_accounts, []) if scope is None else []

        return {
            "overview": {
                "total_monitored_jobs": active_jobs,
                "total_archived_jobs": archived_jobs,
                "total_sourced_candidates": total_candidates,
                "total_active_recruiters": len(recruiter_stats)
            },
            "candidates_by_status": candidates_by_status,
            "jobs_by_customer": jobs_by_customer,
            "top_recruiters": top_recruiters,
            "candidates_by_source": candidates_by_source,
            "jobs_timeline": jobs_timeline.get("rows", []),
            "jobs_timeline_total": jobs_timeline.get("total", 0),
            # False: the timeline's candidate columns are None this time and
            # the page says so, instead of showing zeros that look real.
            "jobs_timeline_metrics_available": job_metrics is not None,
            # False: the Step 5 columns are None because the read failed, not
            # because the jobs predate the tracking.
            "jobs_timeline_step_time_available": step_metrics is not None,
            "launch_speed": launch_speed,
            "weekly_trends": weekly_trends,
            "submission_metrics": submission_metrics,
            "linkedin_accounts": linkedin_accounts,
            "team_scope": team_scope_out,
        }
    except LookupError:
        raise
    except Exception as e:
        logger.error(f"Error computing admin analytics: {e}")
        # If tables do not exist or error occurs, return fallback zeros
        return {
            "overview": {
                "total_monitored_jobs": 0,
                "total_archived_jobs": 0,
                "total_sourced_candidates": 0,
                "total_active_recruiters": 0
            },
            "candidates_by_status": {},
            "jobs_by_customer": [],
            "top_recruiters": [],
            "candidates_by_source": [],
            "jobs_timeline": [],
            "jobs_timeline_total": 0,
            "jobs_timeline_metrics_available": False,
            "jobs_timeline_step_time_available": False,
            "launch_speed": {},
            "weekly_trends": {},
            "submission_metrics": {},
            "linkedin_accounts": [],
            "team_scope": team_scope_out,
            "warning": f"Analytics partially unavailable: {e}"
        }
    finally:
        conn.close()


@router.get("/admin/linkedin-accounts")
async def get_admin_linkedin_accounts(user: UserIdentity = Depends(get_current_user)):
    """
    Live view of the LinkedIn accounts attached to the Unipile workspace,
    merged with the round-robin usage/cooldown state from the local DB.
    """
    if not user.is_admin:
        raise HTTPException(
            status_code=403,
            detail="Access denied. Admin access required."
        )

    from services.unipile import unipile_service

    try:
        live = await unipile_service.list_linkedin_accounts(force_refresh=True)
    except Exception as e:
        logger.error(f"Unipile account listing failed: {e}")
        live = []

    try:
        usage = await asyncio.to_thread(unipile_service.get_account_usage_sync)
    except Exception as e:
        logger.warning(f"Unipile usage table unavailable: {e}")
        usage = []

    # Unipile error types that mean "a human must reconnect this LinkedIn
    # account in the Unipile dashboard" — LinkedIn logged the seat out
    # (multiple_sessions), the session expired, or a checkpoint is pending.
    _RECONNECT_MARKERS = (
        "disconnected_account", "multiple_session", "credentials",
        "checkpoint", "expired", "invalid account",
    )
    # ...and the ones that mean "this account has no Recruiter seat" — the
    # search falls back to LinkedIn classic people search on it.
    _NO_SEAT_MARKERS = ("feature_not_subscribed", "feature_not_available", "insufficient_permissions")

    def _annotate(row: Dict[str, Any], live_status: str) -> Dict[str, Any]:
        err = str(row.get("last_error") or "").lower()
        status = (live_status or "").upper()
        row["needs_reconnect"] = bool(
            (status and status not in ("OK", "DETACHED"))
            or any(m in err for m in _RECONNECT_MARKERS)
        )
        # `search_api` is what last WORKED (set on success, which also
        # clears last_error); a stale 403 with no success yet reads as
        # classic too, since that is what the next search will do.
        api = str(row.get("search_api") or "").lower()
        if not api and any(m in err for m in _NO_SEAT_MARKERS):
            api = "classic"
        row["search_api"] = api or None
        return row

    usage_by_id = {u["account_id"]: u for u in usage}
    merged: List[Dict[str, Any]] = []
    seen = set()
    for acc in live:
        u = usage_by_id.get(acc["id"], {})
        merged.append(_annotate({
            "account_id": acc["id"],
            "account_name": acc.get("name") or u.get("account_name") or "",
            "status": acc.get("status") or "",
            "use_count": u.get("use_count", 0),
            "last_used_at": u.get("last_used_at"),
            "cooldown_until": u.get("cooldown_until"),
            "last_error": u.get("last_error", ""),
            "search_api": u.get("search_api"),
        }, acc.get("status") or ""))
        seen.add(acc["id"])
    # Accounts with usage history that are no longer attached to the workspace
    for u in usage:
        if u["account_id"] not in seen:
            merged.append(_annotate({**u, "status": "DETACHED"}, "DETACHED"))

    return {"status": "success", "data": {"accounts": merged}}


@router.get("/admin/analytics")
async def get_admin_analytics(
    team_id: Optional[str] = Query(default=None),
    user: UserIdentity = Depends(get_current_user),
):
    """
    Analytics for administrators and team leads.

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
            detail="Access denied. Admin or team lead access required to view analytics."
        )

    try:
        data = await asyncio.to_thread(_compute_analytics_sync, scope_team_id)
        return {
            "status": "success",
            "data": data
        }
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to fetch admin analytics: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch analytics: {str(e)}")
