import asyncio
import datetime
import json
import logging
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
)

router = APIRouter(prefix="/api/v1", tags=["Admin Analytics"])
logger = logging.getLogger(__name__)

def _iso(value) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else (value or None)


def _compute_jobs_timeline(conn, scope: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Per-job lifecycle: when it was posted on JobDiva vs launched on Curate.

    Timestamps are declared UTC (AT TIME ZONE 'UTC') so the serialized values
    carry an explicit offset — otherwise browsers parse the naive strings in
    the viewer's local timezone and dates can shift by a day.
    """
    cond, params = _mj_filter(scope)
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM monitored_jobs WHERE {cond}", params)
        total_jobs = int(cur.fetchone()[0] or 0)

        cur.execute(f"""
            SELECT
                job_id,
                jobdiva_id,
                COALESCE(NULLIF(TRIM(enhanced_title), ''), NULLIF(TRIM(title), ''), 'Untitled') AS title,
                COALESCE(NULLIF(TRIM(customer_name), ''), 'Unknown') AS customer_name,
                posted_date,
                ({_ts('created_at')}) AT TIME ZONE 'UTC' AS created_at,
                ({_ts('pair_launched_at')}) AT TIME ZONE 'UTC' AS pair_launched_at,
                ({_ts('outreach_stopped_at')}) AT TIME ZONE 'UTC' AS outreach_stopped_at,
                COALESCE(is_archived, FALSE) AS is_archived,
                status,
                {_int('candidates_sourced')},
                {_int('candidates_launched')},
                {_int('jobdiva_total_subs')},
                campaign_id
            FROM monitored_jobs
            WHERE {cond}
            ORDER BY COALESCE({_ts('pair_launched_at')}, {_ts('created_at')}) DESC NULLS LAST
        """, params)
        rows = cur.fetchall()

    timeline = []
    for (job_id, jobdiva_id, title, customer, posted_raw, created_at,
         launched_at, stopped_at, is_archived, status, sourced, launched_count,
         jobdiva_subs, campaign_id) in rows:
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

        timeline.append({
            "job_id": str(job_id or ""),
            "jobdiva_id": str(jobdiva_id or ""),
            "title": title,
            "customer_name": customer,
            "posted_date_raw": str(posted_raw or ""),
            "jobdiva_posted_on": _iso(posted_on),
            "added_to_curate_at": _iso(created_at),
            "curate_launched_at": _iso(launched_at),
            "outreach_stopped_at": _iso(stopped_at),
            "posted_to_launch_days": lag_days,
            "is_archived": bool(is_archived),
            "jobdiva_status": str(status or ""),
            "pair_status": pair_status,
            "candidates_sourced": int(sourced or 0),
            "candidates_launched": int(launched_count or 0),
            "jobdiva_submittals": int(jobdiva_subs or 0),
            "campaign_id": str(campaign_id or "") or None,
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
                                 AND {_ts('created_at')} < NOW() - INTERVAL '7 days') AS aged_unlaunched_jobs
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

    return {
        "weeks": labels,
        "jobs_added": series(f"""
            SELECT date_trunc('week', {_ts('created_at')})::date, COUNT(*)
            FROM monitored_jobs
            WHERE {_ts('created_at')} >= date_trunc('week', NOW()) - make_interval(weeks => %s)
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


def _compute_submission_metrics(conn, scope: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Submission funnel for the admin / team-lead dashboards.

    Two sources:
      - monitored_jobs counters (refreshed each auto-sync cycle):
        complete/pass submissions (local PAIR funnel), pair_submits
        (recruiter pressed Submit in PAIR), pair_external_subs (JobDiva
        submittals matching the strict PAIR criteria) and
        jobdiva_total_subs (raw JobDiva submittal count per job).
      - jobdiva_submittals raw records (BI JobSubmittalsDetail mirror) for
        distinct-candidate and last-30-days cuts plus the top-jobs table.

    All-time across active AND archived jobs — a submittal on a since-closed
    job still happened.
    """
    cond, params = _mj_filter(scope)
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT
                COALESCE(SUM({_int('complete_submissions')}), 0),
                COALESCE(SUM({_int('pass_submissions')}), 0),
                COALESCE(SUM({_int('pair_external_subs')}), 0),
                COALESCE(SUM({_int('pair_submits')}), 0),
                COALESCE(SUM({_int('jobdiva_total_subs')}), 0)
            FROM monitored_jobs
            WHERE {cond}
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
                        WHEN LOWER(COALESCE(sc.data->>'engage_status', '')) IN ('fail', 'failed', 'rejected', 'disqualified', 'declined') THEN 'failed'
                        WHEN LOWER(COALESCE(sc.data->>'engage_status', '')) IN ('in_progress', 'in progress', 'screening', 'interview_completed', 'interview completed', 'contacted') THEN 'in_progress'
                        WHEN COALESCE(NULLIF(sc.data->>'engage_interview_id', ''), '') <> ''
                             OR EXISTS (
                                 SELECT 1 FROM engage_interview_audit ea
                                 WHERE ea.candidate_id = sc.candidate_id
                                   AND COALESCE(NULLIF(ea.interview_id, ''), '') <> ''
                             ) THEN 'launched'
                        WHEN LOWER(COALESCE(sc.status, '')) IN ('launched', 'submitted') THEN 'launched'
                        WHEN LOWER(COALESCE(sc.status, '')) IN ('pass', 'passed', 'qualified', 'shortlisted') THEN 'passed'
                        WHEN LOWER(COALESCE(sc.status, '')) IN ('fail', 'failed', 'rejected') THEN 'failed'
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

        jobs_timeline = _section(_compute_jobs_timeline, {"rows": [], "total": 0}, scope)
        launch_speed = _section(_compute_launch_speed, {}, scope)
        weekly_trends = _section(_compute_weekly_trends, {}, scope)
        submission_metrics = _section(_compute_submission_metrics, {}, scope)
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

    usage_by_id = {u["account_id"]: u for u in usage}
    merged: List[Dict[str, Any]] = []
    seen = set()
    for acc in live:
        u = usage_by_id.get(acc["id"], {})
        merged.append({
            "account_id": acc["id"],
            "account_name": acc.get("name") or u.get("account_name") or "",
            "status": acc.get("status") or "",
            "use_count": u.get("use_count", 0),
            "last_used_at": u.get("last_used_at"),
            "cooldown_until": u.get("cooldown_until"),
            "last_error": u.get("last_error", ""),
        })
        seen.add(acc["id"])
    # Accounts with usage history that are no longer attached to the workspace
    for u in usage:
        if u["account_id"] not in seen:
            merged.append({**u, "status": "DETACHED"})

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
