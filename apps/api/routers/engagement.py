from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

router = APIRouter(prefix="/candidates", tags=["Engagement"])

class EngagementRequest(BaseModel):
    candidate_id: str
    job_id: Optional[str] = None
    message: Optional[str] = None

class AssessmentRequest(BaseModel):
    candidate_id: str
    assessment_type: str = "general"

@router.post("/{candidate_id}/engage")
async def engage_candidate(candidate_id: str, request: EngagementRequest):
    """
    Initiates a prescreen workflow via the AI Interviewer.
    Currently a mock implementation until integration code is provided.
    """
    # TODO: Connect to AI Interviewer project via shared library or API
    return {
        "status": "success",
        "message": f"AI Prescreen workflow initiated for candidate {candidate_id}",
        "action": "engage",
        "detail": "Outbound reachout scheduled via AI Interviewer"
    }

@router.post("/{candidate_id}/assess")
async def assess_candidate(candidate_id: str, request: AssessmentRequest):
    """
    Sends an assessment link to the candidate.
    Mock implementation.
    """
    return {
        "status": "success",
        "message": f"Assessment ({request.assessment_type}) link sent to candidate {candidate_id}",
        "action": "assess"
    }
