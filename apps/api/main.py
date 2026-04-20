from fastapi import FastAPI, HTTPException, BackgroundTasks, Body, Query
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Dict, Any, Optional
import asyncio
import json
import logging
import os
import psycopg2
import psycopg2.extras
from psycopg2.extras import RealDictCursor
import httpx
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import text
from contextlib import asynccontextmanager

from core import (
    OPENAI_API_KEY, DATABASE_URL, 
    JOBDIVA_JOB_NOTES_UDF_ID, ALLOWED_ORIGINS
)

# Load environment variables (core handles .env, but keeping load_dotenv for compatibility)
load_dotenv()

# Helper function for readable IST timestamps
def readable_ist_now() -> str:
    """Returns current IST time in readable format: 2026-02-24 16:25:59 IST"""
    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist).strftime("%Y-%m-%d %H:%M:%S IST")

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

from services.ai_service import ai_service
from models import (
    JobDescription, MatchResult, ParsedJobRequest, ParsedJobResponse,
    ChatRequest, ChatResponse, CandidateSearchRequest, CandidateMessageRequest, CandidatesSaveRequest, JobFetchRequest,
    CandidateAnalysisRequest, CandidateAnalysisResponse, JobCriterion, JobCriteriaResponse,
    JobCriteriaUpdate, JobDraftData, JobDraftRequirement, JobDraftRequirements, 
    JobDraftResponse, JobPublishRequest, JobBasicInfoUpdate, SkillsExtractionRequest, SkillsExtractionResponse,
    JobSkillsSummaryResponse, JobSyncFiltersRequest
)
from matcher import mock_match_candidates
from services.extractor import llm_extractor
from services.jobdiva import jobdiva_service
from services.unipile import unipile_service
from services.chat_service import chat_service
from services.monitored_jobs_storage import MonitoredJobsStorage
from services.job_rubric_db import JobRubricDB

# Legacy file-based tracking replaced by monitored_jobs SQL table

# Global scheduler
scheduler = AsyncIOScheduler()

