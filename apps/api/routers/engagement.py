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

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Engagement"])

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
EXTERNAL_INTERVIEW_API_URL = os.getenv("EXTERNAL_INTERVIEW_API_URL", "https://pairbotqa.hoonr.ai")

# ---------------------------------------------------------------------------
# Auto-Migration: Ensure audit table exists
# ---------------------------------------------------------------------------
def _ensure_audit_table():
    """Create engage_interview_audit table if it doesn't exist, and patch any missing columns."""
    try:
        # connect_timeout=5 → slow/unreachable DB must fail fast. Previously an
        # unbounded wait here (called at module import) could hang FastAPI
        # startup past systemd's TimeoutStartSec, triggering a restart loop
        # that returned 404 for every route until the DB recovered.
        conn = get_db_connection()
        cur = conn.cursor()
        # Create table (no-op if already exists)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS engage_interview_audit (
                id SERIAL PRIMARY KEY,
                candidate_id VARCHAR(255) NOT NULL,
                job_id VARCHAR(255),
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
        # Patch columns that may be missing if table was created before schema updates
        missing_columns = [
            ("job_id",         "VARCHAR(255)"),
            ("interview_id",   "VARCHAR(255)"),
            ("candidate_name", "VARCHAR(255)"),
            ("candidate_email","VARCHAR(255)"),
            ("payload",        "JSONB"),
            ("response",       "JSONB"),
            ("status",         "VARCHAR(50) DEFAULT 'sent'"),
            ("updated_at",     "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
        ]
        for col_name, col_def in missing_columns:
            cur.execute(f"""
                ALTER TABLE engage_interview_audit
                ADD COLUMN IF NOT EXISTS {col_name} {col_def};
            """)
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
            ON engage_interview_audit(job_id, candidate_id, id DESC);
        """)
        conn.commit()
        cur.close()
        conn.close()
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

class SyncInterviewDetailsRequest(BaseModel):
    interview_ids: List[Any]

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
        resumes = []
        candidate_phone = ""
        for cid in request.candidate_ids:
            cur.execute("""
                SELECT candidate_id, name, email, phone, resume_text, headline, location, data
                FROM sourced_candidates
                WHERE candidate_id = %s
                ORDER BY updated_at DESC
                LIMIT 1
            """, (cid,))
            row = cur.fetchone()

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
        cur.execute("""
            SELECT * FROM monitored_jobs
            WHERE job_id = %s OR jobdiva_id = %s
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
            pre_screen_questions = [
                {
                    "question_text": r["question_text"],
                    "pass_criteria": r["pass_criteria"],
                    "is_default": r["is_default"],
                    "category": r["category"],
                }
                for r in rows
            ]

        cur.close()
        conn.close()

        # Build JD section — must match pairbotqa expected structure:
        # { title, jobdiva_description, pre_screen_questions, job_id, jobdiva_id }
        if job_row:
            jd = {
                "job_id": job_row.get("job_id", request.job_id),
                "jobdiva_id": job_row.get("jobdiva_id", ""),
                "title": job_row.get("title", ""),
                "jobdiva_description": (
                    job_row.get("jobdiva_description") or
                    job_row.get("ai_description") or ""
                ),
                "pre_screen_questions": pre_screen_questions,
            }
        else:
            jd = {
                "job_id": request.job_id,
                "jobdiva_id": "",
                "title": "",
                "jobdiva_description": "",
                "pre_screen_questions": [],
            }

        # Assemble final payload matching pairbotqa /api/bulk-interviews schema
        payload = {
            "resumes": resumes,
            "jd": jd,
            "interview_duration": "20-25"
        }

        payload_str = json.dumps(payload, indent=2)

        return {
            "success": True,
            "payload": payload_str,
            "candidate_count": len(resumes)
        }

    except Exception as e:
        logger.error(f"❌ generate-payload failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# 2. POST /engage/send-bulk-interview
# ---------------------------------------------------------------------------
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
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON format in payload")

        # Send to external PAIR API
        external_url = f"{EXTERNAL_INTERVIEW_API_URL}/api/bulk-interviews"
        logger.info(f"📤 Sending bulk interview to {external_url}")

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                external_url,
                json=payload_obj,
                headers={"Content-Type": "application/json"}
            )

        response_data = response.json()
        is_success = response.status_code == 200

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
                    str(payload_obj.get("jd", {}).get("jobdiva_id", "") or ""),
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
                        str(payload_obj.get("jd", {}).get("jobdiva_id", "") or ""),
                    ),
                )

        interview_results = []

        if is_success and isinstance(response_data, dict):
            # Extract interview data from response
            data_list = response_data.get("data", [])
            if not isinstance(data_list, list):
                data_list = [response_data] if response_data else []

            for idx, candidate_id in enumerate(request.real_candidate_ids):
                interview_info = data_list[idx] if idx < len(data_list) else {}

                interview_id = str(interview_info.get("interview_id", ""))
                candidate_name = interview_info.get("candidate_name", "")
                candidate_email = interview_info.get("candidate_email", "")

                # Extract job_id from payload
                job_id = payload_obj.get("jd", {}).get("job_id", "")

                cur.execute("""
                    INSERT INTO engage_interview_audit
                        (candidate_id, job_id, interview_id, candidate_name, candidate_email, payload, response, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    candidate_id,
                    job_id,
                    interview_id,
                    candidate_name,
                    candidate_email,
                    json.dumps(payload_obj),
                    json.dumps(interview_info),
                    "sent"
                ))

                _write_candidate_engage_status(
                    candidate_id=candidate_id,
                    status_value="sent",
                    job_id_value=job_id,
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
                job_id = payload_obj.get("jd", {}).get("job_id", "")
                cur.execute("""
                    INSERT INTO engage_interview_audit
                        (candidate_id, job_id, payload, response, status)
                    VALUES (%s, %s, %s, %s, %s)
                """, (
                    candidate_id,
                    job_id,
                    json.dumps(payload_obj),
                    json.dumps(response_data),
                    "failed"
                ))

                _write_candidate_engage_status(
                    candidate_id=candidate_id,
                    status_value="failed",
                    job_id_value=job_id,
                    interview_id_value="",
                    response_fragment=response_data if isinstance(response_data, dict) else {"response": response_data},
                )

        conn.commit()
        cur.close()
        conn.close()

        if is_success:
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
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# 3. GET /latest-interview/by-id/{candidate_id}
# ---------------------------------------------------------------------------
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
            SELECT interview_id, candidate_name, candidate_email, job_id, status, created_at
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
# 4. POST /engage/interviews/details-sync
# ---------------------------------------------------------------------------
@router.post("/engage/interviews/details-sync")
@router.post("/interviews/details-sync")
async def sync_interview_details(request: SyncInterviewDetailsRequest):
    """
    Fetch interview detail(s) from PAIR for provided interview IDs, then:
      1) store the full detail payload in engage_interview_audit.response
      2) update engage_interview_audit.status from detail.interview.status
      3) sync sourced_candidates.data engage fields for rank-list consumption

    Note: PAIR detail endpoint currently supports single interview_id per call,
    so this endpoint fans out one request per id and returns aggregated results.
    """
    # Normalize incoming IDs: keep only positive integers, de-duplicated.
    normalized_ids: List[int] = []
    seen = set()
    for raw_id in request.interview_ids or []:
        try:
            parsed = int(str(raw_id).strip())
            if parsed <= 0:
                continue
            if parsed in seen:
                continue
            seen.add(parsed)
            normalized_ids.append(parsed)
        except Exception:
            continue

    if not normalized_ids:
        return {"success": True, "count": 0, "results": []}

    conn = _get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    results: List[Dict[str, Any]] = []

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            for interview_id in normalized_ids:
                try:
                    pair_url = f"{EXTERNAL_INTERVIEW_API_URL}/api/interviews/{interview_id}/detail"
                    pair_res = await client.get(pair_url)

                    if pair_res.status_code != 200:
                        results.append({
                            "interview_id": interview_id,
                            "success": False,
                            "error": f"PAIR returned {pair_res.status_code}",
                        })
                        continue

                    detail_payload = pair_res.json()
                    interview_block = detail_payload.get("interview", {}) if isinstance(detail_payload, dict) else {}
                    status_value = str(interview_block.get("status") or "pending")
                    overall_score = interview_block.get("overall_score")
                    completed_at = interview_block.get("completed_at")
                    now_iso = datetime.now(timezone.utc).isoformat()

                    # Update only the latest audit row for this interview_id.
                    cur.execute(
                        """
                        WITH latest AS (
                            SELECT id
                            FROM engage_interview_audit
                            WHERE interview_id = %s
                            ORDER BY id DESC
                            LIMIT 1
                        )
                        UPDATE engage_interview_audit eia
                        SET response = %s::jsonb,
                            status = %s,
                            updated_at = CURRENT_TIMESTAMP
                        FROM latest
                        WHERE eia.id = latest.id
                        RETURNING eia.candidate_id, eia.job_id
                        """,
                        (str(interview_id), json.dumps(detail_payload), status_value),
                    )
                    audit_row = cur.fetchone()

                    candidate_id = str((audit_row or {}).get("candidate_id") or "")
                    job_id = str((audit_row or {}).get("job_id") or "")

                    candidate_blob: Dict[str, Any] = {
                        "engage_status": status_value,
                        "engage_updated_at": now_iso,
                        "engage_interview_id": str(interview_id),
                        "engage_last_response": detail_payload,
                    }
                    if overall_score is not None:
                        candidate_blob["engage_score"] = overall_score
                    if completed_at:
                        candidate_blob["engage_completed_at"] = completed_at

                    candidate_rows_updated = 0
                    if candidate_id:
                        if job_id:
                            cur.execute(
                                """
                                UPDATE sourced_candidates
                                SET data = COALESCE(data, '{}'::jsonb) || %s::jsonb,
                                    updated_at = CURRENT_TIMESTAMP
                                WHERE candidate_id = %s
                                  AND (jobdiva_id = %s OR jobdiva_id = %s)
                                """,
                                (json.dumps(candidate_blob), candidate_id, job_id, job_id),
                            )
                            candidate_rows_updated = cur.rowcount or 0

                        # Fallback when job_id does not map directly to sourced_candidates.jobdiva_id.
                        if candidate_rows_updated == 0:
                            cur.execute(
                                """
                                UPDATE sourced_candidates
                                SET data = COALESCE(data, '{}'::jsonb) || %s::jsonb,
                                    updated_at = CURRENT_TIMESTAMP
                                WHERE candidate_id = %s
                                """,
                                (json.dumps(candidate_blob), candidate_id),
                            )
                            candidate_rows_updated = cur.rowcount or 0

                    conn.commit()

                    results.append({
                        "interview_id": interview_id,
                        "success": True,
                        "status": status_value,
                        "overall_score": overall_score,
                        "completed_at": completed_at,
                        "candidate_id": candidate_id or None,
                        "candidate_rows_updated": candidate_rows_updated,
                        "detail": detail_payload,
                    })
                except Exception as item_err:
                    conn.rollback()
                    logger.warning(
                        f"⚠️ interview detail sync failed for {interview_id}: {item_err}",
                        exc_info=True,
                    )
                    results.append({
                        "interview_id": interview_id,
                        "success": False,
                        "error": str(item_err),
                    })
    finally:
        cur.close()
        conn.close()

    return {
        "success": True,
        "count": len(results),
        "results": results,
    }


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
