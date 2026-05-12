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

router = APIRouter(tags=["Engagement"])

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
EXTERNAL_INTERVIEW_API_URL = os.getenv("EXTERNAL_INTERVIEW_API_URL", "https://pairbotqa.hoonr.ai")
PASS_SCORE_THRESHOLD = float(os.getenv("PASS_SCORE_THRESHOLD", "70"))
PASS_CANDIDATE_SCORE_RATIO = float(os.getenv("PASS_CANDIDATE_SCORE_RATIO", "0.7"))
HARD_FILTER_PASS_STATUS = os.getenv("HARD_FILTER_PASS_STATUS", "passed").lower()
ENGAGE_PASSED_STATUSES = os.getenv("ENGAGE_PASSED_STATUSES", "completed,passed").lower().split(",")

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
                phone = row.get("phone", "") or ""
                email = row.get("email", "") or ""
                resume_text = row.get("resume_text", "") or ""

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
                    "name": name,
                    "email": email,
                    "phone": phone,
                    # pairbotqa expects experience / summary / skills — map raw resume
                    "experience": resume_text,
                    "summary": headline,
                    "skills": "",
                    "education": "",
                })
            else:
                # Fallback for candidates not found in DB
                resumes.append({
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
            pre_screen_questions = [
                {
                    "question_text": _humanize_question_text(r["question_text"]),
                    "pass_criteria": r["pass_criteria"],
                    "is_default": r["is_default"],
                    "category": r["category"],
                }
                for r in rows
            ]

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
                "context": {
                    "title": job_row.get("title", ""),
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
                "pre_screen_questions": []
            }

        # Build resumes list using raw_resume_text
        final_resumes = []
        for r in resumes:
            candidate_name = r.get("name") or "Unknown"
            candidate_email = r.get("email") or ""
            # LiveKit DB has chk_interviews_email_format — empty string fails the constraint.
            # If email is missing, generate a safe placeholder so the interview can still be created.
            if not candidate_email:
                safe_name = candidate_name.lower().replace(" ", ".").replace(",", "")
                candidate_email = f"{safe_name}@noemail.pair.ai"
            final_resumes.append({
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

        return {
            "success": True,
            "payload": payload_str,
            "candidate_count": len(resumes),
            "dnc_blocked_count": len(dnc_blocked_ids),
            "dnc_blocked_ids": dnc_blocked_ids,
        }

    except Exception as e:
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

        # ── Email #2: Job Posting Request (skipped on re-launch) ────────────
        if send_job_posting:
            await asyncio.to_thread(
                notify_job_posting,
                jobdiva_id=jobdiva_id,
                job_title=job_title,
                recruiter_emails=clean_emails,
                job_boards=job_boards,
                ai_description=ai_desc,
                app_base_url=app_base_url,
            )
        else:
            logger.info(
                "📧 Skipping job-posting email for job %s (re-launch — already sent on initial launch)",
                jobdiva_id or job_id,
            )

    except Exception as exc:
        logger.warning("📧 _send_pair_launch_email failed silently: %s", exc, exc_info=True)


async def _provision_candidate_to_jobdiva(candidate_id_internal: str, job_id_internal: str):
    """
    Ensures a candidate exists in JobDiva as an applicant for the specified job.
    """
    try:
        from routers._helpers import get_db_connection
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Resolve both numeric and alphanumeric IDs for the job
        numeric_job_id = None
        ref_job_id = None
        
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
        
        logger.info(f"🔍 [Provisioning] Checking JobDiva for Job {ref_job_id} ({numeric_job_id})")

        # 1. Fetch candidate record from our DB
        cur.execute("""
            SELECT name, email, phone, resume_text, data, jobdiva_id, source
            FROM sourced_candidates
            WHERE candidate_id = %s 
              AND (jobdiva_id = %s OR jobdiva_id = %s OR jobdiva_id = %s OR jobdiva_id = %s OR jobdiva_id = 'unknown')
            LIMIT 1
        """, (candidate_id_internal, job_id_internal, numeric_job_id, ref_job_id, "unknown"))
        row = cur.fetchone()
        
        if not row:
            logger.warning(f"⚠️ [Provisioning] Candidate {candidate_id_internal} not found in sourced_candidates. Cannot provision.")
            return None
            
        cand_data = row.get("data") or {}
        if isinstance(cand_data, str):
            cand_data = json.loads(cand_data)

        email = row.get("email")
        existing_jd_id = cand_data.get("jobdiva_candidate_id")
        if not existing_jd_id and str(candidate_id_internal).isdigit():
            existing_jd_id = int(candidate_id_internal)

        # 2. Check JobDiva Applicants Detail (LIVE)
        if numeric_job_id:
            logger.info(f"🔍 [Provisioning] Fetching live applicants for Job {numeric_job_id} from JobDiva...")
            applicants = await jobdiva_service.get_job_applicants_detail(int(numeric_job_id))
            
            for app in applicants:
                app_cid = app.get("candidateId") or app.get("CANDIDATEID")
                app_email = str(app.get("EMAIL") or app.get("email") or "").lower()
                
                if (existing_jd_id and app_cid and int(app_cid) == int(existing_jd_id)) or (email and app_email == email.lower()):
                    logger.info(f"✅ [Provisioning] Match found! Candidate {candidate_id_internal} is already an applicant (JobDiva ID: {app_cid})")
                    if not cand_data.get("jobdiva_candidate_id"):
                        cand_data["jobdiva_candidate_id"] = app_cid
                        cur.execute("UPDATE sourced_candidates SET data = %s WHERE candidate_id = %s", 
                                    (json.dumps(cand_data), candidate_id_internal))
                        conn.commit()
                    return app_cid
            
            logger.info(f"❓ [Provisioning] Candidate {candidate_id_internal} not found in JobDiva applicants list.")

        # 3. Provisioning: call CreateJobApplicationWithResume directly.
        # NOTE: This endpoint ALWAYS creates a new candidate from the textfile —
        # passing ?candidateId is ignored. So we ensure the name is parseable by
        # ALWAYS putting "FIRSTNAME LASTNAME" as the very first line of the textfile,
        # matching the format JobDiva's parser expects (like the Swati Pandey example).
        candidate_name = row.get("name") or ""
        name_parts = candidate_name.strip().split(" ", 1) if candidate_name else ["", ""]
        first_name = name_parts[0]
        last_name = name_parts[1] if len(name_parts) > 1 else ""
        safe_name = (candidate_name or "Candidate").replace(" ", "_")
        phone = row.get("phone") or ""

        # Always prepend name as first line so JobDiva parser picks it up
        actual_resume = row.get("resume_text") or ""
        resume_text = (
            f"{candidate_name.upper()}\n"
            f"Email: {email or 'N/A'} | Phone: {phone or 'N/A'}\n\n"
            + (actual_resume if actual_resume else "(Profile sourced via PAIR)")
        )

        success, new_jd_id = await jobdiva_service.create_job_application_with_resume(
            candidate_id=None,   # Always omit — endpoint ignores it anyway
            job_id=numeric_job_id or job_id_internal,
            resume_text=resume_text,
            filename=f"{safe_name}_Resume.txt",
            first_name=first_name,
            last_name=last_name,
            email=email or ""
        )
        
        if success:
            logger.info(f"🎉 [Provisioning] Success! Candidate {candidate_id_internal} → JobDiva ID: {new_jd_id}")
            if new_jd_id:
                cand_data["jobdiva_candidate_id"] = new_jd_id
                cur.execute("UPDATE sourced_candidates SET data = %s WHERE candidate_id = %s",
                            (json.dumps(cand_data), candidate_id_internal))
                conn.commit()

        return new_jd_id

    except Exception as e:
        logger.error(f"❌ [Provisioning] Error for {candidate_id_internal}: {e}", exc_info=True)
        return None
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@router.post("/engage/send-bulk-interview")
async def send_bulk_interview(request: SendBulkInterviewRequest):
    """
    Send the (potentially edited) interview payload to the PAIR bulk-interviews API.
    Saves the request and response to engage_interview_audit for traceability.
    """
    try:
        # Parse the payload
        try:
            payload_obj = json.loads(request.payload)
            jd_block = payload_obj.get("jd", {})
            job_id_from_payload = jd_block.get("job_id") or jd_block.get("jobdiva_id") or "unknown"
            print(f"DEBUG: send_bulk_interview called for job {job_id_from_payload}")
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON format in payload")

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

            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    external_url,
                    json=payload_obj,
                    headers={"Content-Type": "application/json"}
                )

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

            # ── TRIGGER PROVISIONING (JobDiva Application) ─────────────
            # ONLY trigger if the interview was successfully sent
            provision_tasks = []
            for cand_id in request.real_candidate_ids:
                provision_tasks.append(
                    _provision_candidate_to_jobdiva(cand_id, job_id_from_payload)
                )
            # Fire them off in the background
            asyncio.gather(*provision_tasks)

            for idx, candidate_id in enumerate(request.real_candidate_ids):
                interview_info = data_list[idx] if idx < len(data_list) else {}

                interview_id = str(interview_info.get("interview_id", ""))
                candidate_name = interview_info.get("candidate_name", "")
                candidate_email = interview_info.get("candidate_email", "")

                # Extract job_id from payload (prefer reference jobdiva_id for UI consistency)
                job_id_resolved = payload_obj.get("jd", {}).get("jobdiva_id") or payload_obj.get("jd", {}).get("job_id", "")

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
                    "Initiated"
                ))

                _write_candidate_engage_status(
                    candidate_id=candidate_id,
                    status_value="sent",
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
            # Launch/re-source flows use dry_run for recruiter notifications.
            if request.is_initial_launch or request.dry_run:
                asyncio.create_task(
                    _send_pair_launch_email(
                        job_id=job_id_from_payload,
                        candidate_count=len(interview_results),
                        send_job_posting=request.is_initial_launch,
                        app_base_url=request.app_base_url,
                    )
                )
            return {
                "success": True,
                "message": "Interview(s) sent successfully",
                "data": interview_results,
                "raw_response": response_data
            }
        else:
            return {
                "success": False,
                "message": response_data.get("message", f"PAIR API returned status {response.status_code}"),
                "data": [],
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
        
        # Pass logic: Must have 'passed' status AND (if scores provided) score >= 70% of total
        meets_criteria = (hf_status == HARD_FILTER_PASS_STATUS)
        
        normalized_score_display = "Passed"
        if meets_criteria and cand_score is not None and total_possible:
            # Check ratio (e.g. 35/40 = 0.875 >= 0.7) and normalize for email display.
            ratio = float(cand_score) / float(total_possible)
            if ratio < PASS_CANDIDATE_SCORE_RATIO:
                logger.info(f"⏭️ Candidate {candidate_id} passed hard filters but score ratio {ratio:.2f} is below threshold {PASS_CANDIDATE_SCORE_RATIO}")
                meets_criteria = False
            else:
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
            SELECT job_id, title, city, state, pay_rate, recruiter_emails, jobdiva_id
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

        # Deduplication: Check if we already sent the passed email
        cand_data = cand_row.get("data") or {}
        if cand_data.get("engage_passed_email_sent"):
            cur.close()
            conn.close()
            return

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
                
                value_str = a_text
                if score is not None:
                    value_str += f" (Score: {score}/{total})"
                if hf_status_item:
                    value_str += f" [HF: {hf_status_item.capitalize()}]"
                if reason:
                    value_str += f"\nReason: {reason}"
                
                screening_summary.append({
                    "field": q_text,
                    "value": value_str
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
        pair_job_title = job_row.get("title") or "the"
        report_link = f"{base_url}/jobs/{app_job_id}/report?candidateId={candidate_id}"
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
            job_title=job_row["title"],
            location=f"{job_row['city']}, {job_row['state']}" if job_row['city'] else "—",
            salary_range=job_row["pay_rate"] or "—",
            recruiter_emails=recruiter_emails,
            resume_bytes=resume_bytes,
            resume_filename=resume_filename,
            candidate_id=candidate_id,
            job_id=app_job_id
        )

        if success:
            # Mark as sent
            cand_data["engage_passed_email_sent"] = True
            cur.execute("""
                UPDATE sourced_candidates
                SET data = %s
                WHERE candidate_id = %s AND jobdiva_id = %s
            """, (json.dumps(cand_data), candidate_id, job_row["jobdiva_id"]))
            conn.commit()

            # 6. Refresh Performance Metrics for this job (e.g. Time to First Pass)
            # We fire this asynchronously so it doesn't block the webhook response
            asyncio.create_task(auto_assign_service.refresh_job_performance_metrics(job_id))

        cur.close()
        conn.close()

    except Exception as e:
        logger.error(f"❌ Failed to process Candidate Passed notification: {e}", exc_info=True)
