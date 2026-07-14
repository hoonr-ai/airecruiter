import asyncio
import json
import logging
from typing import Dict, Any, List
from fastapi import APIRouter, HTTPException, Depends

from core.auth import get_current_user, UserIdentity
from routers._helpers import get_db_connection

router = APIRouter(prefix="/api/v1", tags=["Admin Analytics"])
logger = logging.getLogger(__name__)

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
                "candidates_by_source": candidates_by_source
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
            "warning": f"Analytics partially unavailable: {e}"
        }
    finally:
        conn.close()


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
