from fastapi import FastAPI, HTTPException, BackgroundTasks, Body, Query, UploadFile, File, Request
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

# Setup structured logging. JSON by default (override LOG_FORMAT=text for
# local dev readability) and picks up LOG_LEVEL from env. New Relic /
# Datadog / OpenTelemetry can layer on later with zero code change.
from core.logging import configure_logging, RequestIDMiddleware
from core.amplitude import track_event_async
configure_logging()
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

    # 3. Initialize engagement audit table (moved out of module import in
    # engagement.py so a slow/locked DB can no longer crash-loop the app).
    # Wrapped in wait_for so even a hung DB does not block readiness.
    if engagement is not None and hasattr(engagement, "init_engagement_tables"):
        try:
            await asyncio.wait_for(engagement.init_engagement_tables(), timeout=10)
        except asyncio.TimeoutError:
            logger.error("engagement_audit_init_timeout (10s); continuing without init")
        except Exception as e:  # noqa: BLE001
            logger.error(f"engagement_audit_init_failed: {e}; continuing")

    # 4. Provision monitored_jobs columns once at startup. Previously two
    # handlers in routers/jobs.py ran `ALTER TABLE monitored_jobs ADD COLUMN
    # IF NOT EXISTS ...` on every request — ACCESS EXCLUSIVE lock queued
    # behind the auto-sync's shared lock, so `GET /jobs/monitored` could
    # stall for 60-90+ seconds. Run it once here; skip it in the hot path.
    if jobs_router is not None and hasattr(jobs_router, "init_monitored_jobs_schema"):
        try:
            await asyncio.wait_for(jobs_router.init_monitored_jobs_schema(), timeout=10)
        except asyncio.TimeoutError:
            logger.error("monitored_jobs_schema_init_timeout (10s); continuing")
        except Exception as e:  # noqa: BLE001
            logger.error(f"monitored_jobs_schema_init_failed: {e}; continuing")

    # 5. Provision sourced_candidates + candidate_enhanced_info schema.
    # Pre-v22: `_ensure_table` ran CREATE TABLE + 6x ALTER on every save, and
    # `save_candidate_enhanced_info` ran its own CREATE TABLE + ALTER + CREATE
    # INDEX per call. ALTER TABLE grabs ACCESS EXCLUSIVE and serialized every
    # concurrent reader/writer. Budget 15s because candidate_enhanced_info is
    # larger than monitored_jobs and the initial CREATE INDEX can take longer.
    try:
        from services import sourced_candidates_storage as _scs
        await asyncio.wait_for(_scs.init_sourced_candidates_schema(), timeout=15)
    except asyncio.TimeoutError:
        logger.error("sourced_candidates_schema_init_timeout (15s); continuing")
    except Exception as e:  # noqa: BLE001
        logger.error(f"sourced_candidates_schema_init_failed: {e}; continuing")

    # Delay first auto-sync 60s. Previously the sync ran immediately and held
    # the DB pool for minutes, while fresh user requests queued behind it —
    # manifesting as site-wide slowness right after every deploy. The 15-min
    # interval schedule above still catches everything; the delay just keeps
    # the cold-start window uncontested.
    async def _delayed_first_sync():
        await asyncio.sleep(60)
        await auto_sync_all_jobs()

    asyncio.create_task(_delayed_first_sync())

    yield
    
    # Shutdown
    logger.info("📋 Stopping scheduler...")
    scheduler.shutdown()
    
# Defensive router import. Previously a single `from routers import engagement,
# ai_generation, voice_agent, boolean_agent, candidate_processing, job_archive`
# meant one broken module (e.g. engagement's `_ensure_audit_table()` raising at
# import time on a slow/locked DB) blew up the whole import — so all six
# routers failed to register and FastAPI answered 404 for every `/api/v1/*`
# route beneath them. Load each module in isolation; log and continue on
# failure so an isolated outage in one router does not black-hole unrelated
# traffic (e.g. the AI-JD generator on `/api/v1/ai-generation`).
def _safe_import(module_name: str):
    try:
        return __import__(f"routers.{module_name}", fromlist=["router"])
    except Exception as e:  # noqa: BLE001 — broad by design; we never want import errors to crash boot
        logger.error(
            "router_import_failed",
            extra={"router": module_name, "error": str(e)},
            exc_info=True,
        )
        return None

