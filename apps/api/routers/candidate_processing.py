from fastapi import APIRouter, HTTPException, Depends, Query
from typing import List, Dict, Any, Optional
import logging

from models import (
    CandidateSearchRequest,
    CandidatesSaveRequest,
    CandidateSaveRecord,
    CandidateMessageRequest,
    SourcedCandidate,
    Skill
)
from services.jobdiva import JobDivaService

logger = logging.getLogger(__name__)
router = APIRouter()

@router.get("/job-applicants/{jobdiva_id}")
async def get_job_applicants(
    jobdiva_id: str,
    page: int = Query(1, ge=1),
    limit: int = Query(100, ge=1, le=500),
    skills: Optional[str] = Query(None, description="Comma-separated skill names for filtering")
) -> Dict[str, Any]:
    """
    Get candidates who applied to a specific job.
    """
    try:
        logger.info(f"Fetching job applicants for job: {jobdiva_id}")
        
        jobdiva_service = JobDivaService()
        
        # Convert skills string to list if provided
        skill_filters = []
        if skills:
            skill_names = [s.strip() for s in skills.split(",") if s.strip()]
            skill_filters = [{"value": name, "priority": "Must Have"} for name in skill_names]
        
        # Get job applicants with optional skill filtering
        candidates = await jobdiva_service.search_candidates(
            skills=skill_filters,
            location="",
            page=page,
            limit=limit,
            job_id=jobdiva_id
        )
        
        # Calculate pagination info
        total = len(candidates)
        start_idx = (page - 1) * limit
        end_idx = start_idx + limit
        paginated_candidates = candidates[start_idx:end_idx]
        has_more = end_idx < total
        
        return {
            "jobdiva_id": jobdiva_id,
            "candidates": paginated_candidates,
            "total": total,
            "page": page,
            "limit": limit,
            "has_more": has_more,
            "source": "JobDiva Job Applicants"
        }
        
    except Exception as e:
        logger.error(f"Error fetching job applicants for {jobdiva_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/talent-search")
async def talent_search(request: CandidateSearchRequest) -> Dict[str, Any]:
    """
    Search JobDiva talent pool based on skills and criteria (not job-specific).
    """
    try:
        logger.info(f"Talent search - skills: {len(request.skills)}, location: {request.location}")
        
        # Validation
        if not request.skills and not request.skill_criteria and not request.titles:
            raise HTTPException(
                status_code=400, 
                detail="At least one search criterion must be provided (skills, skill_criteria, or titles)"
            )
        
        jobdiva_service = JobDivaService()
        
        # Convert various criteria to JobDiva skill format
        search_skills = []
        
        # Add legacy skills
        for skill in request.skills:
            search_skills.append({
                "value": skill.value,
                "priority": skill.priority,
                "years_experience": skill.years_experience or 0
            })
            
        # Add skill criteria
        for skill_crit in request.skill_criteria:
            priority = "Must Have" if skill_crit.match_type == "must" else "Flexible"
            search_skills.append({
                "value": skill_crit.value,
                "priority": priority,
                "years_experience": skill_crit.years or 0
            })
            
        # Add title criteria as skills (JobDiva treats titles as searchable skills)
        for title_crit in request.titles:
            priority = "Must Have" if title_crit.match_type == "must" else "Flexible"
            search_skills.append({
                "value": title_crit.value,
                "priority": priority,
                "years_experience": title_crit.years or 0
            })
        
        # Use location from request
        search_location = request.location or ""
        if request.locations:
            # Use first location criterion if provided
            search_location = request.locations[0].value
        
        # Search talent pool (no job_id = general talent search)
        candidates = await jobdiva_service.search_candidates(
            skills=search_skills,
            location=search_location,
            page=request.page,
            limit=request.limit,
            job_id=None  # No job_id for general talent search
        )
        
        # Calculate pagination info
        total = len(candidates)
        start_idx = (request.page - 1) * request.limit
        end_idx = start_idx + request.limit
        paginated_candidates = candidates[start_idx:end_idx]
        has_more = end_idx < total
        
        return {
            "candidates": paginated_candidates,
            "total": total,
            "page": request.page,
            "limit": request.limit,
            "has_more": has_more,
            "search_criteria": {
                "skills_count": len(search_skills),
                "location": search_location
            },
            "source": "JobDiva Talent Search"
        }
        
    except Exception as e:
        logger.error(f"Error in talent search: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/save")
