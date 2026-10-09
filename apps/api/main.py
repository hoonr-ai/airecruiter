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
from core.email import notify_pair_launched, notify_job_posting, notify_pair_inactive

PAIR_INACTIVE_STATUSES = os.getenv("PAIR_INACTIVE_STATUSES", "closed,filled,cancelled,canceled,expired,ignored,declined").lower().split(",")

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
from core.advisory_lock import AdvisoryLock
from core.startup_once import claim_once, run_schema_once
from core.amplitude import track_event_async
configure_logging()
logger = logging.getLogger(__name__)

# Initialise New Relic as early as possible so all subsequent import-time and
# runtime errors are captured. Safe no-op when NEW_RELIC_LICENSE_KEY is not set.
from core.newrelic import init as _newrelic_init, is_enabled as _newrelic_enabled
_newrelic_init()

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
from services import jobdiva_rate_limit
from services.jobdiva import jobdiva_service
from services.unipile import unipile_service
from services.chat_service import chat_service
from services.monitored_jobs_storage import MonitoredJobsStorage
from services.job_rubric_db import JobRubricDB

# Legacy file-based tracking replaced by monitored_jobs SQL table

# Global scheduler
scheduler = AsyncIOScheduler()

# Job-status poll cadence. Was 5 min with ~2 JobDiva calls per monitored job;
# statuses now come from batched BI JobsDetail, and 15 min is ample for the
# PAIR Inactive email.
JOB_STATUS_POLL_MINUTES = max(1, int(os.getenv("JOB_STATUS_POLL_MINUTES", "15") or 15))


