from fastapi import FastAPI, HTTPException, BackgroundTasks, Body, Query, UploadFile, File
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
    JobSkillsSummaryResponse, ExternalJobCreateRequest, ManualCandidateRequest
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
    
    # 1. Schedule job status polling (existing)
    schedule_next_poll()

    # 2. Schedule "Always-On" JobDiva Sync (Zero-Setup / Production-Safe)
    from services.auto_assign_service import auto_assign_service
    
    async def auto_sync_all_jobs():
        """
        Global sync agent that runs inside the app process.
        Uses a simple 'skip loop' if a sync is already in progress.
        """
        if getattr(app, "sync_in_progress", False):
            logger.info("🤖 [AutoSync] Cycle skipped: A sync is already running.")
            return
            
        app.sync_in_progress = True
        logger.info("🤖 [AutoSync] Starting built-in 15-minute synchronization cycle...")
        
        try:
            from psycopg2.extras import RealDictCursor
            conn = get_db_connection()
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SELECT job_id, title FROM monitored_jobs")
            jobs = cur.fetchall()
            cur.close()
            conn.close()
            
            if not jobs:
                logger.info("🤖 [AutoSync] No jobs to sync.")
                return

            for job in jobs:
                jid = job['job_id']
                logger.info(f"🤖 [AutoSync] Syncing: {job.get('title', jid)}")
                await auto_assign_service.synchronize_job_applicants(jid)
                await asyncio.sleep(2) # Prevent hammering the API
                
            logger.info(f"✅ [AutoSync] Cycle complete for {len(jobs)} jobs.")
        except Exception as e:
            logger.error(f"❌ [AutoSync] Cycle failed: {e}")
        finally:
            app.sync_in_progress = False

    # Interval: Every 15 minutes
    scheduler.add_job(auto_sync_all_jobs, "interval", minutes=15, id="always_on_sync")
    
    # Trigger first run immediately on startup
    asyncio.create_task(auto_sync_all_jobs())

    yield
    
    # Shutdown
    logger.info("📋 Stopping scheduler...")
    scheduler.shutdown()
    
from routers import engagement, ai_generation, voice_agent, boolean_agent, candidate_processing, job_archive
from routers import chat as chat_router
from routers import job_criteria as job_criteria_router
from routers import manual_candidates as manual_candidates_router
from routers import candidates as candidates_router
from routers import jobs as jobs_router

app = FastAPI(title="Hoonr.ai API", lifespan=lifespan)
app.include_router(ai_generation.router, prefix="/api/v1/ai-generation")
app.include_router(ai_generation.router, prefix="/api/v1/gemini")
app.include_router(voice_agent.router, prefix="/api/v1/voice")
app.include_router(boolean_agent.router, prefix="/api/v1/boolean")
app.include_router(candidate_processing.router, prefix="/api/v1/candidates")
app.include_router(job_archive.router)
app.include_router(chat_router.router)
app.include_router(job_criteria_router.router)
app.include_router(manual_candidates_router.router)
app.include_router(candidates_router.router)
app.include_router(jobs_router.router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)












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



# =====================================================
# JOB DRAFTS API ENDPOINTS
# =====================================================

from core.db import get_db_connection  # re-exported for callers that import from main



























if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