def schedule_next_poll():
    """Schedule next poll 5 minutes from now, canceling any existing poll"""
    try:
        # Remove existing scheduled poll if any
        if scheduler.get_job("job_status_poll"):
            scheduler.remove_job("job_status_poll")
        
        # Schedule new poll 5 minutes from now
        scheduler.add_job(
            poll_all_jobs,
            "date",
            run_date=datetime.now(timezone(timedelta(hours=5, minutes=30))) + timedelta(minutes=5),
            id="job_status_poll",
            replace_existing=True
        )
        
        next_run = datetime.now(timezone(timedelta(hours=5, minutes=30))) + timedelta(minutes=5)
        logger.info(f"🔄 Next auto-poll scheduled for: {next_run.strftime('%Y-%m-%d %H:%M:%S IST')}")
    except Exception as e:
        logger.error(f"Failed to schedule next poll: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle - start/stop scheduler"""
    # Startup
    logger.info("🚀 Starting dynamic job status monitoring scheduler...")
    
    scheduler.start()
    # Schedule first poll 5 minutes from now
    schedule_next_poll()
    
    yield
    
    # Shutdown
    logger.info("📋 Stopping scheduler...")
    scheduler.shutdown()
    
from routers import engagement, ai_generation, voice_agent, boolean_agent, candidate_processing, job_archive

app = FastAPI(title="Hoonr.ai API", lifespan=lifespan)
app.include_router(ai_generation.router, prefix="/api/v1/ai-generation")
app.include_router(ai_generation.router, prefix="/api/v1/gemini")
app.include_router(voice_agent.router, prefix="/api/v1/voice")
app.include_router(boolean_agent.router, prefix="/api/v1/boolean")
app.include_router(candidate_processing.router, prefix="/api/v1/candidates")
app.include_router(job_archive.router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.post("/jobs/parse", response_model=ParsedJobResponse)
async def parse_job_description(request: ParsedJobRequest):
    """
    Parses raw text JD into structured format (skills, location, etc). 
    Also saves processed data to monitored_jobs if job_id is provided.
    """
    try:
        data = await llm_extractor.extract_from_jd(request.text)
        # Step 2 Parse: READ-ONLY (No DB population for discrete skills/roles)
        # This allows the recruiter to edit the AI JD before grounding is triggered.
        if request.job_id:
            storage = MonitoredJobsStorage()
            processing_metadata = {
                "model": "gpt-4o-mini",  # LLM extractor now strictly uses OpenAI
                "processing_time_ms": 0,
                "tokens_used": 0,
                "confidence": 0.95
            }
            # Still update the monitored_jobs master record with the initial enhanced text
            storage.update_job_with_extracted_data(
                job_id=request.job_id,
                extracted_data=data,
                processing_metadata=processing_metadata
            )
            logger.info(f"✅ Step 2: Generated initial AI JD for {request.job_id} (Discrete tables not yet populated)")
        
        return ParsedJobResponse(
            title=data.title,
            summary=data.summary,
            hard_skills=data.hard_skills,
            soft_skills=data.soft_skills,
            experience_level=data.experience_level,
            location_type=data.location_type
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/candidates/search")
async def search_jobdiva_candidates(request: CandidateSearchRequest):
    """
    Unified candidate search with hierarchical skills/titles and intelligent resume processing.
    
    TIER 1: JobDiva Job Applicants with hierarchical matching
    TIER 2: TalentSearch Pool with hierarchical matching
    
    Features:
    - Hierarchical skill/title matching from taxonomy
    - Smart resume deduplication and extraction tracking
    - Company experience matching from resume text
    - Two-pool search strategy with prioritization
    """
    logger.info(f"🔍 Unified candidate search for job_id: {request.job_id}")
    
    if not request.job_id:
        return {"candidates": [], "message": "job_id required for candidate search"}
    
    try:
        from services.unified_candidate_search import unified_search_service, SearchCriteria
        
        # Convert request to search criteria
        titles = []
        skills = []
        
        # Extract titles from various sources
        combined_title_criteria = (request.title_criteria or []) + (request.titles or [])
        if combined_title_criteria:
            titles.extend([t.value for t in combined_title_criteria if t.match_type != 'exclude'])
        
        # Extract skills from various sources 
        if request.skill_criteria:
            skills.extend([s.value for s in request.skill_criteria if s.match_type != 'exclude'])
        if request.skills:
            skills.extend([s.value for s in request.skills])
        
        # Extract location
        location = ""
        if request.locations:
            location = request.locations[0].value
        elif request.location:
            location = request.location
        
        # Extract companies
        companies = request.companies or []
        
        # Load resume match filters from database if not provided in request
        resume_match_filters = []
        if request.resume_match_filters and len(request.resume_match_filters) > 0:
            # Use filters from request if provided
            resume_match_filters = [f.dict() for f in request.resume_match_filters]
            logger.info(f"Using {len(resume_match_filters)} resume match filters from request")
        else:
            # Load from database
            try:
                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT resume_match_filters FROM monitored_jobs WHERE job_id = %s OR jobdiva_id = %s LIMIT 1",
                    (request.job_id, request.job_id)
                )
                row = cursor.fetchone()
                if row and row[0]:
                    resume_match_filters = row[0] if isinstance(row[0], list) else json.loads(row[0])
                    logger.info(f"Loaded {len(resume_match_filters)} resume match filters from database for job {request.job_id}")
                cursor.close()
                conn.close()
            except Exception as e:
                logger.warning(f"Failed to load resume match filters from database: {e}")
        
        # Build search criteria
        criteria = SearchCriteria(
            job_id=request.job_id,
            titles=titles,
            skills=skills,
            title_criteria=[t.dict() for t in combined_title_criteria],
            skill_criteria=[s.dict() for s in request.skill_criteria],
            keywords=request.keywords or [],
            resume_match_filters=resume_match_filters,
            location=location,
            within_miles=25,  # Default radius
            companies=companies,
            page_size=request.limit or 100,
            sources=request.sources or ["JobDiva"],
            open_to_work=request.open_to_work,
            boolean_string=request.boolean_string or ""
        )
        
        # Execute unified search as a stream
        async def stream_candidates():
            try:
                async for event in unified_search_service.search_candidates(criteria):
                    yield json.dumps(event) + "\n"
            except Exception as e:
                logger.error(f"Error in search stream: {e}", exc_info=True)
                yield json.dumps({"type": "error", "message": str(e)}) + "\n"

        return StreamingResponse(
            stream_candidates(),
            media_type="application/x-ndjson"
        )
        
    except Exception as e:
        logger.error(f"❌ Unified search failed: {e}")
        # Fallback to original search logic
        try:
            from services.jobdiva import JobDivaService
            jobdiva_service = JobDivaService()
            
            # Lightweight fallback: do not hydrate every applicant during search.
            token = await jobdiva_service.authenticate()
            candidates = await jobdiva_service._get_all_job_applicants(
                request.job_id,
                request.limit or 100,
                token
            ) if token else []
            for candidate in candidates:
                candidate["source"] = "JobDiva-Applicants"
            
            return {
                "candidates": candidates[:request.limit or 100],
                "total": len(candidates),
                "job_applicants": len(candidates),
                "talent_pool": 0,
                "message": f"Found {len(candidates)} candidates using fallback search (unified search failed)"
            }
            
        except Exception as fallback_error:
            logger.error(f"❌ Fallback search also failed: {fallback_error}")
            raise HTTPException(status_code=500, detail=f"Search failed: {str(e)}")

@app.post("/candidates/search/legacy")
async def search_jobdiva_candidates_legacy(request: CandidateSearchRequest):
    """
    Legacy search endpoint (preserved for backward compatibility)
    
    Enhanced multi-criteria candidate search with separate title, skill, and location filtering.
    
    TIER 1: JobDiva Job Applicants filtered by titles, skills, and locations
    TIER 2: TalentSearch Pool filtered by same criteria
    
    Supports both legacy format (combined skills array) and enhanced format (separate criteria).
    """
    print(f"🔍 Enhanced multi-criteria search for job_id: {request.job_id}")
    
    if not request.job_id:
        return {"candidates": [], "message": "jobdiva_id required for candidate search"}
    
    try:
        combined_results = []
        applicant_count = 0
        talent_pool_count = 0
        
        # Parse filtering criteria - support both legacy and enhanced formats
        title_filters = []
        skill_filters = []
        location_filters = []
        
        # Enhanced format: separate title, skill, location criteria
        if request.titles:
            title_filters = [t for t in request.titles if t.match_type != 'exclude']
        if request.skill_criteria:
            skill_filters = [s for s in request.skill_criteria if s.match_type != 'exclude']  
        if request.locations:
            location_filters = [l for l in request.locations]
            
        # Legacy format: extract from skills array (for backward compatibility)
        legacy_skills = []
        if request.skills:
            for skill in request.skills:
                legacy_skills.append({
                    "value": skill.value,
                    "priority": skill.priority,
                    "years_experience": skill.years_experience or 0
                })
        
        # Use location from either enhanced or legacy format
        primary_location = ""
        if location_filters:
            primary_location = location_filters[0].value
        elif request.location:
            primary_location = request.location
            
        print(f"📋 Search criteria - Titles: {len(title_filters)}, Skills: {len(skill_filters)}, Locations: {len(location_filters)}")
        
        # TIER 1: JobDiva Job Applicants with enhanced filtering
        print("🎯 TIER 1: Searching job applicants with enhanced resume fetching...")
        try:
            # For job applicant search, use enhanced method to get full resume text
            if not title_filters and not skill_filters and not legacy_skills:
                # Simple applicant search - use enhanced method for complete data
                print("📝 Using enhanced job applicants method (no complex filters)")
                applicants = await jobdiva_service.get_enhanced_job_candidates(request.job_id)
                
                # Filter by location if provided
                if location_filters or primary_location:
                    filtered_applicants = []
                    search_location = location_filters[0].value if location_filters else primary_location
                    for candidate in applicants:
                        candidate_location = candidate.get("location", "").lower()
                        if search_location.lower() in candidate_location or candidate_location in search_location.lower():
                            filtered_applicants.append(candidate)
                    applicants = filtered_applicants
                    
            else:
                # Complex criteria search - use existing enhanced filtering
                applicants = await jobdiva_service.search_job_candidates_enhanced(
                    job_id=request.job_id,
                    title_criteria=title_filters,
                    skill_criteria=skill_filters, 
                    location_criteria=location_filters,
                    legacy_skills=legacy_skills  # Fallback to legacy format
                )
                
            applicant_count = len(applicants)
            print(f"✅ Found {applicant_count} job applicants matching criteria")
            
            # Mark applicants as priority source
            for candidate in applicants:
                candidate["source"] = "JobDiva-Applicants"
                candidate["priority"] = True
            combined_results.extend(applicants)
            
        except Exception as e:
            print(f"⚠️ Job applicants search failed: {e}")
            # Fallback to legacy search if enhanced search fails
            try:
                applicants = await jobdiva_service.search_candidates(
                    skills=request.skills or legacy_skills,
                    location=primary_location,
                    job_id=request.job_id
                )
                applicant_count = len(applicants)
                for candidate in applicants:
                    candidate["source"] = "JobDiva-Applicants"
                    candidate["priority"] = True
                combined_results.extend(applicants)
                print(f"✅ Fallback search found {applicant_count} applicants")
            except Exception as fallback_e:
                print(f"❌ Fallback search also failed: {fallback_e}")
                applicant_count = 0
        
        # TIER 2: TalentSearch Pool with enhanced filtering
        # Only search if we need more candidates and have search criteria
        if (title_filters or skill_filters or legacy_skills) and (applicant_count < request.limit):
            print("🌐 TIER 2: Searching talent pool with multi-criteria filters...")
            try:
                remaining_limit = max(0, request.limit - applicant_count)
                
                talent_pool = await jobdiva_service.search_talent_pool_enhanced(
                    title_criteria=title_filters,
                    skill_criteria=skill_filters,
                    location_criteria=location_filters,
                    legacy_skills=legacy_skills,
                    page=request.page,
                    limit=remaining_limit if remaining_limit > 0 else request.limit
                )
                talent_pool_count = len(talent_pool)
                print(f"✅ Found {talent_pool_count} additional candidates from talent pool")
                
                # Mark talent pool as secondary source  
                for candidate in talent_pool:
                    candidate["source"] = "JobDiva-TalentSearch"
                    candidate["priority"] = False
                combined_results.extend(talent_pool)
                
            except Exception as e:
                print(f"⚠️ Enhanced talent pool search failed: {e}")
                # Fallback to legacy talent search
                try:
                    talent_pool = await jobdiva_service.search_candidates(
                        skills=request.skills or legacy_skills,
                        location=primary_location,
                        page=request.page,
                        limit=remaining_limit if remaining_limit > 0 else request.limit,
                        job_id=None
                    )
                    talent_pool_count = len(talent_pool)
                    for candidate in talent_pool:
                        candidate["source"] = "JobDiva-TalentSearch"
                        candidate["priority"] = False
                    combined_results.extend(talent_pool)
                    print(f"✅ Fallback talent search found {talent_pool_count} candidates")
                except Exception as fallback_e:
                    print(f"❌ Fallback talent search also failed: {fallback_e}")
                    talent_pool_count = 0
        
        # Summary
        total_found = len(combined_results)
        criteria_summary = []
        if title_filters: criteria_summary.append(f"{len(title_filters)} title criteria")
        if skill_filters: criteria_summary.append(f"{len(skill_filters)} skill criteria")  
        if location_filters: criteria_summary.append(f"{len(location_filters)} location criteria")
        
        message = f"Found {total_found} candidates"
        if criteria_summary:
            message += f" matching {', '.join(criteria_summary)}"
        if applicant_count > 0 and talent_pool_count > 0:
            message += f" ({applicant_count} job applicants + {talent_pool_count} from talent pool)"
        elif applicant_count > 0:
            message += f" ({applicant_count} job applicants)"
        elif talent_pool_count > 0:
            message += f" ({talent_pool_count} from talent pool)"
        
        # Deduplicate candidates by email or name+location
        print("🔄 Deduplicating candidates...")
        seen_candidates = {}
        deduplicated_results = []
        
        for candidate in combined_results:
            # Create unique key based on email (preferred) or name+location
            email_key = candidate.get("email", "").lower().strip()
            name_location_key = f"{candidate.get('firstName', '').lower()}_{candidate.get('lastName', '').lower()}_{candidate.get('location', '').lower()}"
            
            # Use email as primary key, fallback to name+location
            unique_key = email_key if email_key else name_location_key
            
            if unique_key and unique_key not in seen_candidates:
                # First time seeing this candidate
                seen_candidates[unique_key] = candidate
                deduplicated_results.append(candidate)
            elif unique_key in seen_candidates:
                # Duplicate found - prefer JobDiva-Applicants over TalentSearch
                existing = seen_candidates[unique_key]
                current_source = candidate.get("source", "")
                existing_source = existing.get("source", "")
                
                if current_source == "JobDiva-Applicants" and existing_source != "JobDiva-Applicants":
                    # Replace with job applicant version (higher priority)
                    seen_candidates[unique_key] = candidate
                    # Replace in results list
                    for i, result in enumerate(deduplicated_results):
                        if result == existing:
                            deduplicated_results[i] = candidate
                            break
        
        dedup_count = len(combined_results) - len(deduplicated_results)
        if dedup_count > 0:
            print(f"🔄 Removed {dedup_count} duplicate candidates")
            message += f" (removed {dedup_count} duplicates)"
        
        print(f"🎯 SEARCH COMPLETE: {message}")
        
        return {"candidates": deduplicated_results, "message": message}
        
    except Exception as e:
        print(f"❌ Enhanced candidate search failed: {e}")
        return {"candidates": [], "message": f"Search failed: {str(e)}"}

@app.post("/candidates/message")
async def message_candidate(request: CandidateMessageRequest):
    """
    Sends a message to a candidate via the specified source provider.
    Currently supports: LinkedIn (via Unipile).
    """
    if request.source == "LinkedIn":
        success = await unipile_service.send_message(request.candidate_provider_id, request.message)
        if success:
            return {"status": "success", "detail": "Message queued/sent via LinkedIn"}
        else:
            raise HTTPException(status_code=500, detail="Failed to send LinkedIn message")
            
    elif request.source in ["JobDiva", "VettedDB", "Email"]:
        # Mock Email Send (Log it)
        print(f"📧 EMAIL OUTREACH: Sending email to candidate {request.candidate_provider_id}")
        print(f"📧 Subject: (Auto-generated)")
        print(f"📧 Body: {request.message}")
        return {"status": "success", "detail": f"Email simulation successful for {request.source}"}
        
    else:
        raise HTTPException(status_code=400, detail=f"Messaging not supported for source: {request.source}")

@app.get("/jobs/{job_id}/candidates")
async def get_job_candidates(job_id: str, background_tasks: BackgroundTasks):
    """
    Fetches all sourced candidates tied to a specific job.
    Also triggers a background sync to auto-assign any new organic JobDiva applicants.
    """
    try:
        from services.sourced_candidates_storage import sourced_candidates_storage
        candidates = sourced_candidates_storage.get_candidates_for_job(job_id)
        
        # Trigger an auto-sync for any new applicants who applied since the last check
        # We reuse the logic from the auto-assign endpoint by invoking it internally.
        # This gives us a seamless organic sync on view.
        try:
            # We must await the execution of the route handler to get its return dict,
            # which adds the task to `background_tasks`.
            await auto_assign_applicants(job_id, background_tasks)
        except Exception as bg_err:
            logger.warning(f"Failed to trigger auto-assign background task for job {job_id}: {bg_err}")

        return {"status": "success", "candidates": candidates}
    except Exception as e:
        logger.error(f"Error fetching candidates for job {job_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/jobs/{job_id}/auto-assign-applicants")
async def auto_assign_applicants(job_id: str, background_tasks: BackgroundTasks):
    """
    Triggered after 'Launch PAIR'. Fetches all JobDiva applicants for this job,
    scores them using the job's resume match filters, and upserts each one into
    sourced_candidates with their match_score so they appear in the Rankings page.

    Runs as a background task — returns immediately so the UI is not blocked.
    """
    async def _run_auto_assign(job_id: str):
        try:
            logger.info(f"🤖 [AutoAssign] Starting background applicant assignment for job {job_id}")

            from services.unified_candidate_search import SearchCriteria
            from services.unified_candidate_search import unified_search_service
            import psycopg2
            from core.config import DATABASE_URL

            # 1. Load job rubric / resume_match_filters from DB
            resume_match_filters = []
            sourcing_filters = {}
            jobdiva_numeric_id = None
            
            try:
                conn_lookup = get_db_connection()
                cur_lookup = conn_lookup.cursor()
                cur_lookup.execute(
                    "SELECT resume_match_filters, sourcing_filters, jobdiva_id FROM monitored_jobs "
                    "WHERE job_id = %s OR jobdiva_id = %s LIMIT 1",
                    (job_id, job_id)
                )
                row = cur_lookup.fetchone()
                if row:
                    resume_match_filters = row[0] if isinstance(row[0], list) else (json.loads(row[0]) if row[0] else [])
                    sourcing_filters = row[1] if isinstance(row[1], dict) else (json.loads(row[1]) if row[1] else {})
                    jobdiva_numeric_id = row[2]
                cur_lookup.close()
                conn_lookup.close()
            except Exception as e:
                logger.warning(f"[AutoAssign] Could not load rubric filters for job {job_id}: {e}")

            # Use the resolved jobdiva_numeric_id if available, otherwise fallback to passed job_id
            search_job_id = jobdiva_numeric_id if jobdiva_numeric_id else job_id
            logger.info(f"🤖 [AutoAssign] Using JobDiva ID {search_job_id} for applicant search")

            # 2. Build minimal SearchCriteria pointing ONLY at applicants pool
            title_criteria = []
            if sourcing_filters.get("titles"):
                title_criteria = [
                    {"value": t.get("value", ""), "match_type": t.get("matchType", "must"), "years": t.get("years", 0),
                     "recent": t.get("recent", False), "similar_terms": t.get("selectedSimilarTitles") or []}
                    for t in (sourcing_filters.get("titles") or [])
                ]
            
            skill_criteria = []
            if sourcing_filters.get("skills"):
                skill_criteria = [
                    {"value": s.get("value", ""), "match_type": s.get("matchType", "must"), "years": s.get("years", 0),
                     "recent": s.get("recent", False), "similar_terms": s.get("selectedSimilarSkills") or []}
                    for s in (sourcing_filters.get("skills") or [])
                ]
            
            primary_location = ""
            locs = sourcing_filters.get("locations") or []
            if locs:
                primary_location = locs[0].get("value", "")

            criteria = SearchCriteria(
                job_id=search_job_id,
                title_criteria=title_criteria,
                skill_criteria=skill_criteria,
                keywords=sourcing_filters.get("keywords") or [],
                companies=sourcing_filters.get("companies") or [],
                resume_match_filters=resume_match_filters,
                location=primary_location,
                page_size=500,
                sources=["JobDiva"],
                bypass_screening=True,
            )

            # 3. Stream candidates through the unified search (applicants path)
            total_assigned = 0
            with psycopg2.connect(DATABASE_URL) as conn:
                with conn.cursor() as cur:
                    async for event in unified_search_service.search_candidates(criteria):
                        if event.get("type") != "candidate":
                            continue
                        cand = event["data"]
                        try:
                            # Use the job_id (UUID) for DB linking to ensure it matches the monitored job
                            db_job_id = job_id 
                            candidate_id = str(cand.get("candidate_id") or cand.get("id") or "")
                            
                            candidate_data_json = json.dumps({
                                "skills": cand.get("skills") or [],
                                "experience_years": cand.get("experience_years") or 0,
                                "education": cand.get("enhanced_info", {}).get("candidate_education") or [],
                                "certifications": cand.get("enhanced_info", {}).get("candidate_certification") or [],
                                "company_experience": cand.get("enhanced_info", {}).get("company_experience") or [],
                                "urls": cand.get("enhanced_info", {}).get("urls") or {},
                                "is_selected": True,
                                "match_score": cand.get("match_score") or 0,
                                "missing_skills": cand.get("missing_skills") or [],
                                "matched_skills": cand.get("matched_skills") or [],
                                "explainability": cand.get("explainability") or "",
                                "match_score_details": cand.get("match_score_details") or {},
                                "enhanced_info": cand.get("enhanced_info"),
                                "auto_assigned": True,
                            })
                            
                            cur.execute("""
                                INSERT INTO sourced_candidates (
                                    jobdiva_id, candidate_id, source, name, email, phone,
                                    headline, location, resume_text, data, status,
                                    resume_match_percentage, updated_at
                                ) VALUES (
                                    %s, %s, %s, %s, %s, %s,
                                    %s, %s, %s, %s, %s,
                                    %s, CURRENT_TIMESTAMP
                                )
                                ON CONFLICT (jobdiva_id, candidate_id, source) DO UPDATE SET
                                    name       = EXCLUDED.name,
                                    email      = EXCLUDED.email,
                                    phone      = EXCLUDED.phone,
                                    headline   = EXCLUDED.headline,
                                    location   = EXCLUDED.location,
                                    resume_text= EXCLUDED.resume_text,
                                    data       = EXCLUDED.data,
                                    status     = EXCLUDED.status,
                                    resume_match_percentage= EXCLUDED.resume_match_percentage,
                                    updated_at = CURRENT_TIMESTAMP
                            """, (
                                db_job_id,
                                candidate_id,
                                cand.get("source", "JobDiva-Applicants"),
                                cand.get("name") or "",
                                cand.get("email"),
                                cand.get("phone"),
                                cand.get("headline") or cand.get("title"),
                                cand.get("location"),
                                cand.get("resume_text"),
                                candidate_data_json,
                                "sourced",
                                cand.get("match_score") or 0,
                            ))
                            total_assigned += 1
                            # Commit per batch or periodically if needed, but here we do it after each row for safety in background
                            conn.commit()
                        except Exception as row_err:
                            logger.warning(f"[AutoAssign] Failed to upsert candidate {cand.get('candidate_id')}: {row_err}")

            logger.info(f"✅ [AutoAssign] Auto-assigned {total_assigned} applicants for job {job_id}")

        except Exception as e:
            logger.error(f"❌ [AutoAssign] Background task failed for job {job_id}: {e}", exc_info=True)

    background_tasks.add_task(_run_auto_assign, job_id)
    return {
        "status": "accepted",
        "message": f"Auto-assignment of JobDiva applicants started for job {job_id}",
        "job_id": job_id,
    }

@app.post("/jobs/{job_id}/sync-filters")
async def sync_job_filters(job_id: str, request: JobSyncFiltersRequest):
    """
    Called before 'Launch PAIR' or during search to ensure the database has the latest
    rubric and sourcing filters. This allows background tasks to use the correct screening criteria.
    """
    try:
        logger.info(f"🔄 Syncing filters for job {job_id}")
        
        with psycopg2.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                # Update monitored_jobs with provided filters
                cur.execute("""
                    UPDATE monitored_jobs 
                    SET resume_match_filters = %s,
                        sourcing_filters = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE job_id = %s OR jobdiva_id = %s
                """, (
                    json.dumps(request.resume_match_filters) if request.resume_match_filters is not None else None,
                    json.dumps(request.sourcing_filters) if request.sourcing_filters is not None else None,
                    job_id, job_id
                ))
                
                if cur.rowcount == 0:
                    logger.warning(f"⚠️ [SyncFilters] No job found to update for ID {job_id}")
                else:
                    logger.info(f"✅ [SyncFilters] Successfully updated filters for job {job_id}")
                
                conn.commit()
                
        return {"status": "success", "message": "Filters synced successfully"}
    except Exception as e:
        logger.error(f"❌ [SyncFilters] Failed to sync filters for job {job_id}: {e}")
        return {"status": "error", "message": str(e)}



@app.post("/candidates/save")
async def save_candidates(request: CandidatesSaveRequest):
    """
    Saves a batch of candidates to the sourced_candidates table.
    """
    try:
        print(f"🔄 Saving {len(request.candidates)} candidates for job: {request.jobdiva_id}")
        
        # Filter only selected candidates for saving
        selected_candidates = [c for c in request.candidates if c.is_selected]
        print(f"📝 Saving {len(selected_candidates)} selected candidates out of {len(request.candidates)} total")
        
        for idx, c in enumerate(selected_candidates):
            print(f"   Selected Candidate {idx+1}: {c.name} (ID: {c.candidate_id}, Source: {c.source})")
        
        import psycopg2
        import json
        from core.config import DATABASE_URL
        
        saved_count = 0
        processing_payloads = []
        
        with psycopg2.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                for c in selected_candidates:
                    try:
                        # Prepare candidate data with clean schema
                        candidate_data = {
                            "jobdiva_id": request.jobdiva_id,
                            "candidate_id": c.candidate_id,
                            "source": c.source,
                            "name": c.name,
                            "email": getattr(c, 'email', None),
                            "phone": getattr(c, 'phone', None),
                            "headline": getattr(c, 'headline', None) or getattr(c, 'title', None),
                            "location": getattr(c, 'location', None),
                            "profile_url": getattr(c, 'profile_url', None),
                            "image_url": getattr(c, 'image_url', None),
                            "resume_id": getattr(c, 'resume_id', None),
                            "resume_text": getattr(c, 'resume_text', None),
                            "data": json.dumps({
                                "skills": c.skills or [],
                                "experience_years": c.experience_years or 0,
                                "education": getattr(c, 'education', []) or getattr(c, 'candidate_education', []),
                                "certifications": getattr(c, 'certifications', []) or getattr(c, 'candidate_certification', []),
                                "company_experience": getattr(c, 'company_experience', []),
                                "urls": getattr(c, 'urls', {}),
                                "is_selected": True,
                                "match_score": getattr(c, 'match_score', 0),
                                "enhanced_info": getattr(c, 'enhanced_info', None)  # Full LLM extraction data
                            }),
                            "status": "sourced",
                            "resume_match_percentage": getattr(c, 'match_score', 0)
                        }
                        
                        cur.execute("""
                            INSERT INTO sourced_candidates (
                                jobdiva_id, candidate_id, source, name, email, phone, headline, location, 
                                profile_url, image_url, resume_id, resume_text, data, status, 
                                resume_match_percentage, updated_at
                            ) VALUES (
                                %(jobdiva_id)s, %(candidate_id)s, %(source)s, %(name)s, %(email)s, %(phone)s, %(headline)s, %(location)s,
                                %(profile_url)s, %(image_url)s, %(resume_id)s, %(resume_text)s, %(data)s, %(status)s, 
                                %(resume_match_percentage)s, CURRENT_TIMESTAMP
                            )
                            ON CONFLICT (jobdiva_id, candidate_id, source) DO UPDATE SET
                                name = EXCLUDED.name,
                                email = EXCLUDED.email,
                                phone = EXCLUDED.phone,
                                headline = EXCLUDED.headline,
                                location = EXCLUDED.location,
                                profile_url = EXCLUDED.profile_url,
                                image_url = EXCLUDED.image_url,
                                resume_id = EXCLUDED.resume_id,
                                resume_text = EXCLUDED.resume_text,
                                data = EXCLUDED.data,
                                status = EXCLUDED.status,
                                resume_match_percentage = EXCLUDED.resume_match_percentage,
                                updated_at = CURRENT_TIMESTAMP
                        """, candidate_data)
                        
                        saved_count += 1
                        processing_payloads.append(candidate_data)
                        
                    except Exception as e:
                        print(f"❌ Error saving candidate {c.candidate_id}: {e}")
                        continue
                        
            conn.commit()
            
        print(f"✅ Successfully saved {saved_count} sourced candidates to database")
        enhanced_count = 0
        if processing_payloads:
            from services.sourced_candidates_storage import process_jobdiva_candidate, process_linkedin_candidate

            for payload in processing_payloads:
                try:
                    source = str(payload.get("source", ""))
                    
                    # Handle JobDiva candidates
                    if source.startswith("JobDiva") and not payload.get("resume_text"):
                        resume_data = await jobdiva_service.get_candidate_resume(
                            payload["candidate_id"],
                            resume_id=payload.get("resume_id"),
                        )
                        resume_text = (resume_data or {}).get("resume_text", "")
                        if resume_text and "Resume content unavailable" not in resume_text:
                            payload.update({
                                "resume_text": resume_text,
                                "resume_id": (resume_data or {}).get("resume_id") or payload.get("resume_id"),
                                "email": payload.get("email") or (resume_data or {}).get("email"),
                                "phone": payload.get("phone") or (resume_data or {}).get("phone"),
                                "headline": payload.get("headline") or (resume_data or {}).get("title"),
                                "location": payload.get("location") or (resume_data or {}).get("location"),
                            })

                    # Process JobDiva candidates with resume text
                    if payload.get("resume_text") and source.startswith("JobDiva"):
                        await process_jobdiva_candidate(payload)
                        enhanced_count += 1
                    # Process LinkedIn candidates
                    elif source == "LinkedIn":
                        await process_linkedin_candidate(payload)
                        enhanced_count += 1
                except Exception as e:
                    print(f"⚠️ Enhanced processing failed for candidate {payload.get('candidate_id')}: {e}")

        return {
            "status": "success",
            "detail": f"Saved {saved_count} sourced candidates",
            "saved_count": saved_count,
            "enhanced_count": enhanced_count
        }
        
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Error saving candidates: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/candidates")
async def get_all_candidates(limit: int = Query(100, ge=1, le=1000)):
    """Get all sourced candidates across all jobs."""
    try:
        import services.sourced_candidates_storage as scs
        storage = scs.SourcedCandidatesStorage()
        candidates = storage.get_all_candidates(limit=limit)
        
        # Map jobdiva_id to job_id in the response for backwards compatibility
        for candidate in candidates:
            if 'jobdiva_id' in candidate:
                candidate['job_id'] = candidate['jobdiva_id']  # Backend compatibility
                # Keep jobdiva_id for frontend
        
        return {
            "status": "success", 
            "candidates": candidates,
            "total": len(candidates)
        }
    except Exception as e:
        logger.error(f"Error fetching all candidates: {e}")
        return {"status": "error", "candidates": [], "message": str(e)}

@app.post("/candidates/enhanced-fetch")
async def fetch_enhanced_candidates(request: Dict[str, str]):
    """
    Enhanced candidate fetching using combined JobDiva API calls:
    - JobApplicantsDetail: Get job applicants
    - CandidateDetail: Get candidate info  
    - ResumeDetail: Get full resume text
    """
    try:
        job_id = request.get("job_id") or request.get("jobdiva_id")
        if not job_id:
            return {"status": "error", "candidates": [], "message": "job_id required"}
            
        print(f"🚀 Enhanced candidate fetch for job: {job_id}")
        
        # Use the new enhanced method
        enhanced_candidates = await jobdiva_service.get_enhanced_job_candidates(job_id)
        
        # Save to database with deduplication
        saved_count = await jobdiva_service.save_enhanced_candidates_to_db(job_id, enhanced_candidates)
        
        return {
            "status": "success",
            "candidates": enhanced_candidates,
            "total_found": len(enhanced_candidates),
            "total_saved": saved_count,
            "message": f"Found {len(enhanced_candidates)} enhanced candidates with full resume text"
        }
        
    except Exception as e:
        print(f"❌ Enhanced fetch error: {e}")
        return {"status": "error", "candidates": [], "message": str(e)}

@app.post("/candidates/{candidate_id}/update-resume")
async def update_candidate_resume(candidate_id: str):
    """Update resume text for an existing candidate using enhanced JobDiva integration."""
    try:
        print(f"🔄 Updating resume for candidate: {candidate_id}")
        
        success = await jobdiva_service.update_candidate_resume_text(candidate_id)
        
        if success:
            return {
                "status": "success",
                "message": f"Successfully updated resume text for candidate {candidate_id}"
            }
        else:
            return {
                "status": "error", 
                "message": f"Failed to update resume text for candidate {candidate_id}"
            }
            
    except Exception as e:
        logger.error(f"Error updating resume for candidate {candidate_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/candidates/{candidate_id}/resume")
async def get_candidate_resume(candidate_id: str):
    """
    Fetch individual candidate resume by candidate ID from JobDiva.
    Only returns real resumes - no auto-generated content.
    """
    try:
        print(f"📄 Fetching resume for candidate: {candidate_id}")
        resume_data = await jobdiva_service.get_candidate_resume(candidate_id)
        
        if not resume_data or resume_data is None:
            return {
                "status": "error",
                "resume_text": "Resume content is not available for this candidate.",
                "message": "No real resume found in JobDiva - auto-generated content disabled"
            }
        
        # Extract resume text from the response
        resume_text = resume_data.get("resume_text", "")
        
        # Check for auto-generated content patterns and reject them
        if (resume_text and (
            "Professional experience details available upon request" in resume_text or
            "Experienced professional with a strong background" in resume_text or
            "Contact information and detailed work history available upon request" in resume_text
        )):
            print(f"⚠️ Detected auto-generated content for {candidate_id} - rejecting")
            return {
                "status": "error", 
                "resume_text": "Resume content is not available for this candidate.",
                "message": "Only real JobDiva resumes are displayed - auto-generated content filtered out"
            }
        
        if not resume_text or resume_text.strip() == "":
            return {
                "status": "error",
                "resume_text": "Resume content is not available for this candidate.",
                "message": "No resume text found in JobDiva response"
            }
        
        return {
            "status": "success",
            "resume_text": resume_text,
            "candidate_id": candidate_id
        }
        
    except Exception as e:
        print(f"❌ Resume fetch error for {candidate_id}: {e}")
        return {
            "status": "error",
            "resume_text": "Resume content is not available for this candidate.",
            "message": f"Error fetching resume: {str(e)}"
        }

@app.post("/candidates/analyze", response_model=CandidateAnalysisResponse)
async def analyze_candidates(request: CandidateAnalysisRequest):
    """
    Batch analyzes candidates against JD using AI.
    """
    candidates_to_process = []
    
    # We need to ensure we have resume text for analysis.
    # If the client sent it, great. If not, we fetch it given the ID.
    for c in request.candidates: 
        c_text = c.get("resume_text")
        if not c_text:
             # Try fetch if missing
             try:
                # Determine Source to Route Correctly
                source = c.get("source", "JobDiva")
                if source == "VettedDB":
                    from services.vetted import vetted_service
                    c_text = await vetted_service.get_candidate_resume(c.get("id"))
                else:
                    # Default to JobDiva
                    c_text = await jobdiva_service.get_candidate_resume(c.get("id"))
                
                c["resume_text"] = c_text
             except Exception as e:
                # Log but continue, AI will just have less context
                print(f"Error fetching resume for {c.get('id')}: {e}")
                pass
        candidates_to_process.append(c)

    results = await ai_service.analyze_candidates_batch(
        candidates_to_process, 
        request.job_description,
        structured_jd=request.structured_jd
    )
    return {"results": results, "name": "", "email": "", "skills": [], "experience_years": 0} # Dummy fields to satisfy model if strict

@app.post("/jobs/fetch")
async def fetch_job_from_jobdiva(request: JobFetchRequest, background_tasks: BackgroundTasks):
    """
    Fetches Full Job Details from JobDiva and ensures complete population in monitored_jobs table.
    Enhanced version with validation and retry logic.
    """
    logger.info(f"📋 Fetching job {request.job_id} from JobDiva")
    
    try:
        # Resolve numeric ID first if input looks like a reference code
        search_id = request.job_id
        numeric_id = search_id
        ref_code = search_id
        
        # 1. Fetch from JobDiva to get the ULTIMATE source of truth (both IDs)
        job = await jobdiva_service.get_job_by_id(search_id)
        if job:
            numeric_id = str(job.get("id"))
            # Safely fetch the explicitly mapped job reference string (26-06182)
            fetched_ref = job.get("jobdiva_id")
            ref_code = str(fetched_ref) if fetched_ref and str(fetched_ref).strip() and str(fetched_ref) != "None" else search_id
            
            # Ensure the returned job object has both IDs clearly labeled
            job["id"] = numeric_id
            job["jobdiva_id"] = ref_code
            
        # 2. Check local DB using the NUMERIC ID as the primary key
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        query = "SELECT * FROM monitored_jobs WHERE job_id = %s"
        cursor.execute(query, (numeric_id,))
        local_data = cursor.fetchone()
        cursor.close()
        conn.close()
        
        if local_data:
            logger.info(f"📍 Found local data for Job {numeric_id} (Ref: {ref_code})")
            # If JobDiva fetch failed but local exists, we still have some data
            if not job: 
                job = {
                    "id": numeric_id, 
                    "job_id": ref_code,
                    "jobdiva_id": ref_code # Ensure both keys point to ref code if missing JobDiva fetch
                } 
            
            # Sync mapping again after potentially merging local data
            job["id"] = str(numeric_id)
            job["jobdiva_id"] = str(ref_code)
            
            # Merge local overrides
            if "recruiter_notes" in local_data and local_data["recruiter_notes"] is not None:
                job["recruiter_notes"] = local_data["recruiter_notes"]
            if "ai_description" in local_data and local_data["ai_description"] is not None:
                job["ai_description"] = local_data["ai_description"]
            if "enhanced_title" in local_data and local_data["enhanced_title"] is not None:
                job["enhanced_title"] = local_data["enhanced_title"]
            if "screening_level" in local_data and local_data["screening_level"]:
                job["screening_level"] = local_data["screening_level"]
                
            # Merge saved selection arrays to prevent DB wipe during save
            if "selected_employment_types" in local_data and local_data["selected_employment_types"]:
                val = local_data["selected_employment_types"]
                try:
                    job["selected_employment_types"] = json.loads(val) if isinstance(val, str) else val
                except:
                    pass
                    
            if "recruiter_emails" in local_data and local_data["recruiter_emails"]:
                val = local_data["recruiter_emails"]
                try:
                    job["recruiter_emails"] = json.loads(val) if isinstance(val, str) else val
                except:
                    pass
                    
            if "selected_job_boards" in local_data and local_data["selected_job_boards"]:
                val = local_data["selected_job_boards"]
                try:
                    job["selected_job_boards"] = json.loads(val) if isinstance(val, str) else val
                except:
                    pass
            
            # Always continue to re-save from JobDiva to ensure all columns are fresh.
            # priority, pay_rate, max_allowed_submittals etc. are now standard schema columns.
            if not _validate_job_completeness(local_data) and job.get("title"):
                pass # Already have title from JD fetch
            
        if not job or not job.get("title"):
            raise HTTPException(status_code=404, detail="Job not found or incomplete data in JobDiva")
            
        logger.info(f"📋 Successfully fetched job {numeric_id} (Ref: {ref_code}) from JobDiva API")
        
        # 3. Immediately save to monitoring to ensure we have a local cache
        # During a manual fetch, JobDiva values are strictly saved.
        # However, we handle the mapping to ensure "" (cleared) is respected.
        success = await save_job_to_monitoring_enhanced(numeric_id, job)
        if not success:
            logger.error(f"📋 Failed to save job {numeric_id} to monitoring, adding retry task")
            background_tasks.add_task(retry_job_monitoring_save, numeric_id, job, max_retries=3)

        # 4. Merge local overrides (like cleared notes) into the response
        # This ensures the UI respects local manual imports and clears
        # Re-fetch local_data after save, or use the one from before if it existed
        if not local_data: # If it was a new job or incomplete, refetch after save
            conn = get_db_connection()
            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cursor.execute(query, (numeric_id,))
            local_data = cursor.fetchone()
            cursor.close()
            conn.close()

        if local_data:
            # If recruiter_notes is explicitly empty string in DB, we want to return ""
            # to prevent the UI from falling back to JobDiva's raw notes.
            if "recruiter_notes" in local_data:
                job["recruiter_notes"] = local_data["recruiter_notes"]
            if "ai_description" in local_data:
                job["ai_description"] = local_data["ai_description"]
                
            # Merge stored supplemental fields back into the live response
            if local_data.get("priority") and not job.get("priority"):
                job["priority"] = local_data["priority"]
            if local_data.get("pay_rate") and not job.get("pay_rate"):
                job["pay_rate"] = local_data["pay_rate"]
            if local_data.get("max_allowed_submittals") and not job.get("max_allowed_submittals"):
                job["max_allowed_submittals"] = local_data["max_allowed_submittals"]
            if local_data.get("program_duration") and not job.get("program_duration"):
                job["program_duration"] = local_data["program_duration"]
            
            # Explicitly merge screening_level to preserve UI state on refetch
            if "screening_level" in local_data and local_data["screening_level"]:
                job["screening_level"] = local_data["screening_level"]
        
        return job
        
    except Exception as e:
        logger.error(f"📋 Error fetching job {request.job_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch job from JobDiva: {str(e)}")

def _validate_job_completeness(job_data: dict) -> bool:
    """Validate that essential job fields are present and not empty"""
    essential_fields = ['title', 'customer_name', 'status']
    for field in essential_fields:
        if not job_data.get(field) or str(job_data.get(field)).strip() == "":
            return False
    return True

async def save_job_to_monitoring_enhanced(job_id: str, job_details: dict) -> bool:
    """
    Enhanced job saving with complete field mapping and validation.
    Uses the JobDiva Numeric ID (e.g. 31920032) as the primary key (job_id column).
    The JobDiva Reference Number (e.g. 26-06182) is stored in the jobdiva_id column.
    """
    # job_id here is expected to be the NUMERIC ID (31920032)
    try:
        numeric_id = str(job_id)
        
        # Prioritize the explicitly mapped jobdiva_id reference string we just added to the service
        ref_code = job_details.get("jobdiva_id")
        
        # Fallbacks for older mapped records
        if not ref_code or not str(ref_code).strip() or "None" in str(ref_code):
            ref_code = str(job_details.get("job_id") or job_details.get("JobDivaID") or job_id)
            if "-" not in ref_code and job_details.get("status_data", {}).get("JobDivaNo"):
                ref_code = str(job_details.get("status_data", {}).get("JobDivaNo"))
            elif "-" not in ref_code and job_details.get("jobNo"):
                ref_code = str(job_details.get("jobNo"))
                
        # Ensure it's a string
        ref_code = str(ref_code)
        
        db_job_id = numeric_id # This is the PK for the monitored_jobs table
            
        logger.info(f"📋 Saving job {db_job_id} to monitoring (Reference: {ref_code})")
        
        monitoring_data = {
            "job_id": db_job_id,        # 31920032 (Numeric)
            "jobdiva_id": ref_code,    # 26-06182 (Ref)
            # Core job information
            "status": job_details.get("job_status") or job_details.get("status") or "OPEN",
            "customer_name": job_details.get("customer_name") or job_details.get("company") or "Unknown",
            "title": job_details.get("title") or "",
            
            # Location details
            "city": job_details.get("city") or "",
            "state": job_details.get("state") or "",
            "zip_code": job_details.get("zip_code") or "",
            "location_type": job_details.get("location_type") or "Onsite",
            
            # Job descriptions and notes
            "jobdiva_description": job_details.get("jobdiva_description") or job_details.get("description") or "",
            "ai_description": job_details.get("ai_description") if job_details.get("ai_description") is not None else "",
            "recruiter_notes": job_details.get("recruiter_notes") if job_details.get("recruiter_notes") is not None else (job_details.get("job_notes") or ""),
            
            # Employment and compensation details
            "employment_type": job_details.get("employment_type") or "",
            "pay_rate": job_details.get("pay_rate") or "",
            "work_authorization": job_details.get("work_authorization") or "",
            
            # Job logistics
            "openings": job_details.get("openings") or "",
            "posted_date": job_details.get("posted_date") or "",
            "start_date": job_details.get("start_date") or "",
            
            # Additional Job Details
            "priority": job_details.get("priority") or "",
            "program_duration": job_details.get("program_duration") or "",
            "max_allowed_submittals": job_details.get("max_allowed_submittals") or "",
            
            # Recruiter information
            "recruiter_emails": job_details.get("recruiter_emails") or [],
            
            # Application state
            "processing_status": "pending",
            "screening_level": job_details.get("screening_level") or "L1.5"
        }
        
        # Save using centralized service logic
        result = jobdiva_service.monitor_job_locally(db_job_id, monitoring_data)
        
        if result:
            # Verify the job was actually saved by checking the database (using ref code)
            saved_job = jobdiva_service.get_local_job(db_job_id)
            if saved_job and _validate_job_completeness(saved_job):
                logger.info(f"✅ Job {db_job_id} successfully saved and verified in monitoring")
                return True
            else:
                logger.error(f"❌ Job {db_job_id} save verification failed")
                return False
        else:
            logger.error(f"❌ Job {db_job_id} save returned false")
            return False
            
    except Exception as e:
        logger.error(f"❌ Failed to save job {job_id} to monitoring: {e}")
        return False

async def sync_jobdiva_udf_task(job_id: str, ai_description: str, recruiter_notes: str, current_step: int = 0, jobdiva_id: str = None):
    """Background task to sync AI description and notes back to JobDiva UDFs.
    
    Aligned with 'Next' button triggers:
    - Step 1 (Intake) -> Click Next sends Step 2: Push UDF 231 (Notes)
    - Step 2 (Publish) -> Click Next sends Step 3: Push UDF 230 (AI JD)
    """
    try:
        # TODO: UDF push temporarily disabled — re-enable when JobDiva field size is resolved
        logger.info(f"⏭️  UDF push skipped (disabled) for job {job_id} on step {current_step}")
        return

        # job_id here is the reference string (e.g., 26-06182) for logging
        # jobdiva_id here is the numeric ID (e.g., 31920032) for the actual API call
        target_id = jobdiva_id # Use the numeric ID for the JobDiva API call
        
        udf_fields = []
        
        # When moving TO Step 2, it means Step 1 (Intake) was just COMPLETED.
        if current_step == 2 and recruiter_notes is not None:
            logger.info(f"🔄 Syncing Step 1 UDF (notes) for job {job_id} (Numeric: {target_id})...")
            udf_fields.append({"userfieldId": "231", "userfieldValue": recruiter_notes})
            
        # When moving TO Step 3, it means Step 2 (Publish) was just COMPLETED.
        elif current_step == 3 and ai_description is not None:
            logger.info(f"🔄 Syncing Step 2 UDF (AI description) for job {job_id} (Numeric: {target_id})...")
            udf_fields.append({"userfieldId": "230", "userfieldValue": ai_description})
            
        if not udf_fields:
            logger.info(f"No UDF field mapping for job {job_id} on step {current_step}")
            return
            
        success = await jobdiva_service.update_job_user_fields(target_id, udf_fields)
        if success:
            logger.info(f"✅ Successfully synced UDFs to JobDiva for job {job_id} (Numeric: {target_id})")
        else:
            logger.warning(f"⚠️ Failed to sync UDFs to JobDiva for job {job_id} (Numeric: {target_id})")
    except Exception as e:
        logger.error(f"❌ Error in sync_jobdiva_udf_task for job {job_id} (Numeric: {target_id}): {e}")

async def retry_job_monitoring_save(job_id: str, job_details: dict, max_retries: int = 3):
    """
    Retry mechanism for failed job saves with exponential backoff
    """
    import asyncio
    
    for attempt in range(1, max_retries + 1):
        logger.info(f"📋 Retry attempt {attempt}/{max_retries} for job {job_id}")
        
        try:
            success = await save_job_to_monitoring_enhanced(job_id, job_details)
            if success:
                logger.info(f"✅ Job {job_id} successfully saved on retry attempt {attempt}")
                return
            else:
                if attempt < max_retries:
                    wait_time = 2 ** attempt  # Exponential backoff: 2, 4, 8 seconds
                    logger.warning(f"📋 Retry {attempt} failed for job {job_id}, waiting {wait_time}s before next attempt")
                    await asyncio.sleep(wait_time)
                    
        except Exception as e:
            logger.error(f"❌ Retry attempt {attempt} failed for job {job_id}: {e}")
            if attempt < max_retries:
                wait_time = 2 ** attempt
                await asyncio.sleep(wait_time)
    
    logger.error(f"❌ All retry attempts exhausted for job {job_id}")
    # Could potentially send alert/notification here

def add_job_to_monitoring_internal(job_id: str, job_details: dict):
    """
    DEPRECATED: Use save_job_to_monitoring_enhanced instead.
    Internal function to add job to monitoring using centralized service logic
    """
    logger.warning(f"📋 Using deprecated add_job_to_monitoring_internal for job {job_id}, consider updating caller")
    print(f"📋 DEBUG: Background task started for job {job_id}")
    try:
        # Use the enhanced version instead
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        success = loop.run_until_complete(save_job_to_monitoring_enhanced(job_id, job_details))
        if success:
            print(f"📋 DEBUG: Job {job_id} saved to monitoring")
            logger.info(f"📋 Auto-added Job {job_id} to monitoring")
        else:
            print(f"📋 DEBUG: Failed to save job {job_id} to monitoring")
            logger.error(f"Failed to auto-add job {job_id} to monitoring")
        
        # Reset 5-minute timer since new job was added
        schedule_next_poll()
        print(f"📋 DEBUG: Background task completed for job {job_id}")
    except Exception as e:
        print(f"📋 DEBUG: Background task failed for job {job_id}: {e}")
        logger.error(f"Failed to auto-add job {job_id} to monitoring: {e}")

@app.post("/jobs/validate-monitoring")
async def validate_and_fix_monitored_jobs():
    """
    Endpoint to validate and fix incomplete jobs in the monitored_jobs table
    """
    try:
        fixed_count = await validate_and_fix_incomplete_jobs()
        return {"status": "success", "message": f"Validated monitored jobs, fixed {fixed_count} incomplete entries"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Validation failed: {str(e)}")

async def validate_and_fix_incomplete_jobs() -> int:
    """
    Check monitored_jobs table for incomplete entries and attempt to refetch missing data
    Returns count of jobs fixed
    """
    logger.info("📋 Starting validation of monitored_jobs table")
    fixed_count = 0
    
    try:
        # Get all jobs from monitored_jobs
        if not jobdiva_service.engine:
            logger.error("Database engine not available")
            return 0
            
        with jobdiva_service.engine.connect() as conn:
            result = conn.execute(text("SELECT job_id, title, customer_name, status, jobdiva_description FROM monitored_jobs"))
            jobs = result.fetchall()
            
            logger.info(f"📋 Found {len(jobs)} jobs to validate")
            
            for row in jobs:
                job_data = dict(row._mapping)
                job_id = job_data['job_id']
                
                # Check if job is incomplete
                if not _validate_job_completeness(job_data):
                    logger.info(f"📋 Job {job_id} is incomplete, attempting to refetch")
                    
                    try:
                        # Refetch from JobDiva
                        fresh_job = await jobdiva_service.get_job_by_id(job_id)
                        if fresh_job:
                            success = await save_job_to_monitoring_enhanced(job_id, fresh_job)
                            if success:
                                fixed_count += 1
                                logger.info(f"✅ Fixed incomplete job {job_id}")
                            else:
                                logger.error(f"❌ Failed to fix job {job_id}")
                        else:
                            logger.warning(f"📋 Could not refetch job {job_id} from JobDiva")
                            
                    except Exception as e:
                        logger.error(f"❌ Error fixing job {job_id}: {e}")
                        continue
            
            logger.info(f"📋 Validation complete. Fixed {fixed_count} jobs")
            return fixed_count
            
    except Exception as e:
        logger.error(f"❌ Error during validation: {e}")
        return 0
        print(f"📋 DEBUG: Job {job_id} saved to monitoring")
        logger.info(f"📋 Auto-added Job {job_id} to monitoring")
        
        # NOTE: Skills extraction moved to publish workflow "Next" button
        # No longer auto-extracting during job import
        
        # Reset 5-minute timer since new job was added
        schedule_next_poll()
        print(f"📋 DEBUG: Background task completed for job {job_id}")
    except Exception as e:
        print(f"📋 DEBUG: Background task failed for job {job_id}: {e}")
        logger.error(f"Failed to auto-add job {job_id} to monitoring: {e}")

def auto_extract_job_skills(job_id: str, job_details: dict):
    """Auto-trigger skills extraction for newly imported jobs"""
    print(f"🧠 DEBUG: Auto-extracting skills for job {job_id}")
    try:
        from services.job_skills_extractor import JobSkillsExtractor
        from services.job_skills_db import JobSkillsDB
        
        logger.info(f"🧠 Auto-extracting skills for job {job_id}...")
        
        # Initialize extractor
        extractor = JobSkillsExtractor(OPENAI_API_KEY)
        
        # Extract skills from the job data
        analysis = extractor.analyze_job_skills(
            job_id=job_id,
            jobdiva_description=job_details.get("jobdiva_description") or job_details.get("description"),
            ai_description=job_details.get("ai_description"),
            recruiter_notes=job_details.get("job_notes") or job_details.get("recruiter_notes")
        )
        
        # Save to database
        db_service = JobSkillsDB()
        save_result = db_service.save_job_skills(
            job_id=job_id,
            extracted_skills=analysis.extracted_skills,
            analysis_metadata=analysis.analysis_metadata
        )
        
        logger.info(f"✅ Auto-extracted and saved {save_result['skills_saved']} skills for job {job_id}")
        print(f"🧠 DEBUG: Skills extraction completed for job {job_id} - saved {save_result['skills_saved']} skills")
        
    except Exception as e:
        print(f"🧠 DEBUG: Skills extraction failed for job {job_id}: {e}")
        logger.error(f"Auto skills extraction failed for job {job_id}: {e}")
        # Don't raise exception - job import should still succeed even if skills extraction fails

# Job monitoring utility functions
def load_monitored_jobs() -> Dict[str, Any]:
    """Load the list of jobs being monitored from PostgreSQL via JobDivaService"""
    return jobdiva_service.get_all_monitored_jobs()

def save_monitored_jobs(jobs_data: Dict[str, Any]):
    """Save/Update monitored jobs in PostgreSQL via JobDivaService"""
    for jid, details in jobs_data.get("jobs", {}).items():
        jobdiva_service.monitor_job_locally(jid, details)

async def poll_all_jobs():
    """Background task to poll all monitored jobs for status changes"""
    logger.info("🔄 Starting job status polling...")
    
    jobs_data = load_monitored_jobs()
    job_ids = list(jobs_data.get("jobs", {}).keys())
    
    if not job_ids:
        logger.info("No jobs to monitor")
        return
    
    logger.info(f"Polling {len(job_ids)} jobs: {job_ids}")
    
    # Batch fetch statuses
    statuses = await jobdiva_service.get_multiple_jobs_status(job_ids)
    
    # Update local tracking in-place to avoid deleting jobs that failed to poll
    import time
    changes_detected = []
    
    # We use the existing jobs_data dict and update it
    current_jobs = jobs_data.get("jobs", {})
    
    for status in statuses:
        job_id = status["job_id"]
        current_status = status["status"]
        
        if job_id not in current_jobs:
            continue
            
        old_data = current_jobs[job_id]
        old_status = old_data.get("status", "UNKNOWN")
        
        # Track changes
        if old_status != current_status:
            changes_detected.append({
                "job_id": job_id,
                "old_status": old_status,
                "new_status": current_status,
                "title": status.get("title", "")
            })
        
        # Safety Check: Do not overwrite with NOT_FOUND if we already have data
        if current_status == "NOT_FOUND":
            logger.warning(f"⚠️ Polling returned NOT_FOUND for {job_id}. Preserving status '{old_status}'.")
            continue

        # UPDATE in-place for legacy file compatibility
        old_data.update({
            "status":       current_status,
            "customer_name": status.get("customer_name", "Unknown"),
            "title":        status.get("title", ""),
        })
        
        # NEW: Update the Database (PostgreSQL) as well
        db_data = {
            "status": current_status,
            "customer_name": status.get("customer_name"),
            "title": status.get("title")
        }
        jobdiva_service.monitor_job_locally(job_id, db_data)
    
    # Save updated data
    jobs_data["last_sync"] = readable_ist_now()
    save_monitored_jobs(jobs_data)
    
    if changes_detected:
        logger.info(f"📢 Status changes detected: {changes_detected}")
    else:
        logger.info("✅ No status changes detected")
    
    # Schedule next poll 5 minutes from now
    schedule_next_poll()
    
    return {"polled": len(job_ids), "changes": changes_detected}

def persist_rubric_background_task(jobdiva_id: str, rubric: Any, recruiter_notes: Optional[str], bot_introduction: Optional[str] = None):
    """
    Background task to persist the structured rubric.
    This runs in a separate connection after the main save has committed.
    """
    try:
        logger.info(f"⏳ [Background] Persisting rubric for Job {jobdiva_id}...")
        rubric_db = JobRubricDB()
        rubric_db.save_full_rubric(jobdiva_id, rubric, recruiter_notes, bot_introduction)
        logger.info(f"✅ [Background] Rubric persisted for Job {jobdiva_id}")
    except Exception as e:
        logger.error(f"❌ [Background] Failed to persist rubric for {jobdiva_id}: {e}")


# =====================================================
# JOB DRAFTS API ENDPOINTS
# =====================================================

def get_db_connection():
    """Get database connection using existing pattern"""
    import psycopg2
    import os
    db_url = DATABASE_URL
    if not db_url:
        raise Exception("DATABASE_URL not configured")
    return psycopg2.connect(db_url)

@app.post("/jobs/{job_id}/save")
async def save_job_draft(job_id: str, draft_data: JobDraftData, background_tasks: BackgroundTasks):
    """
    Save or update job data with real database persistence.
    Consolidated into monitored_jobs using the reference number as job_id.
    """
    try:
        import json
        import psycopg2.extras
        
        # SWAPPED IDENTIFIERS:
        # db_job_id (PK) = Numeric ID (31920032)
        # jobdiva_id = Reference String (26-06182)
        
        # If job_id passed in is hyphenated, it's a ref code, we need the numeric one
        if "-" in str(job_id):
            job_info = await jobdiva_service.get_job_by_id(job_id)
            db_job_id = str(job_info.get("id")) if job_info else job_id # Use numeric ID as PK
            ref_code = job_id # This is the reference string
        else:
            db_job_id = job_id # Assume job_id is already numeric
            # Fetch ref code from DB if possible
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT jobdiva_id FROM monitored_jobs WHERE job_id = %s", (db_job_id,))
            row = cursor.fetchone()
            ref_code = row[0] if row else db_job_id # Fallback to numeric if ref not found
            cursor.close()
            conn.close()

        conn = get_db_connection()
        cursor = conn.cursor()
        
        # 1. Update monitored_jobs using the NUMERIC ID as key (job_id)
        logger.info(f"🔄 Updating monitored_jobs for Job {db_job_id} (Ref: {ref_code})...")
        cursor.execute("""
            UPDATE monitored_jobs 
            SET 
                title = COALESCE(%s, title),
                enhanced_title = %s,
                ai_description = %s,
                selected_job_boards = %s,
                recruiter_notes = %s,
                recruiter_emails = %s,
                selected_employment_types = %s,
                work_authorization = %s,
                bot_introduction = %s,
                screening_level = %s,
                processing_status = %s,
                current_step = %s,
                customer_name = CASE 
                    WHEN %s IS NOT NULL AND %s NOT ILIKE 'Unknown%%' AND %s != '' THEN %s 
                    ELSE customer_name 
                END,
                jobdiva_id = %s, -- This is the reference string
                sourcing_filters = %s,
                resume_match_filters = %s,
                updated_at = NOW()
            WHERE job_id = %s -- This is the numeric ID
        """, (
            draft_data.title,                                    # title
            draft_data.enhanced_title or draft_data.title,       # enhanced_title
            draft_data.ai_description,                           # ai_description
            json.dumps(draft_data.selected_job_boards or []),    # selected_job_boards
            draft_data.recruiter_notes,                          # recruiter_notes
            json.dumps(draft_data.recruiter_emails or []),       # recruiter_emails
            json.dumps(draft_data.selected_employment_types or []), # selected_employment_types
            draft_data.work_authorization,                       # work_authorization
            draft_data.bot_introduction,                        # bot_introduction
            draft_data.screening_level,                         # screening_level
            f"step_{draft_data.current_step}_complete",         # processing_status
            draft_data.current_step,                            # current_step
            draft_data.customer_name, draft_data.customer_name,  # for CASE customer_name
            draft_data.customer_name, draft_data.customer_name,  # for CASE customer_name
            ref_code,                                           # 26-06182 (swapped)
            json.dumps(draft_data.sourcing_filters or {}),      # sourcing_filters
            json.dumps(draft_data.resume_match_filters or []),  # resume_match_filters
            db_job_id                                           # 31920032 (PK)
        ))
        
        if cursor.rowcount == 0:
            logger.warning(f"⚠️ No monitored_jobs record found for {db_job_id}, creating row...")
            cursor.execute("""
                INSERT INTO monitored_jobs (
                    job_id, title, enhanced_title, ai_description, 
                    selected_job_boards, recruiter_notes, recruiter_emails, 
                    selected_employment_types, work_authorization, bot_introduction,
                    screening_level,
                    processing_status, current_step, customer_name, jobdiva_id, sourcing_filters, resume_match_filters, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
            """, (
                db_job_id,                                           # 31920032 (Numeric PK)
                draft_data.title,
                draft_data.enhanced_title or draft_data.title,
                draft_data.ai_description,
                json.dumps(draft_data.selected_job_boards or []),
                draft_data.recruiter_notes,
                json.dumps(draft_data.recruiter_emails or []),
                json.dumps(draft_data.selected_employment_types or []),
                draft_data.work_authorization,
                draft_data.bot_introduction,
                draft_data.screening_level,
                f"step_{draft_data.current_step}_complete",
                draft_data.current_step,
                draft_data.customer_name,                            # NEW: customer_name
                ref_code,                                            # 26-06182 (Ref)
                json.dumps(draft_data.sourcing_filters or {}),      # sourcing_filters
                json.dumps(draft_data.resume_match_filters or [])   # resume_match_filters
            ))
            logger.info(f"✅ Created new monitored_jobs record for job {db_job_id}")
        
        conn.commit()
        cursor.close()
        conn.close()
        
        # 2. Persist Structured Rubric (Titles, Skills, etc.) via Background Task
        # to ensure the main record is UNLOCKED before the rubric update starts.
        if draft_data.rubric:
            logger.info(f"📋 Queuing rubric persistence for Job {db_job_id}...")
            background_tasks.add_task(
                persist_rubric_background_task, 
                ref_code, 
                draft_data.rubric, 
                draft_data.recruiter_notes,
                draft_data.bot_introduction
            )
        
        # 2. Synchronize with JobDiva UDFs in background (using Numeric PK)
        background_tasks.add_task(
            sync_jobdiva_udf_task, 
            ref_code,                       # 26-06182 (for logs)
            draft_data.ai_description,
            draft_data.recruiter_notes,
            draft_data.current_step,
            db_job_id                       # 31920032 (The Numeric PK)
        )
        
        save_type = "Auto-saved" if draft_data.is_auto_saved else "Manually saved"
        logger.info(f"✅ {save_type} data for job {job_id}, step {draft_data.current_step}")
        
        return {
            "status": "success",
            "message": f"{save_type} job data successfully",
            "job_id": db_job_id,
            "current_step": draft_data.current_step,
            "saved_at": readable_ist_now()
        }
        
    except Exception as e:
        logger.error(f"Save Job Error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Failed to save: {str(e)}")

@app.get("/jobs/{job_id}/draft")
async def get_job_draft(job_id: str, user_session: str = "default"):
    """
    Retrieve existing job data from monitored_jobs.
    Tries job_id first, then falls back to jobdiva_id to handle both
    numeric PK and reference string formats.
    """
    try:
        db_job_id = job_id
        
        conn = get_db_connection()
        import psycopg2.extras
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        # Primary lookup: by job_id (numeric PK stored as text)
        cursor.execute(
            "SELECT * FROM monitored_jobs WHERE job_id = %s OR job_id = %s",
            (db_job_id, db_job_id.lstrip('0'))
        )
        job_row = cursor.fetchone()
        
        # Fallback: try jobdiva_id (the ref string like '26-06182')
        if not job_row:
            logger.info(f"No match on job_id={db_job_id}, trying jobdiva_id fallback")
            cursor.execute(
                "SELECT * FROM monitored_jobs WHERE jobdiva_id = %s",
                (db_job_id,)
            )
            job_row = cursor.fetchone()

        cursor.close()
        conn.close()
        
        if not job_row:
             return {"status": "error", "message": f"No data found for job {db_job_id}"}
        
        import json
        
        # Helper to safely parse JSON from DB
        def parse_json(val):
            if not val: return []
            if isinstance(val, (list, dict)): return val
            try: return json.loads(val)
            except: return []

        # Map database columns back to JobDraftData format
        return {
            "status": "success",
            "data": {
                "id": db_job_id,                      # 26-06182
                "job_id": db_job_id,                  # 26-06182
                "title": job_row.get("title") or "",
                "enhanced_title": job_row.get("enhanced_title") or job_row.get("title") or "",
                "ai_description": job_row.get("ai_description") or "",
                "recruiter_notes": job_row.get("recruiter_notes") or "",
                "work_authorization": job_row.get("work_authorization") or "",
                "selected_job_boards": parse_json(job_row.get("selected_job_boards")),
                "recruiter_emails": parse_json(job_row.get("recruiter_emails")),
                "selected_employment_types": parse_json(job_row.get("selected_employment_types")),
                "current_step": job_row.get("current_step") or 1,
                "screening_level": job_row.get("screening_level") or "L1.5",
                "bot_introduction": job_row.get("bot_introduction") or "",
                "resume_match_filters": parse_json(job_row.get("resume_match_filters")),
                "sourcing_filters": job_row.get("sourcing_filters") or {}
            }
        }
        
    except Exception as e:
        logger.error(f"Get Job Data Error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return {"status": "error", "message": str(e)}

@app.post("/jobs/{job_id}/save-step")
async def save_step_progress(job_id: str, step: int, draft_data: JobDraftData, background_tasks: BackgroundTasks):
    """
    Auto-save progress when user navigates between steps.
    """
    try:
        draft_data.current_step = step
        draft_data.is_auto_saved = True
        
        # Mark the completed step
        if step > 1:
            draft_data.step1_completed = True
        if step > 2:
            draft_data.step2_completed = True
        if step > 3:
            draft_data.step3_completed = True
        
        # Reuse the save_job_draft logic
        result = await save_job_draft(job_id, draft_data, background_tasks)
        
        logger.info(f"🔄 Auto-saved progress for job {job_id} at step {step}")
        return result
        
    except Exception as e:
        logger.error(f"Save Step Progress Error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to auto-save step: {str(e)}")

@app.post("/jobs/{job_id}/monitor")
async def save_job_to_monitored_jobs_only(job_id: str, draft_data: JobDraftData):
    """
    Save job data directly to monitored_jobs table without touching drafts table.
    This is used when the user wants form data to go straight to monitoring.
    """
    try:
        import json
        
        # 1. Resolve to Numeric PK if Reference ID was provided
        # job_id could be '26-06182' (ref) or '31920032' (numeric pk)
        numeric_id = job_id
        ref_id = draft_data.jobdiva_id or job_id
        
        # If it's a reference ID (has a hyphen), we must resolve it to the numeric PK
        if "-" in str(job_id):
            logger.info(f"🔍 Identifier Resolution: {job_id} looks like a Reference ID. Looking up Numeric PK...")
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT job_id FROM monitored_jobs WHERE jobdiva_id = %s", (job_id,))
            row = cursor.fetchone()
            if row:
                numeric_id = row[0]
                logger.info(f"✅ Identifier Resolution: {job_id} resolved to Numeric PK {numeric_id}")
            else:
                # If not found in DB, try to fetch from JobDiva API as fallback
                job_info = await jobdiva_service.get_job_by_id(job_id)
                if job_info and job_info.get("id"):
                    numeric_id = str(job_info.get("id"))
                    logger.info(f"✅ Identifier Resolution (API): {job_id} resolved to Numeric {numeric_id}")
            cursor.close()
            conn.close()
        
        # 2. DELETE DUPLICATE RECORD (Row 1 from screenshot)
        # If numeric_id != job_id, it means we have a duplicate row where PK = Reference ID
        if numeric_id != job_id:
            try:
                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute("DELETE FROM monitored_jobs WHERE job_id = %s", (job_id,))
                if cursor.rowcount > 0:
                    logger.info(f"🗑️ Identifier Cleanup: Removed duplicate record where PK was Reference ID '{job_id}'")
                conn.commit()
                cursor.close()
                conn.close()
            except Exception as cleanup_err:
                logger.warning(f"⚠️ Identifier Cleanup: Failed to remove potential duplicate: {cleanup_err}")

        logger.info(f"🔄 Saving form data for job {numeric_id} (Ref: {ref_id}) to monitored_jobs")
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Update monitored_jobs with ALL current form data
        # CRITICAL: title = COALESCE(%s, title) ensures we don't overwrite the original JobDiva title unless explicitly desired
        cursor.execute("""
            UPDATE monitored_jobs 
            SET 
                title = COALESCE(%s, title),
                enhanced_title = %s,
                ai_description = %s,
                selected_job_boards = %s,
                recruiter_notes = %s,
                recruiter_emails = %s,
                selected_employment_types = %s,
                work_authorization = %s,
                screening_level = %s,
                current_step = %s,
                user_session = %s,
                customer_name = CASE 
                    WHEN %s IS NOT NULL AND %s NOT ILIKE 'Unknown%%' AND %s != '' THEN %s 
                    ELSE customer_name 
                END,
                ai_enhanced = CASE WHEN %s IS NOT NULL AND %s != '' THEN TRUE ELSE ai_enhanced END,
                processing_status = CONCAT('step_', %s, '_complete'),
                updated_at = NOW()
            WHERE job_id = %s -- This is the Numeric ID (31920032)
        """, (
            draft_data.title,                                    # original title (preserved if possible)
            draft_data.enhanced_title or draft_data.title,       # enhanced_title (AI version)
            draft_data.ai_description,                           # ai_description  
            json.dumps(draft_data.selected_job_boards or []),    # selected_job_boards
            draft_data.recruiter_notes,                          # recruiter_notes
            json.dumps(draft_data.recruiter_emails or []),       # recruiter_emails
            json.dumps(draft_data.selected_employment_types or []), # selected_employment_types
            draft_data.work_authorization,                       # work_authorization
            draft_data.screening_level,                         # screening_level
            draft_data.current_step,                            # current_step
            draft_data.user_session or 'default',              # user_session
            draft_data.customer_name, draft_data.customer_name, # for customer_name CASE
            draft_data.customer_name, draft_data.customer_name, # for customer_name CASE
            draft_data.ai_description,                          # for ai_enhanced check
            draft_data.ai_description,                          # for ai_enhanced check
            draft_data.current_step,                            # for processing_status
            numeric_id                                          # WHERE condition (Numeric ID)
        ))
        
        rows_updated = cursor.rowcount
        if rows_updated > 0:
            logger.info(f"✅ Successfully updated form data for Numeric PK {numeric_id} (Step {draft_data.current_step})")
        else:
            logger.warning(f"⚠️ No monitored_jobs record found for Numeric ID: {numeric_id}, creating new record")
            # If no record exists, create new one
            cursor.execute("""
                INSERT INTO monitored_jobs (
                    job_id, title, enhanced_title, ai_description, selected_job_boards,
                    recruiter_notes, recruiter_emails, selected_employment_types, 
                    work_authorization, screening_level, current_step, user_session, customer_name,
                    ai_enhanced, processing_status, created_at, updated_at, jobdiva_id
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW(), %s
                )""", (
                numeric_id,
                draft_data.title,
                draft_data.enhanced_title or draft_data.title,
                draft_data.ai_description,
                json.dumps(draft_data.selected_job_boards or []),
                draft_data.recruiter_notes,
                json.dumps(draft_data.recruiter_emails or []),
                json.dumps(draft_data.selected_employment_types or []),
                draft_data.work_authorization,
                draft_data.screening_level,
                draft_data.current_step,
                draft_data.user_session or 'default',
                draft_data.customer_name,                            # NEW: customer_name
                bool(draft_data.ai_description),
                f'step_{draft_data.current_step}_complete',
                ref_id
            ))
            logger.info(f"✅ Created new monitored_jobs record for job {numeric_id}")
        
        conn.commit()
        cursor.close()
        conn.close()
        
        # Push recruiter notes to JobDiva UDF so it's reflected in the system
        if draft_data.recruiter_notes:
            try:
                udf_notes = JOBDIVA_JOB_NOTES_UDF_ID
                fields = [{"userfieldId": udf_notes, "value": draft_data.recruiter_notes[:3900]}]
                jobdiva_ok = await jobdiva_service.update_job_user_fields(job_id, fields)
                if jobdiva_ok:
                    logger.info(f"✅ Pushed recruiter_notes to JobDiva UDF {udf_notes} for job {job_id}")
                else:
                    logger.warning(f"⚠️ Failed to push recruiter_notes to JobDiva for job {job_id}")
            except Exception as e:
                logger.error(f"❌ Error pushing recruiter_notes to JobDiva: {e}")
        
        # 3. IF STEP 2 COMPLETE: Perform Grounded Extraction & Populate Discrete Tables
        if draft_data.current_step == 2 and draft_data.ai_description:
            try:
                from services.taxonomy_service import extract_grounded_rubric
                from services.job_rubric_db import JobRubricDB
                from core.config import OPENAI_API_KEY
                from openai import AsyncOpenAI
                
                logger.info(f"⚡ Step 2 Next Click: Triggering Grounded Extraction for {numeric_id} (Ref: {ref_id})")
                
                client = AsyncOpenAI(api_key=OPENAI_API_KEY)
                grounded = await extract_grounded_rubric(
                    job_text=draft_data.ai_description,
                    job_title=draft_data.enhanced_title or draft_data.title,
                    client=client
                )
                
                # Only include Original JobDiva Title and Enhanced Title (both as PAIR)
                titles_payload = [{
                    "value": draft_data.title,
                    "minYears": 0,
                    "recent": False,
                    "matchType": "Similar",
                    "required": "Required",
                    "source": "PAIR"
                }]
                if draft_data.enhanced_title and draft_data.enhanced_title != draft_data.title:
                    titles_payload.append({
                        "value": draft_data.enhanced_title,
                        "minYears": 0,
                        "recent": False,
                        "matchType": "Similar",
                        "required": "Preferred",
                        "source": "PAIR"
                    })
                
                # Format for JobRubricDB.save_full_rubric
                rubric_payload = {
                    "skills": [
                        {
                            "value": s["value"],
                            "minYears": s.get("minYears", 3),
                            "recent": s.get("recent", True),
                            "matchType": s.get("matchType", "Similar"),
                            "required": s.get("required", "Required"),
                            "category": "hard",
                            "importance": s.get("importance", s.get("required", "Required").lower()),
                            "evidence_type": s.get("evidence_type", "direct")
                        } for s in grounded.get("hard_skills", [])
                    ],
                    "soft_skills": [
                        {
                            "value": s["value"],
                            "minYears": s.get("minYears", 0),
                            "recent": s.get("recent", False),
                            "matchType": s.get("matchType", "Similar"),
                            "required": s.get("required", "Preferred"),
                            "category": "soft",
                            "importance": s.get("importance", s.get("required", "Preferred").lower()),
                            "evidence_type": s.get("evidence_type", "direct")
                        } for s in grounded.get("soft_skills", [])
                    ],
                    "titles": titles_payload
                }
                
                rubric_db = JobRubricDB()
                # Use ref_id (the one with the hyphen) as it's the identifier for the discrete tables
                rubric_db.save_full_rubric(jobdiva_id=ref_id, rubric_obj=rubric_payload)
                logger.info(f"✅ Step 2 Next Click: Successfully persisted {len(rubric_payload['skills'])} grounded skills for {ref_id}")
                
            except Exception as grounding_err:
                logger.error(f"⚠️ Step 2 Next Click Grounding Error: {grounding_err}")

        save_type = "Auto-saved" if draft_data.is_auto_saved else "Manually saved"
        logger.info(f"✅ {save_type} job {job_id} directly to monitored_jobs, step {draft_data.current_step}")
        
        return {
            "status": "success",
            "message": f"{save_type} job data to monitored_jobs successfully",
            "current_step": draft_data.current_step,
            "saved_at": readable_ist_now()
        }
        
    except Exception as e:
        logger.error(f"Save Job to Monitored Jobs Error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to save to monitored_jobs: {str(e)}")

@app.post("/jobs/{job_id}/draft/requirements")
async def save_draft_requirements(job_id: str, requirements_data: JobDraftRequirements):
    """
    Save requirements directly to monitored_jobs table (simplified workflow).
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Prepare requirements as structured JSON for monitored_jobs
        requirements_json = [
            {
                "requirement_type": req.requirement_type,
                "value": req.value,
                "field": req.field,
                "priority": req.priority,
                "min_years": req.min_years,
                "is_user_added": req.is_user_added,
                "display_order": req.display_order
            }
            for req in requirements_data.requirements
        ]
        
        # Ensure the requirements column exists in monitored_jobs
        cursor.execute("""
            ALTER TABLE monitored_jobs 
            ADD COLUMN IF NOT EXISTS job_requirements JSONB DEFAULT '[]'
        """)
        
        # Update monitored_jobs with requirements data directly
        cursor.execute("""
            UPDATE monitored_jobs 
            SET job_requirements = %s,
                processing_status = 'step_3_complete',
                updated_at = NOW()
            WHERE job_id = %s
        """, (
            json.dumps(requirements_json),
            job_id
        ))
        
        rows_updated = cursor.rowcount
        if rows_updated > 0:
            logger.info(f"✅ Saved {len(requirements_data.requirements)} requirements directly to monitored_jobs for job {job_id}")
        else:
            logger.warning(f"⚠️ No monitored_jobs record found for job_id: {job_id}")
            conn.rollback()
            cursor.close()
            conn.close()
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found in monitored_jobs")
        
        conn.commit()
        cursor.close()
        conn.close()
        
        return {
            "status": "success",
            "message": f"Saved {len(requirements_data.requirements)} requirements to monitored_jobs",
            "job_id": job_id
        }
        
    except Exception as e:
        logger.error(f"Save Requirements Error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to save requirements: {str(e)}")

@app.get("/jobs/{job_id}/monitored-data")
async def get_monitored_job_data(job_id: str):
    """
    Get current data from monitored_jobs table for verification.
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # STAGE 1: DEFENSIVE MINI-QUERY (Just for the title)
        # This will NOT crash even if new columns (skills/requirements) are missing
        title_data = None
        try:
            cursor.execute("SELECT job_id, title, jobdiva_id, openings, max_allowed_submittals FROM monitored_jobs WHERE job_id = %s OR jobdiva_id = %s", (job_id, job_id))
            title_row = cursor.fetchone()
            if title_row:
                title_data = {
                    "job_id": title_row[0], 
                    "title": title_row[1], 
                    "jobdiva_id": title_row[2],
                    "openings": title_row[3],
                    "max_allowed_submittals": title_row[4]
                }
        except Exception as e:
            logger.warning(f"Mini-query failed: {e}")

        # STAGE 2: FULL QUERY (May crash if schema is old)
        try:
            cursor.execute("""
                SELECT job_id, title, enhanced_title, ai_description, selected_job_boards,
                    recruiter_notes, recruiter_emails, selected_employment_types,
                    work_authorization, screening_level, current_step, processing_status,
                    job_requirements, ai_enhanced, created_at, updated_at,
                    hard_skills, soft_skills, resume_match_filters, jobdiva_id,
                    openings, max_allowed_submittals
                FROM monitored_jobs 
                WHERE job_id = %s OR jobdiva_id = %s
            """, (job_id, job_id))
            
            row = cursor.fetchone()
            
            if row:
                columns = ["job_id", "title", "enhanced_title", "ai_description", "selected_job_boards",
                        "recruiter_notes", "recruiter_emails", "selected_employment_types", 
                        "work_authorization", "screening_level", "current_step", "processing_status",
                        "job_requirements", "ai_enhanced", "created_at", "updated_at",
                        "hard_skills", "soft_skills", "resume_match_filters", "jobdiva_id",
                        "openings", "max_allowed_submittals"]
                data = dict(zip(columns, row))
                cursor.close()
                conn.close()
                return {"status": "success", "job_id": job_id, "data": data}
        except Exception as full_query_error:
            logger.warning(f"Full query failed (likely schema mismatch): {full_query_error}")

        # STAGE 3: IF FULL QUERY FAILED BUT MINI-QUERY WORKED, RETURN MINI-DATA
        if title_data:
            cursor.close()
            conn.close()
            return {
                "status": "success",
                "job_id": job_id,
                "data": {
                    "job_id": title_data["job_id"],
                    "title": title_data["title"],
                    "jobdiva_id": title_data["jobdiva_id"],
                    "openings": title_data.get("openings"),
                    "max_allowed_submittals": title_data.get("max_allowed_submittals"),
                    "processing_status": "partially_loaded"
                }
            }

        # STAGE 4: CHECK DRAFTS
        cursor.execute("SELECT job_id, title, workflow_status FROM job_drafts WHERE job_id = %s OR draft_id::text = %s", (job_id, job_id))
        draft_row = cursor.fetchone()
        if draft_row:
            data = {"job_id": draft_row[0], "title": f"{draft_row[1]} (draft)", "processing_status": draft_row[2] or "draft"}
            cursor.close()
            conn.close()
            return {"status": "success", "job_id": job_id, "data": data}

        cursor.close()
        conn.close()
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
        
    except Exception as e:
        logger.error(f"Get Monitored Job Data Error: {e}")
        if isinstance(e, HTTPException): raise e
        raise HTTPException(status_code=500, detail=f"Failed to get data: {str(e)}")

@app.post("/jobs/{job_id}/publish")
async def publish_job_draft(job_id: str, publish_request: JobPublishRequest):
    """
    Publish completed draft to live monitored_jobs table.
    This moves the draft to production and marks it as complete.
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Call the publish function
        cursor.execute(
            "SELECT publish_draft_to_live(%s)",
            (publish_request.draft_id,)
        )
        result = cursor.fetchone()[0]
        conn.commit()
        cursor.close()
        conn.close()
        
        if result == "Draft published successfully":
            logger.info(f"🚀 Published draft {publish_request.draft_id} for job {job_id}")
            return {
                "status": "success",
                "message": "Job published successfully",
                "job_id": job_id,
                "published_at": readable_ist_now()
            }
        else:
            raise HTTPException(status_code=400, detail=result)
            
    except Exception as e:
        logger.error(f"Publish Job Draft Error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to publish draft: {str(e)}")

@app.get("/drafts")
async def list_job_drafts(user_session: str = "default", workflow_status: str = None):
    """
    List all job drafts for a user session.
    Useful for "Continue Previous Work" functionality.
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        query = """
        SELECT * FROM draft_progress_overview 
        WHERE user_session = %s
        """
        params = [user_session]
        
        if workflow_status:
            query += " AND workflow_status = %s"
            params.append(workflow_status)
        
        query += " ORDER BY updated_at DESC"
        
        cursor.execute(query, params)
        drafts = cursor.fetchall()
        cursor.close()
        conn.close()
        
        return {
            "drafts": [
                {
                    "draft_id": str(draft[0]),
                    "job_id": draft[1],
                    "current_step": draft[3],
                    "workflow_status": draft[4],
                    "title": draft[8],
                    "hours_since_update": round(draft[16], 1),
                    "updated_at": str(draft[13])
                }
                for draft in drafts
            ],
            "total_count": len(drafts)
        }
        
    except Exception as e:
        logger.error(f"List Job Drafts Error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to list drafts: {str(e)}")

@app.delete("/drafts/{draft_id}")
async def delete_job_draft(draft_id: str):
    """
    Delete a job draft and its requirements.
    Useful for cleaning up abandoned drafts.
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute(
            "DELETE FROM job_drafts WHERE draft_id = %s",
            (draft_id,)
        )
        deleted_count = cursor.rowcount
        conn.commit()
        cursor.close()
        conn.close()
        
        if deleted_count > 0:
            logger.info(f"🗑️ Deleted draft {draft_id}")
            return {"status": "success", "message": "Draft deleted successfully"}
        else:
            raise HTTPException(status_code=404, detail="Draft not found")
            
    except Exception as e:
        logger.error(f"Delete Job Draft Error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to delete draft: {str(e)}")

@app.post("/jobs/{job_id}/monitor")
async def add_job_to_monitoring(job_id: str, background_tasks: BackgroundTasks):
    """
    Add a job to the monitoring list AND save to monitored_jobs database table.
    Call this when a user imports/views a job they want to track.
    """
    try:
        # Get initial status from JobDiva
        status_info = await jobdiva_service.get_job_status(job_id)
        
        # Prepare comprehensive job data for database storage
        monitoring_data = {
            "status": status_info.get("status", "OPEN"),
            "customer_name": status_info.get("customer_name", "Unknown"), 
            "title": status_info.get("title", ""),
            "work_authorization": status_info.get("work_authorization", ""),
            "jobdiva_description": status_info.get("description", ""),
            "city": status_info.get("city", ""),
            "state": status_info.get("state", ""),
            "location_type": status_info.get("location_type", "Onsite"),
            "employment_type": status_info.get("employment_type", ""),
            "pay_rate": status_info.get("pay_rate", ""),
            "posted_date": status_info.get("posted_date", ""),
            "start_date": status_info.get("start_date", ""),
            "openings": status_info.get("openings", ""),
            "priority": status_info.get("priority", ""),
            "program_duration": status_info.get("program_duration", ""),
            "max_allowed_submittals": status_info.get("max_allowed_submittals", ""),
            "processing_status": "monitoring_added",
            "created_at": readable_ist_now(),
            "updated_at": readable_ist_now()
        }
        
        # Save to database (primary storage)
        db_success = jobdiva_service.monitor_job_locally(job_id, monitoring_data)
        if not db_success:
            logger.error(f"❌ Failed to save job {job_id} to monitored_jobs database")
            raise HTTPException(status_code=500, detail="Failed to save job to database")
            
        logger.info(f"✅ Job {job_id} added to monitored_jobs database with status: {status_info.get('status', 'OPEN')}")
        
        # Also update legacy file-based system for compatibility (optional)
        try:
            jobs_data = load_monitored_jobs()
            jobs_data["jobs"][job_id] = {
                "status": status_info.get("status", "OPEN"),
                "customer_name": status_info.get("customer_name", "Unknown"),
                "title": status_info.get("title", ""),
                "work_authorization": status_info.get("work_authorization", ""),
                "priority": status_info.get("priority", ""),
                "program_duration": status_info.get("program_duration", ""),
                "max_allowed_submittals": status_info.get("max_allowed_submittals", ""),
                "created_at": readable_ist_now(),
                "updated_at": readable_ist_now()
            }
            save_monitored_jobs(jobs_data)
            logger.info(f"📁 Also updated legacy file-based monitoring for job {job_id}")
        except Exception as legacy_error:
            logger.warning(f"⚠️ Legacy file update failed for job {job_id}: {legacy_error}")
            # Don't fail the request if legacy update fails
        
        return {
            "message": f"Job {job_id} added to monitoring and saved to database",
            "current_status": status_info.get("status", "OPEN"),
            "database_saved": True,
            "job_id": job_id
        }
        
    except Exception as e:
        logger.error(f"❌ Error adding job {job_id} to monitoring: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to add job to monitoring: {str(e)}")

@app.post("/jobs/create")
async def create_new_job(job_data: Dict[str, Any]):
    """
    Create a new job from scratch and save it to monitored_jobs database.
    Used when manually creating jobs rather than importing from JobDiva.
    """
    try:
        # Generate job_id if not provided
        job_id = job_data.get("job_id") or f"MANUAL_{int(time.time())}"
        
        # Prepare job data with defaults
        monitoring_data = {
            # Core required fields
            "job_id": job_id,
            "title": job_data.get("title", "New Job"),
            "customer_name": job_data.get("customer_name", "Manual Entry"),
            "status": job_data.get("status", "OPEN"),
            
            # Location information  
            "city": job_data.get("city", ""),
            "state": job_data.get("state", ""),
            "zip_code": job_data.get("zip_code", ""),
            "location_type": job_data.get("location_type", "Onsite"),
            
            # Job details
            "jobdiva_description": job_data.get("description", ""),
            "ai_description": job_data.get("ai_description", ""),
            "employment_type": job_data.get("employment_type", ""),
            "work_authorization": job_data.get("work_authorization", ""),
            "pay_rate": job_data.get("pay_rate", ""),
            "openings": job_data.get("openings", "1"),
            
            # Additional Job Details
            "priority": job_data.get("priority", ""),
            "program_duration": job_data.get("program_duration", ""),
            "max_allowed_submittals": job_data.get("max_allowed_submittals", ""),
            
            # Dates
            "posted_date": job_data.get("posted_date", readable_ist_now()),
            "start_date": job_data.get("start_date", ""),
            
            # Configuration defaults
            "recruiter_notes": job_data.get("recruiter_notes", ""),
            "recruiter_emails": json.dumps(job_data.get("recruiter_emails", [])),
            "selected_employment_types": json.dumps(job_data.get("selected_employment_types", [])),
            "selected_job_boards": json.dumps(job_data.get("selected_job_boards", [])),
            "screening_level": job_data.get("screening_level", "L1.5"),
            "pair_enabled": job_data.get("pair_enabled", True),
            "pair_enhanced": job_data.get("pair_enhanced", False),
            "processing_status": "manual_created",
            
            # Timestamps
            "created_at": readable_ist_now(),
            "updated_at": readable_ist_now()
        }
        
        # Save to monitored_jobs database
        success = jobdiva_service.monitor_job_locally(job_id, monitoring_data)
        
        if success:
            logger.info(f"✅ Successfully created and saved new job {job_id} to database")
            return {
                "status": "success",
                "message": f"Job {job_id} created and saved to database",
                "job_id": job_id,
                "data": monitoring_data
            }
        else:
            logger.error(f"❌ Failed to save new job {job_id} to database")
            raise HTTPException(status_code=500, detail="Failed to save job to database")
            
    except Exception as e:
        logger.error(f"❌ Error creating job: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to create job: {str(e)}")

@app.delete("/jobs/{job_id}/monitor")
async def remove_job_from_monitoring(job_id: str):
    """
    Remove a job from monitoring (e.g., when permanently closed or no longer relevant).
    """
    jobs_data = load_monitored_jobs()
    
    if job_id in jobs_data["jobs"]:
        del jobs_data["jobs"][job_id]
        save_monitored_jobs(jobs_data)
        logger.info(f"🗑️ Removed Job {job_id} from monitoring")
        return {"message": f"Job {job_id} removed from monitoring"}
    else:
        raise HTTPException(status_code=404, detail="Job not in monitoring list")

@app.get("/jobs/monitored")
async def get_monitored_jobs(include_archived: bool = False):
    """
    Get all jobs currently being monitored from the database with live candidate stats.
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Ensure is_archived column exists
        try:
            cursor.execute("""
                ALTER TABLE monitored_jobs 
                ADD COLUMN IF NOT EXISTS is_archived BOOLEAN DEFAULT FALSE
            """)
            conn.commit()
        except Exception as e:
            logger.warning(f"Could not add is_archived column: {e}")
        
        # Fetch all columns from monitored_jobs, optionally filtering out archived
        if include_archived:
            cursor.execute("SELECT * FROM monitored_jobs WHERE is_archived = TRUE ORDER BY created_at DESC")
        else:
            cursor.execute("SELECT * FROM monitored_jobs WHERE is_archived = FALSE OR is_archived IS NULL ORDER BY created_at DESC")
        
        columns = [desc[0] for desc in cursor.description]
        rows = cursor.fetchall()
        
        jobs = {}
        job_id_map = {}  # jid -> {"job_id": ..., "jobdiva_id": ...}
        for row in rows:
            job_data = dict(zip(columns, row))
            jid = str(job_data.get("jobdiva_id") or job_data.get("job_id"))
            
            if job_data.get("created_at") and hasattr(job_data["created_at"], "isoformat"):
                job_data["created_at"] = job_data["created_at"].isoformat()
            if job_data.get("updated_at") and hasattr(job_data["updated_at"], "isoformat"):
                job_data["updated_at"] = job_data["updated_at"].isoformat()
            
            jobs[jid] = job_data
            job_id_map[jid] = {
                "job_id": str(job_data.get("job_id") or ""),
                "jobdiva_id": str(job_data.get("jobdiva_id") or ""),
            }
            
        # --- Live candidate stats from sourced_candidates ---
        try:
            cursor.execute("""
                SELECT
                    sc.jobdiva_id AS sc_key,
                    COUNT(*) AS candidates_sourced,
                    COUNT(*) FILTER (
                        WHERE (sc.resume_match_percentage) IS NOT NULL
                          AND (sc.resume_match_percentage)::numeric >= 70
                    ) AS resumes_shortlisted,
                    COUNT(*) FILTER (
                        WHERE (sc.resume_match_percentage) IS NOT NULL
                          AND (sc.resume_match_percentage)::numeric >= 70
                    ) AS pass_submissions,
                    COUNT(*) FILTER (
                        WHERE sc.data->>'engage_status' IS NOT NULL
                          AND sc.data->>'engage_status' != ''
                    ) AS complete_submissions,
                    COUNT(*) FILTER (
                        WHERE sc.data->>'engage_status' ILIKE '%%pass%%'
                    ) AS pair_external_subs,
                    COUNT(*) FILTER (
                        WHERE sc.data->>'engage_completed_at' IS NOT NULL
                          AND sc.data->>'engage_completed_at' != ''
                    ) AS feedback_completed,
                    COALESCE(ROUND(AVG(
                        CASE
                            WHEN sc.data->>'engage_completed_at' IS NOT NULL
                             AND sc.data->>'engage_completed_at' != ''
                            THEN EXTRACT(EPOCH FROM (
                                (sc.data->>'engage_completed_at')::timestamp - sc.created_at
                            )) / 60.0
                        END
                    )), 0)::int AS time_to_first_pass
                FROM sourced_candidates sc
                GROUP BY sc.jobdiva_id
            """)
            stats_cols = [desc[0] for desc in cursor.description]
            stats_lookup = {}
            for srow in cursor.fetchall():
                sdata = dict(zip(stats_cols, srow))
                sc_key = str(sdata.pop("sc_key", "") or "")
                if sc_key:
                    stats_lookup[sc_key] = sdata

            for jid, job_data in jobs.items():
                ids = job_id_map.get(jid, {})
                stats = (
                    stats_lookup.get(ids.get("jobdiva_id", ""))
                    or stats_lookup.get(ids.get("job_id", ""))
                    or stats_lookup.get(jid)
                    or {}
                )
                job_data["candidates_sourced"]   = int(stats.get("candidates_sourced",   0) or 0)
                job_data["resumes_shortlisted"]  = int(stats.get("resumes_shortlisted",  0) or 0)
                job_data["complete_submissions"] = int(stats.get("complete_submissions", 0) or 0)
                job_data["pass_submissions"]     = int(stats.get("pass_submissions",     0) or 0)
                job_data["pair_external_subs"]   = int(stats.get("pair_external_subs",   0) or 0)
                job_data["feedback_completed"]   = int(stats.get("feedback_completed",   0) or 0)
                job_data["time_to_first_pass"]   = int(stats.get("time_to_first_pass",   0) or 0)

        except Exception as stats_err:
            logger.warning(f"Could not compute candidate stats: {stats_err}")
            for job_data in jobs.values():
                for field in ["candidates_sourced", "resumes_shortlisted", "complete_submissions",
                              "pass_submissions", "pair_external_subs", "feedback_completed", "time_to_first_pass"]:
                    job_data.setdefault(field, 0)

        cursor.close()
        conn.close()
        
        return {
            "jobs": jobs,
            "total_count": len(jobs),
            "source": "database"
        }
    except Exception as e:
        logger.error(f"Error fetching monitored jobs from DB: {e}")
        # Fallback to legacy file only on catastrophic DB failure
        jobs_data = load_monitored_jobs()
        return jobs_data

@app.post("/jobs/poll-now")
async def trigger_manual_poll(background_tasks: BackgroundTasks):
    """
    Manually trigger a status poll for all monitored jobs.
    Useful for testing or immediate sync.
    """
    background_tasks.add_task(poll_all_jobs)
    return {"message": "Manual poll triggered"}

@app.get("/jobs/{job_id}/sync")
async def sync_job_status(job_id: str):
    """
    Check the current status of a job in JobDiva and update the monitoring data.
    Useful for ensuring the local job state matches JobDiva (e.g. if Closed).
    """
    logger.info(f"Syncing status for Job {job_id}")
    try:
        status_info = await jobdiva_service.get_job_status(job_id)
        logger.info(f"Sync result for {job_id}: {status_info}")
        
        # Safety Check: Do not overwrite with NOT_FOUND if we already have valid data
        if status_info.get("status") == "NOT_FOUND":
            logger.warning(f"⚠️ Sync returned NOT_FOUND for {job_id}. Database remains unchanged.")
            return {"job_id": job_id, "status": "NOT_FOUND_SKIPPED", "message": "Preserved existing status"}

        # 1. Update the Database (Primary Storage)
        # Prepare update payload
        update_data = {
            "status": status_info.get("status"),
            "customer_name": status_info.get("customer_name"),
            "title": status_info.get("title"),
            "updated_at": readable_ist_now()
        }
        
        # Use our safe monitor_job_locally which has the "Unknown" shield
        db_ok = jobdiva_service.monitor_job_locally(job_id, update_data)
        if db_ok:
            logger.info(f"✅ Synced status for {job_id} to database")
        
        # 2. Update legacy monitored jobs file for compatibility
        try:
            jobs_data = load_monitored_jobs()
            if job_id in jobs_data.get("jobs", {}):
                old_data = jobs_data["jobs"][job_id]
                jobs_data["jobs"][job_id].update({
                    "status": status_info["status"],
                    # Only update name if it's not Unknown
                    "customer_name": status_info.get("customer_name") or old_data.get("customer_name", "Unknown"),
                    "title": status_info.get("title") or old_data.get("title", ""),
                })
                jobs_data["last_sync"] = readable_ist_now()
                save_monitored_jobs(jobs_data)
                logger.info(f"📁 Updated legacy monitoring file for {job_id}")
        except: pass # Don't fail if legacy file sync errors
        
        return status_info
    except Exception as e:
        logger.error(f"Sync failed for job {job_id}: {e}")
        return {"job_id": job_id, "status": "ERROR", "error": str(e)}

@app.post("/chat", response_model=ChatResponse)
async def chat_with_aria(request: ChatRequest):
    response = await chat_service.get_response(request.message, request.history)
    return {"response": response}

@app.get("/api/jobs/{job_id}/criteria", response_model=JobCriteriaResponse)
async def get_job_criteria(job_id: str):
    """Fetch criteria for a job."""
    criteria = criteria_service.get_job_criteria(job_id)
    return JobCriteriaResponse(job_id=job_id, criteria=criteria)

@app.post("/api/jobs/{job_id}/criteria/sync", response_model=JobCriteriaResponse)
async def sync_job_criteria(job_id: str):
    """Wait for criteria to be generated then return them."""
    criteria = await criteria_service.generate_and_save_criteria(job_id)
    return JobCriteriaResponse(job_id=job_id, criteria=criteria)

@app.put("/api/jobs/{job_id}/criteria")
async def update_job_criteria(job_id: str, update: JobCriteriaUpdate):
    """Manually update criteria for a job."""
    success = criteria_service.save_criteria(job_id, [c.dict() for c in update.criteria])
    if success:
        return {"status": "SUCCESS"}
    return {"status": "ERROR"}


# =====================================================  
# RONAK SKILLS INTEGRATION ENDPOINTS
# =====================================================

@app.post("/jobs/{job_id}/extract-skills", response_model=SkillsExtractionResponse)
async def extract_job_skills(job_id: str, request: SkillsExtractionRequest):
    """
    Extract and map skills from job descriptions using Ronak's skills ontology.
    Uses AI to analyze JobDiva descriptions, AI enhancements, and recruiter notes.
    """
    try:
        from services.job_skills_extractor import JobSkillsExtractor
        from services.job_skills_db import JobSkillsDB
        
        # Initialize extractor with Ronak's ontology
        extractor = JobSkillsExtractor(OPENAI_API_KEY)
        
        # Extract skills and map to Ronak's ontology
        analysis = extractor.analyze_job_skills(
            job_id=job_id,
            jobdiva_description=request.jobdiva_description,
            ai_description=request.ai_description,
            recruiter_notes=request.recruiter_notes
        )
        
        # Save to database  
        db_service = JobSkillsDB()
        save_result = db_service.save_job_skills(
            job_id=job_id,
            extracted_skills=analysis.extracted_skills,
            analysis_metadata=analysis.analysis_metadata
        )
        
        logger.info(f"✅ Extracted and saved {save_result['skills_saved']} skills for job {job_id}")
        
        # Format response
        from models import ExtractedSkillResponse
        formatted_skills = [
            ExtractedSkillResponse(
                skill_id=skill.skill_id,
                normalized_name=skill.normalized_name,
                original_text=skill.original_text,
                importance=skill.importance,
                min_years=skill.min_years,
                confidence=skill.confidence
            )
            for skill in analysis.extracted_skills
        ]
        
        return SkillsExtractionResponse(
            job_id=job_id,
            extracted_skills=formatted_skills,
            unmapped_skills=analysis.unmapped_skills,
            analysis_metadata=analysis.analysis_metadata,
            mapping_rate=analysis.analysis_metadata.get('mapping_rate', 0.0)
        )
        
    except Exception as e:
        logger.error(f"Skills extraction error for job {job_id}: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Skills extraction failed: {str(e)}")

@app.get("/jobs/{job_id}/skills", response_model=JobSkillsSummaryResponse)
async def get_job_skills(job_id: str):
    """
    Get skills summary for a job that has already been analyzed.
    Returns skills stored in database with importance breakdown.
    """
    try:
        from services.job_skills_db import JobSkillsDB
        
        db_service = JobSkillsDB()
        summary = db_service.get_skills_summary(job_id)
        
        if summary['total_skills'] == 0:
            raise HTTPException(
                status_code=404, 
                detail=f"No skills found for job {job_id}. Extract skills first using /jobs/{job_id}/extract-skills"
            )
        
        return JobSkillsSummaryResponse(
            job_id=job_id,
            total_skills=summary['total_skills'],
            by_importance=summary['by_importance'],
            analysis_metadata=summary['analysis_metadata']
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Get job skills error for job {job_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get job skills: {str(e)}")

@app.get("/jobs/{job_id}/skills/detailed")
async def get_detailed_job_skills(job_id: str):
    """
    Get detailed skills data for a job including all extracted skills with metadata.
    Useful for Step 3 requirements display and debugging.
    """
    try:
        from services.job_skills_db import JobSkillsDB
        
        db_service = JobSkillsDB()
        skills = db_service.get_job_skills(job_id)
        
        if not skills:
            raise HTTPException(
                status_code=404, 
                detail=f"No skills found for job {job_id}. Extract skills first using /jobs/{job_id}/extract-skills"
            )
        
        return {
            "job_id": job_id,
            "skills": skills,
            "total_count": len(skills),
            "last_extraction": skills[0].get('extracted_at') if skills else None
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Get detailed job skills error for job {job_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get detailed job skills: {str(e)}")

# =====================================================  
# JOB BASIC INFO UPDATE ENDPOINT
# =====================================================

@app.put("/jobs/{job_id}/basic-info")
async def update_job_basic_info(job_id: str, update: JobBasicInfoUpdate):
    """Update basic job information like employment type and recruiter notes."""
    try:
        logger.info(f"Updating basic info for job {job_id}: {update}")
        
        # Update the monitored_jobs table directly using jobdiva_service
        success = jobdiva_service.update_job_basic_info(job_id, update.dict(exclude_unset=True))
        
        if success:
            return {"status": "SUCCESS", "job_id": job_id}
        else:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
            
    except Exception as e:
        logger.error(f"Error updating basic info for job {job_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to update job: {str(e)}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
