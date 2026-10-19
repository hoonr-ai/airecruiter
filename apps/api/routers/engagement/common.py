"""Engagement package: common."""

import asyncio
from fastapi import APIRouter
from typing import Any, Dict
import json
import logging
import os
from datetime import datetime, timezone
from routers._helpers import (
    get_db_connection,
)


# Keep the pre-split logger name so log routing/filters are unchanged.
logger = logging.getLogger("routers.engagement")

router = APIRouter(tags=["Engagement"])

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
EXTERNAL_INTERVIEW_API_URL = os.getenv("EXTERNAL_INTERVIEW_API_URL", "https://pairbotqa.hoonr.ai")
PASS_SCORE_THRESHOLD = float(os.getenv("PASS_SCORE_THRESHOLD", "70"))
PASS_CANDIDATE_SCORE_RATIO = float(os.getenv("PASS_CANDIDATE_SCORE_RATIO", "0.7"))
HARD_FILTER_PASS_STATUS = os.getenv("HARD_FILTER_PASS_STATUS", "passed").lower()
ENGAGE_PASSED_STATUSES = os.getenv("ENGAGE_PASSED_STATUSES", "completed,passed").lower().split(",")

# Bound concurrent JobDiva provisioning calls. send_bulk_interview fan-outs one
# _provision_candidate_to_jobdiva per candidate; without this, a 50-candidate
# bulk-engage fires 50 simultaneous JobDiva HTTP calls AND grabs 50 DB pool
# slots (briefly, after Fix 1). 5 matches the scale jobdiva_ratelimit_probe.py
# has been probing — tunable via env if JobDiva's rate budget changes.
_PROVISION_CONCURRENCY = asyncio.Semaphore(int(os.getenv("PROVISION_CONCURRENCY", "5")))

# A person PAIR creates in JobDiva gets a résumé built from everything we hold
# (services/profile_resume.py). When that is thin -- no work history, summary or
# résumé text, typically an Exa deep-search row -- the LinkedIn profile is read
# once more through Unipile before the JobDiva profile is created. Bounded per
# provisioning batch: each read is a LinkedIn profile view on a recruiter seat.
JOBDIVA_PROVISION_LINKEDIN_REFETCH = os.getenv(
    "JOBDIVA_PROVISION_LINKEDIN_REFETCH", "true"
).strip().lower() in {"1", "true", "yes", "on"}
JOBDIVA_PROVISION_LINKEDIN_REFETCH_CAP = max(0, int(os.getenv("JOBDIVA_PROVISION_LINKEDIN_REFETCH_CAP", "40") or 0))
# Grep / alert marker for a create the provisioner refused because the person
# would have landed in JobDiva as a blank profile (no usable name, nothing else).
JOBDIVA_BLANK_PROFILE_REFUSED = "JOBDIVA_BLANK_PROFILE_REFUSED"


def jobdiva_provisioning_enabled() -> bool:
    """JOBDIVA_PROVISIONING_ENABLED kill switch (default true), read at call
    time. false = no launch path creates or links JobDiva candidates/
    applications (the inline per-batch provisioning, the launch-session
    stage, /engage/re-provision): a local end-to-end launch must never
    write JobDiva."""
    return os.getenv("JOBDIVA_PROVISIONING_ENABLED", "true").strip().lower() not in {
        "0", "false", "no", "off",
    }
_NR_RESPONSE_BODY_LIMIT = 16000


