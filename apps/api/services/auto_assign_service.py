import logging
import json
import psycopg2
from datetime import datetime
from typing import Dict, Any, List, Optional
from core.config import DATABASE_URL, JOBDIVA_PAIR_QUALIFICATION_NAME, JOBDIVA_PASS_QUALIFICATION_VALUE
from services.unified_candidate_search import SearchCriteria, unified_search_service
from services.candidate_profiles_db import candidate_profiles_db

logger = logging.getLogger(__name__)

class AutoAssignService:
    def __init__(self, db_url: str = DATABASE_URL):
        self.db_url = db_url

    def _get_db_connection(self):
        return psycopg2.connect(self.db_url, connect_timeout=5)

    def _normalize_text(self, value: Any) -> str:
        return str(value or "").strip()

    def _normalize_email(self, value: Any) -> str:
        return self._normalize_text(value).lower()

    def _normalize_phone(self, value: Any) -> str:
        return "".join(ch for ch in self._normalize_text(value) if ch.isdigit())

    def _find_existing_candidate_row(
        self,
        cur,
        target_job_id: str,
        cand: Dict[str, Any],
        candidate_id: str,
    ) -> Optional[Dict[str, Any]]:
        email = self._normalize_email(cand.get("email"))
        phone = self._normalize_phone(cand.get("phone"))
        name = self._normalize_text(cand.get("name"))
        profile_url = self._normalize_text(cand.get("profile_url"))

        lookup_clauses = ["candidate_id = %s", "data->>'jobdiva_candidate_id' = %s"]
        params: List[Any] = [candidate_id, candidate_id]

        if email:
            lookup_clauses.extend(
                [
                    "LOWER(COALESCE(email, '')) = %s",
                    "LOWER(COALESCE(data->>'email', '')) = %s",
                ]
            )
            params.extend([email, email])

        if phone:
            lookup_clauses.extend(
                [
                    "regexp_replace(COALESCE(phone, ''), '\\D', '', 'g') = %s",
                    "regexp_replace(COALESCE(data->>'phone', ''), '\\D', '', 'g') = %s",
                ]
            )
            params.extend([phone, phone])

        if profile_url:
            lookup_clauses.extend(
                [
                    "COALESCE(profile_url, '') = %s",
                    "COALESCE(data->'urls'->>'linkedin', '') = %s",
                ]
            )
            params.extend([profile_url, profile_url])

        if name and (email or phone):
            lookup_clauses.append(
                "LOWER(COALESCE(name, '')) = %s"
            )
            params.append(name.lower())

        cur.execute(
            f"""
                SELECT id, candidate_id, source, email, phone, name, headline, location,
                       profile_url, resume_text, resume_match_percentage, data
                FROM sourced_candidates
                WHERE jobdiva_id = %s
                  AND ({' OR '.join(lookup_clauses)})
                ORDER BY
                    CASE
                        WHEN candidate_id = %s THEN 0
                        WHEN data->>'jobdiva_candidate_id' = %s THEN 1
                        WHEN LOWER(COALESCE(email, '')) = %s AND %s <> '' THEN 2
                        WHEN regexp_replace(COALESCE(phone, ''), '\\D', '', 'g') = %s AND %s <> '' THEN 3
                        ELSE 4
                    END,
                    CASE WHEN LOWER(COALESCE(source, '')) LIKE '%%applicants%%' THEN 1 ELSE 0 END,
                    created_at DESC
                LIMIT 1
            """,
            [
                target_job_id,
                *params,
                candidate_id,
                candidate_id,
                email,
                email,
                phone,
                phone,
            ],
        )
        row = cur.fetchone()
        if not row:
            return None
        return row if isinstance(row, dict) else None

    def _build_candidate_payload(
        self,
        cand: Dict[str, Any],
        candidate_id: str,
        existing_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        existing_data = existing_data if isinstance(existing_data, dict) else {}
        payload = dict(existing_data)
        payload.update({
            "skills": cand.get("skills") or existing_data.get("skills") or [],
            "experience_years": cand.get("experience_years") or existing_data.get("experience_years") or 0,
            "education": cand.get("enhanced_info", {}).get("candidate_education") or existing_data.get("education") or [],
            "certifications": cand.get("enhanced_info", {}).get("candidate_certification") or existing_data.get("certifications") or [],
            "company_experience": cand.get("enhanced_info", {}).get("company_experience") or existing_data.get("company_experience") or [],
            "urls": cand.get("enhanced_info", {}).get("urls") or existing_data.get("urls") or {},
            "is_selected": True,
            "match_score": cand.get("match_score") if cand.get("match_score") is not None else existing_data.get("match_score", 0),
            "missing_skills": cand.get("missing_skills") or existing_data.get("missing_skills") or [],
            "matched_skills": cand.get("matched_skills") or existing_data.get("matched_skills") or [],
            "explainability": cand.get("explainability") or existing_data.get("explainability") or "",
            "match_score_details": cand.get("match_score_details") or existing_data.get("match_score_details") or {},
            "enhanced_info": cand.get("enhanced_info") or existing_data.get("enhanced_info"),
            "auto_assigned": True,
        })
        if candidate_id:
            payload["jobdiva_candidate_id"] = candidate_id
        return payload

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

    async def _count_feedback_completed(self, target_job_id: str) -> int:
        """
        Count candidates with recruiter action (submit/reject with reason)
        stored locally in sourced_candidates.data.
        """
        try:
            with self._get_db_connection() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    # Count where feedback_type is set and feedback_reason is not null/empty
                    cur.execute(
                        """
                        SELECT COUNT(*)
                        FROM sourced_candidates
                        WHERE jobdiva_id = %s
                          AND data->>'feedback_type' IS NOT NULL
                          AND data->>'feedback_reason' IS NOT NULL
                          AND data->>'feedback_reason' != ''
                        """,
                        (target_job_id,)
                    )
                    row = cur.fetchone()
                    return row[0] if row else 0
        except Exception as e:
            logger.warning(f"[AutoAssignService] Failed to count feedback for job {target_job_id}: {e}")
            return 0

    async def _calculate_time_to_first_pass(self, target_job_id: str) -> float:
        """
        Calculate minutes between first PAIR interview send and first candidate PASS.
        PAIRLaunched  = MIN(created_at)  from engage_interview_audit for this job.
        FirstPAIRPass = MIN(data->>'engage_updated_at') from sourced_candidates
                        where engage_status IN ('pass', 'passed').
        Returns the difference in minutes, or None if data is insufficient.
        """
        try:
            with self._get_db_connection() as conn:
                with conn.cursor() as cur:
                    # Get job activation and canonical IDs from monitored_jobs table
                    cur.execute(
                        """
                        SELECT pair_launched_at, jobdiva_id, job_id
                        FROM monitored_jobs
                        WHERE (jobdiva_id = %s OR job_id = %s)
                        """,
                        (target_job_id, target_job_id)
                    )
                    row = cur.fetchone()
                    if not row:
                        return None
                    
                    pair_launched, ref_id, num_id = row

                    if not pair_launched:
                        return None

                    # Get earliest pass timestamp from sourced_candidates. We check both 
                    # possible ID formats (alphanumeric ref and numeric PK) to be safe.
                    cur.execute(
                        """
                        SELECT MIN(
                            NULLIF(data->>'engage_updated_at', '')::timestamptz
                        ) AS first_pass_ts
                        FROM sourced_candidates
                        WHERE (jobdiva_id = %s OR jobdiva_id = %s)
                          AND (data->>'engage_status') IN ('pass', 'passed', 'Passed', 'PASS', 'completed', 'Completed', 'COMPLETED')
                        """,
                        (ref_id, num_id)
                    )
                    row2 = cur.fetchone()
                    first_pass_ts = row2[0] if row2 else None

                    if not first_pass_ts:
                        return None

                    # Ensure both are timezone-aware before subtracting
                    from datetime import timezone
                    if hasattr(pair_launched, 'tzinfo') and pair_launched.tzinfo is None:
                        pair_launched = pair_launched.replace(tzinfo=timezone.utc)
                    if hasattr(first_pass_ts, 'tzinfo') and first_pass_ts.tzinfo is None:
                        first_pass_ts = first_pass_ts.replace(tzinfo=timezone.utc)

                    delta_minutes = (first_pass_ts - pair_launched).total_seconds() / 60.0
                    return round(delta_minutes, 1) if delta_minutes >= 0 else None

        except Exception as e:
            logger.warning(f"[AutoAssignService] Failed to calculate time_to_first_pass for job {target_job_id}: {e}")
            return None

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

            # 3. Fetch existing candidates to avoid redundant inserts and allow
            # applicant sync to enrich the existing sourced row instead of
            # creating a second human-visible candidate entry.
            existing_ids = set()
            try:
                with self._get_db_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            SELECT candidate_id, data->>'jobdiva_candidate_id'
                            FROM sourced_candidates
                            WHERE jobdiva_id = %s
                            """,
                            (target_job_id,)
                        )
                        for row in cur.fetchall():
                            if row[0]:
                                existing_ids.add(str(row[0]))
                            if len(row) > 1 and row[1]:
                                existing_ids.add(str(row[1]))
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
                            existing_row = self._find_existing_candidate_row(cur, target_job_id, cand, candidate_id)
                            if existing_row:
                                existing_data = existing_row.get("data")
                                if isinstance(existing_data, str):
                                    try:
                                        existing_data = json.loads(existing_data)
                                    except Exception:
                                        existing_data = {}
                                merged_data = self._build_candidate_payload(cand, candidate_id, existing_data)
                                cur.execute(
                                    """
                                    UPDATE sourced_candidates
                                    SET
                                        email = COALESCE(NULLIF(email, ''), %s),
                                        phone = COALESCE(NULLIF(phone, ''), %s),
                                        headline = COALESCE(NULLIF(headline, ''), %s),
                                        location = COALESCE(NULLIF(location, ''), %s),
                                        profile_url = COALESCE(NULLIF(profile_url, ''), %s),
                                        resume_text = COALESCE(NULLIF(resume_text, ''), %s),
                                        data = %s,
                                        updated_at = CURRENT_TIMESTAMP
                                    WHERE id = %s
                                    """,
                                    (
                                        cand.get("email"),
                                        cand.get("phone"),
                                        cand.get("headline") or cand.get("title"),
                                        cand.get("location"),
                                        cand.get("profile_url"),
                                        cand.get("resume_text"),
                                        json.dumps(merged_data),
                                        existing_row["id"],
                                    ),
                                )
                                existing_ids.add(candidate_id)
                            else:
                                if candidate_id in existing_ids:
                                    continue

                                candidate_data_json = json.dumps(
                                    self._build_candidate_payload(cand, candidate_id)
                                )

                                cur.execute("""
                                    INSERT INTO sourced_candidates (
                                        jobdiva_id, candidate_id, source, name, email, phone,
                                        headline, location, profile_url, resume_text, data, status,
                                        resume_match_percentage, updated_at
                                    ) VALUES (
                                        %s, %s, %s, %s, %s, %s,
                                        %s, %s, %s, %s, %s, %s,
                                        %s, CURRENT_TIMESTAMP
                                    )
                                    ON CONFLICT (jobdiva_id, candidate_id, source) DO UPDATE SET
                                        name       = EXCLUDED.name,
                                        email      = EXCLUDED.email,
                                        phone      = EXCLUDED.phone,
                                        headline   = EXCLUDED.headline,
                                        location   = EXCLUDED.location,
                                        profile_url= EXCLUDED.profile_url,
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
                                    cand.get("profile_url"),
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

            # 5. Update performance metrics (Time to First Pass, External Subs, etc.)
            await self.refresh_job_performance_metrics(target_job_id)

            return total_assigned

        except Exception as e:
            logger.error(f"❌ [AutoAssignService] Sync failed for job {job_id}: {e}", exc_info=True)
            return 0

    async def refresh_job_performance_metrics(self, target_job_id: str):
        """
        Recalculates and persists performance metrics for a specific job:
        - Time to First Pass (minutes)
        - External Curate Submittals (from JobDiva)
        - Feedback Completed (local actions)
        """
        try:
            from services.jobdiva import jobdiva_service
            numeric_jd_id = await jobdiva_service._resolve_jobdiva_job_id(str(target_job_id))
            if not numeric_jd_id:
                logger.debug(f"[AutoAssignService] Could not resolve numeric ID for {target_job_id} — skipping metrics refresh")
                return

            # 1. Count and persist external curate submittals
            ext_subs = await self._count_external_curate_submittals(numeric_jd_id)
            # 2. Count and persist feedback completed (local actions)
            feedback_count = await self._count_feedback_completed(target_job_id)
            # 3. Calculate time to first PASS candidate (in minutes)
            time_to_pass = await self._calculate_time_to_first_pass(target_job_id)

            with self._get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE monitored_jobs SET pair_external_subs = %s, feedback_completed = %s, "
                        "time_to_first_pass = %s, updated_at = NOW() "
                        "WHERE job_id = %s OR jobdiva_id = %s",
                        (ext_subs, feedback_count, time_to_pass, str(target_job_id), str(target_job_id))
                    )
                    conn.commit()
            logger.info(f"📊 [AutoAssignService] Metrics refreshed for {target_job_id}: pass_time={time_to_pass}min, ext_subs={ext_subs}, feedback={feedback_count}")
        except Exception as e:
            logger.warning(f"[AutoAssignService] Metrics refresh failed for job {target_job_id}: {e}")


auto_assign_service = AutoAssignService()
