from fastapi import APIRouter, HTTPException
from services.job_rubric_db import JobRubricDB
from services.jobdiva import jobdiva_service
import psycopg2
import psycopg2.extras
from core.config import DATABASE_URL
import json

router = APIRouter(tags=["Boolean Agent Integration"])

@router.get("/jobs/{job_id}/context")
async def get_boolean_agent_context(job_id: str):
    """
    Returns a unified JSON object for the Boolean String Generator agent team.
    Strictly follows Page 5 (Sourcing) UI state requirements.
    """
    try:
        # 1. Resolve ID (Ref code vs Numeric)
        ref_id = job_id
        numeric_id = job_id
        if "-" not in job_id:
            job_context = await jobdiva_service.get_job_by_id(job_id)
            if job_context:
                ref_id = job_context.get('jobdiva_id', job_id)
        else:
             # If it's a ref code, try to find the numeric ID
             with psycopg2.connect(DATABASE_URL) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT job_id FROM monitored_jobs WHERE jobdiva_id = %s", (job_id,))
                    row = cur.fetchone()
                    if row: numeric_id = str(row[0])

        # 2. Fetch Job Details from monitored_jobs
        with psycopg2.connect(DATABASE_URL, connect_timeout=5) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT job_id, jobdiva_id, title, customer_name, city, state, location_type, 
                           jobdiva_description, ai_description, recruiter_notes,
                           employment_type, work_authorization, sourcing_filters
                    FROM monitored_jobs 
                    WHERE jobdiva_id = %s OR job_id = %s
                    LIMIT 1
                """, (ref_id, numeric_id))
                job_base = cur.fetchone()

        if not job_base:
            raise HTTPException(status_code=404, detail="Job details not found in monitored_jobs")

        # 3. Handle Sourcing Filters (UI Overrides)
        sourcing_filters = job_base.get('sourcing_filters')
        if isinstance(sourcing_filters, str):
            try:
                sourcing_filters = json.loads(sourcing_filters)
            except:
                sourcing_filters = None

        if sourcing_filters:
            # PULL DATA DIRECTLY FROM PAGE 5 STATE
            titles = [
                {
                    "value": t.get('value'),
                    "match_type": t.get('matchType', 'must').lower(),
                    "years": t.get('years', 0),
                    "recent": t.get('recent', False),
                    "selected_similar": t.get('selectedSimilarTitles', [])
                }
                for t in sourcing_filters.get('titles', [])
            ]
            hard_skills = [
                {
                    "value": s.get('value'),
                    "match_type": s.get('matchType', 'must').lower(),
                    "years": s.get('years', 0),
                    "recent": s.get('recent', False),
                    "selected_similar": s.get('selectedSimilarSkills', [])
                }
                for s in sourcing_filters.get('skills', [])
            ]
            locations = [
                {
                    "value": l.get('value'),
                    "radius": l.get('radius', "within 25 mi")
                }
                for l in sourcing_filters.get('locations', [])
            ]
            keywords = sourcing_filters.get('keywords', [])
            companies = sourcing_filters.get('companies', [])
            
            ui_sources = sourcing_filters.get('sources', {})
            sources = [name for name, active in ui_sources.items() if active] if isinstance(ui_sources, dict) else []
        else:
            # FALLBACK TO RUBRIC IF NO PAGE 5 STATE YET
            rubric_db = JobRubricDB()
            rubric = rubric_db.get_full_rubric(job_base['jobdiva_id']) or {}
            
            hard_skills = [
                {
                    "value": s['value'],
                    "match_type": s.get('matchType', 'must').lower(),
                    "years": s.get('minYears', 0),
                    "recent": s.get('recent', False),
                    "selected_similar": s.get('similar_skills', []) # Fallback to all similar
                }
                for s in rubric.get('skills', [])
            ]
            titles = [
                {
                    "value": t['value'],
                    "match_type": t.get('matchType', 'must').lower(),
                    "years": t.get('minYears', 0),
                    "recent": t.get('recent', False),
                    "selected_similar": t.get('similar_titles', []) # Fallback to all similar
                }
                for t in rubric.get('titles', [])
            ]
            locations = []
            if job_base.get('city') and job_base.get('state'):
                locations.append({"value": f"{job_base['city']}, {job_base['state']}", "radius": "within 25 mi"})
            
            sources = ["JobDiva", "JobDiva Hotlist", "LinkedIn", "Dice"]
            keywords = []
            companies = []

        # 5. Assemble Final Context
        return {
            "job_id": job_base['job_id'],
            "jobdiva_id": job_base['jobdiva_id'],
            "criteria": {
                "titles": titles,
                "hard_skills": hard_skills,
                "locations": locations,
                "keywords": keywords,
                "companies": companies
            },
            "sources": sources,
            "context": {
                "job_title": job_base['title'],
                "customer": job_base['customer_name'],
                "original_description": job_base['jobdiva_description'],
                "recruiter_notes": job_base['recruiter_notes'],
                "ai_description": job_base['ai_description']
            }
        }

    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Error in boolean agent context API: {e}")
        raise HTTPException(status_code=500, detail=str(e))