def schedule_next_poll():
    """Schedule the next status poll JOB_STATUS_POLL_MINUTES from now, canceling any existing poll"""
    try:
        # Remove existing scheduled poll if any
        if scheduler.get_job("job_status_poll"):
            scheduler.remove_job("job_status_poll")
        
        scheduler.add_job(
            poll_all_jobs,
            "date",
            run_date=datetime.now(timezone(timedelta(hours=5, minutes=30))) + timedelta(minutes=JOB_STATUS_POLL_MINUTES),
            id="job_status_poll",
            replace_existing=True
        )
        
        next_run = datetime.now(timezone(timedelta(hours=5, minutes=30))) + timedelta(minutes=JOB_STATUS_POLL_MINUTES)
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

        Every uvicorn worker schedules this, and all of them draw on the same
        JobDiva account quota as live Step 5 searches. A Postgres session
        advisory lock lets only ONE worker run the cycle; the rest skip.
        app.sync_in_progress still guards against overlap within a worker.
        """
        if getattr(app, "sync_in_progress", False):
            logger.info("🤖 [AutoSync] Cycle skipped: A sync is already running.")
            return

        lock = AdvisoryLock(AUTO_SYNC_LOCK_KEY, "AutoSync")
        if not await lock.try_acquire():
            logger.info("🔒 [AutoSync] Another worker is syncing this cycle; skipping.")
            return

        app.sync_in_progress = True
        logger.info("🤖 [AutoSync] Starting synchronization cycle...")

        from services import auto_sync_queue

        def _db(fn, *args):
            conn = get_db_connection()
            try:
                return fn(conn, *args)
            finally:
                conn.close()

        try:
            jobs = await asyncio.to_thread(_db, auto_sync_queue.pick_jobs)
            if not jobs:
                logger.info("🤖 [AutoSync] No jobs to sync.")
                return

            # Time-boxed slice: oldest-synced open jobs first, closed jobs at
            # most daily. The next cycle continues where this one stopped.
            started = time.monotonic()
            done = 0
            for job in jobs:
                if time.monotonic() - started >= auto_sync_queue.CYCLE_BUDGET_S:
                    break
                # If the lock connection dropped, Postgres released the lock
                # and another worker may already be running a cycle: stop.
                if not await lock.still_held():
                    logger.error(
                        f"❌ [AutoSync] Lost advisory lock after {done}/{len(jobs)} jobs; stopping this cycle."
                    )
                    return
                jid = job['job_id']
                logger.debug(f"🤖 [AutoSync] Syncing: {job.get('title', jid)}")
                try:
                    # Background priority: every JobDiva call underneath yields
                    # to interactive requests and is paced by the shared limiter.
                    with jobdiva_rate_limit.background_context():
                        await auto_assign_service.synchronize_job_applicants(jid)
                except Exception as e:
                    logger.warning(f"[AutoSync] Sync failed for {jid}: {e}")
                # Marked even on failure so one broken job can't pin the head
                # of the queue; it comes round again after the others.
                await asyncio.to_thread(_db, auto_sync_queue.mark_synced, [jid])
                done += 1
                await asyncio.sleep(0.5)  # breathing room for Postgres between jobs

            logger.info(
                f"✅ [AutoSync] Cycle synced {done}/{len(jobs)} due jobs in {time.monotonic() - started:.0f}s."
            )
        except Exception as e:
            logger.error(f"❌ [AutoSync] Cycle failed: {e}")
        finally:
            app.sync_in_progress = False
            await lock.release()

    # Interval: Every 15 minutes
    scheduler.add_job(auto_sync_all_jobs, "interval", minutes=15, id="always_on_sync")

    # 3. Schema init. Every step is idempotent DDL; ALTER TABLE takes ACCESS
    # EXCLUSIVE, so running it in all 8 workers (and again on every worker
    # recycle) queued them behind each other. One worker runs the steps under
    # an advisory lock; the rest wait for it, then skip (core/startup_once).
    async def _schema_step(name: str, coro_fn, timeout: float) -> bool:
        try:
            await asyncio.wait_for(coro_fn(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            logger.error(f"{name}_timeout ({timeout:.0f}s); continuing")
        except Exception as e:  # noqa: BLE001
            logger.error(f"{name}_failed: {e}; continuing")
        return False

    try:
        from services import jobdiva_bi_sync
    except Exception as e:  # noqa: BLE001 - never let the dashboard mirror block boot
        logger.error(f"jobdiva_bi_sync_import_failed: {e}; continuing")
        jobdiva_bi_sync = None

    async def _init_schemas() -> bool:
        steps = []
        # Engagement audit table (moved out of module import in engagement.py
        # so a slow/locked DB can no longer crash-loop the app).
        if engagement is not None and hasattr(engagement, "init_engagement_tables"):
            steps.append(("engagement_audit_init", engagement.init_engagement_tables, 10))
        # monitored_jobs columns. Previously two handlers in routers/jobs.py ran
        # ALTER TABLE on every request and stalled GET /jobs/monitored for 60-90s.
        if jobs_router is not None and hasattr(jobs_router, "init_monitored_jobs_schema"):
            steps.append(("monitored_jobs_schema_init", jobs_router.init_monitored_jobs_schema, 10))
        if job_criteria_router is not None and hasattr(job_criteria_router, "init_job_criteria_schema"):
            steps.append(("job_criteria_schema_init", job_criteria_router.init_job_criteria_schema, 10))
        # campaigns after monitored_jobs (whose campaign_id column links child jobs).
        if campaigns_router is not None and hasattr(campaigns_router, "init_campaigns_schema"):
            steps.append(("campaigns_schema_init", campaigns_router.init_campaigns_schema, 10))
        # teams + team_members (team_lead role and team-scoped views).
        if teams_router is not None and hasattr(teams_router, "init_teams_schema"):
            steps.append(("teams_schema_init", teams_router.init_teams_schema, 10))
        # org_members. If this times out, identity lookups fail CLOSED.
        if org_hierarchy_router is not None and hasattr(org_hierarchy_router, "init_org_hierarchy_schema"):
            steps.append(("org_hierarchy_schema_init", org_hierarchy_router.init_org_hierarchy_schema, 10))
        # JobDiva BI mirror behind the PAIR Dashboard.
        if jobdiva_bi_sync is not None:
            steps.append(("jobdiva_bi_schema_init", jobdiva_bi_sync.init_jobdiva_bi_schema, 10))
        # In-app notifications table.
        if notifications_router is not None and hasattr(notifications_router, "init_notifications_schema"):
            steps.append(("notifications_schema_init", notifications_router.init_notifications_schema, 10))

        # Imported inside each step so a broken module fails only its step.
        async def _scs_init():
            from services import sourced_candidates_storage as _scs
            await _scs.init_sourced_candidates_schema()

        async def _dnc_init():
            from services import dnc_storage as _dnc
            await _dnc.init_dnc_schema()

        async def _rubric_init():
            from services import job_rubric_db as _jrd
            await _jrd.init_rubric_schema()

        # sourced_candidates + candidate_enhanced_info. 15s: the initial CREATE
        # INDEX on candidate_enhanced_info can take longer.
        steps.append(("sourced_candidates_schema_init", _scs_init, 15))
        # dnc_list + sourced_candidates.dnc_stopped_at; after sourced_candidates.
        steps.append(("dnc_schema_init", _dnc_init, 10))
        # job_skills / job_education `source` column (Recruiter vs PAIR chip).
        steps.append(("rubric_schema_init", _rubric_init, 10))

        ok = True
        for name, fn, timeout in steps:
            ok = await _schema_step(name, fn, timeout) and ok
        return ok

    try:
        await run_schema_once("schemas", _init_schemas)
    except Exception as e:  # noqa: BLE001 - schema init must never block boot
        logger.error(f"startup_schema_init_failed: {e}; continuing")

    # JobDiva BI mirror sync. Every worker schedules it; an advisory lock lets
    # one run each cycle. Delayed like the auto-sync so boot stays uncontested.
    if jobdiva_bi_sync is not None and jobdiva_bi_sync.SYNC_ENABLED:
        try:
            scheduler.add_job(
                jobdiva_bi_sync.scheduled_sync,
                "interval",
                minutes=jobdiva_bi_sync.SYNC_INTERVAL_MINUTES,
                id="jobdiva_bi_sync",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
                next_run_time=datetime.now(timezone.utc) + timedelta(seconds=120),
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"jobdiva_bi_sync_schedule_failed: {e}; continuing")

    # 4b. Keep the /jobs/monitored cache warm. Per-worker in-memory cache
    # has a 30s TTL, but the live query can spike to 20-30s during
    # poll-loop lock contention — long enough that the frontend's 8s
    # AbortController fires and the dashboard goes blank. Running a 25s
    # interval warmer keeps each worker's cache populated under all but
    # the most severe DB outages; combined with the empty-payload
    # fallback in get_monitored_jobs, the dashboard never goes blank.
    async def warm_monitored_jobs_cache():
        if jobs_router is None:
            return
        sync_fn = getattr(jobs_router, "_get_monitored_jobs_sync", None)
        setter = getattr(jobs_router, "_set_cached_monitored_jobs", None)
        if sync_fn is None or setter is None:
            return
        for include_archived in (False, True):
            try:
                payload = await asyncio.to_thread(sync_fn, include_archived, "summary")
                setter(include_archived, "summary", payload)
            except Exception as e:  # noqa: BLE001
                # Escalated from warning → error with traceback. A failing
                # warmer is the upstream cause of cold-cache → frontend
                # AbortError on the dashboard; on qacurate it stayed buried
                # at warning level and the actual DB/schema fault was
                # invisible. Surface it loudly so monitoring catches it.
                logger.error(
                    f"monitored_jobs_cache_warmer failed (include_archived={include_archived}): {e}",
                    exc_info=True,
                )

    scheduler.add_job(
        warm_monitored_jobs_cache,
        "interval",
        seconds=25,
        id="monitored_jobs_cache_warmer",
        replace_existing=True,
    )

    # Safety net for the webhook-triggered in-app notification (a crashed
    # worker loses an asyncio.create_task before it completes, or a status
    # changes outside the webhook path). Backfills anything the webhook path
    # missed; dedup is the notifications table's own unique index, so this
    # can never double-insert.
    try:
        from services.notifications_service import reconcile_missed_pass_notifications
        scheduler.add_job(
            reconcile_missed_pass_notifications,
            "interval",
            minutes=10,
            id="notifications_reconciliation",
            replace_existing=True,
        )
    except Exception as e:  # noqa: BLE001
        logger.error(f"notifications_reconciliation_schedule_failed: {e}; continuing")
    # Prime cache once at startup so the first dashboard request after
    # deploy hits warm cache instead of a cold DB. Fire-and-forget — any
    # failure is logged inside the warmer and never blocks boot. Stays
    # per-worker: the cache is process-local and the warmer only reads Postgres.
    asyncio.create_task(warm_monitored_jobs_cache())

    # v30: one-time backfill of the denormalized counter columns. After
    # the column-add ALTER above, candidates_sourced/launched/
    # complete_submissions/pass_submissions default to 0 — the backfill
    # populates them from sourced_candidates aggregates so the dashboard
    # shows real numbers without waiting for the next 15-min sync cycle.
    # Fire-and-forget so a slow aggregate never blocks boot.
    # Claimed by one worker per boot (core/startup_once).
    async def _backfill_counters_once():
        if await claim_once("backfill_monitored_jobs_counters"):
            await jobs_router.backfill_monitored_jobs_counters()

    if jobs_router is not None and hasattr(jobs_router, "backfill_monitored_jobs_counters"):
        asyncio.create_task(_backfill_counters_once())

    # Delay first auto-sync 60s. Previously the sync ran immediately and held
    # the DB pool for minutes, while fresh user requests queued behind it —
    # manifesting as site-wide slowness right after every deploy. The 15-min
    # interval schedule above still catches everything; the delay just keeps
    # the cold-start window uncontested.
    # One worker per boot: recycled workers (--limit-max-requests) would
    # otherwise each kick off an extra cycle a minute after they start.
    async def _delayed_first_sync():
        if not await claim_once("first_auto_sync"):
            return
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
dnc_router = _safe_import("dnc")
no_contact_router = _safe_import("no_contact")
outreach_optout_router = _safe_import("outreach_optout")
admin_analytics_router = _safe_import("admin_analytics")
launch_report_router = _safe_import("launch_report")
recruiter_analytics_router = _safe_import("recruiter_analytics")
job_step_time_router = _safe_import("job_step_time")
campaigns_router = _safe_import("campaigns")
teams_router = _safe_import("teams")
org_hierarchy_router = _safe_import("org_hierarchy")
cross_submissions_router = _safe_import("cross_submissions")
live_report_router = _safe_import("live_report")
pair_dashboard_router = _safe_import("pair_dashboard")
apollo_webhook_router = _safe_import("apollo_webhook")
dnc_webhook_router = _safe_import("dnc_webhook")
notifications_router = _safe_import("notifications")

# redirect_slashes=False: never auto-307 between `/foo` and `/foo/`. Behind the
# prod reverse proxy a 307 with the wrong scheme (when uvicorn isn't running
# with --proxy-headers, or if a future deploy drops that flag) becomes an
# http↔https loop via nginx. Failing loudly with 404 on slash mismatch is a
# cheap fence around that whole class of misconfig.
app = FastAPI(title="Hoonr.ai API", lifespan=lifespan, redirect_slashes=False)

if _newrelic_enabled():
    try:
        import newrelic.agent
        app = newrelic.agent.ASGIApplicationWrapper(app)
    except Exception:
        pass

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
_mount(dnc_router, "dnc")
_mount(no_contact_router, "no_contact")
_mount(outreach_optout_router, "outreach_optout")
_mount(admin_analytics_router, "admin_analytics")
_mount(launch_report_router, "launch_report")
_mount(recruiter_analytics_router, "recruiter_analytics")
_mount(job_step_time_router, "job_step_time")
_mount(teams_router, "teams")
_mount(org_hierarchy_router, "org_hierarchy")
_mount(cross_submissions_router, "cross_submissions")
_mount(live_report_router, "live_report")
_mount(pair_dashboard_router, "pair_dashboard")
# Mounted under /api so the existing nginx `location /api/` passthrough routes
# it to the backend — avoids a collision with the frontend's /campaigns pages
# (same trick keeps job_criteria under /api/jobs/...). No nginx changes needed.
_mount(campaigns_router, "campaigns", prefix="/api")
# Apollo phone-reveal webhook (POST /api/webhooks/apollo/phone?token=...). Token-
# guarded, not login-guarded: Apollo is the caller. nginx has an exact location
# for it with its own rate zone (Apollo delivers in bursts after a big launch).
_mount(apollo_webhook_router, "apollo_webhook", prefix="/api")
_mount(dnc_webhook_router, "dnc_webhook")
_mount(engagement, "engagement", prefix="/api/v1/engagement")
_mount(notifications_router, "notifications", prefix="/api/v1/notifications")

from core.auth import auth_router
app.include_router(auth_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    # Was two print() lines per request straight to stdout/syslog (no level,
    # not filterable); DEBUG keeps them available without the volume.
    logger.debug(f"Incoming {request.method} {request.url.path}")
    response = await call_next(request)
    logger.debug(f"Response {response.status_code} for {request.url.path}")
    return response


@app.get("/api/health", include_in_schema=False)
async def api_health():
    """Unauthenticated liveness + dependency check for uptime monitoring.

    503 only when Postgres is unreachable (nothing works without it). Redis
    trouble is reported as "degraded" with 200: the app keeps serving with
    per-worker fallbacks, and a separate Redis alert covers it.
    """
    from fastapi.responses import JSONResponse

    def _db_ok() -> bool:
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
            return True
        finally:
            conn.close()

    checks: Dict[str, str] = {}
    try:
        checks["db"] = "ok" if await asyncio.wait_for(asyncio.to_thread(_db_ok), timeout=5) else "error"
    except Exception as exc:
        # Public endpoint: details go to the log, not the response.
        logger.warning(f"api_health: db check failed: {type(exc).__name__}: {exc}")
        checks["db"] = "error"

    redis_client = jobdiva_rate_limit._get_redis()
    if redis_client is None:
        checks["redis"] = "unavailable"
    else:
        try:
            await asyncio.wait_for(redis_client.ping(), timeout=2)
            checks["redis"] = "ok"
        except Exception as exc:
            logger.warning(f"api_health: redis check failed: {type(exc).__name__}: {exc}")
            checks["redis"] = "error"

    if checks["db"] != "ok":
        status, code = "down", 503
    elif checks["redis"] != "ok":
        status, code = "degraded", 200
    else:
        status, code = "ok", 200
    return JSONResponse({"status": status, "checks": checks}, status_code=code)

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

POLL_LOCK_KEY = 728193  # app-wide constant for the cross-worker poll advisory lock
AUTO_SYNC_LOCK_KEY = 728195  # cross-worker lock for auto_sync_all_jobs (728194 is jobdiva_bi_sync)


async def poll_all_jobs():
    """Scheduler entry point (runs in every uvicorn worker).

    A Postgres session advisory lock ensures only ONE worker executes the
    poll body per cycle (the others skip), so JobDiva is polled once and the
    "PAIR Inactive" email fires once. Every worker still reschedules its next
    poll so the 5-min cadence survives even if the current lock-holder dies.
    """
    lock_conn = None
    got_lock = False
    try:
        lock_conn = get_db_connection()
        lc = lock_conn.cursor()
        lc.execute("SELECT pg_try_advisory_lock(%s)", (POLL_LOCK_KEY,))
        got_lock = bool(lc.fetchone()[0])
        lock_conn.commit()
        if not got_lock:
            logger.info("🔒 [Poll] Another worker is polling this cycle; skipping.")
            return
        return await _run_poll_cycle()
    finally:
        if lock_conn is not None:
            try:
                if got_lock:
                    uc = lock_conn.cursor()
                    uc.execute("SELECT pg_advisory_unlock(%s)", (POLL_LOCK_KEY,))
                    lock_conn.commit()
            except Exception as e:
                logger.warning(f"poll advisory unlock failed: {e}")
            finally:
                lock_conn.close()
        # Always reschedule so every worker keeps the 5-min cadence — including
        # those that skipped this cycle and the lock-holder once it finishes.
        schedule_next_poll()


async def _run_poll_cycle():
    """The actual poll body — runs in exactly one worker per cycle."""
    logger.info("🔄 Starting job status polling...")

    jobs_data = load_monitored_jobs()
    # Archived jobs are never shown or synced; polling them only spent quota.
    job_ids = [
        jid for jid, j in jobs_data.get("jobs", {}).items()
        if not (j or {}).get("is_archived")
    ]

    if not job_ids:
        logger.info("No jobs to monitor")
        return

    logger.info(f"Polling {len(job_ids)} jobs")

    # Batched BI JobsDetail at background priority (yields to user requests).
    with jobdiva_rate_limit.background_context():
        statuses = await jobdiva_service.get_multiple_jobs_status(job_ids, jobs_data.get("jobs", {}))
    
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

        # ── Email #4: PAIR Inactive Notification ─────────────────────────
        if current_status.lower() in PAIR_INACTIVE_STATUSES and old_status.lower() not in PAIR_INACTIVE_STATUSES:
            logger.info(f"⏸️ Job {job_id} switched to Inactive ({current_status}). Triggering notification...")
            asyncio.create_task(_fire_pair_inactive_notification(job_id))
        elif old_status.lower() in PAIR_INACTIVE_STATUSES and current_status.lower() not in PAIR_INACTIVE_STATUSES:
            logger.info(f"▶️ Job {job_id} reactivated ({current_status}). Clearing inactive-notified marker.")
            asyncio.create_task(_clear_pair_inactive_marker(job_id))
    
    # Save updated data
    jobs_data["last_sync"] = readable_ist_now()
    save_monitored_jobs(jobs_data)
    
    if changes_detected:
        logger.info(f"📢 Status changes detected: {changes_detected}")
    else:
        logger.info("✅ No status changes detected")

    return {"polled": len(job_ids), "changes": changes_detected}



async def _fire_pair_inactive_notification(job_id: str):
    """Claim-and-send Email #4 (PAIR Is Now Inactive).

    An atomic conditional UPDATE acts as the lock: only the worker that flips
    pair_inactive_notified_at from NULL->NOW() gets a row back and sends. The
    other 7 workers (and any re-fire after a restart) get no row and skip, so
    exactly one email goes out per inactive event.
    """
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Atomic claim. Match on job_id only — the poll loop always passes the
        # PK, and the monitored_jobs indexes are non-unique so `OR jobdiva_id`
        # could touch stray rows.
        cur.execute(
            """
            UPDATE monitored_jobs
               SET pair_inactive_notified_at = NOW()
             WHERE job_id = %s
               AND pair_inactive_notified_at IS NULL
            RETURNING recruiter_emails, jobdiva_id
            """,
            (job_id,),
        )
        row = cur.fetchone()
        # MANDATORY: pooled conn is autocommit=False and rolls back on close
        # (core/db.py) — without this commit the claim is never persisted.
        conn.commit()

        if not row:
            logger.info(f"Inactive notification already claimed for {job_id}; skipping duplicate.")
            return

        import json
        emails_raw = row.get("recruiter_emails", [])
        if isinstance(emails_raw, str):
            try:
                recruiter_emails = json.loads(emails_raw)
            except Exception:
                recruiter_emails = [emails_raw] if emails_raw else []
        else:
            recruiter_emails = emails_raw or []

        try:
            await asyncio.to_thread(
                notify_pair_inactive,
                jobdiva_id=row["jobdiva_id"] or job_id,
                recruiter_emails=recruiter_emails,
                job_id=job_id,  # enables the live JobDiva deep-link
            )
        except Exception as send_err:
            # Send failed after we claimed — release the marker so the next
            # poll cycle retries instead of silently losing the notification.
            logger.error(f"❌ Inactive email send failed for {job_id}: {send_err}; releasing marker for retry")
            try:
                cur.execute(
                    "UPDATE monitored_jobs SET pair_inactive_notified_at = NULL WHERE job_id = %s",
                    (job_id,),
                )
                conn.commit()
            except Exception:
                pass
    except Exception as e:
        logger.error(f"❌ Failed to fire Inactive notification for {job_id}: {e}")
    finally:
        if conn is not None:
            conn.close()


async def _clear_pair_inactive_marker(job_id: str):
    """Reset the inactive-notified marker when a job becomes active again, so a
    genuine new active->inactive transition later sends one fresh email."""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "UPDATE monitored_jobs SET pair_inactive_notified_at = NULL "
            "WHERE job_id = %s AND pair_inactive_notified_at IS NOT NULL",
            (job_id,),
        )
        conn.commit()
    except Exception as e:
        logger.error(f"Failed to clear inactive marker for {job_id}: {e}")
    finally:
        if conn is not None:
            conn.close()

# =====================================================
# JOB DRAFTS API ENDPOINTS
# =====================================================

from core.db import get_db_connection  # re-exported for callers that import from main



























if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
