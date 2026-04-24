from fastapi import APIRouter, HTTPException, Depends, Query
from typing import List, Dict, Any, Optional
import logging
import sqlalchemy

from models import (
    CandidateSearchRequest,
    CandidatesSaveRequest,
    CandidateSaveRecord,
    CandidateMessageRequest,
    SourcedCandidate,
    Skill
)
from services.jobdiva import JobDivaService
from core.config import DATABASE_URL, SUPABASE_DB_URL

logger = logging.getLogger(__name__)
router = APIRouter()

# Module-level engine singleton. v21/v22: unpooled per-request create_engine()
# leaks DB connections until Postgres hits max_connections. Pool these instead
# and bound connect_timeout so a slow DB fails fast rather than hanging workers
# for the TCP default (~2 min).
_engine: Optional[sqlalchemy.engine.Engine] = None


def _get_engine() -> sqlalchemy.engine.Engine:
    global _engine
    if _engine is None:
        db_url = DATABASE_URL or SUPABASE_DB_URL
        if not db_url:
            raise HTTPException(status_code=500, detail="Database not configured")
        _engine = sqlalchemy.create_engine(
            db_url,
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,
            pool_recycle=1800,
            connect_args={"connect_timeout": 5},
        )
    return _engine

@router.get("/job-applicants/{jobdiva_id}")
async def get_job_applicants(
    jobdiva_id: str,
    page: int = Query(1, ge=1),
    limit: int = Query(100, ge=1, le=500),
    skills: Optional[str] = Query(None, description="Comma-separated skill names for filtering"),
    extract: bool = Query(False, description="Auto-extract resume info using Azure Agent + LLM")
) -> Dict[str, Any]:
    """
    Get candidates who applied to a specific job.
    
    Set extract=true to automatically process resumes through Azure Agent 
    for skill extraction and save to candidate_enhanced_info table.
    """
    try:
        logger.info(f"Fetching job applicants for job: {jobdiva_id} (extract={extract})")
        
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
        
        # Auto-extract resume info if requested
        extracted_count = 0
        if extract and candidates:
            from services.sourced_candidates_storage import process_jobdiva_candidate
            
            logger.info(f"Auto-extracting resume info for {len(candidates)} candidates")
            for candidate in candidates:
                try:
                    if candidate.get("resume_text"):
                        await process_jobdiva_candidate(candidate)
                        extracted_count += 1
                except Exception as proc_err:
                    logger.warning(f"Failed to extract info for candidate {candidate.get('candidate_id')}: {proc_err}")
        
        # Calculate pagination info
        total = len(candidates)
        start_idx = (page - 1) * limit
        end_idx = start_idx + limit
        paginated_candidates = candidates[start_idx:end_idx]
        has_more = end_idx < total
        
        response = {
            "jobdiva_id": jobdiva_id,
            "candidates": paginated_candidates,
            "total": total,
            "page": page,
            "limit": limit,
            "has_more": has_more,
            "source": "JobDiva Job Applicants"
        }
        
        if extract:
            response["extraction_summary"] = {
                "processed": extracted_count,
                "total": len(candidates),
                "status": "completed"
            }
        
        return response
        
    except Exception as e:
        logger.error(f"Error fetching job applicants for {jobdiva_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/job-applicants/{jobdiva_id}/extract-all")
async def extract_all_job_applicants(
    jobdiva_id: str,
    background_tasks: Optional[Any] = None
) -> Dict[str, Any]:
    """
    Bulk extract resume information for all applicants of a specific job.
    
    This endpoint:
    1. Fetches all applicants for the job from JobDiva
    2. Sends each resume to Azure Agent for skill extraction
    3. Uses LLM to extract enhanced profile details
    4. Saves all data to candidate_enhanced_info table
    
    Returns immediately with a processing status. Use the GET endpoint
    to check individual candidate enhanced info.
    """
    try:
        logger.info(f"Starting bulk extraction for job applicants: {jobdiva_id}")
        
        from fastapi import BackgroundTasks
        from services.sourced_candidates_storage import process_jobdiva_candidate
        
        jobdiva_service = JobDivaService()
        
        # Get all job applicants
        candidates = await jobdiva_service.search_candidates(
            skills=[],
            location="",
            page=1,
            limit=500,  # Get maximum
            job_id=jobdiva_id
        )
        
        if not candidates:
            return {
                "status": "completed",
                "jobdiva_id": jobdiva_id,
                "message": "No applicants found for this job",
                "processed": 0,
                "total": 0
            }
        
        # Process candidates with resume text
        candidates_with_resume = [c for c in candidates if c.get("resume_text")]
        
        async def process_all_candidates():
            """Background task to process all candidates"""
            processed = 0
            failed = 0
            
            for candidate in candidates_with_resume:
                try:
                    await process_jobdiva_candidate(candidate)
                    processed += 1
                except Exception as e:
                    logger.error(f"Failed to process candidate {candidate.get('candidate_id')}: {e}")
                    failed += 1
            
            logger.info(f"Bulk extraction completed for job {jobdiva_id}: {processed} processed, {failed} failed")
        
        # Run processing in background
        import asyncio
        asyncio.create_task(process_all_candidates())
        
        return {
            "status": "processing",
            "jobdiva_id": jobdiva_id,
            "message": f"Processing {len(candidates_with_resume)} candidates in background",
            "total_candidates": len(candidates),
            "with_resume": len(candidates_with_resume),
            "check_status": f"Use GET /candidates/{{candidate_id}}/enhanced to check individual results"
        }
        
    except Exception as e:
        logger.error(f"Error starting bulk extraction for {jobdiva_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/talent-search")
