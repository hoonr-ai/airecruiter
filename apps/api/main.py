from fastapi import FastAPI, HTTPException, BackgroundTasks, Body
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
from contextlib import asynccontextmanager

from core import (
    OPENAI_API_KEY, GEMINI_API_KEY, DATABASE_URL, 
    JOBDIVA_JOB_NOTES_UDF_ID, ALLOWED_ORIGINS, GEMINI_MODEL
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
    ChatRequest, ChatResponse, CandidateSearchRequest, CandidateMessageRequest, JobFetchRequest,
    CandidateAnalysisRequest, CandidateAnalysisResponse, JobCriterion, JobCriteriaResponse,
    JobCriteriaUpdate, JobDraftData, JobDraftRequirement, JobDraftRequirements, 
    JobDraftResponse, JobPublishRequest, JobBasicInfoUpdate, SkillsExtractionRequest, SkillsExtractionResponse,
    JobSkillsSummaryResponse
)
from services.criteria import criteria_service
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
    
from routers import engagement, gemini, voice_agent

app = FastAPI(title="Hoonr.ai API", lifespan=lifespan)
app.include_router(gemini.router, prefix="/api/v1/gemini")
app.include_router(voice_agent.router, prefix="/api/v1/voice")

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
        
        # If job_id is provided, save the parsed data to the database
        if request.job_id:
            storage = MonitoredJobsStorage()
            processing_metadata = {
                "model": GEMINI_MODEL,
                "processing_time_ms": 0,  # Could track actual processing time
                "tokens_used": 0,  # Could track actual token usage
                "confidence": 0.8
            }
            
            success = storage.update_job_with_extracted_data(
                job_id=request.job_id,
                extracted_data=data,
                processing_metadata=processing_metadata
            )
            
            if success:
                logger.info(f"✅ Saved processed data for job {request.job_id}")
            else:
                logger.warning(f"⚠️ Failed to save processed data for job {request.job_id}")
        
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
    Searches JobDiva, Vetted Database, AND LinkedIn (via Unipile) for candidates.
    """
    # Determine effective location based on location_type
    effective_location = None if request.location_type.lower() == "remote" else request.location
    
    print(f"🔥 DEBUG: SEARCH REQUEST: {request.model_dump_json()}")
    print(f"🔥 DEBUG: SEARCH: location_type={request.location_type}, effective_location={effective_location}, sources={request.sources}, open_to_work={request.open_to_work}")
    
    # 1. Define Helper Wrapper
    async def safe_search(coro, name):
        try:
            print(f"🔍 Starting {name} search...")
            result = await asyncio.wait_for(coro, timeout=30.0)
            print(f"✅ {name} returned {len(result)} results")
            return result
        except asyncio.TimeoutError:
            print(f"⚠️ {name} Search Timed Out (>30s). Skipping.")
            return []
        except Exception as e:
            print(f"❌ {name} Search Failed: {e}")
            import traceback
            traceback.print_exc()
            return []

    # 2. Prepare Tasks
    tasks = []
    
    # Task A: JobDiva
    if "JobDiva" in request.sources:
        tasks.append(safe_search(
            jobdiva_service.search_candidates(request.skills, effective_location, request.page, request.limit), 
            "JobDiva"
        ))
    else:
        tasks.append(asyncio.sleep(0, result=[]))

    # Task B: Vetted DB
    if "VettedDB" in request.sources:
        skill_names = []
        for s in request.skills:
             if isinstance(s, dict): skill_names.append(s.get("name"))
             elif hasattr(s, "name"): skill_names.append(s.name)
             else: skill_names.append(str(s))
        
        from services.vetted import vetted_service
        tasks.append(safe_search(
            vetted_service.search_candidates(skill_names, effective_location, request.page, request.limit),
            "VettedDB"
        ))
    else:
        tasks.append(asyncio.sleep(0, result=[]))

    # Task C: LinkedIn (Unipile)
    if "LinkedIn" in request.sources:
        tasks.append(safe_search(
            unipile_service.search_candidates(
                request.skills, 
                effective_location, 
                request.open_to_work, 
                25 if request.limit > 25 else request.limit # Cap at 25 as requested
            ),
            "LinkedIn"
        ))
    else:
        tasks.append(asyncio.sleep(0, result=[]))

    # 3. Execute in Parallel
    results = await asyncio.gather(*tasks)
    
    jd_results = results[0] if isinstance(results[0], list) else []
    vet_results = results[1] if isinstance(results[1], list) else []
    li_results = results[2] if isinstance(results[2], list) else []
    
    print(f"✅ SEARCH COMPLETE: JobDiva={len(jd_results)}, Vetted={len(vet_results)}, LinkedIn={len(li_results)}")
    
    # 4. Combine
    if request.page == 1:
        # Prioritize Vetted, then LinkedIn, then JobDiva? Or Mix?
        # User implies LinkedIn is "extra".
        combined = vet_results + li_results + jd_results
    else:
        combined = jd_results # Pagination logic weak, assume others fit in page 1
        
    return combined

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
                
            # Merge saved selection fields (Draft Data)
            if "selected_employment_types" in local_data and local_data["selected_employment_types"]:
                val = local_data["selected_employment_types"]
                try:
                    job["selected_employment_types"] = json.loads(val) if isinstance(val, str) else val
                except:
                    job["selected_employment_types"] = []
                    
            if "recruiter_emails" in local_data and local_data["recruiter_emails"]:
                val = local_data["recruiter_emails"]
                try:
                    job["recruiter_emails"] = json.loads(val) if isinstance(val, str) else val
                except:
                    job["recruiter_emails"] = []
            
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

@app.get("/candidates/{candidate_id}/resume")
async def get_candidate_resume(candidate_id: str):
    """
    Fetches the resume text for a candidate.
    Waterfall: LinkedIn (via Unipile + AI), JobDiva, then Vetted API.
    """
    # 1. LinkedIn (Unipile)
    if candidate_id.startswith("unipile_"):
        real_id = candidate_id.replace("unipile_", "")
        print(f"🔍 Fetching LinkedIn Profile for {real_id}...")
        profile = await unipile_service.get_candidate_profile(real_id)
        
        if profile:
            print(f"✅ Profile found. Generating Resume with AI...")
            resume_text = await ai_service.generate_resume_from_profile(profile)
            return {"resume_text": resume_text}
        else:
            raise HTTPException(status_code=404, detail="LinkedIn Profile not found or accessible")

    try:
        resume_text = await jobdiva_service.get_candidate_resume(candidate_id)
        
        # Check if JobDiva returned error string (it doesn't raise Exception)
        if not resume_text or "Resume content unavailable" in resume_text:
             raise Exception("JobDiva Resume Not Found")
             
        return {"resume_text": resume_text}
    except Exception:
        # If JobDiva fails (404), try Vetted DB
        try:
            from services.vetted import vetted_service
            resume_text = await vetted_service.get_candidate_resume(candidate_id)
            if resume_text:
                return {"resume_text": resume_text}
            raise HTTPException(status_code=404, detail="Resume not found in any source")
        except Exception:
            raise HTTPException(status_code=404, detail="Resume not found")

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
        
        # UPDATE in-place. This preserves ai_description, job_notes, etc.
        old_data.update({
            "status":       current_status,
            "customer_name": status.get("customer_name", "Unknown"),
            "title":        status.get("title", ""),
        })
    
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
                jobdiva_id = %s, -- This is the reference string
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
            ref_code,                                           # 26-06182 (swapped)
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
                    processing_status, current_step, jobdiva_id, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
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
                ref_code                                              # 26-06182 (Ref)
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
                "bot_introduction": job_row.get("bot_introduction") or ""
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
                    work_authorization, screening_level, current_step, user_session, 
                    ai_enhanced, processing_status, created_at, updated_at, jobdiva_id
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW(), %s
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
        
        cursor.execute("""
            SELECT job_id, title, enhanced_title, ai_description, selected_job_boards,
                   recruiter_notes, recruiter_emails, selected_employment_types,
                   work_authorization, screening_level, current_step, processing_status,
                   job_requirements, ai_enhanced, created_at, updated_at
            FROM monitored_jobs 
            WHERE job_id = %s
        """, (job_id,))
        
        row = cursor.fetchone()
        cursor.close()
        conn.close()
        
        if not row:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found in monitored_jobs")
        
        # Column names for reference
        columns = ["job_id", "title", "enhanced_title", "ai_description", "selected_job_boards",
                   "recruiter_notes", "recruiter_emails", "selected_employment_types", 
                   "work_authorization", "screening_level", "current_step", "processing_status",
                   "job_requirements", "ai_enhanced", "created_at", "updated_at"]
        
        data = dict(zip(columns, row))
        
        # Parse JSON fields for better readability
        for json_field in ["selected_job_boards", "recruiter_emails", "selected_employment_types", "job_requirements"]:
            if data[json_field]:
                try:
                    data[json_field] = json.loads(data[json_field]) if isinstance(data[json_field], str) else data[json_field]
                except:
                    pass
        
        # Convert datetime objects to strings
        if data.get("created_at"):
            data["created_at"] = data["created_at"].isoformat()
        if data.get("updated_at"):
            data["updated_at"] = data["updated_at"].isoformat()
        
        logger.info(f"📊 Retrieved monitored_jobs data for {job_id}")
        return {
            "status": "success",
            "job_id": job_id,
            "data": data
        }
        
    except Exception as e:
        logger.error(f"Get Monitored Job Data Error: {e}")
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
async def get_monitored_jobs():
    """
    Get all jobs currently being monitored and their latest statuses.
    """
    jobs_data = load_monitored_jobs()
    return {
        "jobs": jobs_data.get("jobs", {}),
        "total_count": len(jobs_data.get("jobs", {})),
        "last_sync": jobs_data.get("last_sync")
    }

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
        logger.info(f"Sync result: {status_info}")
        
        # Update monitored jobs file with latest data
        jobs_data = load_monitored_jobs()
        if job_id in jobs_data.get("jobs", {}):
            old_data = jobs_data["jobs"][job_id]
            jobs_data["jobs"][job_id] = {
                "status": status_info["status"],
                "customer_name": status_info.get("customer_name", "Unknown"),
                "title": status_info.get("title", ""),
                "work_authorization": status_info.get("work_authorization") or old_data.get("work_authorization", ""),
            }
            jobs_data["last_sync"] = readable_ist_now()
            save_monitored_jobs(jobs_data)
            logger.info(f"Updated monitoring data for job {job_id}")
            
            # Reset 5-minute timer since user manually reloaded
            schedule_next_poll()
        
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