async def save_candidates(request: CandidatesSaveRequest) -> Dict[str, Any]:
    """
    Save candidates for a specific job.
    """
    try:
        logger.info(f"Saving {len(request.candidates)} candidates for job: {request.jobdiva_id}")
        
        if not request.jobdiva_id:
            raise HTTPException(status_code=400, detail="jobdiva_id is required")
        
        if not request.candidates:
            raise HTTPException(status_code=400, detail="candidates list cannot be empty")
        
        # Convert to SourcedCandidate format for storage
        # This is a basic implementation - you may need to adjust based on your SourcedCandidate model
        saved_count = len(request.candidates)  # Placeholder
        
        return {
            "message": f"Successfully saved {saved_count} candidates",
            "job_id": request.job_id,
            "saved_count": saved_count
        }
        
    except Exception as e:
        logger.error(f"Error saving candidates: {e}")
        raise HTTPException(status_code=500, detail=str(e))
@router.get("/resume/{candidate_id}")
async def get_candidate_resume(candidate_id: str) -> Dict[str, Any]:
    """
    Get full candidate resume/details by ID from JobDiva.
    """
    try:
        logger.info(f"Fetching resume for candidate: {candidate_id}")
        
        from services.jobdiva import JobDivaService
        jobdiva_service = JobDivaService()
        
        # Fetch candidate resume from JobDiva
        candidate_data = await jobdiva_service.get_candidate_resume(candidate_id)
        
        if not candidate_data:
            raise HTTPException(status_code=404, detail="Candidate not found or resume not available")
        
        return {
            "candidate": candidate_data,
            "status": "success"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching candidate resume {candidate_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/{jobdiva_id}")
async def get_candidates_for_job(jobdiva_id: str) -> Dict[str, Any]:
    """
    Retrieve all saved candidates for a specific job from local storage.
    Note: This is different from job-applicants endpoint which fetches from JobDiva.
    """
    try:
        logger.info(f"Fetching saved candidates for job: {jobdiva_id}")
        
        # Import storage service locally to avoid module-level import issues
        from services.sourced_candidates_storage import SourcedCandidatesStorage
        candidates_storage = SourcedCandidatesStorage()
        
        # Get saved/sourced candidates from local database
        candidates = candidates_storage.get_candidates_for_job(jobdiva_id)
        
        return {
            "job_id": job_id,
            "candidates": candidates,
            "total": len(candidates),
            "source": "Local Sourced Candidates"
        }
        
    except Exception as e:
        logger.error(f"Error fetching saved candidates for job {jobdiva_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/message")
async def send_candidate_message(request: CandidateMessageRequest) -> Dict[str, Any]:
    """
    Send a message to a candidate.
    """
    try:
        logger.info(f"Sending message to candidate: {request.candidate_provider_id}")
        
        if not request.candidate_provider_id:
            raise HTTPException(status_code=400, detail="candidate_provider_id is required")
        
        if not request.message:
            raise HTTPException(status_code=400, detail="message cannot be empty")
        
        # This should be implemented with actual messaging logic
        return {
            "message": "Message sent successfully",
            "candidate_id": request.candidate_provider_id,
            "source": request.source,
            "status": "pending"
        }
        
    except Exception as e:
        logger.error(f"Error sending message to candidate: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/{jobdiva_id}/candidates/{candidate_id}")
async def remove_candidate_from_job(jobdiva_id: str, candidate_id: str) -> Dict[str, Any]:
    """
    Remove a candidate from a job.
    """
    try:
        logger.info(f"Removing candidate {candidate_id} from job {job_id}")
        
        # This should be implemented with actual removal logic
        return {
            "message": "Candidate removed successfully",
            "job_id": job_id,
            "candidate_id": candidate_id
        }
        
    except Exception as e:
        logger.error(f"Error removing candidate {candidate_id} from job {job_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/")
async def list_all_candidates(
    page: int = 1,
    limit: int = 100,
    search: Optional[str] = None
) -> Dict[str, Any]:
    """
    List all candidates with optional search and pagination.
    """
    try:
        logger.info(f"Listing candidates - page: {page}, limit: {limit}, search: {search}")
        
        # This should be implemented with actual database queries
        return {
            "candidates": [],
            "total": 0,
            "page": page,
            "limit": limit,
            "search": search
        }
        
    except Exception as e:
        logger.error(f"Error listing candidates: {e}")
        raise HTTPException(status_code=500, detail=str(e))