from fastapi import APIRouter, HTTPException
from services.job_rubric_db import JobRubricDB
from services.jobdiva import jobdiva_service
import psycopg2
import psycopg2.extras
from core.config import DATABASE_URL
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
import asyncio
import json
from datetime import datetime, timezone

router = APIRouter(tags=["Voice Agent Integration"])

@router.get("/jobs/{job_id}")
async def get_voice_job_context(job_id: str):
    """
    Returns a unified JSON object for the Voice Agent team.
    Includes: Metadata, AI JD, Recruiter Notes, and the Full Rubric.
    """
    try:
        # 1. Resolve ID (Ref code vs Numeric)
        ref_id = job_id
        if "-" not in job_id:
            job_context = await jobdiva_service.get_job_by_id(job_id)
            if job_context:
                ref_id = job_context.get('jobdiva_id', job_id)

        # 2. Fetch Job Details from monitored_jobs
        with psycopg2.connect(DATABASE_URL, connect_timeout=5) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT job_id, jobdiva_id, title, customer_name, city, state, location_type, 
                           jobdiva_description, ai_description, recruiter_notes
                    FROM monitored_jobs 
                    WHERE jobdiva_id = %s OR job_id = %s
                    LIMIT 1
                """, (ref_id, job_id))
                job_base = cur.fetchone()

        if not job_base:
            raise HTTPException(status_code=404, detail="Job details not found in monitored_jobs")

        # 3. Fetch Full Rubric (Smart Retrieval)
        rubric_db = JobRubricDB()
        # Try fetching by ref_id first (e.g. 26-06182)
        rubric = rubric_db.get_full_rubric(job_base['jobdiva_id'])
        
        # If not found or empty, try fetching by numeric job_id (e.g. 31920112)
        if not rubric or (not rubric.get('skills') and not rubric.get('titles')):
            alt_rubric = rubric_db.get_full_rubric(job_base['job_id'])
            if alt_rubric and (alt_rubric.get('skills') or alt_rubric.get('titles')):
                rubric = alt_rubric

        # 4. Combine into Unified Response
        # Pop IDs from context to avoid duplication in JSON
        numeric_id = job_base.pop('job_id')
        ref_id_val = job_base.pop('jobdiva_id')
        
        # Pop fields from rubric to avoid duplication or unwanted fields
        pre_screen_questions = rubric.pop('screen_questions', [])
        for q in pre_screen_questions:
            q.pop('order_index', None)
        rubric.pop('bot_introduction', None)
        
        return {
            "job_id": numeric_id,           # Numeric ID at top level
            "jobdiva_id": ref_id_val,       # Ref Code at top level
            "context": job_base,            # No more duplicate IDs here
            "rubric": rubric,
            "pre_screen_questions": pre_screen_questions
        }

    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Error in unified voice API: {e}")
        raise HTTPException(status_code=500, detail=str(e))

class VoiceAgentInterviewWebhook(BaseModel):
    interview_id: str
    jobdiva_id: str
    candidate_id: str
    status: str
    hard_filter_status: Optional[str] = None
    total_score: Optional[float] = None
    candidate_score: Optional[float] = None
    completed_at: Optional[str] = None
    transcriptions: Optional[List[Dict[str, Any]]] = None

@router.post("/interviews/webhook")
async def receive_interview_results(payload: VoiceAgentInterviewWebhook):
    """
    Webhook for the Voice Agent team to send interview results (score, status, completed_at, etc).
    """
    try:
        # Construct the detail payload expected by the downstream logic
        detail_payload = {
            "interview": {
                "status": payload.status,
                "overall_score": payload.total_score,  # Map total_score internally
                "candidate_score": payload.candidate_score,
                "completed_at": payload.completed_at
            },
            "transcriptions": payload.transcriptions or []
        }

        target_job_id = payload.jobdiva_id
        
        # Update DB - similar to sync_interview_details
        with psycopg2.connect(DATABASE_URL, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                # 1. Update engage_interview_audit (latest matching interview_id)
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
                    """,
                    (str(payload.interview_id), json.dumps(detail_payload), payload.status)
                )
                
                # 2. Update sourced_candidates.data
                now_iso = datetime.now(timezone.utc).isoformat()
                candidate_blob: Dict[str, Any] = {
                    "engage_status": payload.status,
                    "engage_updated_at": now_iso,
                    "engage_interview_id": str(payload.interview_id),
                    "engage_last_response": detail_payload,
                }
                if payload.total_score is not None:
                    candidate_blob["engage_score"] = payload.total_score
                if payload.completed_at:
                    candidate_blob["engage_completed_at"] = payload.completed_at

                cur.execute(
                    """
                    UPDATE sourced_candidates
                    SET data = COALESCE(data, '{}'::jsonb) || %s::jsonb,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE candidate_id = %s
                      AND (jobdiva_id = %s OR jobdiva_id = %s)
                    """,
                    (json.dumps(candidate_blob), payload.candidate_id, target_job_id, target_job_id),
                )
                
                # Fallback if job_id mapping missing
                if cur.rowcount == 0:
                    cur.execute(
                        """
                        UPDATE sourced_candidates
                        SET data = COALESCE(data, '{}'::jsonb) || %s::jsonb,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE candidate_id = %s
                        """,
                        (json.dumps(candidate_blob), payload.candidate_id),
                    )
                
            conn.commit()

        # Check for pass condition and fire email if needed
        # Prioritize hard_filter_status if provided, otherwise fallback to status
        check_status = (payload.hard_filter_status or payload.status).lower()
        
        # Safe import of ENGAGE_PASSED_STATUSES
        try:
            from routers.engagement import ENGAGE_PASSED_STATUSES
        except ImportError:
            ENGAGE_PASSED_STATUSES = ["passed", "completed", "hired"]
            
        if check_status in [s.lower() for s in ENGAGE_PASSED_STATUSES] and payload.total_score is not None:
            try:
                from routers.engagement import _check_and_fire_candidate_passed_notification
                # interview_id should be parsed to int if it's digit
                int_id = int(payload.interview_id) if str(payload.interview_id).isdigit() else payload.interview_id
                asyncio.create_task(
                    _check_and_fire_candidate_passed_notification(
                        interview_id=int_id,
                        detail_payload=detail_payload,
                        job_id=target_job_id,
                        candidate_id=payload.candidate_id,
                    )
                )
            except (ImportError, AttributeError):
                import logging
                logging.getLogger(__name__).warning("Candidate passed but _check_and_fire_candidate_passed_notification is not available in engagement.py. Skipping email.")
            
        return {"success": True, "message": "Interview results processed successfully"}

    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Error processing voice agent webhook: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