def _log_generate_payload_response_to_newrelic(response_obj: Dict[str, Any], *, level: str = "info") -> None:
    """Best-effort New Relic logging for /engage/generate-payload responses.

    Sizes and counts only. The payload is every candidate's name, email, phone
    and résumé, and the error text can quote them, so neither goes to New
    Relic (the event used to carry the first 16 KB of the response)."""
    try:
        from core.newrelic import is_enabled, record_custom_event, record_message
        if not is_enabled():
            return
    except Exception:
        return

    try:
        response_text = json.dumps(response_obj, default=str)
    except Exception:
        response_text = str(response_obj)

    try:
        event_data: Dict[str, Any] = {
            "api_endpoint": "/engage/generate-payload",
            "api_operation": "generate_payload",
            "success": bool(response_obj.get("success")) if isinstance(response_obj, dict) else False,
            "response_size_bytes": len(response_text),
            "truncated": len(response_text) > _NR_RESPONSE_BODY_LIMIT,
        }
        if isinstance(response_obj, dict):
            for key in ("candidate_count", "dnc_blocked_count"):
                if isinstance(response_obj.get(key), int):
                    event_data[key] = response_obj[key]
            if isinstance(response_obj.get("candidate_ids"), list):
                event_data["requested_count"] = len(response_obj["candidate_ids"])
            if response_obj.get("job_id"):
                event_data["job_id"] = str(response_obj["job_id"])[:64]
            if response_obj.get("error_type"):
                event_data["error_type"] = str(response_obj["error_type"])[:80]
        record_custom_event("EngageGeneratePayload", event_data)
        record_message("Engage generate-payload response", attributes=event_data, level=level)
    except Exception:
        # New Relic logging must never affect the API path.
        return


def _nr_suppress_apdex() -> None:
    """Keep a minutes-long streaming endpoint out of Apdex (core.newrelic
    helper when present; fail-open no-op otherwise)."""
    try:
        from core import newrelic as _nr
        fn = getattr(_nr, "suppress_apdex", None)
        if callable(fn):
            fn()
    except Exception:  # noqa: BLE001 — telemetry never breaks a request
        pass


def _log_recordings_upstream_failure(interview_id: str, exc: BaseException) -> None:
    """Best-effort New Relic event for a PAIR-detail failure on the
    recordings endpoint. The endpoint itself returns 200 (unavailable: true)
    so the modal degrades gracefully, which means a status-code-based
    monitor would never see the outage — this event is what an alert can
    fire on instead.
    """
    try:
        from core.newrelic import is_enabled, record_custom_event, record_message
        if not is_enabled():
            return
    except Exception:
        return

    try:
        event_data = {
            "api_endpoint": "/interviews/{interview_id}/recordings",
            "interview_id": interview_id,
            "error": str(exc)[:500],
        }
        record_custom_event("RecordingsUpstreamFailure", event_data)
        record_message("Call recordings upstream (PAIR) lookup failed", attributes=event_data, level="warning")
    except Exception:
        return

def _parse_json_list(val) -> list:
    """Safely parse a value that may be a JSON string, list, or empty."""
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return [e.strip() for e in val.split(",") if e.strip()]
    return []

# ---------------------------------------------------------------------------
# Auto-Migration: Ensure audit table exists
# ---------------------------------------------------------------------------
def _ensure_audit_table():
    """Create engage_interview_audit table if it doesn't exist, and patch any missing columns."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                # Create table (no-op if already exists)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS engage_interview_audit (
                        id SERIAL PRIMARY KEY,
                        candidate_id VARCHAR(255) NOT NULL,
                        jobdiva_id VARCHAR(255),
                        interview_id VARCHAR(255),
                        candidate_name VARCHAR(255),
                        candidate_email VARCHAR(255),
                        payload JSONB,
                        response JSONB,
                        status VARCHAR(50) DEFAULT 'sent',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                # Idempotent column adds (ALTER TABLE) removed to prevent
                # lock contention. These should be handled via manual migrations.
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_engage_audit_candidate
                    ON engage_interview_audit(candidate_id);
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_engage_audit_interview
                    ON engage_interview_audit(interview_id);
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_engage_audit_job_candidate_id_desc
                    ON engage_interview_audit(jobdiva_id, candidate_id, id DESC);
                """)
                conn.commit()
        logger.info("✅ engage_interview_audit table ready")
    except Exception as e:
        logger.error(f"❌ Failed to create engage_interview_audit table: {e}")

# NOTE: _ensure_audit_table used to run at module import. That meant any
# DB slowness or lock blocked `from routers import engagement, ai_generation, …`
# in main.py — which in turn prevented every other router in that import
# statement from registering, producing 404s across the API. The call has
# been moved to `init_engagement_tables` which main.py awaits from lifespan
# with a timeout.

async def init_engagement_tables() -> None:
    """Async wrapper for the sync migration. Called from main.py lifespan."""
    await asyncio.to_thread(_ensure_audit_table)
    from services import call_recordings
    call_recordings.warn_if_misconfigured()

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------
def _get_db_connection():
    return get_db_connection()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
