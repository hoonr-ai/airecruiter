from fastapi import APIRouter, HTTPException
from services.job_rubric_db import JobRubricDB
from services.jobdiva import jobdiva_service
import psycopg2
import psycopg2.extras
from core.config import DATABASE_URL

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
