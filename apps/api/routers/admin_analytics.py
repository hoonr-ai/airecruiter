import asyncio
import datetime
import json
import logging
import statistics
from typing import Dict, Any, List
from fastapi import APIRouter, HTTPException, Depends

from core.auth import get_current_user, UserIdentity
from routers._helpers import get_db_connection

router = APIRouter(prefix="/api/v1", tags=["Admin Analytics"])
logger = logging.getLogger(__name__)

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


def _iso(value) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else (value or None)


def _ts(col: str) -> str:
    """Type-agnostic timestamp expression for monitored_jobs date columns.

    monitored_jobs mixes TIMESTAMP columns (prod) with TEXT columns holding
    either NOW()-style strings or readable_ist_now() strings like
    "2026-05-20 20:46:36 IST" (dev / legacy rows). Truncating the ::text form
    to 19 chars ("YYYY-MM-DD HH:MM:SS") parses every observed shape.
    """
    return f"NULLIF(substring({col}::text from 1 for 19), '')::timestamp"


def _int(col: str) -> str:
    """Type-agnostic integer expression for counter columns (INT or TEXT)."""
    return f"COALESCE(NULLIF(TRIM({col}::text), '')::numeric, 0)::int"


def _compute_jobs_timeline(conn) -> Dict[str, Any]:
    """Per-job lifecycle: when it was posted on JobDiva vs launched on Curate.

    Timestamps are declared UTC (AT TIME ZONE 'UTC') so the serialized values
    carry an explicit offset — otherwise browsers parse the naive strings in
    the viewer's local timezone and dates can shift by a day.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM monitored_jobs")
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
                campaign_id
            FROM monitored_jobs
            ORDER BY COALESCE({_ts('pair_launched_at')}, {_ts('created_at')}) DESC NULLS LAST
            LIMIT 200
        """)
        rows = cur.fetchall()

    timeline = []
    for (job_id, jobdiva_id, title, customer, posted_raw, created_at,
         launched_at, stopped_at, is_archived, status, sourced, launched_count,
         campaign_id) in rows:
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
            "campaign_id": str(campaign_id or "") or None,
        })
    return {"rows": timeline, "total": total_jobs}


def _compute_launch_speed(conn) -> Dict[str, Any]:
    """Posted→launched velocity across ALL jobs (not just the timeline page)."""
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
        """)
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
            WHERE pair_launched_at IS NOT NULL
        """)
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


