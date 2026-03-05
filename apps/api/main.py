from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Dict, Any
import asyncio
import logging
import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from contextlib import asynccontextmanager

# Load environment variables
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
    CandidateAnalysisRequest, CandidateAnalysisResponse
)
from matcher import mock_match_candidates
from services.extractor import llm_extractor
from services.jobdiva import jobdiva_service
from services.unipile import unipile_service
from services.chat_service import chat_service

# Simple file-based job tracking (in production, use proper DB)
JOBS_DB_FILE = "monitored_jobs.json"

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
    
from routers import engagement, gemini

app = FastAPI(title="Hoonr.ai API", lifespan=lifespan)
app.include_router(gemini.router, prefix="/api/v1/gemini")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Allow all origins for local dev
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.post("/jobs/parse", response_model=ParsedJobResponse)
async def parse_job_description(request: ParsedJobRequest):
    """
    Parses raw text JD into structured format (skills, location, etc).
    """
    try:
        data = await llm_extractor.extract_from_jd(request.text)
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
    Fetches Full Job Details from JobDiva by ID.
    Automatically adds the job to monitoring list.
    """
    job = await jobdiva_service.get_job_by_id(request.job_id)
    if not job:
         raise HTTPException(status_code=404, detail="Job not found in JobDiva")
    
    # Auto-add to monitoring when imported
    background_tasks.add_task(add_job_to_monitoring_internal, request.job_id)
    
    return job

async def add_job_to_monitoring_internal(job_id: str):
    """Internal function to add job to monitoring without HTTP response"""
    try:
        jobs_data = load_monitored_jobs()
        
        # Get initial status
        status_info = await jobdiva_service.get_job_status(job_id)
        
        # Add to monitoring
        jobs_data["jobs"][job_id] = {
            "status": status_info["status"],
            "customer": status_info.get("customer", "Unknown"),
            "title": status_info.get("title", ""),
            "added_at": readable_ist_now(),
            "last_updated": readable_ist_now()
        }
        
        save_monitored_jobs(jobs_data)
        logger.info(f"📋 Auto-added Job {job_id} to monitoring")
        
        # Reset 5-minute timer since new job was added
        schedule_next_poll()
    except Exception as e:
        logger.error(f"Failed to auto-add job {job_id} to monitoring: {e}")

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
    """Load the list of jobs being monitored from file"""
    if os.path.exists(JOBS_DB_FILE):
        try:
            with open(JOBS_DB_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading monitored jobs: {e}")
    return {"jobs": {}, "last_sync": None}

def save_monitored_jobs(jobs_data: Dict[str, Any]):
    """Save the monitored jobs data to file"""
    try:
        with open(JOBS_DB_FILE, 'w') as f:
            json.dump(jobs_data, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving monitored jobs: {e}")

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
    
    # Update local tracking
    import time
    updated_jobs = {}
    changes_detected = []
    
    for status in statuses:
        job_id = status["job_id"]
        current_status = status["status"]
        old_data = jobs_data["jobs"].get(job_id, {})
        old_status = old_data.get("status", "UNKNOWN")
        
        # Track changes
        if old_status != current_status:
            changes_detected.append({
                "job_id": job_id,
                "old_status": old_status,
                "new_status": current_status,
                "title": status.get("title", "")
            })
        
        # Merge poll result into existing entry so ai_description / job_notes are preserved
        updated_jobs[job_id] = {
            **old_data,                                      # keep everything (ai_description, job_notes, etc.)
            "status":       current_status,
            "customer":     status.get("customer", "Unknown"),
            "title":        status.get("title", ""),
            "last_updated": readable_ist_now(),
        }
    
    # Save updated data
    jobs_data["jobs"] = updated_jobs
    jobs_data["last_sync"] = readable_ist_now()
    save_monitored_jobs(jobs_data)
    
    if changes_detected:
        logger.info(f"📢 Status changes detected: {changes_detected}")
    else:
        logger.info("✅ No status changes detected")
    
    # Schedule next poll 5 minutes from now
    schedule_next_poll()
    
    return {"polled": len(job_ids), "changes": changes_detected}

@app.post("/jobs/{job_id}/monitor")
async def add_job_to_monitoring(job_id: str, background_tasks: BackgroundTasks):
    """
    Add a job to the monitoring list so its status gets polled regularly.
    Call this when a user imports/views a job they want to track.
    """
    
    jobs_data = load_monitored_jobs()
    
    # Get initial status
    status_info = await jobdiva_service.get_job_status(job_id)
    
    # Add to monitoring
    jobs_data["jobs"][job_id] = {
        "status": status_info["status"],
        "customer": status_info.get("customer", "Unknown"),
        "title": status_info.get("title", ""),
        "added_at": readable_ist_now(),
        "last_updated": readable_ist_now()
    }
    
    save_monitored_jobs(jobs_data)
    
    logger.info(f"📋 Added Job {job_id} to monitoring with status: {status_info['status']}")
    
    return {
        "message": f"Job {job_id} added to monitoring",
        "current_status": status_info["status"],
        "total_monitored": len(jobs_data["jobs"])
    }

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
                "customer": status_info.get("customer", "Unknown"),
                "title": status_info.get("title", ""),
                "last_updated": readable_ist_now(),
                "added_at": old_data.get("added_at", readable_ist_now())
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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
