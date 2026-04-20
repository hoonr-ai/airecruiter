from fastapi import APIRouter, HTTPException
from typing import List, Dict, Any, Optional
import logging

from models import JobCriteriaResponse, JobCriteriaUpdate

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/api/jobs/{job_id}/criteria", response_model=JobCriteriaResponse)
async def get_job_criteria(job_id: str):
    """Fetch criteria for a job."""
    criteria = criteria_service.get_job_criteria(job_id)
    return JobCriteriaResponse(job_id=job_id, criteria=criteria)

@router.post("/api/jobs/{job_id}/criteria/sync", response_model=JobCriteriaResponse)
async def sync_job_criteria(job_id: str):
    """Wait for criteria to be generated then return them."""
    criteria = await criteria_service.generate_and_save_criteria(job_id)
    return JobCriteriaResponse(job_id=job_id, criteria=criteria)

@router.put("/api/jobs/{job_id}/criteria")
async def update_job_criteria(job_id: str, update: JobCriteriaUpdate):
    """Manually update criteria for a job."""
    success = criteria_service.save_criteria(job_id, [c.dict() for c in update.criteria])
    if success:
        return {"status": "SUCCESS"}
    return {"status": "ERROR"}
