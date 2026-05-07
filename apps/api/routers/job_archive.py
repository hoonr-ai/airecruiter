"""
Jobs Router - Handles job management endpoints including archiving.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
import logging
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timezone, timedelta

from core.db import get_db_connection
from core.email import notify_pair_inactive
import json

router = APIRouter(prefix="/jobs", tags=["Jobs"])
logger = logging.getLogger(__name__)


def _invalidate_monitored_jobs_cache() -> None:
    """Drop the /jobs/monitored TTL cache so the next read sees this write.

    Lazy-imported because main.py loads job_archive before jobs; we don't
    want a load-time failure in jobs.py to take down archive too.
    """
    try:
        from routers.jobs import invalidate_monitored_jobs_cache
        invalidate_monitored_jobs_cache()
    except Exception as e:
        logger.warning(f"monitored_jobs cache invalidation skipped: {e}")


# Helper function for readable IST timestamps
def readable_ist_now() -> str:
    """Returns current IST time in readable format: 2026-02-24 16:25:59 IST"""
    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist).strftime("%Y-%m-%d %H:%M:%S IST")


# ============================================================================
# Models
# ============================================================================

class JobArchiveRequest(BaseModel):
    """Request to archive a job"""
    reason: Optional[str] = None


# ============================================================================
# Archive Endpoints
# ============================================================================

@router.put("/{job_id}/archive")
async def archive_job(job_id: str, request: JobArchiveRequest = None):
    """
    Archive a job by setting its status to 'archived'.
    This soft-deletes the job from active view but keeps it in the database.
    """
    try:
        logger.info(f"Archiving job {job_id}")
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # First check if job exists (by job_id or jobdiva_id)
        logger.info(f"Looking for job: {job_id}")
        cursor.execute("""
            SELECT job_id, jobdiva_id FROM monitored_jobs
            WHERE job_id = %s
            UNION ALL
            SELECT job_id, jobdiva_id FROM monitored_jobs
            WHERE jobdiva_id = %s AND job_id <> %s
            LIMIT 1
        """, (job_id, job_id, job_id))
        row = cursor.fetchone()
        logger.info(f"Query result: {row}")
        if not row:
            # Let's check what jobs exist
            cursor.execute("SELECT job_id, jobdiva_id FROM monitored_jobs LIMIT 5")
            sample_jobs = cursor.fetchall()
            logger.info(f"Sample jobs in DB: {sample_jobs}")
            cursor.close()
            conn.close()
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
        
        actual_job_id = row[0]  # Use the numeric job_id from database
        logger.info(f"Found job_id: {actual_job_id}, jobdiva_id: {row[1]}")
        
        # Archive the job (set is_archived flag, don't change status)
        archive_reason = request.reason if request and request.reason else "Manually archived"
        cursor.execute("""
            UPDATE monitored_jobs 
            SET is_archived = TRUE, 
                updated_at = %s,
                archive_reason = %s,
                archived_at = %s
            WHERE job_id = %s
            RETURNING jobdiva_id, recruiter_emails
        """, (readable_ist_now(), archive_reason, readable_ist_now(), actual_job_id))
        
        ret = cursor.fetchone()
        conn.commit()
        
        # Email notification for archived jobs has been disabled per user request

        cursor.close()
        conn.close()

        _invalidate_monitored_jobs_cache()

        logger.info(f"✅ Job {job_id} archived successfully")
        return {
            "status": "SUCCESS",
            "job_id": job_id,
            "message": "Job archived successfully"
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error archiving job {job_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to archive job: {str(e)}")


@router.post("/{job_id}/stop-activity")
async def stop_job_activity(job_id: str):
    """
    One-way action: stops new outreach for an Active job by setting
    outreach_stopped_at. The HC status flips to Inactive on the next read.

    Blocks future Launch PAIR calls and engagement sends. The external
    PAIR scheduler (pairbotqa.hoonr.ai) keeps running until candidates
    respond/timeout — TODO: integrate an external stop API once available.
    """
    try:
        logger.info(f"Stopping activity for job {job_id}")

        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT job_id, jobdiva_id, pair_launched_at, outreach_stopped_at FROM monitored_jobs
            WHERE job_id = %s
            UNION ALL
            SELECT job_id, jobdiva_id, pair_launched_at, outreach_stopped_at FROM monitored_jobs
            WHERE jobdiva_id = %s AND job_id <> %s
            LIMIT 1
        """, (job_id, job_id, job_id))
        row = cursor.fetchone()
        if not row:
            cursor.close()
            conn.close()
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

        actual_job_id, jobdiva_id, pair_launched_at, outreach_stopped_at = row
        if pair_launched_at is None:
            cursor.close()
            conn.close()
            raise HTTPException(
                status_code=409,
                detail="Cannot stop activity on an unpublished job",
            )
        if outreach_stopped_at is not None:
            cursor.close()
            conn.close()
            return {
                "status": "ALREADY_STOPPED",
                "job_id": job_id,
                "stopped_at": outreach_stopped_at.isoformat() if hasattr(outreach_stopped_at, "isoformat") else str(outreach_stopped_at),
            }

        cursor.execute("""
            UPDATE monitored_jobs
            SET outreach_stopped_at = NOW(),
                updated_at = %s
            WHERE job_id = %s
            RETURNING outreach_stopped_at
        """, (readable_ist_now(), actual_job_id))
        stopped_row = cursor.fetchone()
        conn.commit()
        cursor.close()
        conn.close()

        _invalidate_monitored_jobs_cache()

        stopped_at_value = stopped_row[0] if stopped_row else None
        logger.info(f"✅ Job {job_id} activity stopped at {stopped_at_value}")
        return {
            "status": "SUCCESS",
            "job_id": job_id,
            "stopped_at": stopped_at_value.isoformat() if hasattr(stopped_at_value, "isoformat") else str(stopped_at_value),
            "message": "Job activity stopped",
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error stopping activity for job {job_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to stop job activity: {str(e)}")


@router.put("/{job_id}/unarchive")
async def unarchive_job(job_id: str):
    """
    Unarchive a job by setting its status back to 'open'.
    """
    try:
        logger.info(f"Unarchiving job {job_id}")
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # First check if job exists (by job_id or jobdiva_id)
        cursor.execute("""
            SELECT job_id FROM monitored_jobs
            WHERE job_id = %s
            UNION ALL
            SELECT job_id FROM monitored_jobs
            WHERE jobdiva_id = %s AND job_id <> %s
            LIMIT 1
        """, (job_id, job_id, job_id))
        row = cursor.fetchone()
        if not row:
            cursor.close()
            conn.close()
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
        
        actual_job_id = row[0]  # Use the numeric job_id from database
        
        # Unarchive the job (clear is_archived flag, don't change status)
        cursor.execute("""
            UPDATE monitored_jobs 
            SET is_archived = FALSE, 
                updated_at = %s,
                archive_reason = NULL,
                archived_at = NULL
            WHERE job_id = %s
        """, (readable_ist_now(), actual_job_id))
        
        conn.commit()
        cursor.close()
        conn.close()

        _invalidate_monitored_jobs_cache()

        logger.info(f"✅ Job {job_id} unarchived successfully")
        return {
            "status": "SUCCESS",
            "job_id": job_id,
            "message": "Job unarchived successfully"
        }
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error unarchiving job {job_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to unarchive job: {str(e)}")
