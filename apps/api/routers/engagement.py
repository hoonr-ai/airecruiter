"""
Engagement Router - Engage & Assess Button Endpoints

Provides endpoints for:
1. Generating interview payloads for candidates (Engage)
2. Sending bulk interview requests to PAIR API (Engage)
3. Looking up latest interview for a candidate (Assess)
4. Proxying PAIR dashboard data for assessment display (Assess)

Auto-creates the engage_interview_audit table on startup.
"""

import asyncio
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, List, Any, Dict
import psycopg2.extras
import json
import logging
import os
import httpx
import re
from datetime import datetime, timezone, timedelta
from routers._helpers import get_db_connection

from core.email import (
    notify_pair_launched,
    notify_job_posting,
    notify_candidate_passed,
    _build_word_resume_document,
    resolve_app_base_url,
)
from services.jobdiva import jobdiva_service
from services.auto_assign_service import auto_assign_service
from core import (
    JOBDIVA_PAIR_RECRUITER_ID,
    JOBDIVA_PAIR_QUALIFICATION_NAME,
    JOBDIVA_PAIR_QUALIFICATION_ID,
    JOBDIVA_PASS_ACTION_NAME,
    JOBDIVA_PASS_QUALIFICATION_VALUE
)

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PLACEHOLDER_EMAILS = {
    "your-email@example.com",
    "email@example.com",
    "example@example.com",
    "test@example.com",
    "candidate@example.com",
    "noreply@example.com",
}
_PLACEHOLDER_DOMAINS = {
    "example.com",
    "example.org",
    "example.net",
    "test.com",
    "invalid",
    "localhost",
    "local",
}

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
_SENTRY_RESPONSE_BODY_LIMIT = 16000


def _log_generate_payload_response_to_sentry(response_obj: Dict[str, Any], *, level: str = "info") -> None:
    """Best-effort Sentry logging for /engage/generate-payload responses."""
    try:
        import sentry_sdk
    except Exception:
        return

    try:
        response_text = json.dumps(response_obj, default=str)
    except Exception:
        response_text = str(response_obj)

    truncated = response_text[:_SENTRY_RESPONSE_BODY_LIMIT]
    was_truncated = len(response_text) > len(truncated)

    try:
        with sentry_sdk.new_scope() as scope:
            scope.set_tag("api_endpoint", "/engage/generate-payload")
            scope.set_tag("api_operation", "generate_payload")
            scope.set_tag("truncated", str(was_truncated).lower())
            scope.set_context(
                "engage_generate_payload_response",
                {
                    "response_size_bytes": len(response_text),
                    "truncated": was_truncated,
                    "response": truncated,
                },
            )
            sentry_sdk.capture_message("Engage generate-payload response", level=level)
    except Exception:
        # Sentry logging must never affect the API path.
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


def _format_normalized_score_100(score: Any, total: Any) -> Optional[str]:
    """Format raw score/total as the same normalized 100-point score used in the report."""
    if score is None or total in (None, 0, 0.0, "0", ""):
        return None

    try:
        normalized_score = (float(score) / float(total)) * 100.0
    except (TypeError, ValueError, ZeroDivisionError):
        return None

    if normalized_score.is_integer():
        return f"{int(normalized_score)}/100"
    return f"{round(normalized_score, 1):.1f}/100"


def _is_placeholder_email(email: str) -> bool:
    normalized = (email or "").strip().lower()
    if not normalized:
        return True
    if normalized in _PLACEHOLDER_EMAILS:
        return True
    if normalized.endswith("@noemail.pair.ai"):
        return True
    if "@" not in normalized:
        return True
    local_part, domain = normalized.rsplit("@", 1)
    if domain in _PLACEHOLDER_DOMAINS:
        return True
    if local_part in {"your-email", "your_email", "email", "test", "example", "candidate"}:
        return True
    # JobDiva auto-generates "Auto_<candidateId>@jobdiva.com" when a candidate
    # has no real email on file — these are dead addresses, not contactable.
    if domain == "jobdiva.com":
        return True
    return False


def _sanitize_pair_candidate_email(raw: str) -> str:
    """Return a usable email for PAIR outreach, or "" when missing/placeholder.

    Email is OPTIONAL for launch — PAIR reaches candidates by phone — so a dead
    or absent address (incl. JobDiva `Auto_<id>@jobdiva.com` placeholders) is
    blanked rather than blocking the launch. A real, well-formed address is
    kept so email outreach can still go out when available.
    """
    cleaned = (raw or "").strip().lower()
    if not cleaned or not _EMAIL_RE.match(cleaned) or _is_placeholder_email(cleaned):
        return ""
    return cleaned


def _pair_phone_digits(raw: Any) -> str:
    return "".join(ch for ch in str(raw or "") if ch.isdigit())


def _validate_pair_payload_contacts(payload_obj: Dict[str, Any]) -> None:
    """PAIR launch gate: require at least ONE usable contact method per resume —
    a usable phone (PAIR calls the candidate) OR a real email (PAIR emails them).
    Either alone is enough. Placeholder/dead emails are blanked, not rejected,
    and only block launch when there is also no usable phone.
    """
    resumes = payload_obj.get("resumes")
    if not isinstance(resumes, list):
        raise HTTPException(status_code=400, detail="Payload is missing resumes")

    for idx, resume in enumerate(resumes, start=1):
        if not isinstance(resume, dict):
            raise HTTPException(status_code=400, detail=f"Resume {idx} payload is invalid")

        # Sanitize the email (blank dead/placeholder addresses). A real address
        # that survives sanitizing counts as a usable contact method.
        clean_email = _sanitize_pair_candidate_email(str(resume.get("email") or ""))
        resume["email"] = clean_email

        # Need at least one way to reach the candidate: a usable phone (≥7
        # digits, matching the frontend launch gate) OR a real email. Either
        # one alone is sufficient to launch.
        has_phone = len(_pair_phone_digits(resume.get("phone"))) >= 7
        has_email = bool(clean_email)
        if not has_phone and not has_email:
            who = resume.get("name") or resume.get("candidate_name") or f"Resume {idx}"
            raise HTTPException(
                status_code=400,
                detail=f"A usable phone number or email is required before launching PAIR ({who}).",
            )


def _sanitize_pre_screen_questions_for_pair(
    questions: List[Dict[str, Any]],
    *,
    fallback_job_title: str = "the role",
) -> List[Dict[str, Any]]:
    """Normalize pre-screen questions to PAIR schema constraints."""
    sanitized: List[Dict[str, Any]] = []
    for q in questions:
        text = " ".join(str(q.get("question_text") or "").split()).strip()
        if not text:
            continue

        if len(text) < 10:
            text = f"Please explain: {text}"
        if len(text) > 1000:
            text = text[:1000].rstrip()

        pass_criteria = str(q.get("pass_criteria") or "").strip()
        if len(pass_criteria) > 500:
            pass_criteria = pass_criteria[:500].rstrip()

        category = str(q.get("category") or "default").strip() or "default"
        if len(category) > 50:
            category = category[:50].rstrip()

        sanitized.append({
            "question_text": text,
            "pass_criteria": pass_criteria,
            "is_default": bool(q.get("is_default", True)),
            "category": category,
        })

    return sanitized


def _extract_pair_error_message(response_data: Dict[str, Any], status_code: int) -> str:
    """Return the most useful PAIR error text for UI surfacing."""
    if not isinstance(response_data, dict):
        return f"PAIR API returned status {status_code}"

    message = str(response_data.get("message") or "").strip()
    if message:
        return message

    detail = response_data.get("detail")
    if isinstance(detail, str) and detail.strip():
        return detail.strip()

    if isinstance(detail, list) and detail:
        parts: List[str] = []
        for item in detail[:3]:
            if isinstance(item, dict):
                loc = item.get("loc")
                where = ".".join(str(p) for p in loc if p != "body") if isinstance(loc, list) else ""
                msg = str(item.get("msg") or "validation error").strip()
                parts.append(f"{where}: {msg}" if where else msg)
            elif isinstance(item, str) and item.strip():
                parts.append(item.strip())
        if parts:
            return "; ".join(parts)

    return f"PAIR API returned status {status_code}"

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

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------
def _get_db_connection():
    return get_db_connection()

# ---------------------------------------------------------------------------
# Request / Response Models
# ---------------------------------------------------------------------------
class GeneratePayloadRequest(BaseModel):
    candidate_ids: List[str]
    job_id: str

class SendBulkInterviewRequest(BaseModel):
    payload: str  # JSON string (editable by user in modal)
    real_candidate_ids: List[str]
    is_initial_launch: bool = False
    dry_run: bool = False
    # Wizard "Launch PAIR" sends this to trigger the recruiter launch email.
    # Manual rankings Engage clicks leave it false so we don't spam emails.
    # Previously, dry_run did double duty (skip pairbot + fire email), which
    # silently caused bulk launches to never reach pairbot.
    notify_recruiters: bool = False
    # When provided by the caller, controls Email #2 (job posting request)
    # explicitly so batched launches can fire it once on the final successful
    # batch only.
    send_job_posting_email: Optional[bool] = None
    app_base_url: str = ""


