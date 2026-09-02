from fastapi import APIRouter, HTTPException
from services.job_rubric_db import JobRubricDB
from services.jobdiva import jobdiva_service
import psycopg2
import psycopg2.extras
import re
from core.config import DATABASE_URL
from core.db import get_db_connection
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
import asyncio
import json
from datetime import datetime, timezone

import logging
logger = logging.getLogger(__name__)

from routers.engagement import (
    ENGAGE_PASSED_STATUSES,
    _check_and_fire_candidate_passed_notification,
)
from routers.hard_filter_utils import count_pending_hard_filters
from routers.launch_report import _normalize_channel, _normalize_phase

router = APIRouter(tags=["Voice Agent Integration"])


# Questions stored verbatim often begin with "You ..." (declarative). When
# spoken aloud by the screener, prepending a soft hedge — "Let's say you ..." —
# lands as a natural lead-in rather than an interrogation. Applied only at the
# voice-agent boundary; the editor and DB keep the original text.
_LEADING_YOU_RE = re.compile(r"^You\b", re.IGNORECASE)


def _humanize_question_text(text: Optional[str]) -> Optional[str]:
    if not text:
        return text
    leading = len(text) - len(text.lstrip())
    body = text[leading:]
    match = _LEADING_YOU_RE.match(body)
    if not match:
        return text
    return f"{text[:leading]}Let's say you{body[match.end():]}"

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
        with get_db_connection() as conn:
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
            if 'question_text' in q:
                q['question_text'] = _humanize_question_text(q.get('question_text'))
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

class TranscriptionItem(BaseModel):
    # All per-row fields are optional so a malformed/partial transcription
    # cannot 422 the whole interview webhook (PAI-154).
    question: Optional[str] = None
    answer: Optional[str] = None
    timestamp: Optional[str] = None
    candidate_score: Optional[float] = None
    total_score: Optional[float] = None
    # PairBot sends "pending" on in-flight HF rows; null/empty is not an HF marker.
    hard_filter_status: Optional[str] = None
    reason: Optional[str] = None
    question_order: Optional[int] = None

class HardFilterResultItem(BaseModel):
    # All fields tolerant: PairBot may send pass_fail OR hard_filter_status (the
    # reader treats them as alternatives), and answer is null for unanswered
    # drop-offs. A strict model would 422 the whole webhook and lose the interview.
    question: Optional[str] = None
    answer: Optional[str] = None
    pass_fail: Optional[str] = None
    hard_filter_status: Optional[str] = None
    reason: Optional[str] = None
    question_order: Optional[int] = None


class VoiceAgentInterviewWebhook(BaseModel):
    interview_id: str
    status: str
    jobdiva_id: Optional[str] = None
    candidate_id: Optional[str] = None
    hard_filter_status: Optional[str] = None  # "passed" or "failed"
    total_score: Optional[float] = None
    candidate_score: Optional[float] = None
    completed_at: Optional[str] = None
    transcriptions: Optional[List[TranscriptionItem]] = None
    hard_filter_results: Optional[List[HardFilterResultItem]] = None
    phase: Optional[str] = None
    outreach_phase: Optional[str] = None
    channel: Optional[str] = None
    outreach_channel: Optional[str] = None



def _normalized_webhook_status(status: Optional[str]) -> str:
    return (status or "").strip().lower()


def effective_status_for_webhook(
    status: str,
    hard_filter_status: Optional[str] = None,
    *,
    has_pending_hf: bool = False,
) -> str:
    """Map PairBot webhook status to the PAIR engage_status we persist."""
    norm = _normalized_webhook_status(status)
    if norm != "completed":
        return norm
    hf_raw = (hard_filter_status or "").lower().strip()
    if hf_raw in ("pending", "in_progress", "awaiting") or has_pending_hf:
        return "in_progress"
    if hf_raw in ("passed", "pass", "", "not_hard_filter"):
        return "passed"
    if hf_raw in ("failed", "fail"):
        return "failed"
    return "failed"