def _compute_weekly_trends(conn, weeks: int = 8) -> Dict[str, Any]:
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

    def series(sql: str) -> List[int]:
        with conn.cursor() as cur:
            cur.execute(sql, (weeks - 1,))
            counts = {row[0].isoformat(): int(row[1]) for row in cur.fetchall() if row[0]}
        return [counts.get(label, 0) for label in labels]

    return {
        "weeks": labels,
        "jobs_added": series(f"""
            SELECT date_trunc('week', {_ts('created_at')})::date, COUNT(*)
            FROM monitored_jobs
            WHERE {_ts('created_at')} >= date_trunc('week', NOW()) - make_interval(weeks => %s)
            GROUP BY 1
        """),
        "jobs_launched": series(f"""
            SELECT date_trunc('week', {_ts('pair_launched_at')})::date, COUNT(*)
            FROM monitored_jobs
            WHERE {_ts('pair_launched_at')} >= date_trunc('week', NOW()) - make_interval(weeks => %s)
            GROUP BY 1
        """),
        "candidates_sourced": series("""
            SELECT date_trunc('week', created_at)::date, COUNT(*)
            FROM sourced_candidates
            WHERE created_at >= date_trunc('week', NOW()) - make_interval(weeks => %s)
            GROUP BY 1
        """),
        "candidates_launched": series("""
            SELECT date_trunc('week', created_at)::date, COUNT(*)
            FROM engage_interview_audit
            WHERE created_at >= date_trunc('week', NOW()) - make_interval(weeks => %s)
              AND COALESCE(NULLIF(interview_id, ''), '') <> ''
            GROUP BY 1
        """),
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


def _compute_analytics_sync() -> Dict[str, Any]:
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # 1. Overview: Monitored vs Archived Jobs
            cur.execute("""
                SELECT COALESCE(is_archived, FALSE), COUNT(DISTINCT COALESCE(jobdiva_id, job_id::text))
                FROM monitored_jobs
                GROUP BY COALESCE(is_archived, FALSE)
            """)
            job_rows = cur.fetchall()
            active_jobs = 0
            archived_jobs = 0
            for is_archived, count in job_rows:
                if is_archived:
                    archived_jobs = count
                else:
                    active_jobs = count

            # 2. Sourced candidates by effective funnel status
            cur.execute("""
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
                GROUP BY effective_status
            """)
            status_rows = cur.fetchall()
            candidates_by_status = {}
            total_candidates = 0
            for status, count in status_rows:
                candidates_by_status[status] = count
                total_candidates += count

            # 3. Jobs by Customer
            cur.execute("""
                SELECT COALESCE(NULLIF(TRIM(customer_name), ''), 'Unknown') AS cust, COUNT(*)
                FROM monitored_jobs
                WHERE COALESCE(is_archived, FALSE) = FALSE
                GROUP BY cust
                ORDER BY COUNT(*) DESC
                LIMIT 10
            """)
            customer_rows = cur.fetchall()
            jobs_by_customer = [
                {"customer_name": cust, "job_count": count}
                for cust, count in customer_rows
            ]

            # 4. Top Recruiters
            # First map candidate counts by jobdiva_id
            cur.execute("""
                SELECT CAST(jobdiva_id AS TEXT), COUNT(*)
                FROM sourced_candidates
                WHERE jobdiva_id IS NOT NULL
                GROUP BY jobdiva_id
            """)
            cand_count_map = {str(row[0]): row[1] for row in cur.fetchall()}

            # Get recruiter emails, jobdiva_id, and job_id for active jobs
            cur.execute("""
                SELECT jobdiva_id, job_id, recruiter_emails
                FROM monitored_jobs
                WHERE COALESCE(is_archived, FALSE) = FALSE
            """)
            recruiter_stats = {}
            for jobdiva_id, job_id, raw_emails in cur.fetchall():
                emails = []
                if isinstance(raw_emails, str):
                    try:
                        emails = json.loads(raw_emails) if raw_emails.strip().startswith("[") else [raw_emails]
                    except Exception:
                        emails = [raw_emails] if raw_emails else []
                elif isinstance(raw_emails, list):
                    emails = raw_emails
                
                clean_emails = set(str(e).strip().lower() for e in emails if e and str(e).strip())
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
            cur.execute("""
                SELECT source, COUNT(*)
                FROM sourced_candidates
                GROUP BY source
            """)
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
        def _section(compute, default):
            try:
                return compute(conn)
            except Exception as e:
                logger.warning(f"Admin analytics section {compute.__name__} unavailable: {e}")
                try:
                    conn.rollback()
                except Exception:
                    pass
                return default

        jobs_timeline = _section(_compute_jobs_timeline, {"rows": [], "total": 0})
        launch_speed = _section(_compute_launch_speed, {})
        weekly_trends = _section(_compute_weekly_trends, {})
        linkedin_accounts = _section(_compute_linkedin_accounts, [])

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
            "linkedin_accounts": linkedin_accounts,
        }
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
            "linkedin_accounts": [],
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
async def get_admin_analytics(user: UserIdentity = Depends(get_current_user)):
    """
    Get system-wide analytics for administrators.
    """
    if not user.is_admin:
        raise HTTPException(
            status_code=403,
            detail="Access denied. Admin access required to view system analytics."
        )

    try:
        data = await asyncio.to_thread(_compute_analytics_sync)
        return {
            "status": "success",
            "data": data
        }
    except Exception as e:
        logger.error(f"Failed to fetch admin analytics: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch analytics: {str(e)}")
