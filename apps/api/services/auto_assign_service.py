import logging
import json
import psycopg2
from datetime import datetime
from typing import Dict, Any, List
from core.config import DATABASE_URL, JOBDIVA_PAIR_QUALIFICATION_NAME, JOBDIVA_PASS_QUALIFICATION_VALUE
from services.unified_candidate_search import SearchCriteria, unified_search_service
from services.candidate_profiles_db import candidate_profiles_db

logger = logging.getLogger(__name__)

class AutoAssignService:
    def __init__(self, db_url: str = DATABASE_URL):
        self.db_url = db_url

    def _get_db_connection(self):
        return psycopg2.connect(self.db_url, connect_timeout=5)

    async def _count_external_curate_submittals(self, numeric_job_id) -> int:
        """
        Count external curate submittals for a job.

        Criteria (all three must be satisfied for a submittal to count):
        1. The submittal recipient name matches the job's contact person (from JobDiva BI JobDetail).
        2. The candidate has a PAIR Candidates qualification with value 'Pass'.
        3. The qualification date is within 60 days of the submittal date.

        Returns the count of valid external submittals.
        """
        from services.jobdiva import jobdiva_service, get_field

        if not numeric_job_id:
            return 0

        # Fetch contact name from JobDiva BI JobDetail
        contact_name = ""
        try:
            token = await jobdiva_service.authenticate()
            if token:
                import httpx
                detail_url = f"{jobdiva_service.api_url}/apiv2/bi/JobDetail"
                headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.get(detail_url, params={"jobIds": [int(numeric_job_id)]}, headers=headers)
                    if resp.status_code == 200:
                        det_data = resp.json()
                        det_list = det_data.get("data", []) if isinstance(det_data, dict) else det_data
                        if det_list:
                            d = det_list[0]
                            first = (d.get("CONTACTFIRSTNAME") or d.get("CONTACT_FIRST_NAME") or "").strip()
                            last = (d.get("CONTACTLASTNAME") or d.get("CONTACT_LAST_NAME") or "").strip()
                            contact_name = f"{first} {last}".strip().lower()
                            logger.info(f"📋 Job {numeric_job_id} contact: '{contact_name}'")
        except Exception as e:
            logger.warning(f"[ExternalSubs] Could not fetch contact for job {numeric_job_id}: {e}")

        if not contact_name:
            logger.debug(f"⏭️ _count_external_curate_submittals: No contact name for job {numeric_job_id} — skipping")
            return 0

        submittals = await jobdiva_service.get_job_submittals(numeric_job_id)
        if not submittals:
            logger.debug(f"📋 No submittals found for job {numeric_job_id}")
            return 0

        logger.info(f"📋 {len(submittals)} submittal(s) found for job {numeric_job_id}, contact='{contact_name}'")

        count = 0
        for sub in submittals:
            # BI fields are uppercase; fall back to camelCase variants
            recipient = (
                get_field(sub, ["RECIPIENTNAME", "RECIPIENT", "recipientName"]) or ""
            ).lower().strip()
            sub_date_raw = get_field(sub, ["SUBMITDATE", "DATE", "submitDate"])
            candidate_id = get_field(sub, ["CANDIDATEID", "ID", "candidateId"])

            if not recipient or not sub_date_raw or not candidate_id:
                continue

            # 1. Recipient must match job contact name
            if contact_name not in recipient and recipient not in contact_name:
                continue

            # Parse submittal date
            try:
                if isinstance(sub_date_raw, datetime):
                    sub_date = sub_date_raw
                else:
                    sub_date = datetime.fromisoformat(str(sub_date_raw).replace("Z", "+00:00"))
            except Exception:
                logger.debug(f"⚠️ Could not parse submittal date '{sub_date_raw}' — skipping")
                continue

            # 2+3. Check candidate has PAIR qualification within 60 days of submittal
            quals = await jobdiva_service.get_candidate_qualifications(candidate_id)
            if not quals:
                continue

            for q in quals:
                qual_name = (get_field(q, ["QUALIFICATION", "qualificationName", "name"]) or "")
                qual_val = (get_field(q, ["QUALIFICATIONVALUE", "value", "qualificationValue"]) or "")
                qual_date_raw = get_field(q, ["DATECREATED", "DATEUPDATED", "date"])

                if qual_name != JOBDIVA_PAIR_QUALIFICATION_NAME:
                    continue
                if qual_val != JOBDIVA_PASS_QUALIFICATION_VALUE:
                    continue

                try:
                    if isinstance(qual_date_raw, datetime):
                        qual_date = qual_date_raw
                    else:
                        qual_date = datetime.fromisoformat(str(qual_date_raw).replace("Z", "+00:00"))
                except Exception:
                    continue

                # Make both dates timezone-naive for comparison if needed
                if sub_date.tzinfo and not qual_date.tzinfo:
                    qual_date = qual_date.replace(tzinfo=sub_date.tzinfo)
                elif qual_date.tzinfo and not sub_date.tzinfo:
                    sub_date = sub_date.replace(tzinfo=qual_date.tzinfo)

                diff_days = abs((sub_date - qual_date).days)
                if diff_days <= 60:
                    logger.info(
                        f"✅ External sub counted: candidate={candidate_id}, "
                        f"submittal={sub_date.date()}, qual={qual_date.date()}, diff={diff_days}d"
                    )
                    count += 1
                    break  # Only count once per submittal

        return count

    async def synchronize_job_applicants(self, job_id: str):
        """
        Fetches all JobDiva applicants for a job, scores them,
        and upserts them into sourced_candidates.
        Also counts and persists external curate submittals (pair_external_subs).
        """
        try:
            logger.debug(f"🤖 [AutoAssignService] Starting sync for job {job_id}")

            # 1. Load job rubric / filters from DB
            resume_match_filters = []
            sourcing_filters = {}
            jobdiva_numeric_id = None

            try:
                with self._get_db_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT resume_match_filters, sourcing_filters, jobdiva_id, status FROM monitored_jobs "
                            "WHERE job_id = %s OR jobdiva_id = %s LIMIT 1",
                            (job_id, job_id)
                        )
                        row = cur.fetchone()
                        if row:
                            resume_match_filters = row[0] if isinstance(row[0], list) else (json.loads(row[0]) if row[0] else [])
                            sourcing_filters = row[1] if isinstance(row[1], dict) else (json.loads(row[1]) if row[1] else {})
                            jobdiva_ref_id = row[2]
                            job_status = row[3]

                            # Skip if job is clearly inactive
                            if job_status and job_status.lower() in ['closed', 'cancelled', 'filled', 'inactive']:
                                logger.debug(f"🤖 [AutoAssignService] Skipping sync for job {job_id} - status is {job_status}")
                                return 0
            except Exception as e:
                logger.warning(f"[AutoAssignService] Could not load filters for job {job_id}: {e}")

            # Use alphanumeric ID for sourced_candidates.jobdiva_id to match UI expectations
            target_job_id = jobdiva_ref_id if jobdiva_ref_id else job_id
            # search_job_id for JobDiva API (can be numeric or ref, resolve_jobdiva_job_id handles it)
            search_job_id = job_id

            logger.debug(f"🤖 [AutoAssignService] Targeting JobDiva search for {search_job_id}, persisting to {target_job_id}")

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
                # Explicit applicants source so auto-sync remains applicant-only
                # even though Step-5 "JobDiva" now maps to talent search only.
                sources=["JobDiva Applicants"],
                bypass_screening=True,
            )

            # 3. Fetch existing candidates to avoid redundant inserts
            existing_ids = set()
            try:
                with self._get_db_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT candidate_id FROM sourced_candidates WHERE jobdiva_id = %s",
                            (target_job_id,)
                        )
                        existing_ids = {str(row[0]) for row in cur.fetchall()}
            except Exception as e:
                logger.warning(f"[AutoAssignService] Could not fetch existing candidates for {job_id}: {e}")

            # 4. Process candidates
            total_assigned = 0
            with self._get_db_connection() as conn:
                with conn.cursor() as cur:
                    async for event in unified_search_service.search_candidates(criteria):
                        if event.get("type") == "stage":
                            logger.debug(f"🤖 [AutoAssignService] Sync Stage for {target_job_id}: {event.get('data')}")

                        if event.get("type") != "candidate":
                            continue
                        cand = event["data"]
                        try:
                            candidate_id = str(cand.get("candidate_id") or cand.get("id") or "")

                            # Skip if already exists to reduce DB write volume
                            if candidate_id in existing_ids:
                                continue

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
                                target_job_id,
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

                            # Populate normalized tables
                            try:
                                candidate_profiles_db.upsert_candidate(target_job_id, cand, cand.get("source", "JobDiva-Applicants"))
                            except Exception as norm_err:
                                logger.warning(f"[AutoAssignService] Failed normalized upsert for {candidate_id}: {norm_err}")
                        except Exception as row_err:
                            logger.warning(f"[AutoAssignService] Failed upsert for {cand.get('candidate_id')}: {row_err}")

                    # Commit once at the end of the job sync to reduce transaction overhead
                    if total_assigned > 0:
                        conn.commit()
                        logger.debug(f"🤖 [AutoAssignService] Committed {total_assigned} candidates for job {target_job_id}")

            logger.info(f"✅ [AutoAssignService] Completed. Total assigned: {total_assigned} for job {target_job_id}")

            # 5. Count and persist external curate submittals
            #    Resolve to numeric JobDiva ID for BI API calls
            try:
                from services.jobdiva import jobdiva_service
                numeric_jd_id = await jobdiva_service._resolve_jobdiva_job_id(str(target_job_id))
                if numeric_jd_id:
                    ext_subs = await self._count_external_curate_submittals(numeric_jd_id)
                    # Persist to monitored_jobs
                    with self._get_db_connection() as conn:
                        with conn.cursor() as cur:
                            cur.execute(
                                "UPDATE monitored_jobs SET pair_external_subs = %s, updated_at = NOW() "
                                "WHERE job_id = %s OR jobdiva_id = %s",
                                (ext_subs, job_id, job_id)
                            )
                            conn.commit()
                    logger.info(f"📊 [AutoAssignService] pair_external_subs={ext_subs} persisted for job {target_job_id}")
                else:
                    logger.debug(f"[AutoAssignService] Could not resolve numeric ID for {target_job_id} — skipping external sub count")
            except Exception as e:
                logger.warning(f"[AutoAssignService] External sub count failed for job {job_id}: {e}", exc_info=True)

            return total_assigned

        except Exception as e:
            logger.error(f"❌ [AutoAssignService] Sync failed for job {job_id}: {e}", exc_info=True)
            return 0

auto_assign_service = AutoAssignService()