# ---------------------------------------------------------------------------
# 1. POST /engage/generate-payload
# ---------------------------------------------------------------------------
@router.post("/engage/generate-payload")
async def generate_engage_payload(request: GeneratePayloadRequest):
    """
    Generate an interview payload for a candidate.
    Fetches candidate data from sourced_candidates and job data from monitored_jobs,
    then assembles it into the samplepayload.json structure.
    """
    try:
        conn = _get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # ----- Fetch candidate data -----
        # DNC gate: rows with dnc_stopped_at set are excluded so a candidate
        # added to the Do-Not-Contact list after launch stops receiving
        # interview invites without inactivating the rest of the PAIR.
        resumes = []
        candidate_phone = ""
        dnc_blocked_ids: List[str] = []
        for cid in request.candidate_ids:
            cur.execute("""
                SELECT candidate_id, name, email, phone, resume_text, headline, location, data
                FROM sourced_candidates
                WHERE candidate_id = %s
                  AND dnc_stopped_at IS NULL
                ORDER BY updated_at DESC
                LIMIT 1
            """, (cid,))
            row = cur.fetchone()
            if not row:
                # Either the candidate isn't sourced for any job, or every
                # row is dnc_stopped. Probe a second query to tell them
                # apart so we can log the DNC skip explicitly.
                cur.execute("""
                    SELECT 1 FROM sourced_candidates
                    WHERE candidate_id = %s AND dnc_stopped_at IS NOT NULL
                    LIMIT 1
                """, (cid,))
                if cur.fetchone():
                    dnc_blocked_ids.append(cid)
                    logger.info(
                        "engagement_dnc_skip candidate_id=%s reason=dnc_stopped_at_set",
                        cid,
                    )
                    continue

            if row:
                name = row.get("name", "Unknown")
                parts = name.split(" ", 1)
                first_name = parts[0] if parts else name
                last_name = parts[1] if len(parts) > 1 else ""
                # Sanitize phone: keep only digits + leading '+'. Pairbot enforces
                # max_length=20 on phone; values like 'Available upon request'
                # stored in sourced_candidates cause 422 validation errors.
                raw_phone = row.get("phone", "") or ""
                plus = "+" if raw_phone.strip().startswith("+") else ""
                digits = "".join(ch for ch in raw_phone if ch.isdigit())
                phone = f"{plus}{digits}" if digits else ""
                email = row.get("email", "") or ""
                # Truncate resume to pairbot max_length=100000 chars.
                resume_text = (row.get("resume_text", "") or "")[:100000]

                if not candidate_phone and phone:
                    candidate_phone = phone

                # Extract headline/summary from data blob if available
                data_blob = row.get("data") or {}
                if isinstance(data_blob, str):
                    try:
                        data_blob = json.loads(data_blob)
                    except Exception:
                        data_blob = {}
                headline = row.get("headline") or data_blob.get("headline", "")

                resumes.append({
                    "source_candidate_id": row.get("candidate_id") or cid,
                    "name": name,
                    "email": email,
                    "phone": phone,  # already sanitized to digits only above
                    # pairbotqa expects experience / summary / skills — map raw resume
                    "experience": resume_text,
                    "summary": headline,
                    "skills": "",
                    "education": "",
                })
            else:
                # Fallback for candidates not found in DB
                resumes.append({
                    "source_candidate_id": cid,
                    "name": "Unknown Candidate",
                    "email": "",
                    "phone": "",
                    "experience": "",
                    "summary": "",
                    "skills": "",
                    "education": "",
                })

        # ----- Fetch job data -----
        # Some jobs have duplicate rows where job_id matches jobdiva_id.
        # We prioritize the row with a numeric job_id and the latest creation date.
        cur.execute("""
            SELECT * FROM monitored_jobs 
            WHERE job_id = %s OR jobdiva_id = %s
            ORDER BY (job_id ~ '^[0-9]+$') DESC, created_at DESC 
            LIMIT 1
        """, (request.job_id, request.job_id))
        job_row = cur.fetchone()

        # ----- Fetch pre-screen questions from job_screen_questions table -----
        # Match by job_id first, then fall back to jobdiva_id if needed
        pre_screen_questions = []
        if job_row:
            jobdiva_id_for_lookup = job_row.get("jobdiva_id") or ""
            job_id_for_lookup = job_row.get("job_id") or request.job_id
            cur.execute("""
                SELECT question_text, pass_criteria, is_default, category, order_index
                FROM job_screen_questions
                WHERE jobdiva_id = %s OR jobdiva_id = %s
                ORDER BY order_index
            """, (jobdiva_id_for_lookup, job_id_for_lookup))
            rows = cur.fetchall()
            from routers.voice_agent import _humanize_question_text
            pre_screen_questions_raw = [
                {
                    "question_text": _humanize_question_text(r["question_text"]),
                    "pass_criteria": r["pass_criteria"],
                    "is_default": r["is_default"],
                    "category": r["category"],
                }
                for r in rows
            ]
            pre_screen_questions = _sanitize_pre_screen_questions_for_pair(
                pre_screen_questions_raw,
                fallback_job_title=(job_row.get("enhanced_title") or job_row.get("title") or request.job_id),
            )

        cur.close()
        conn.close()

        # Build JD section — must match pairbotqa expected structure:
        # ----- Fetch full rubric for JD enrichment -----
        from services.job_rubric_db import JobRubricDB
        rubric_db = JobRubricDB()
        jobdiva_id_for_rubric = job_row.get("jobdiva_id") if job_row else request.job_id
        rubric = rubric_db.get_full_rubric(jobdiva_id_for_rubric)
        if rubric:
            rubric.pop("screen_questions", None)
            rubric.pop("soft_skills", None)
            rubric.pop("bot_introduction", None)

        # Build JD block with structured context and rubric
        if job_row:
            jd = {
                "job_id": job_row.get("job_id") or request.job_id,
                "jobdiva_id": job_row.get("jobdiva_id") or "",
                "title": job_row.get("enhanced_title") or job_row.get("title", ""),
                "customer_name": job_row.get("customer_name") or "Unknown",
                "city": job_row.get("city") or "TBD",
                "state": job_row.get("state") or "",
                "location_type": job_row.get("location_type") or "Onsite",
                "context": {
                    "title": job_row.get("enhanced_title") or job_row.get("title", ""),
                    "customer_name": job_row.get("customer_name") or "Unknown",
                    "city": job_row.get("city") or "TBD",
                    "state": job_row.get("state") or "",
                    "location_type": job_row.get("location_type") or "Onsite",
                    "jobdiva_description": job_row.get("jobdiva_description") or "",
                    "ai_description": job_row.get("ai_description") or "",
                    "recruiter_notes": job_row.get("recruiter_notes") or "",
                },
                "rubric": rubric if rubric else {},
                "pre_screen_questions": pre_screen_questions,
            }
        else:
            jd = {
                "job_id": request.job_id,
                "jobdiva_id": "",
                "context": {},
                "rubric": {},
                "pre_screen_questions": _sanitize_pre_screen_questions_for_pair(
                    [],
                    fallback_job_title=request.job_id,
                )
            }

        # Build resumes list using raw_resume_text
        final_resumes = []
        for r in resumes:
            candidate_name = r.get("name") or "Unknown"
            candidate_email = str(r.get("email") or "").strip().lower()
            final_resumes.append({
                "source_candidate_id": r.get("source_candidate_id"),
                "name": candidate_name,
                "email": candidate_email,
                "phone": r.get("phone"),
                "raw_resume_text": r.get("experience", ""), # LiveKit expects raw_resume_text
                "experience": r.get("experience", ""),      # Frontend expects experience for auto-population
                "summary": r.get("summary", ""),
                "skills": r.get("skills", ""),
                "education": r.get("education", ""),
            })

        # Assemble final payload matching pairbotqa /api/bulk-interviews schema
        payload = {
            "resumes": final_resumes,
            "jd": jd,
            "company_intro": (job_row.get("bot_introduction") or "") if job_row else "",
            "interview_duration": "20-25"
        }

        payload_str = json.dumps(payload, indent=2)

        response_body = {
            "success": True,
            "payload": payload_str,
            "candidate_count": len(resumes),
            "dnc_blocked_count": len(dnc_blocked_ids),
            "dnc_blocked_ids": dnc_blocked_ids,
        }
        _log_generate_payload_response_to_sentry(response_body, level="info")
        return response_body

    except Exception as e:
        _log_generate_payload_response_to_sentry(
            {
                "success": False,
                "error": str(e),
                "candidate_ids": request.candidate_ids,
                "job_id": request.job_id,
            },
            level="error",
        )
        logger.error(f"❌ generate-payload failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

# ---------------------------------------------------------------------------
# Helper: fire PAIR launch notifications (background task)
# Fires Email #1 (launch confirmation) + Email #2 (job posting request)
# from a single DB query so we don't hit monitored_jobs twice.
# ---------------------------------------------------------------------------
async def _send_pair_launch_email(*, job_id: str, candidate_count: int, send_job_posting: bool = True, app_base_url: str = "") -> None:
    """
    Fetches job metadata from monitored_jobs and fires launch emails.

    Email #1 (PAIR launch confirmation to recruiters) always fires.
    Email #2 (job posting team request) is gated on `send_job_posting` —
    callers pass False on re-launches so we don't re-spam the posting team
    every time a recruiter sources another batch of candidates.

    Runs inside asyncio.create_task() so failures are fully isolated.
    """
    if not job_id:
        return
    try:
        conn = _get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT job_id, jobdiva_id, title, enhanced_title, customer_name,
                   city, state, location_type, bot_introduction,
                   jobdiva_description, ai_description, recruiter_notes,
                   selected_job_boards, recruiter_emails
            FROM monitored_jobs
            WHERE job_id = %s OR jobdiva_id = %s
            ORDER BY (job_id ~ '^[0-9]+$') DESC, created_at DESC
            LIMIT 1
        """, (job_id, job_id))
        row = cur.fetchone()
        cur.close()
        conn.close()

        if not row:
            logger.warning("📧 _send_pair_launch_email: job '%s' not found in monitored_jobs", job_id)
            return

        recruiter_emails: list = _parse_json_list(row.get("recruiter_emails", []))
        job_boards: list       = _parse_json_list(row.get("selected_job_boards", []))

        jobdiva_id    = str(row.get("jobdiva_id") or "")
        job_title      = row.get("enhanced_title") or row.get("title", "")
        customer_name  = row.get("customer_name", "Unknown")
        location       = f"{row.get('city', 'TBD')}, {row.get('state', '')}"
        db_job_id     = str(row.get("job_id") or job_id)
        clean_emails  = [str(e) for e in recruiter_emails if e]
        ai_desc       = row.get("ai_description") or ""

        # ── Email #1: PAIR Launch Confirmation ───────────────────────────────
        await asyncio.to_thread(
            notify_pair_launched,
            jobdiva_id=jobdiva_id,
            job_title=job_title,
            customer_name=customer_name,
            candidate_count=candidate_count,
            recruiter_emails=clean_emails,
            job_id=db_job_id,
            app_base_url=app_base_url,
        )

        # ── Email #2: Job Posting Request (skipped on re-launch or no boards) ──
        if send_job_posting and job_boards:
            await asyncio.to_thread(
                notify_job_posting,
                jobdiva_id=jobdiva_id,
                job_title=job_title,
                recruiter_emails=clean_emails,
                job_boards=job_boards,
                ai_description=ai_desc,
                job_id=db_job_id,
                app_base_url=app_base_url,
            )
        else:
            logger.info(
                "📧 Skipping job-posting email for job %s (not requested or no job boards selected)",
                jobdiva_id or job_id,
            )

    except Exception as exc:
        logger.warning("📧 _send_pair_launch_email failed silently: %s", exc, exc_info=True)


async def _post_to_pairbot(
    url: str,
    payload_obj: Dict[str, Any],
    *,
    max_attempts: int = 3,
    base_backoff_seconds: float = 2.0,
    timeout_seconds: float = 60.0,
) -> httpx.Response:
    """POST to pairbot with retries on timeout / network error / 5xx.

    Pairbot occasionally returns 5xx or times out under load; a single 60s
    attempt was enough to make manual Engage clicks feel flaky. 4xx responses
    are returned immediately (client error — retry won't help).
    """
    last_exc: Optional[BaseException] = None
    last_response: Optional[httpx.Response] = None
    for attempt in range(max_attempts):
        try:
            async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                response = await client.post(
                    url,
                    json=payload_obj,
                    headers={"Content-Type": "application/json"},
                )
            if response.status_code < 500:
                return response
            last_response = response
            logger.warning(
                "pairbot_5xx attempt=%d/%d status=%d",
                attempt + 1, max_attempts, response.status_code,
            )
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            last_exc = exc
            logger.warning(
                "pairbot_transport_error attempt=%d/%d type=%s msg=%s",
                attempt + 1, max_attempts, type(exc).__name__, str(exc),
            )
        if attempt < max_attempts - 1:
            await asyncio.sleep(base_backoff_seconds * (2 ** attempt))

    if last_response is not None:
        return last_response
    raise last_exc if last_exc is not None else RuntimeError("pairbot call failed")


def _persist_jobdiva_candidate_id(candidate_id_internal: str, cand_data: Dict[str, Any]) -> None:
    """Brief write to stamp jobdiva_candidate_id into sourced_candidates.data.

    Split out of _provision_candidate_to_jobdiva so the pool slot is held only
    for the UPDATE itself, never across the multi-second JobDiva HTTP calls.
    """
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE sourced_candidates SET data = %s WHERE candidate_id = %s",
                (json.dumps(cand_data), candidate_id_internal),
            )
        conn.commit()
    finally:
        conn.close()


async def _resolve_provisioning_job_ids(job_id_internal: str):
    """Resolve (numeric_job_id, ref_job_id) from either form of the job identifier.
    Returns (None, None) if the job is not found.
    """
    numeric_job_id = None
    ref_job_id = None
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if str(job_id_internal).isdigit():
            numeric_job_id = str(job_id_internal)
            cur.execute("SELECT jobdiva_id FROM monitored_jobs WHERE job_id = %s LIMIT 1", (numeric_job_id,))
            row_j = cur.fetchone()
            if row_j:
                ref_job_id = row_j["jobdiva_id"]
        else:
            ref_job_id = job_id_internal
            cur.execute("SELECT job_id FROM monitored_jobs WHERE jobdiva_id = %s LIMIT 1", (ref_job_id,))
            row_j = cur.fetchone()
            if row_j:
                numeric_job_id = row_j["job_id"]
        cur.close()
    finally:
        conn.close()
    return numeric_job_id, ref_job_id


async def _provision_batch_to_jobdiva(
    candidate_ids: List[str],
    job_id_internal: str,
    *,
    label: str = "Batch Provisioning",
) -> Dict[str, Any]:
    """
    Efficiently provisions all candidates as JobDiva applicants for a job.

    KEY IMPROVEMENT over the old per-candidate approach:
    - Resolves job IDs ONCE (not N times)
    - Fetches the applicants list from JobDiva ONCE (not N times)
    - Loads all candidate rows in a SINGLE DB query
    - Processes candidates concurrently, bounded by _PROVISION_CONCURRENCY

    Returns a dict with 'success', 'skipped', 'failed' counts.
    """
    results: Dict[str, int] = {"success": 0, "skipped": 0, "failed": 0}
    if not candidate_ids:
        return results

    try:
        # ── Phase 1: Resolve job IDs (one DB round-trip)
        numeric_job_id, ref_job_id = await _resolve_provisioning_job_ids(job_id_internal)
        if not numeric_job_id and not ref_job_id:
            logger.warning(f"⚠️ [{label}] Cannot resolve job IDs for '{job_id_internal}'. Skipping all.")
            results["failed"] = len(candidate_ids)
            return results

        # ── Phase 2: Load ALL candidate rows in ONE query
        conn = get_db_connection()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("""
                SELECT candidate_id, name, email, phone, resume_text, data, jobdiva_id, source
                FROM sourced_candidates
                WHERE candidate_id = ANY(%s)
                  AND (jobdiva_id = %s OR jobdiva_id = %s OR jobdiva_id = %s OR jobdiva_id = 'unknown')
            """, (list(candidate_ids), job_id_internal, numeric_job_id, ref_job_id))
            candidate_rows: Dict[str, Any] = {str(r["candidate_id"]): r for r in cur.fetchall()}
            cur.close()
        finally:
            conn.close()

        missing = len(candidate_ids) - len(candidate_rows)
        if missing:
            logger.warning(f"⚠️ [{label}] {missing}/{len(candidate_ids)} candidate IDs not found in sourced_candidates for job {ref_job_id}")

        # ── Phase 3: Fetch existing applicants from JobDiva ONCE
        jd_job_id = numeric_job_id or ref_job_id
        existing_applicants: List[Dict[str, Any]] = []
        if jd_job_id:
            logger.info(f"🔍 [{label}] Fetching existing applicants for job {jd_job_id} (single call)...")
            existing_applicants = await jobdiva_service.get_job_applicants_detail(jd_job_id)
            logger.info(f"✅ [{label}] Found {len(existing_applicants)} existing applicants in JobDiva")

        # Build fast-lookup sets for dedup — keyed on normalised email, phone, and JD candidate ID
        existing_jd_ids: set = {
            str(a.get("candidateId") or a.get("CANDIDATEID") or "")
            for a in existing_applicants
        }
        existing_emails: set = {
            str(a.get("EMAIL") or a.get("email") or "").lower().strip()
            for a in existing_applicants
            if not str(a.get("EMAIL") or a.get("email") or "").lower().startswith("auto_")
        }
        existing_phones: set = {
            "".join(ch for ch in str(a.get("PHONE") or a.get("phone") or "") if ch.isdigit())
            for a in existing_applicants
        }

        # ── Phase 4: Provision each candidate (concurrent, semaphore-bounded)
        async def _provision_one(cand_id: str) -> str:
            async with _PROVISION_CONCURRENCY:
                row = candidate_rows.get(str(cand_id))
                if not row:
                    logger.warning(f"⚠️ [{label}] Candidate {cand_id} missing from DB — skipping.")
                    return "failed"

                cand_data = row.get("data") or {}
                if isinstance(cand_data, str):
                    try:
                        cand_data = json.loads(cand_data)
                    except Exception:
                        cand_data = {}

                email = (row.get("email") or "").strip()
                phone = (row.get("phone") or "").strip()
                existing_jd_id = str(cand_data.get("jobdiva_candidate_id") or "")
                if not existing_jd_id and str(cand_id).isdigit():
                    existing_jd_id = str(cand_id)

                phone_norm = "".join(ch for ch in phone if ch.isdigit())
                email_lower = email.lower()

                # Check against pre-fetched applicant sets (no extra API call)
                jcid_match = bool(existing_jd_id and existing_jd_id in existing_jd_ids)
                email_match = bool(
                    email_lower
                    and not email_lower.startswith("auto_")
                    and email_lower in existing_emails
                )
                phone_match = bool(
                    phone_norm and len(phone_norm) >= 7 and phone_norm in existing_phones
                )

                if jcid_match or email_match or phone_match:
                    logger.info(
                        f"✅ [{label}] Candidate {cand_id} already in JobDiva "
                        f"(jcid={jcid_match} email={email_match} phone={phone_match})"
                    )
                    # Stamp the JD id if it wasn't persisted yet
                    if not cand_data.get("jobdiva_candidate_id") and existing_jd_id:
                        cand_data["jobdiva_candidate_id"] = existing_jd_id
                        _persist_jobdiva_candidate_id(cand_id, cand_data)
                    return "skipped"

                # ── Not found → create a new JobDiva application
                candidate_name = (row.get("name") or "").strip()
                name_parts = candidate_name.split(" ", 1) if candidate_name else ["", ""]
                first_name = name_parts[0]
                last_name = name_parts[1] if len(name_parts) > 1 else ""
                safe_name = (candidate_name or "Candidate").replace(" ", "_")

                actual_resume = (row.get("resume_text") or "").strip()
                resume_text = (
                    f"{candidate_name.upper()}\n"
                    f"Email: {email or 'N/A'} | Phone: {phone or 'N/A'}\n\n"
                    + (actual_resume or "(Profile sourced via PAIR)")
                )

                try:
                    success, new_jd_id = await jobdiva_service.create_job_application_with_resume(
                        candidate_id=None,
                        job_id=jd_job_id,
                        resume_text=resume_text,
                        filename=f"{safe_name}_Resume.txt",
                        first_name=first_name,
                        last_name=last_name,
                        email=email or "",
                        phone=phone or "",
                    )
                except Exception as exc:
                    logger.error(f"❌ [{label}] Exception for {cand_id}: {exc}", exc_info=True)
                    return "failed"

                if success and new_jd_id:
                    logger.info(f"🎉 [{label}] Candidate {cand_id} → JobDiva ID: {new_jd_id}")
                    cand_data["jobdiva_candidate_id"] = new_jd_id
                    _persist_jobdiva_candidate_id(cand_id, cand_data)
                    # Add to in-memory set so concurrent siblings don't re-create the same person
                    existing_jd_ids.add(str(new_jd_id))
                    if email_lower and not email_lower.startswith("auto_"):
                        existing_emails.add(email_lower)
                    if phone_norm and len(phone_norm) >= 7:
                        existing_phones.add(phone_norm)
                    return "success"
                elif success:
                    # Created but JD returned no ID (rare — log and treat as partial success)
                    logger.warning(f"⚠️ [{label}] Created for {cand_id} but got no new_jd_id")
                    return "success"
                else:
                    logger.error(f"❌ [{label}] create_job_application_with_resume returned False for {cand_id}")
                    return "failed"

        tasks = [_provision_one(cid) for cid in candidate_ids]
        statuses = await asyncio.gather(*tasks, return_exceptions=True)
        for s in statuses:
            if isinstance(s, Exception):
                results["failed"] += 1
            elif s == "success":
                results["success"] += 1
            elif s == "skipped":
                results["skipped"] += 1
            else:
                results["failed"] += 1

        logger.info(f"📊 [{label}] Done for job {ref_job_id}: {results}")
        return results

    except Exception as e:
        logger.error(f"❌ [{label}] Outer error: {e}", exc_info=True)
        results["failed"] = len(candidate_ids)
        return results


async def _provision_candidate_to_jobdiva(candidate_id_internal: str, job_id_internal: str):
    """Single-candidate shim kept for backward-compat. Delegates to the batch function."""
    result = await _provision_batch_to_jobdiva([candidate_id_internal], job_id_internal)
    return result.get("success", 0) > 0 or result.get("skipped", 0) > 0


@router.post("/engage/send-bulk-interview")
async def send_bulk_interview(request: SendBulkInterviewRequest):
    """
    Send the (potentially edited) interview payload to the PAIR bulk-interviews API.
    Saves the request and response to engage_interview_audit for traceability.
    """
    # Pool-leak safety net for the audit-write block below: if any exception
    # fires between `conn = _get_db_connection()` and the explicit close, the
    # outer try's finally returns the slot. The explicit close stays on the
    # happy path so the slot frees before the post-success email tasks run;
    # _PooledConnection.close() is idempotent (re-close is a no-op).
    conn = None
    cur = None
    try:
        # Parse the payload
        try:
            payload_obj = json.loads(request.payload)
            jd_block = payload_obj.get("jd", {})
            job_id_from_payload = jd_block.get("job_id") or jd_block.get("jobdiva_id") or "unknown"
            print(f"DEBUG: send_bulk_interview called for job {job_id_from_payload}")
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON format in payload")

        _validate_pair_payload_contacts(payload_obj)

        # Defense-in-depth: refuse to engage candidates for jobs whose outreach
        # has been stopped. /candidates/save already blocks earlier, but this
        # endpoint is also reachable directly.
        if job_id_from_payload and job_id_from_payload != "unknown":
            try:
                _stop_conn = _get_db_connection()
                try:
                    _stop_cur = _stop_conn.cursor()
                    _stop_cur.execute("""
                        SELECT outreach_stopped_at FROM monitored_jobs
                        WHERE job_id = %s OR jobdiva_id = %s
                        LIMIT 1
                    """, (str(job_id_from_payload), str(job_id_from_payload)))
                    _stop_row = _stop_cur.fetchone()
                    _stop_cur.close()
                    if _stop_row and _stop_row[0] is not None:
                        raise HTTPException(
                            status_code=409,
                            detail="Job activity has been stopped. Cannot launch new candidates.",
                        )
                finally:
                    _stop_conn.close()
            except HTTPException:
                raise
            except Exception as _stop_check_err:
                logger.warning(f"Could not check outreach_stopped_at for job {job_id_from_payload}: {_stop_check_err}")

        # Idempotency: drop candidates already at engage_status='sent' so retries
        # or staged-launch races don't create duplicate interviews on the PAIR
        # side. We keep their candidate_ids in skipped_already_sent so the caller
        # can show them as already-launched in the UI.
        skipped_already_sent: List[str] = []
        if request.real_candidate_ids:
            try:
                _idem_conn = _get_db_connection()
                try:
                    _idem_cur = _idem_conn.cursor()
                    _idem_cur.execute(
                        """
                        SELECT DISTINCT candidate_id
                        FROM sourced_candidates
                        WHERE candidate_id = ANY(%s)
                          AND (jobdiva_id = %s OR jobdiva_id = %s)
                          AND data ->> 'engage_status' = 'sent'
                        """,
                        (
                            list(request.real_candidate_ids),
                            str(job_id_from_payload or ""),
                            str(payload_obj.get("jd", {}).get("jobdiva_id") or ""),
                        ),
                    )
                    skipped_already_sent = [r[0] for r in _idem_cur.fetchall() or []]
                    _idem_cur.close()
                finally:
                    _idem_conn.close()
            except Exception as _idem_err:
                logger.warning(f"engage idempotency check failed for job {job_id_from_payload}: {_idem_err}")

        if skipped_already_sent:
            skip_set = set(skipped_already_sent)
            kept_indices = [
                idx for idx, cid in enumerate(request.real_candidate_ids) if cid not in skip_set
            ]
            request.real_candidate_ids = [request.real_candidate_ids[i] for i in kept_indices]
            existing_resumes = payload_obj.get("resumes") if isinstance(payload_obj, dict) else None
            if isinstance(existing_resumes, list) and len(existing_resumes) >= max(kept_indices, default=-1) + 1:
                payload_obj["resumes"] = [existing_resumes[i] for i in kept_indices]
            logger.info(
                f"engage idempotency: skipped {len(skipped_already_sent)} already-sent candidates for job {job_id_from_payload}"
            )

        if not request.real_candidate_ids:
            return {
                "success": True,
                "message": "All requested candidates were already launched; nothing to send.",
                "data": [],
                "skipped_already_sent": skipped_already_sent,
                "raw_response": {},
            }

        is_success = False
        response_data = {}

        if request.dry_run:
            logger.info("🧪 Dry run enabled: Skipping external PAIR API call.")
            is_success = True
            response_data = {
                "status": "success",
                "message": "DRY RUN: External call skipped, but local processing continued.",
                "data": [
                    {"interview_id": f"dry_run_{cid}", "candidate_id": cid}
                    for cid in request.real_candidate_ids
                ]
            }
        else:
            # Send to external PAIR API
            external_url = f"{EXTERNAL_INTERVIEW_API_URL}/api/bulk-interviews"
            logger.info(f"📤 Sending bulk interview to {external_url}")

            response = await _post_to_pairbot(external_url, payload_obj)

            response_data = {}
            try:
                if response.content:
                    response_data = response.json()
                else:
                    response_data = {"message": "Success (Empty response)"}
            except Exception:
                response_data = {"message": f"Raw: {response.text[:100]}"}

            is_success = response.status_code in [200, 201]
            logger.info(f"📥 PAIR API response status: {response.status_code}")

        # Save audit log for each candidate
        conn = _get_db_connection()
        cur = conn.cursor()

        def _write_candidate_engage_status(
            candidate_id: str,
            status_value: str,
            job_id_value: str,
            interview_id_value: str = "",
            response_fragment: Optional[Dict[str, Any]] = None,
        ) -> None:
            """Write-through status sync for rank-list source of truth.

            Rank-list reads engage_status from sourced_candidates.data, so we
            must update that blob whenever engage send state changes.
            """
            now_iso = datetime.now(timezone.utc).isoformat()
            cur.execute(
                """
                UPDATE sourced_candidates
                SET data =
                    jsonb_set(
                        jsonb_set(
                            jsonb_set(
                                COALESCE(data, '{}'::jsonb),
                                '{engage_status}',
                                to_jsonb(%s::text),
                                true
                            ),
                            '{engage_updated_at}',
                            to_jsonb(%s::text),
                            true
                        ),
                        '{engage_interview_id}',
                        to_jsonb(%s::text),
                        true
                    ),
                    updated_at = CURRENT_TIMESTAMP
                WHERE candidate_id = %s
                  AND (
                    jobdiva_id = %s
                    OR jobdiva_id = %s
                  )
                """,
                (
                    status_value,
                    now_iso,
                    interview_id_value,
                    candidate_id,
                    str(job_id_value or ""),
                    str(job_id_from_payload or ""),
                ),
            )

            # Preserve last external response snippet for support/debugging.
            if response_fragment is not None:
                cur.execute(
                    """
                    UPDATE sourced_candidates
                    SET data = jsonb_set(
                            COALESCE(data, '{}'::jsonb),
                            '{engage_last_response}',
                            %s::jsonb,
                            true
                        ),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE candidate_id = %s
                      AND (
                        jobdiva_id = %s
                        OR jobdiva_id = %s
                      )
                    """,
                    (
                        json.dumps(response_fragment),
                        candidate_id,
                        str(job_id_value or ""),
                        str(job_id_from_payload or ""),
                    ),
                )

        interview_results = []

        if is_success and isinstance(response_data, dict):
            # Extract interview data from response
            data_list = response_data.get("data", [])
            if not isinstance(data_list, list):
                data_list = [response_data] if response_data else []

            # Map pairbot response entries by a stable per-candidate key first,
            # then fall back to email for older responses.
            interview_by_source_candidate_id: Dict[str, Dict[str, Any]] = {}
            interview_by_email: Dict[str, Dict[str, Any]] = {}
            for item in data_list:
                if not isinstance(item, dict):
                    continue
                item_source_id = str(item.get("source_candidate_id") or "").strip()
                if item_source_id:
                    interview_by_source_candidate_id[item_source_id] = item
                item_email = str(item.get("candidate_email") or "").lower().strip()
                if item_email:
                    interview_by_email[item_email] = item

            # Position N in real_candidate_ids corresponds to position N in
            # payload_obj.resumes (both built from the same selection in
            # generate-payload), so we can recover the submitted identifiers
            # per candidate without another DB lookup.
            payload_resumes = payload_obj.get("resumes") or []
            submitted_source_id_by_idx: List[str] = [
                str((r or {}).get("source_candidate_id") or "").strip()
                for r in payload_resumes
            ]
            submitted_email_by_idx: List[str] = [
                str((r or {}).get("email") or "").lower().strip()
                for r in payload_resumes
            ]

            # ── TRIGGER PROVISIONING (JobDiva Application) ─────────────
            # Background-fire batch provisioning so it doesn't block the
            # HTTP response. The batch function fetches the applicants list
            # ONCE and creates all missing candidates concurrently under
            # _PROVISION_CONCURRENCY, which is far more efficient than the
            # old per-candidate approach (N JobDiva round-trips → 1).
            if request.real_candidate_ids:
                _batch_cids = list(request.real_candidate_ids)
                _batch_job_id = job_id_from_payload

                async def _run_batch_provisioning() -> None:
                    await _provision_batch_to_jobdiva(_batch_cids, _batch_job_id)

                asyncio.create_task(_run_batch_provisioning())

            for idx, candidate_id in enumerate(request.real_candidate_ids):
                submitted_source_id = (
                    submitted_source_id_by_idx[idx]
                    if idx < len(submitted_source_id_by_idx)
                    else ""
                )
                submitted_email = (
                    submitted_email_by_idx[idx]
                    if idx < len(submitted_email_by_idx)
                    else ""
                )
                interview_info = (
                    interview_by_source_candidate_id.get(submitted_source_id)
                    if submitted_source_id
                    else {}
                )
                if not interview_info:
                    interview_info = interview_by_email.get(submitted_email) or {}
                if not interview_info and idx < len(data_list) and isinstance(data_list[idx], dict):
                    # Legacy positional fallback: only used when the email
                    # match missed AND a positional entry actually exists.
                    # Avoids dropping data when pairbot returns entries
                    # without a candidate_email field.
                    interview_info = data_list[idx]

                interview_id = str(interview_info.get("interview_id") or "")
                candidate_name = interview_info.get("candidate_name", "")
                candidate_email = interview_info.get("candidate_email", submitted_email)

                if not interview_id:
                    logger.warning(
                        "engagement_no_interview_match candidate_id=%s submitted_email=%s response_data_count=%d request_count=%d",
                        candidate_id,
                        submitted_email,
                        len(data_list),
                        len(request.real_candidate_ids),
                    )

                # Extract job_id from payload (prefer reference jobdiva_id for UI consistency)
                job_id_resolved = payload_obj.get("jd", {}).get("jobdiva_id") or payload_obj.get("jd", {}).get("job_id", "")

                audit_status = "Initiated" if interview_id else "failed"
                engage_status = "sent" if interview_id else "failed"

                cur.execute("""
                    INSERT INTO engage_interview_audit
                        (candidate_id, jobdiva_id, interview_id, candidate_name, candidate_email, payload, response, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    candidate_id,
                    job_id_resolved,
                    interview_id,
                    candidate_name,
                    candidate_email,
                    json.dumps(payload_obj),
                    json.dumps(interview_info),
                    audit_status
                ))

                _write_candidate_engage_status(
                    candidate_id=candidate_id,
                    status_value=engage_status,
                    job_id_value=job_id_resolved,
                    interview_id_value=interview_id,
                    response_fragment=interview_info,
                )

                interview_results.append({
                    "candidate_id": candidate_id,
                    "interview_id": interview_id,
                    "candidate_name": candidate_name,
                    "candidate_email": candidate_email,
                    "links": interview_info.get("links", {}),
                    "session_token": interview_info.get("session_token", ""),
                    "created_at": interview_info.get("created_at", "")
                })
        else:
            # Still log the failed attempt
            for candidate_id in request.real_candidate_ids:
                job_id_resolved = payload_obj.get("jd", {}).get("jobdiva_id") or payload_obj.get("jd", {}).get("job_id", "")
                cur.execute("""
                    INSERT INTO engage_interview_audit
                        (candidate_id, jobdiva_id, payload, response, status)
                    VALUES (%s, %s, %s, %s, %s)
                """, (
                    candidate_id,
                    job_id_resolved,
                    json.dumps(payload_obj),
                    json.dumps(response_data),
                    "failed"
                ))

                _write_candidate_engage_status(
                    candidate_id=candidate_id,
                    status_value="failed",
                    job_id_value=job_id_resolved,
                    interview_id_value="",
                    response_fragment=response_data if isinstance(response_data, dict) else {"response": response_data},
                )

        conn.commit()
        cur.close()
        conn.close()

        if is_success:
            # ── Fire PAIR launch confirmation email (non-blocking) ──────────
            if request.is_initial_launch:
                # v22: initial launch — immediate sync of existing JobDiva applicants.
                # Applicants are assigned to rankings with match_score=0 (N/A).
                logger.info(f"🚀 [Engagement] Initial launch detected for job {job_id_from_payload}. Triggering applicant sync.")
                asyncio.create_task(auto_assign_service.synchronize_job_applicants(job_id_from_payload))

            # Manual rankings Screen sends should create the interview only.
            # Wizard Launch/re-source flows pass notify_recruiters=True so the
            # launch confirmation email goes out. (Pre-fix, this gate was
            # `is_initial_launch or dry_run` — but dry_run also skipped the
            # pairbot call, so wizard launches never actually created
            # interviews.)
            if request.is_initial_launch or request.notify_recruiters:
                send_job_posting = (
                    request.send_job_posting_email
                    if request.send_job_posting_email is not None
                    else request.is_initial_launch
                )
                asyncio.create_task(
                    _send_pair_launch_email(
                        job_id=job_id_from_payload,
                        candidate_count=len(interview_results),
                        send_job_posting=send_job_posting,
                        app_base_url=request.app_base_url,
                    )
                )
            return {
                "success": True,
                "message": "Interview(s) sent successfully",
                "bulk_id": response_data.get("bulk_id"),
                "data": interview_results,
                "skipped_already_sent": skipped_already_sent,
                "raw_response": response_data
            }
        else:
            return {
                "success": False,
                "message": _extract_pair_error_message(response_data, response.status_code),
                "data": [],
                "skipped_already_sent": skipped_already_sent,
                "raw_response": response_data
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ send-bulk-interview failed: {e}", exc_info=True)
        return {
            "success": False,
            "message": f"Server error: {str(e)}",
            "data": []
        }
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

@router.get("/engage/bulk-status/stream")
async def stream_engagement_status(bulk_id: str):
    from fastapi.responses import StreamingResponse
    import httpx
    
    external_url = f"{EXTERNAL_INTERVIEW_API_URL}/api/bulk-interviews/{bulk_id}/stream"
    
    async def proxy_stream():
        async with httpx.AsyncClient() as client:
            try:
                async with client.stream("GET", external_url, timeout=None) as response:
                    async for chunk in response.aiter_bytes():
                        yield chunk
            except Exception as e:
                logger.error(f"Error proxying SSE stream for bulk_id {bulk_id}: {e}")
                yield b"data: {\"status\": \"error\"}\n\n"
                
    return StreamingResponse(proxy_stream(), media_type="text/event-stream")

# Hard cap so a single sync run can never blast more than this many
# candidates at pairbot, even if the job's filters are loose. The auto-sync
# cron path uses this to avoid an unbounded blast radius.
AUTO_LAUNCH_BATCH_CAP = 25


async def auto_launch_for_candidates(candidate_ids: List[str], job_id: str) -> None:
    """Auto-launch pairbot interviews for newly-synced JobDiva applicants.

    Called from `auto_assign_service.synchronize_job_applicants` after it
    upserts new rows into `sourced_candidates`. Closes the gap where the
    15-min cron created candidate rows but never actually launched them
    to pairbot — so recruiters had to manually click Engage on every new
    applicant.

    Guards:
      - Only runs for jobs that have already been launched at least once
        (≥1 row with engage_status in sent/pending/completed). This stops
        us from auto-blasting brand-new jobs whose recruiter hasn't yet
        clicked "Launch PAIR".
      - Skips candidates that already have an engage_status set, are on
        the DNC list, or whose job has outreach_stopped_at set.
      - Hard-capped at AUTO_LAUNCH_BATCH_CAP per call.

    Failures are logged and swallowed — this runs as a background task and
    must not crash the calling sync cycle.
    """
    if not candidate_ids or not job_id:
        return
    try:
        conn = _get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            # Job-level gating: outreach must not be stopped, and the
            # recruiter must have launched PAIR at least once before — we
            # treat any prior engage event as the signal that auto-launch
            # is wanted for incoming applicants on this job.
            cur.execute(
                """
                SELECT outreach_stopped_at
                FROM monitored_jobs
                WHERE job_id = %s OR jobdiva_id = %s
                LIMIT 1
                """,
                (str(job_id), str(job_id)),
            )
            job_row = cur.fetchone()
            if job_row and job_row.get("outreach_stopped_at") is not None:
                logger.info(
                    "auto_launch_skip job_id=%s reason=outreach_stopped",
                    job_id,
                )
                return

            cur.execute(
                """
                SELECT 1 FROM sourced_candidates
                WHERE (jobdiva_id = %s OR jobdiva_id = %s)
                  AND data->>'engage_status' IN ('sent', 'pending', 'completed')
                LIMIT 1
                """,
                (str(job_id), str(job_id)),
            )
            if not cur.fetchone():
                logger.info(
                    "auto_launch_skip job_id=%s reason=no_prior_launch count=%d",
                    job_id,
                    len(candidate_ids),
                )
                return

            # Filter to candidates that haven't already been engaged and
            # aren't DNC-blocked. We re-check at the DB layer rather than
            # trusting the caller's list, since the sync run could overlap
            # with a manual Engage click.
            cur.execute(
                """
                SELECT candidate_id
                FROM sourced_candidates
                WHERE candidate_id = ANY(%s)
                  AND (jobdiva_id = %s OR jobdiva_id = %s)
                  AND dnc_stopped_at IS NULL
                  AND COALESCE(data->>'engage_status', '') NOT IN ('sent', 'pending', 'completed')
                """,
                (list(candidate_ids), str(job_id), str(job_id)),
            )
            eligible_ids = [str(r["candidate_id"]) for r in cur.fetchall()]
        finally:
            cur.close()
            conn.close()

        if not eligible_ids:
            logger.info(
                "auto_launch_skip job_id=%s reason=no_eligible_candidates",
                job_id,
            )
            return

        if len(eligible_ids) > AUTO_LAUNCH_BATCH_CAP:
            logger.warning(
                "auto_launch_truncated job_id=%s requested=%d cap=%d",
                job_id, len(eligible_ids), AUTO_LAUNCH_BATCH_CAP,
            )
            eligible_ids = eligible_ids[:AUTO_LAUNCH_BATCH_CAP]

        # Generate payload via the existing builder so JD context, rubric
        # and pre-screen questions stay in sync with manual launches.
        payload_result = await generate_engage_payload(
            GeneratePayloadRequest(candidate_ids=eligible_ids, job_id=job_id)
        )
        payload_str = payload_result.get("payload", "")
        if not payload_str:
            logger.warning(
                "auto_launch_skip job_id=%s reason=empty_payload candidate_count=%d",
                job_id, len(eligible_ids),
            )
            return

        logger.info(
            "auto_launch_dispatch job_id=%s candidate_count=%d",
            job_id, len(eligible_ids),
        )
        result = await send_bulk_interview(
            SendBulkInterviewRequest(
                payload=payload_str,
                real_candidate_ids=eligible_ids,
                is_initial_launch=False,
                dry_run=False,
                notify_recruiters=False,
            )
        )
        if not (result or {}).get("success"):
            logger.warning(
                "auto_launch_pairbot_unsuccessful job_id=%s message=%s",
                job_id, (result or {}).get("message"),
            )
    except Exception as exc:  # noqa: BLE001 — background task, must not crash caller
        logger.error(
            "auto_launch_failed job_id=%s candidate_count=%d error=%s",
            job_id, len(candidate_ids), exc,
            exc_info=True,
        )


@router.get("/latest-interview/by-id/{candidate_id}")
async def get_latest_interview(candidate_id: str):
    """
    Look up the latest interview_id for a candidate from the audit table.
    Used by the Assess button to determine which interview to display.
    """
    try:
        conn = _get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cur.execute("""
            SELECT interview_id, candidate_name, candidate_email, jobdiva_id, status, created_at
            FROM engage_interview_audit
            WHERE candidate_id = %s AND interview_id IS NOT NULL AND interview_id::text != ''
            ORDER BY id DESC
            LIMIT 1
        """, (candidate_id,))

        row = cur.fetchone()
        cur.close()
        conn.close()

        if row:
            return {
                "success": True,
                "interview_id": row["interview_id"],
                "candidate_name": row.get("candidate_name", ""),
                "candidate_email": row.get("candidate_email", ""),
                "job_id": row.get("job_id", ""),
                "status": row.get("status", ""),
                "created_at": str(row.get("created_at", ""))
            }
        else:
            return {
                "success": False,
                "interview_id": None,
                "message": "No interview found for this candidate"
            }

    except Exception as e:
        logger.error(f"❌ latest-interview lookup failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))




# ---------------------------------------------------------------------------
# 5. GET /assess/{interview_id}  (Proxy for PAIR dashboard data)
# ---------------------------------------------------------------------------
@router.get("/assess/{interview_id}")
async def get_assessment_data(interview_id: str):
    """
    Aggregates data from multiple PAIR dashboard endpoints into a single response
    for the Assess modal:
      - Interview info (status, score, progress)
      - Evaluation (per-question Q&A with scores)
      - Transcriptions (conversation messages)
      - Outreach status (communication timeline)
    """
    base_url = EXTERNAL_INTERVIEW_API_URL

    async with httpx.AsyncClient(timeout=30.0) as client:
        # Fire all 4 requests in parallel
        interview_task = client.get(f"{base_url}/api/interviews/{interview_id}")
        evaluation_task = client.get(f"{base_url}/api/interviews/{interview_id}/evaluation")
        transcription_task = client.get(f"{base_url}/api/interviews/{interview_id}/transcriptions")
        outreach_task = client.get(f"{base_url}/api/interviews/{interview_id}/outreach-status")

        # Await all
        interview_res = await interview_task
        evaluation_res = await evaluation_task
        transcription_res = await transcription_task
        outreach_res = await outreach_task

    # Parse responses (gracefully handle failures)
    def safe_json(response, label):
        try:
            if response.status_code == 200:
                return response.json()
            else:
                logger.warning(f"⚠️ {label} returned {response.status_code}")
                return None
        except Exception as e:
            logger.warning(f"⚠️ Failed to parse {label}: {e}")
            return None

    interview_data = safe_json(interview_res, "interview")
    evaluation_data = safe_json(evaluation_res, "evaluation")
    transcription_data = safe_json(transcription_res, "transcriptions")
    outreach_data = safe_json(outreach_res, "outreach-status")

    # Handle the case where the response wraps data in a "data" key
    if interview_data and "data" in interview_data:
        interview_data = interview_data["data"]
    if evaluation_data and "data" in evaluation_data:
        evaluation_data = evaluation_data["data"]
    if outreach_data and "data" in outreach_data:
        outreach_data = outreach_data["data"]
    if transcription_data and "data" in transcription_data:
        transcription_data = transcription_data["data"]

    # Prefer Curate's resolved pass/fail status when available so assessment
    # surfaces the effective engagement result instead of Pair Bot's raw
    # completion state.
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT status
                    FROM engage_interview_audit
                    WHERE interview_id = %s
                    ORDER BY updated_at DESC NULLS LAST, id DESC
                    LIMIT 1
                    """,
                    (interview_id,),
                )
                audit_row = cur.fetchone()
                if audit_row and interview_data:
                    effective_status = str(audit_row.get("status") or "").strip()
                    if effective_status:
                        interview_data["status"] = effective_status
    except Exception as e:
        logger.warning(f"⚠️ Failed to overlay effective interview status for {interview_id}: {e}")

    return {
        "success": True,
        "interview_id": interview_id,
        "interview": interview_data,
        "evaluation": evaluation_data,
        "transcriptions": transcription_data if isinstance(transcription_data, list) else (transcription_data or []),
        "outreach": outreach_data
    }


# ---------------------------------------------------------------------------
# 6. Outreach API Proxies
# ---------------------------------------------------------------------------
async def _proxy_get(path: str, params: dict = None):
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            res = await client.get(f"{EXTERNAL_INTERVIEW_API_URL}{path}", params=params)
            res.raise_for_status()
            return res.json()
    except Exception as e:
        logger.error(f"❌ Proxy GET {path} failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

async def _proxy_post(path: str, json_data: dict = None):
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            res = await client.post(f"{EXTERNAL_INTERVIEW_API_URL}{path}", json=json_data)
            res.raise_for_status()
            return res.json()
    except Exception as e:
        logger.error(f"❌ Proxy POST {path} failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/dashboard/pair-outreach")
async def get_pair_outreach(status: Optional[str] = None, phase: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None, jd_id: Optional[str] = None, search: Optional[str] = None):
    params = {k: v for k, v in {"status": status, "phase": phase, "date_from": date_from, "date_to": date_to, "jd_id": jd_id, "search": search}.items() if v is not None}
    return await _proxy_get("/api/dashboard/pair-outreach", params=params)

@router.get("/dashboard/pair-outreach/{jd_id}")
async def get_pair_outreach_jd(jd_id: str):
    return await _proxy_get(f"/api/dashboard/pair-outreach/{jd_id}")

@router.get("/dashboard/pair-metrics")
async def get_pair_metrics():
    return await _proxy_get("/api/dashboard/pair-metrics")

@router.get("/dashboard/pair-passed")
async def get_pair_passed(score_threshold: Optional[int] = None):
    params = {"score_threshold": score_threshold} if score_threshold is not None else {}
    return await _proxy_get("/api/dashboard/pair-passed", params=params)

@router.get("/interviews/{interview_id}/outreach-status")
async def get_outreach_status(interview_id: str):
    return await _proxy_get(f"/api/interviews/{interview_id}/outreach-status")

@router.post("/outreach/start-scheduler")
async def start_scheduler():
    return await _proxy_post("/api/outreach/start-scheduler")

@router.post("/interviews/{interview_id}/trigger-phase2")
async def trigger_phase2(interview_id: str):
    return await _proxy_post(f"/api/interviews/{interview_id}/trigger-phase2")


# ---------------------------------------------------------------------------
# 7. Retrieval of Transcripts API Proxies
# ---------------------------------------------------------------------------
@router.get("/interviews/{interview_id}/transcriptions")
async def get_transcriptions(interview_id: str):
    return await _proxy_get(f"/api/interviews/{interview_id}/transcriptions")

@router.get("/interviews/{interview_id}/activity-logs")
async def get_activity_logs(interview_id: str):
    return await _proxy_get(f"/api/interviews/{interview_id}/activity-logs")

from fastapi.responses import StreamingResponse

@router.get("/interviews/{interview_id}/transcriptions/download")
async def download_transcriptions(interview_id: str):
    try:
        client = httpx.AsyncClient(timeout=30.0)
        req = client.build_request("GET", f"{EXTERNAL_INTERVIEW_API_URL}/api/interviews/{interview_id}/transcriptions/download")
        res = await client.send(req, stream=True)
        return StreamingResponse(res.aiter_bytes(), headers=res.headers)
    except Exception as e:
        logger.error(f"❌ download_transcriptions failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

# ---------------------------------------------------------------------------
# Notification Helpers
# ---------------------------------------------------------------------------

async def _check_and_fire_candidate_passed_notification(
    interview_id: int,
    detail_payload: Dict[str, Any],
    job_id: str,
    candidate_id: str,
):
    """
    Checks if a candidate passed the phone screen criteria and fires Email #3.
    Criteria: PASS on all hard filters AND match score > 70%.
    """
    try:
        if not job_id or not candidate_id:
            return

        # 1. New Pass Criteria (Strictly from Webhook Payload)
        interview_block = detail_payload.get("interview", {})
        hf_status = str(interview_block.get("hard_filter_status") or "").lower()
        cand_score = interview_block.get("candidate_score")
        total_possible = interview_block.get("total_score")
        
        # Send pass email only when all hard filters are explicitly passed.
        meets_criteria = hf_status in (HARD_FILTER_PASS_STATUS, "pass")
        
        normalized_score_display = "Passed"
        if meets_criteria and cand_score is not None and total_possible:
            normalized_score_display = _format_normalized_score_100(cand_score, total_possible) or "Passed"
        
        if not meets_criteria:
            logger.info(f"⏭️ Candidate {candidate_id} did not meet strict pass criteria (HF: {hf_status}, Score: {cand_score}/{total_possible}).")
            return

        score_display = normalized_score_display if cand_score is not None else "Passed"

        # 5. Fetch Job & Candidate metadata for email
        conn = _get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # 4. Fetch Job & Candidate metadata for email
        cur.execute("""
            SELECT job_id, title, enhanced_title, city, state, pay_rate, recruiter_emails, jobdiva_id
            FROM monitored_jobs
            WHERE job_id = %s OR jobdiva_id = %s
            ORDER BY (job_id ~ '^[0-9]+$') DESC, created_at DESC
            LIMIT 1
        """, (job_id, job_id))
        job_row = cur.fetchone()
        
        cur.execute("""
            SELECT name, email, phone, resume_text, data
            FROM sourced_candidates
            WHERE candidate_id = %s AND jobdiva_id = %s
            LIMIT 1
        """, (candidate_id, job_row["jobdiva_id"] if job_row else job_id))
        cand_row = cur.fetchone()

        if not job_row or not cand_row:
            cur.close()
            conn.close()
            return

        # Deduplication: Atomically set engage_passed_email_sent=True and check old value.
        # This prevents a race condition where two concurrent webhook calls both read the
        # flag as False before either has written True, resulting in duplicate emails.
        jobdiva_id_for_dedup = job_row["jobdiva_id"] if job_row else job_id
        cur.execute("""
            UPDATE sourced_candidates
            SET data = COALESCE(data, '{}'::jsonb) || '{"engage_passed_email_sent": true}'::jsonb,
                updated_at = CURRENT_TIMESTAMP
            WHERE candidate_id = %s
              AND jobdiva_id = %s
              AND (data IS NULL OR (data->>'engage_passed_email_sent')::boolean IS NOT TRUE)
            RETURNING id
        """, (candidate_id, jobdiva_id_for_dedup))
        updated = cur.fetchone()
        conn.commit()

        if not updated:
            # Another concurrent request already set the flag — skip to avoid duplicate
            logger.info(f"⏭️ Passed email already sent for candidate {candidate_id} / job {job_id}. Skipping duplicate.")
            cur.close()
            conn.close()
            return

        cand_data = cand_row.get("data") or {}

        # 5. Build screening summary (all items in evaluation/transcriptions)
        screening_summary = []
        transcriptions = detail_payload.get("transcriptions") or []
        
        if transcriptions:
            for item in transcriptions:
                q_text = item.get("question") or "Question"
                a_text = item.get("answer") or "—"
                score = item.get("candidate_score")
                total = item.get("total_score", 10.0)
                reason = item.get("reason")
                hf_status_item = item.get("hard_filter_status")
                q_order_raw = item.get("question_order", 0)

                hf_norm = str(hf_status_item or "").strip().lower().replace(" ", "_")
                is_hard_filter = hf_norm not in ("", "not_hard_filter", "na", "n/a", "none")
                # Keep email behavior aligned with Pairbot UI:
                # - no score for hard-filter questions
                # - no score for info-only questions
                # - score only for truly scored evaluation questions
                score_value = None
                try:
                    score_value = float(score) if score is not None else None
                except (TypeError, ValueError):
                    score_value = None

                try:
                    q_order = int(q_order_raw)
                except (TypeError, ValueError):
                    q_order = 0

                # Pairbot contract:
                # Q1/Q4 hard filters (already excluded above), Q2,3,5-9 info-only, Q10+ scored.
                if q_order > 0:
                    is_info_only = (not is_hard_filter) and (q_order <= 9)
                else:
                    # Legacy fallback when question_order is missing
                    is_info_only = (not is_hard_filter) and (score_value is None or score_value < 0)
                is_scored_question = (not is_hard_filter) and (not is_info_only)

                screening_summary.append({
                    "question": q_text,
                    "answer": a_text,
                    "score": score,
                    "total_score": total,
                    "reason": reason,
                    "question_order": q_order,
                    "hard_filter_status": hf_status_item,
                    "is_hard_filter": is_hard_filter,
                    "is_info_only": is_info_only,
                    "is_scored_question": is_scored_question,
                })
        elif hf_status != "":
            # Fallback for simple status-based payload
            screening_summary.append({"field": "Hard Filter Status", "value": str(hf_status).capitalize()})
            screening_summary.append({"field": "Phone Screen Score", "value": score_display})
        else:
            # Legacy fallback
            for ev in (detail_payload.get("evaluation") or []):
                screening_summary.append({
                    "field": ev.get("question", "Question"),
                    "value": ev.get("answer", ev.get("status", "—"))
                })

        # Resolve Numeric Candidate ID for JobDiva API calls
        jd_candidate_id = candidate_id
        if cand_data and (cand_data.get("jobdiva_candidate_id") or cand_data.get("candidate_id")):
            potential_id = cand_data.get("jobdiva_candidate_id") or cand_data.get("candidate_id")
            if str(potential_id).isdigit():
                jd_candidate_id = str(potential_id)

        # 6. Prepare attachment (resume text as Word-compatible .doc fallback)
        resume_bytes = None
        resume_filename = None
        resume_text = ""

        # Reuse the same quality gate as the View Resume endpoint:
        # attach only real JobDiva resume text, skip placeholder/generated text.
        blocked_resume_markers = (
            "Professional experience details available upon request",
            "Experienced professional with a strong background",
            "Contact information and detailed work history available upon request",
            "Resume content unavailable",
        )

        # Only use JobDiva full resume text for attachment; no local fallback.
        if str(jd_candidate_id).isdigit():
            try:
                jd_resume = await jobdiva_service.get_candidate_resume(candidate_id=str(jd_candidate_id))
                if isinstance(jd_resume, dict):
                    fetched = jd_resume.get("resume_text") or ""
                    if fetched and not any(marker in fetched for marker in blocked_resume_markers):
                        resume_text = fetched
            except Exception as resume_err:
                logger.warning(
                    "Failed to fetch JobDiva resume for candidate %s: %s",
                    jd_candidate_id,
                    resume_err,
                )

        if any(marker in (resume_text or "") for marker in blocked_resume_markers):
            resume_text = ""

        if (resume_text or "").strip():
            resume_bytes = _build_word_resume_document(cand_row["name"] or "Candidate", resume_text)
            # Try to get name from candidate
            safe_name = "".join(c for c in (cand_row["name"] or "Candidate") if c.isalnum() or c in (" ", "-", "_")).strip().replace(" ", "_")
            resume_filename = f"Resume_{safe_name}_{job_id}.doc"
        
        app_job_id = str(job_row.get("job_id") or job_id)
        jd_job_id = str(job_row.get("jobdiva_id") or job_id)

        # 7. Fire the email & Update JobDiva Qualification
        recruiter_emails = _parse_json_list(job_row.get("recruiter_emails", []))
        
        # Determine the completion timestamp for the qualification update
        completion_ts = interview_block.get("completed_at") or interview_block.get("evaluation_completed_at")
        if not completion_ts:
            completion_ts = datetime.now(timezone.utc).isoformat()

        # Update JobDiva Qualification: PAIR Candidates = PASS
        # We fire these asynchronously so they don't block the email or main flow
        asyncio.create_task(
            jobdiva_service.update_candidate_qualification(
                candidate_id=jd_candidate_id,
                qualification_name=JOBDIVA_PAIR_QUALIFICATION_NAME,
                value=JOBDIVA_PASS_QUALIFICATION_VALUE,
                recruiter_id=JOBDIVA_PAIR_RECRUITER_ID,
                update_date=completion_ts,
                qualification_type_id=JOBDIVA_PAIR_QUALIFICATION_ID
            )
        )

        # Create JobDiva Note: PAIR Pass Candidate Report
        # Note: We use the job title from job_row for the message
        base_url = resolve_app_base_url(request.app_base_url if 'request' in locals() else "")
        pair_job_title = job_row.get("enhanced_title") or job_row.get("title") or "the"
        report_link = f"{base_url}/jobs/{jd_job_id}/report?candidateId={candidate_id}"
        note_text = f"Candidate completed Phone Screen for {pair_job_title} position. <a href=\"{report_link}\" target=\"_blank\">Click Here</a> to view the report."
        
        async def create_and_pin_note():
            note_res = await jobdiva_service.create_candidate_note(
                candidate_id=jd_candidate_id,
                job_id=jd_job_id,
                action=JOBDIVA_PASS_ACTION_NAME,
                note_text=note_text,
                recruiter_id=JOBDIVA_PAIR_RECRUITER_ID
            )
            if note_res.get("status") == "success":
                note_id = note_res.get("data")
                if note_id:
                    await jobdiva_service.pin_candidate_note(note_id=note_id, is_pinned=True)

        asyncio.create_task(create_and_pin_note())

        success = await asyncio.to_thread(
            notify_candidate_passed,
            candidate_name=cand_row["name"] or "Candidate",
            candidate_email=cand_row["email"],
            candidate_phone=cand_row["phone"],
            screen_score=score_display,
            summary=interview_block.get("summary") or "Passed screening criteria.",
            screening_summary=screening_summary,
            jobdiva_id=job_row["jobdiva_id"] or job_id,
            job_title=job_row.get("enhanced_title") or job_row.get("title") or "",
            location=f"{job_row['city']}, {job_row['state']}" if job_row['city'] else "—",
            salary_range=job_row["pay_rate"] or "—",
            recruiter_emails=recruiter_emails,
            resume_bytes=resume_bytes,
            resume_filename=resume_filename,
            candidate_id=candidate_id,
            job_id=app_job_id
        )

        if success:
            # Flag was already set atomically before sending (race-condition-safe).
            # Refresh Performance Metrics for this job (e.g. Time to First Pass)
            asyncio.create_task(auto_assign_service.refresh_job_performance_metrics(job_id))

        cur.close()
        conn.close()

    except Exception as e:
        logger.error(f"❌ Failed to process Candidate Passed notification: {e}", exc_info=True)


# ---------------------------------------------------------------------------
# Re-Provision endpoint
# Backfills all launched (engage_status='sent') candidates for a job that do
# not yet have a jobdiva_candidate_id.  Call this after a bulk launch to
# ensure all candidates are registered as JobDiva applicants so Reject/Submit
# and action-notes work correctly.
# ---------------------------------------------------------------------------
class ReProvisionRequest(BaseModel):
    job_id: str
    # If True, provisions ALL launched candidates (even those already provisioned).
    # Defaults to False = only candidates missing a jobdiva_candidate_id.
    force_all: bool = False


@router.post("/engage/re-provision")
async def re_provision_candidates(request: ReProvisionRequest):
    """
    Re-runs JobDiva provisioning for all launched candidates for a job.

    By default only candidates that are missing a jobdiva_candidate_id in
    their data blob are processed (the ones that were silently dropped by the
    old fire-and-forget approach).  Pass force_all=true to re-run for every
    launched candidate (useful to fix 'Unknown Unknown' names).

    Returns counts of { success, skipped, failed, total }.
    """
    job_id = (request.job_id or "").strip()
    if not job_id:
        raise HTTPException(status_code=400, detail="job_id is required")

    try:
        conn = _get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Resolve both forms of the job ID so we can match audit records
        numeric_job_id, ref_job_id = await _resolve_provisioning_job_ids(job_id)
        j1 = job_id
        j2 = numeric_job_id or job_id
        j3 = ref_job_id or job_id

        if request.force_all:
            # All launched candidates for this job
            cur.execute("""
                SELECT DISTINCT sc.candidate_id
                FROM sourced_candidates sc
                JOIN engage_interview_audit eia ON eia.candidate_id = sc.candidate_id
                WHERE eia.jobdiva_id IN (%s, %s, %s)
                  AND sc.jobdiva_id IN (%s, %s, %s)
            """, (j1, j2, j3, j1, j2, j3))
        else:
            # Only those missing a jobdiva_candidate_id
            cur.execute("""
                SELECT DISTINCT sc.candidate_id
                FROM sourced_candidates sc
                JOIN engage_interview_audit eia ON eia.candidate_id = sc.candidate_id
                WHERE eia.jobdiva_id IN (%s, %s, %s)
                  AND sc.jobdiva_id IN (%s, %s, %s)
                  AND (
                    sc.data->>'jobdiva_candidate_id' IS NULL
                    OR sc.data->>'jobdiva_candidate_id' = ''
                  )
            """, (j1, j2, j3, j1, j2, j3))

        rows = cur.fetchall()
        cur.close()
        conn.close()

        candidate_ids = [str(r["candidate_id"]) for r in rows]
        total = len(candidate_ids)
        logger.info(
            f"🔄 [Re-Provision] job={job_id} force_all={request.force_all} "
            f"candidates_to_process={total}"
        )

        if not candidate_ids:
            return {
                "success": True,
                "message": "No candidates require provisioning.",
                "total": 0,
                "success_count": 0,
                "skipped": 0,
                "failed": 0,
            }

        # Run batch provisioning (awaited so the HTTP response carries the result)
        results = await _provision_batch_to_jobdiva(
            candidate_ids,
            job_id,
            label="Re-Provision",
        )

        return {
            "success": True,
            "job_id": job_id,
            "total": total,
            "success_count": results.get("success", 0),
            "skipped": results.get("skipped", 0),
            "failed": results.get("failed", 0),
            "message": (
                f"Re-provisioning complete. "
                f"{results.get('success', 0)} created, "
                f"{results.get('skipped', 0)} already existed, "
                f"{results.get('failed', 0)} failed."
            ),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ [Re-Provision] error for job {job_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