async def talent_search(request: CandidateSearchRequest) -> Dict[str, Any]:
    """
    Search JobDiva talent pool based on skills and criteria (not job-specific).
    """
    try:
        logger.info(
            f"Talent search - skill_criteria: {len(request.skill_criteria)}, "
            f"title_criteria: {len(request.title_criteria)}, location: {request.location}"
        )

        # Validation
        if not request.skill_criteria and not request.title_criteria:
            raise HTTPException(
                status_code=400,
                detail="At least one search criterion must be provided (skill_criteria or title_criteria)"
            )

        jobdiva_service = JobDivaService()

        # Convert criteria to JobDiva skill format. Skips exclude match_type so
        # negative clauses don't get searched as required terms.
        search_skills = []

        for skill_crit in request.skill_criteria:
            if skill_crit.match_type == "exclude":
                continue
            priority = "Must Have" if skill_crit.match_type == "must" else "Flexible"
            search_skills.append({
                "value": skill_crit.value,
                "priority": priority,
                "years_experience": skill_crit.years or 0
            })

        # Title criteria also get fanned out as skill-like tokens — JobDiva
        # talent search treats titles as searchable text.
        for title_crit in request.title_criteria:
            if title_crit.match_type == "exclude":
                continue
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


@router.post("/{candidate_id}/extract")
async def extract_candidate_info(candidate_id: str, source: str = "JobDiva") -> Dict[str, Any]:
    """
    Extract and process candidate resume from JobDiva (applicants or talent search).
    
    - Fetches resume from JobDiva
    - Sends to Azure Agent for skill extraction
    - Uses LLM to extract enhanced profile details
    - Saves to candidate_enhanced_info table
    
    Args:
        candidate_id: The candidate ID from JobDiva
        source: Source type - "JobDiva-Applicants" or "JobDiva-TalentSearch"
    """
    try:
        logger.info(f"Extracting candidate info for {candidate_id} from {source}")
        
        from services.jobdiva import JobDivaService
        from services.sourced_candidates_storage import process_jobdiva_candidate
        
        jobdiva_service = JobDivaService()
        
        # Step 1: Fetch candidate resume from JobDiva
        candidate_data = await jobdiva_service.get_candidate_resume(candidate_id)
        
        if not candidate_data:
            raise HTTPException(
                status_code=404, 
                detail=f"Candidate {candidate_id} not found or resume not available in JobDiva"
            )
        
        resume_text = candidate_data.get("resume_text", "")
        if not resume_text or len(resume_text.strip()) < 50:
            raise HTTPException(
                status_code=400,
                detail=f"Candidate {candidate_id} has insufficient resume text for processing"
            )
        
        # Step 2: Prepare candidate object for processing
        candidate = {
            "candidate_id": candidate_id,
            "name": candidate_data.get("name", ""),
            "email": candidate_data.get("email", ""),
            "phone": candidate_data.get("phone", ""),
            "title": candidate_data.get("title", ""),
            "location": candidate_data.get("location", ""),
            "resume_text": resume_text,
            "source": source
        }
        
        # Step 3: Process candidate through Azure Agent + LLM
        logger.info(f"Processing candidate {candidate_id} through Azure Agent for skill extraction")
        result = await process_jobdiva_candidate(candidate)
        
        return {
            "status": "success",
            "candidate_id": candidate_id,
            "source": source,
            "message": "Candidate resume processed successfully",
            "data": {
                "name": result.get("name"),
                "current_title": result.get("current_title"),
                "location": result.get("location"),
                "years_experience": result.get("years_experience"),
                "skills_count": len(result.get("skills", [])),
                "skills": result.get("skills", []),
                "summary": result.get("summary")
            }
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error extracting candidate info for {candidate_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to extract candidate info: {str(e)}")


@router.get("/{candidate_id}/enhanced")
async def get_candidate_enhanced_info(candidate_id: str) -> Dict[str, Any]:
    """
    Retrieve enhanced candidate information from candidate_enhanced_info table.
    This contains AI-extracted skills, summary, experience, etc.
    """
    try:
        logger.info(f"Fetching enhanced info for candidate: {candidate_id}")

        from sqlalchemy import text

        engine = _get_engine()
        with engine.connect() as conn:
            result = conn.execute(text("""
                SELECT candidate_id, candidate_name, email, phone, job_title,
                       current_location, years_of_experience, key_skills,
                       company_experience, candidate_education,
                       candidate_certification, urls, resume_text,
                       resume_extraction_status, extracted_at
                FROM candidate_enhanced_info
                WHERE candidate_id = :candidate_id
            """), {"candidate_id": candidate_id})
            
            row = result.fetchone()
            if not row:
                raise HTTPException(
                    status_code=404, 
                    detail=f"No enhanced info found for candidate {candidate_id}. Run extract endpoint first."
                )
            
            return {
                "status": "success",
                "candidate_id": row[0],
                "candidate_name": row[1],
                "email": row[2],
                "phone": row[3],
                "job_title": row[4],
                "location": row[5],
                "years_experience": row[6],
                "skills": row[7] or [],
                "company_experience": row[8] or [],
                "candidate_education": row[9] or [],
                "candidate_certification": row[10] or [],
                "urls": row[11] or {},
                "resume_preview": row[12][:500] + "..." if row[12] and len(row[12]) > 500 else row[12],
                "resume_extraction_status": row[13],
                "extracted_at": str(row[14]) if row[14] else None,
            }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching enhanced info for {candidate_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
