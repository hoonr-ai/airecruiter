import logging
import json
import psycopg2
from typing import Dict, Any, List
from core.config import DATABASE_URL
from services.unified_candidate_search import SearchCriteria, unified_search_service

logger = logging.getLogger(__name__)

class AutoAssignService:
    def __init__(self, db_url: str = DATABASE_URL):
        self.db_url = db_url

    def _get_db_connection(self):
        return psycopg2.connect(self.db_url)

    async def synchronize_job_applicants(self, job_id: str):
        """
        Fetches all JobDiva applicants for a job, scores them,
        and upserts them into sourced_candidates.
        """
        try:
            logger.info(f"🤖 [AutoAssignService] Starting sync for job {job_id}")

            # 1. Load job rubric / filters from DB
            resume_match_filters = []
            sourcing_filters = {}
            jobdiva_numeric_id = None
            
            try:
                with self._get_db_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT resume_match_filters, sourcing_filters, jobdiva_id FROM monitored_jobs "
                            "WHERE job_id = %s OR jobdiva_id = %s LIMIT 1",
                            (job_id, job_id)
                        )
                        row = cur.fetchone()
                        if row:
                            resume_match_filters = row[0] if isinstance(row[0], list) else (json.loads(row[0]) if row[0] else [])
                            sourcing_filters = row[1] if isinstance(row[1], dict) else (json.loads(row[1]) if row[1] else {})
                            jobdiva_numeric_id = row[2]
            except Exception as e:
                logger.warning(f"[AutoAssignService] Could not load filters for job {job_id}: {e}")

            search_job_id = jobdiva_numeric_id if jobdiva_numeric_id else job_id
            logger.info(f"🤖 [AutoAssignService] Targeting JobDiva ID {search_job_id}")

            # 2. Build SearchCriteria
            title_criteria = []
            if sourcing_filters.get("titles"):
                title_criteria = [
                    {"value": t.get("value", ""), "match_type": t.get("matchType", "must"), "years": t.get("years", 0),
                     "recent": t.get("recent", False), "similar_terms": t.get("selectedSimilarTitles") or []}
                    for t in (sourcing_filters.get("titles") or [])
                ]
            
            skill_criteria = []
            if sourcing_filters.get("skills"):
                skill_criteria = [
                    {"value": s.get("value", ""), "match_type": s.get("matchType", "must"), "years": s.get("years", 0),
                     "recent": s.get("recent", False), "similar_terms": s.get("selectedSimilarSkills") or []}
                    for s in (sourcing_filters.get("skills") or [])
                ]
            
            primary_location = ""
            locs = sourcing_filters.get("locations") or []
            if locs:
                primary_location = locs[0].get("value", "")

            criteria = SearchCriteria(
                job_id=search_job_id,
                title_criteria=title_criteria,
                skill_criteria=skill_criteria,
                keywords=sourcing_filters.get("keywords") or [],
                companies=sourcing_filters.get("companies") or [],
                resume_match_filters=resume_match_filters,
                location=primary_location,
                page_size=500,
                sources=["JobDiva"],
                bypass_screening=True,
            )

            # 3. Process candidates
            total_assigned = 0
            with self._get_db_connection() as conn:
                with conn.cursor() as cur:
                    async for event in unified_search_service.search_candidates(criteria):
                        if event.get("type") != "candidate":
                            continue
                        cand = event["data"]
                        try:
                            candidate_id = str(cand.get("candidate_id") or cand.get("id") or "")
                            
                            candidate_data_json = json.dumps({
                                "skills": cand.get("skills") or [],
                                "experience_years": cand.get("experience_years") or 0,
                                "education": cand.get("enhanced_info", {}).get("candidate_education") or [],
                                "certifications": cand.get("enhanced_info", {}).get("candidate_certification") or [],
                                "company_experience": cand.get("enhanced_info", {}).get("company_experience") or [],
                                "urls": cand.get("enhanced_info", {}).get("urls") or {},
                                "is_selected": True,
                                "match_score": cand.get("match_score") or 0,
                                "missing_skills": cand.get("missing_skills") or [],
                                "matched_skills": cand.get("matched_skills") or [],
                                "explainability": cand.get("explainability") or "",
                                "match_score_details": cand.get("match_score_details") or {},
                                "enhanced_info": cand.get("enhanced_info"),
                                "auto_assigned": True,
                            })
                            
                            cur.execute("""
                                INSERT INTO sourced_candidates (
                                    jobdiva_id, candidate_id, source, name, email, phone,
                                    headline, location, resume_text, data, status,
                                    resume_match_percentage, updated_at
                                ) VALUES (
                                    %s, %s, %s, %s, %s, %s,
                                    %s, %s, %s, %s, %s,
                                    %s, CURRENT_TIMESTAMP
                                )
                                ON CONFLICT (jobdiva_id, candidate_id, source) DO UPDATE SET
                                    name       = EXCLUDED.name,
                                    email      = EXCLUDED.email,
                                    phone      = EXCLUDED.phone,
                                    headline   = EXCLUDED.headline,
                                    location   = EXCLUDED.location,
                                    resume_text= EXCLUDED.resume_text,
                                    data       = EXCLUDED.data,
                                    status     = EXCLUDED.status,
                                    resume_match_percentage= EXCLUDED.resume_match_percentage,
                                    updated_at = CURRENT_TIMESTAMP
                            """, (
                                job_id,
                                candidate_id,
                                cand.get("source", "JobDiva-Applicants"),
                                cand.get("name") or "",
                                cand.get("email"),
                                cand.get("phone"),
                                cand.get("headline") or cand.get("title"),
                                cand.get("location"),
                                cand.get("resume_text"),
                                candidate_data_json,
                                "sourced",
                                cand.get("match_score") or 0,
                            ))
                            total_assigned += 1
                            conn.commit()
                        except Exception as row_err:
                            logger.warning(f"[AutoAssignService] Failed upsert for {cand.get('candidate_id')}: {row_err}")

            logger.info(f"✅ [AutoAssignService] Completed. Total assigned: {total_assigned} for job {job_id}")
            return total_assigned

        except Exception as e:
            logger.error(f"❌ [AutoAssignService] Sync failed for job {job_id}: {e}", exc_info=True)
            return 0

auto_assign_service = AutoAssignService()
