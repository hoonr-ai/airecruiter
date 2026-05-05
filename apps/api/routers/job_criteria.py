from fastapi import APIRouter, HTTPException
from typing import List, Dict, Any, Optional
import logging
import json

from models import JobCriteriaResponse, JobCriteriaUpdate
from routers._helpers import get_db_connection

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/api/jobs/{job_id}/criteria", response_model=JobCriteriaResponse)
async def get_job_criteria(job_id: str):
    """Fetch criteria for a job."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT criteria FROM job_criteria WHERE job_id = %s ORDER BY updated_at DESC LIMIT 1",
                    (job_id,)
                )
                row = cur.fetchone()
                criteria = row[0] if row else []
                if isinstance(criteria, str):
                    criteria = json.loads(criteria)
                return JobCriteriaResponse(job_id=job_id, criteria=criteria or [])
    except Exception as e:
        logger.error(f"get_job_criteria failed for {job_id}: {e}")
        return JobCriteriaResponse(job_id=job_id, criteria=[])

@router.post("/api/jobs/{job_id}/criteria/sync", response_model=JobCriteriaResponse)
async def sync_job_criteria(job_id: str):
    """Return current criteria (sync no longer needed as criteria are pre-generated)."""
    return await get_job_criteria(job_id)

@router.put("/api/jobs/{job_id}/criteria")
async def update_job_criteria(job_id: str, update: JobCriteriaUpdate):
    """Manually update criteria for a job."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                criteria_data = json.dumps([c.dict() for c in update.criteria])
                cur.execute(
                    """
                    INSERT INTO job_criteria (job_id, criteria)
                    VALUES (%s, %s)
                    ON CONFLICT (job_id) DO UPDATE SET criteria = EXCLUDED.criteria, updated_at = CURRENT_TIMESTAMP
                    """,
                    (job_id, criteria_data)
                )
                conn.commit()
        return {"status": "SUCCESS"}
    except Exception as e:
        logger.error(f"update_job_criteria failed for {job_id}: {e}")
        return {"status": "ERROR"}