def should_persist_engage_scores(payload_status: str, effective_status: str) -> bool:
    """Scores are written only for terminal completed outcomes (passed/failed)."""
    if _normalized_webhook_status(payload_status) != "completed":
        return False
    return effective_status.lower() in ("passed", "failed")


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
                "overall_score": payload.candidate_score,
                "candidate_score": payload.candidate_score,
                "total_score": payload.total_score,
                "hard_filter_status": payload.hard_filter_status,
                "completed_at": payload.completed_at
            },
            "transcriptions": [t.model_dump(mode="json") for t in payload.transcriptions] if payload.transcriptions else [],
            "hard_filter_results": [h.model_dump(mode="json") for h in payload.hard_filter_results] if payload.hard_filter_results else []
        }

        status_norm = _normalized_webhook_status(payload.status)
        target_job_id = payload.jobdiva_id
        target_candidate_id = payload.candidate_id
        
        # Update DB - similar to sync_interview_details
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                # 0. Lookup the real candidate_id and job_id from our audit logs using interview_id
                # This ensures we don't need LiveKit to send candidate_id or jobdiva_id
                cur.execute(
                    "SELECT candidate_id, jobdiva_id FROM engage_interview_audit WHERE interview_id = %s LIMIT 1",
                    (str(payload.interview_id),)
                )
                audit_row = cur.fetchone()
                
                if audit_row:
                    target_candidate_id = audit_row[0]
                    target_job_id = audit_row[1]
                    logger.info(f"Webhook: Matched interview {payload.interview_id} to candidate {target_candidate_id} for job {target_job_id}")
                else:
                    logger.warning(f"Webhook: No audit log found for interview {payload.interview_id}")

                # Pass logic: completed interview → pass = hard filters passed (no score threshold)
                # Pair Bot sends 'completed' when all questions are answered.
                # Curate decides the final pass/fail from that completed result.
                # For in_progress: no evaluation yet — just track the status.
                pending_hf = count_pending_hard_filters(
                    payload.hard_filter_results, payload.transcriptions
                )
                effective_status = effective_status_for_webhook(
                    payload.status,
                    payload.hard_filter_status,
                    has_pending_hf=pending_hf > 0,
                )
                if status_norm == "completed":
                    logger.info(
                        f"Webhook: interview {payload.interview_id} → {effective_status.upper()} "
                        f"(hf={payload.hard_filter_status or 'none'}, score={payload.candidate_score})"
                    )

                # 1. Update engage_interview_audit (matching interview_id)
                cur.execute(
                    """
                    UPDATE engage_interview_audit
                    SET status = %s,
                        response = %s::jsonb,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE interview_id = %s
                    """,
                    (effective_status, json.dumps(payload.model_dump(mode="json")), str(payload.interview_id))
                )

                # 2. Update sourced_candidates.data
                now_iso = datetime.now(timezone.utc).isoformat()
                candidate_blob: Dict[str, Any] = {
                    "engage_status": effective_status,
                    "engage_updated_at": now_iso,
                    "engage_interview_id": str(payload.interview_id),
                    "engage_last_response": detail_payload,
                }

                raw_phase = payload.outreach_phase or payload.phase
                raw_channel = payload.outreach_channel or payload.channel
                norm_phase = _normalize_phase(raw_phase)
                norm_channel = _normalize_channel(raw_channel)

                if norm_phase:
                    candidate_blob["phase"] = norm_phase
                    candidate_blob["outreach_phase"] = norm_phase
                if norm_channel:
                    candidate_blob["channel"] = norm_channel
                    candidate_blob["outreach_channel"] = norm_channel


                # Keep engage_completed_at fresh when webhook carries completion time.
                if payload.completed_at:
                    candidate_blob["engage_completed_at"] = payload.completed_at

                if pending_hf:
                    candidate_blob["engage_hard_filter_pending_count"] = pending_hf
                if should_persist_engage_scores(payload.status, effective_status):
                    if payload.total_score is not None:
                        candidate_blob["engage_total_score"] = payload.total_score
                    if payload.candidate_score is not None:
                        candidate_blob["engage_score"] = payload.candidate_score
                        candidate_blob["engage_candidate_score"] = payload.candidate_score
                    if payload.hard_filter_status:
                        candidate_blob["engage_hard_filter_status"] = payload.hard_filter_status

                # Employer backstop (2026-09-02): the launch payload asks every
                # candidate where they work now (services/stated_employer.py).
                # Persist the answer as a durable employer signal — the launch
                # gate, the no-contact flag and /candidates/save all read
                # stated_current_employer — and re-run the checks the launch
                # may have run blind. Must never break webhook processing.
                try:
                    from services.stated_employer import (
                        extract_stated_employer,
                        stated_employer_conflict,
                    )
                    stated = extract_stated_employer(detail_payload.get("transcriptions"))
                    if stated:
                        # Stamp with the INTERVIEW's completion time when the
                        # payload carries one (fall back to delivery time):
                        # the propagation below orders answers by this stamp,
                        # so a re-delivered webhook for an older interview
                        # must not look newer than it is.
                        stated_at = now_iso
                        parsed_completed = None
                        if payload.completed_at:
                            try:
                                parsed_completed = datetime.fromisoformat(
                                    str(payload.completed_at).replace("Z", "+00:00")
                                )
                            except Exception:  # noqa: BLE001
                                parsed_completed = None
                        if parsed_completed is not None:
                            # Normalize to UTC: the recency guard below is a
                            # lexicographic string compare, so every stamp must
                            # come from one clock in one format. A naive
                            # completed_at is taken as UTC (PairBot's stamps
                            # carry no zone).
                            if parsed_completed.tzinfo is None:
                                parsed_completed = parsed_completed.replace(tzinfo=timezone.utc)
                            stated_at = parsed_completed.astimezone(timezone.utc).isoformat()
                        candidate_blob["stated_current_employer"] = stated
                        candidate_blob["stated_employer_at"] = stated_at
                        client_name = ""
                        if target_job_id:
                            cur.execute(
                                """
                                SELECT customer_name FROM monitored_jobs
                                WHERE jobdiva_id = %s OR job_id = %s
                                LIMIT 1
                                """,
                                (str(target_job_id), str(target_job_id)),
                            )
                            client_row = cur.fetchone()
                            client_name = (
                                str(client_row[0]) if client_row and client_row[0] else ""
                            )
                        conflict = stated_employer_conflict(stated, client_name)
                        if conflict:
                            candidate_blob["stated_employer_conflict"] = conflict
                            logger.warning(
                                "post_interview_employer_conflict interview=%s candidate=%s "
                                "job=%s reason=%s stated=%r",
                                payload.interview_id, target_candidate_id, target_job_id,
                                conflict, stated[:120],
                            )
                except Exception as _stated_err:  # noqa: BLE001
                    logger.warning(
                        "stated-employer backstop failed (webhook continues): %s",
                        _stated_err,
                    )

                cur.execute(
                    """
                    UPDATE sourced_candidates
                    SET data = (
                            WITH merged AS (
                                SELECT COALESCE(data, '{}'::jsonb) || %s::jsonb AS doc
                            ),
                            attempted AS (
                                SELECT CASE
                                    WHEN COALESCE(BTRIM(COALESCE(doc->>'first_attempted_at', '')), '') = '' THEN
                                        jsonb_set(doc, '{first_attempted_at}', to_jsonb(%s::text), true)
                                    ELSE doc
                                END AS doc
                                FROM merged
                            )
                            SELECT CASE
                                WHEN %s IS NOT NULL
                                  AND COALESCE(BTRIM(COALESCE(doc->>'first_completed_at', '')), '') = '' THEN
                                    jsonb_set(doc, '{first_completed_at}', to_jsonb(%s::text), true)
                                ELSE doc
                            END
                            FROM attempted
                        ),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE candidate_id = %s
                      AND (jobdiva_id = %s OR jobdiva_id = %s)
                    """,
                    (
                        json.dumps(candidate_blob),
                        now_iso,
                        payload.completed_at,
                        payload.completed_at,
                        target_candidate_id,
                        target_job_id,
                        target_job_id,
                    ),
                )
                
                # Fallback if job_id mapping missing
                if cur.rowcount == 0:
                    cur.execute(
                        """
                        UPDATE sourced_candidates
                        SET data = (
                                WITH merged AS (
                                    SELECT COALESCE(data, '{}'::jsonb) || %s::jsonb AS doc
                                ),
                                attempted AS (
                                    SELECT CASE
                                        WHEN COALESCE(BTRIM(COALESCE(doc->>'first_attempted_at', '')), '') = '' THEN
                                            jsonb_set(doc, '{first_attempted_at}', to_jsonb(%s::text), true)
                                        ELSE doc
                                    END AS doc
                                    FROM merged
                                )
                                SELECT CASE
                                    WHEN %s IS NOT NULL
                                      AND COALESCE(BTRIM(COALESCE(doc->>'first_completed_at', '')), '') = '' THEN
                                        jsonb_set(doc, '{first_completed_at}', to_jsonb(%s::text), true)
                                    ELSE doc
                                END
                                FROM attempted
                            ),
                            updated_at = CURRENT_TIMESTAMP
                        WHERE candidate_id = %s
                        """,
                        (
                            json.dumps(candidate_blob),
                            now_iso,
                            payload.completed_at,
                            payload.completed_at,
                            target_candidate_id,
                        ),
                    )

            conn.commit()

            # Person-wide stated-employer propagation (phase 3): the answer is
            # a fact about the PERSON, and for JobDiva rows candidate_id IS
            # the person id — stamp it on the candidate's rows for every other
            # job so their launch gates and no-contact flags see it too. The
            # conflict string stays on this job's row only (it names this
            # job's client).
            #
            # Runs in its OWN transaction AFTER the interview writes are
            # committed: this multi-row update can deadlock or hit the pool's
            # statement timeout against a concurrent same-person webhook or
            # /candidates/save, and a swallowed error BEFORE the commit would
            # poison the shared transaction — turning the commit into a
            # silent ROLLBACK of the engage status itself.
            #
            # Guards: numeric JobDiva person ids only (candidate_id is
            # person-scoped only for JobDiva rows — an identical id string
            # from another source would stamp a different person); value
            # inequality so repeat webhooks don't rewrite rows; and recency
            # (stored stated_employer_at must be OLDER than this answer's) so
            # a re-delivered webhook for an older interview can't overwrite a
            # newer stated answer.
            _stated_answer = candidate_blob.get("stated_current_employer")
            _stated_at = str(candidate_blob.get("stated_employer_at") or "")
            if _stated_answer and target_candidate_id and str(target_candidate_id).isdigit():
                try:
                    with conn.cursor() as prop_cur:
                        prop_cur.execute(
                            """
                            UPDATE sourced_candidates
                            SET data = COALESCE(data, '{}'::jsonb) || %s::jsonb,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE candidate_id = %s
                              AND COALESCE(data->>'stated_current_employer', '')
                                  IS DISTINCT FROM %s
                              AND COALESCE(data->>'stated_employer_at', '') < %s
                            """,
                            (
                                json.dumps({
                                    "stated_current_employer": _stated_answer,
                                    "stated_employer_at": _stated_at,
                                }),
                                str(target_candidate_id),
                                _stated_answer,
                                _stated_at,
                            ),
                        )
                        _prop_rows = prop_cur.rowcount
                    conn.commit()
                    if _prop_rows:
                        logger.info(
                            "stated_employer_propagated candidate=%s rows=%d",
                            target_candidate_id, _prop_rows,
                        )
                except Exception as _prop_err:  # noqa: BLE001
                    try:
                        conn.rollback()
                    except Exception:  # noqa: BLE001
                        pass
                    logger.warning(
                        "stated-employer propagation failed (interview writes "
                        "already committed): %s",
                        _prop_err,
                    )

        # Check for pass condition and fire email if needed.
        check_status = effective_status.lower()
        if (
            check_status in [s.lower() for s in ENGAGE_PASSED_STATUSES]
            and payload.total_score is not None
        ):
            if target_job_id and target_candidate_id:
                int_id = int(payload.interview_id) if str(payload.interview_id).isdigit() else payload.interview_id
                asyncio.create_task(
                    _check_and_fire_candidate_passed_notification(
                        interview_id=int_id,
                        detail_payload=detail_payload,
                        job_id=target_job_id,
                        candidate_id=target_candidate_id,
                    )
                )
            
        return {"success": True, "message": "Interview results processed successfully"}

    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Error processing voice agent webhook: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