engagement = _safe_import("engagement")
ai_generation = _safe_import("ai_generation")
voice_agent = _safe_import("voice_agent")
boolean_agent = _safe_import("boolean_agent")
candidate_processing = _safe_import("candidate_processing")
job_archive = _safe_import("job_archive")
chat_router = _safe_import("chat")
tira_router = _safe_import("tira")
job_criteria_router = _safe_import("job_criteria")
manual_candidates_router = _safe_import("manual_candidates")
candidates_router = _safe_import("candidates")
jobs_router = _safe_import("jobs")

# redirect_slashes=False: never auto-307 between `/foo` and `/foo/`. Behind the
# prod reverse proxy a 307 with the wrong scheme (when uvicorn isn't running
# with --proxy-headers, or if a future deploy drops that flag) becomes an
# http↔https loop via nginx. Failing loudly with 404 on slash mismatch is a
# cheap fence around that whole class of misconfig.
app = FastAPI(title="Hoonr.ai API", lifespan=lifespan, redirect_slashes=False)
# Request-correlation middleware. Must wrap every route so downstream
# handlers and services see the same request_id via contextvars.
app.add_middleware(RequestIDMiddleware)

def _mount(module, label: str, **kwargs) -> None:
    """Mount a router defensively. Same rationale as _safe_import."""
    if module is None or not hasattr(module, "router"):
        logger.warning("router_skip_not_loaded", extra={"router": label})
        return
    try:
        app.include_router(module.router, **kwargs)
    except Exception as e:  # noqa: BLE001
        logger.error(
            "router_mount_failed",
            extra={"router": label, "error": str(e)},
            exc_info=True,
        )

_mount(ai_generation, "ai_generation", prefix="/api/v1/ai-generation")
_mount(ai_generation, "ai_generation(gemini)", prefix="/api/v1/gemini")
_mount(voice_agent, "voice_agent", prefix="/api/v1/voice")
_mount(boolean_agent, "boolean_agent", prefix="/api/v1/boolean")
_mount(candidate_processing, "candidate_processing", prefix="/api/v1/candidates")
_mount(job_archive, "job_archive")
_mount(chat_router, "chat")
_mount(tira_router, "tira")
_mount(job_criteria_router, "job_criteria")
_mount(manual_candidates_router, "manual_candidates")
_mount(candidates_router, "candidates")
_mount(jobs_router, "jobs")
_mount(engagement, "engagement", prefix="/api/v1/engagement")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def amplitude_request_tracking(request: Request, call_next):
    """Best-effort API telemetry: request journey + failures."""
    started = time.perf_counter()
    method = request.method
    path = request.url.path
    query = request.url.query
    user_id = request.headers.get("x-user-id") or request.headers.get("x-user-email")

    try:
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        track_event_async(
            "api_request",
            {
                "method": method,
                "path": path,
                "query": query,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
            },
            user_id=user_id,
            device_id="airecruiter-api",
        )
        if response.status_code >= 500:
            track_event_async(
                "api_server_error",
                {
                    "method": method,
                    "path": path,
                    "status_code": response.status_code,
                    "duration_ms": duration_ms,
                },
                user_id=user_id,
                device_id="airecruiter-api",
            )
        return response
    except Exception as e:
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        track_event_async(
            "api_exception",
            {
                "method": method,
                "path": path,
                "query": query,
                "duration_ms": duration_ms,
                "error": str(e),
            },
            user_id=user_id,
            device_id="airecruiter-api",
        )
        raise












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
