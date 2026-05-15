import logging
import json
import psycopg2
import psycopg2.extras
from datetime import datetime
from typing import Dict, Any, List, Optional
from core.config import DATABASE_URL, JOBDIVA_PAIR_QUALIFICATION_NAME, JOBDIVA_PASS_QUALIFICATION_VALUE
from core.db import get_db_connection
from services.unified_candidate_search import SearchCriteria, unified_search_service
from services.candidate_profiles_db import candidate_profiles_db
from services.metrics_service import metrics_service

logger = logging.getLogger(__name__)

class AutoAssignService:
    def __init__(self, db_url: str = DATABASE_URL):
        self.db_url = db_url

    def _get_db_connection(self):
        # Route through the shared per-worker pool. Pre-fix this was a raw
        # `psycopg2.connect(...)`, so every borrow paid a fresh TCP+TLS+auth
        # handshake AND the resulting `with` block leaked the socket on
        # exit (psycopg2.connection.__exit__ doesn't close). The pool's
        # _PooledConnection wraps __exit__ to return the slot. The DSN
        # passed to __init__ is now ignored — we always use the pool's
        # DATABASE_URL — but that matches every existing call site.
        return get_db_connection()

    def _normalize_text(self, value: Any) -> str:
        return str(value or "").strip()

    def _normalize_email(self, value: Any) -> str:
        return self._normalize_text(value).lower()

    def _normalize_phone(self, value: Any) -> str:
        return "".join(ch for ch in self._normalize_text(value) if ch.isdigit())

    def _build_candidate_lookup_index(
        self, cur, target_job_id: str
    ) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """Bulk-fetch existing candidates for a job and build in-memory
        lookup dicts so the per-row match becomes O(1) instead of an
        N+1 of non-sargable OR/LOWER/regex/JSON predicates that pinned
        Postgres at 95% CPU during auto-sync.

        Caller must pass a RealDictCursor (rows accessed via .get()).

        Five lookup dicts, each maps a normalized key → row:
          - by_candidate_id
          - by_jcid   (data->>'jobdiva_candidate_id')
          - by_email  (covers both `email` and `data->>'email'`)
          - by_phone  (digits-only)
          - by_url    (profile_url and data->'urls'->>'linkedin')

        Within each dict, the highest-priority row wins on collision —
        mirroring the original ORDER BY (applicants source first, then
        most-recent created_at).
        """
        cur.execute(
            r"""
            SELECT id, candidate_id, source, email, phone, name, headline, location,
                   profile_url, resume_text, resume_match_percentage, data, created_at,
                   data->>'jobdiva_candidate_id'                            AS jcid,
                   LOWER(COALESCE(email, ''))                                AS email_lc,
                   LOWER(COALESCE(data->>'email', ''))                       AS data_email_lc,
                   regexp_replace(COALESCE(phone, ''), '\D', '', 'g')        AS phone_norm,
                   regexp_replace(COALESCE(data->>'phone', ''), '\D', '', 'g') AS data_phone_norm,
                   COALESCE(profile_url, '')                                 AS profile_url_norm,
                   COALESCE(data->'urls'->>'linkedin', '')                   AS linkedin_norm
            FROM sourced_candidates
            WHERE jobdiva_id = %s
            """,
            (target_job_id,),
        )
        rows = cur.fetchall() or []

        # Highest-priority first: applicants source, then newest.
        def _sort_key(r):
            src = (r.get("source") or "").lower()
            applicants_rank = 0 if "applicants" in src else 1
            ts = r.get("created_at")
            ts_rank = -(ts.timestamp()) if ts else 0
            return (applicants_rank, ts_rank)

        rows.sort(key=_sort_key)

        idx: Dict[str, Dict[str, Dict[str, Any]]] = {
            "by_candidate_id": {},
            "by_jcid": {},
            "by_email": {},
            "by_phone": {},
            "by_url": {},
        }
        for r in rows:
            cid = r.get("candidate_id")
            if cid:
                idx["by_candidate_id"].setdefault(str(cid), r)
            jcid = r.get("jcid")
            if jcid:
                idx["by_jcid"].setdefault(str(jcid), r)
            email_lc = r.get("email_lc")
            if email_lc:
                idx["by_email"].setdefault(email_lc, r)
            data_email_lc = r.get("data_email_lc")
            if data_email_lc:
                idx["by_email"].setdefault(data_email_lc, r)
            phone_norm = r.get("phone_norm")
            if phone_norm:
                idx["by_phone"].setdefault(phone_norm, r)
            data_phone_norm = r.get("data_phone_norm")
            if data_phone_norm:
                idx["by_phone"].setdefault(data_phone_norm, r)
            url_norm = r.get("profile_url_norm")
            if url_norm:
                idx["by_url"].setdefault(url_norm, r)
            linkedin_norm = r.get("linkedin_norm")
            if linkedin_norm:
                idx["by_url"].setdefault(linkedin_norm, r)
        return idx

    def _find_in_index(
        self,
        index: Dict[str, Dict[str, Dict[str, Any]]],
        cand: Dict[str, Any],
        candidate_id: str,
    ) -> Optional[Dict[str, Any]]:
        """In-memory replacement for the old _find_existing_candidate_row.

        Probes the prefetched lookup dicts in the same priority order the
        original ORDER BY CASE used: candidate_id > jcid > email > phone >
        profile_url. The original's `LOWER(name) = '<incoming_name>'` fallback
        is intentionally dropped — it was both expensive and a correctness
        bug, since two unrelated 'Unknown Unknown' candidates would alias to
        each other.
        """
        if candidate_id:
            hit = index["by_candidate_id"].get(str(candidate_id))
            if hit:
                return hit
            hit = index["by_jcid"].get(str(candidate_id))
            if hit:
                return hit

        email = self._normalize_email(cand.get("email"))
        if email:
            hit = index["by_email"].get(email)
            if hit:
                return hit

        phone = self._normalize_phone(cand.get("phone"))
        if phone:
            hit = index["by_phone"].get(phone)
            if hit:
                return hit

        profile_url = self._normalize_text(cand.get("profile_url"))
        if profile_url:
            hit = index["by_url"].get(profile_url)
            if hit:
                return hit

        return None

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

    async def _defer_normalized_candidate_upserts(
        self,
        target_job_id: str,
        candidates: List[Dict[str, Any]],
    ) -> None:
        """Persist normalized candidate tables after the hot sync loop.

        These writes are useful, but they are not needed for the jobs
        dashboard request path, so keep them off the critical portion of
        applicant sync.
        """
        if not candidates:
            return
        try:
            await asyncio.to_thread(
                candidate_profiles_db.bulk_upsert_candidates,
                target_job_id,
                candidates,
                "JobDiva-Applicants",
            )
        except Exception as e:
            logger.warning(
                "[AutoAssignService] Deferred normalized upsert failed for job %s: %s",
                target_job_id,
                e,
            )

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

            # 3. Build an in-memory lookup index of existing candidates for
            # this job in ONE query. Previously this section pre-fetched IDs
            # into a set AND the per-candidate _find_existing_candidate_row()
            # call inside the loop below ran a non-sargable OR/LOWER/regex/
            # JSON predicate against sourced_candidates — pegging Postgres
            # CPU at 95% during auto-sync (N+1 over potentially 500+ rows
            # per job). Bulk-prefetch + dict lookup collapses that to 1+N
            # where the N is in-process work.
            candidate_index: Dict[str, Dict[str, Dict[str, Any]]] = {
                "by_candidate_id": {},
                "by_jcid": {},
                "by_email": {},
                "by_phone": {},
                "by_url": {},
            }
            try:
                with self._get_db_connection() as conn:
                    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                        candidate_index = self._build_candidate_lookup_index(cur, target_job_id)
            except Exception as e:
                logger.warning(f"[AutoAssignService] Could not fetch existing candidates for {job_id}: {e}")

            # Maintain the legacy `existing_ids` set for the in-loop dedup
            # check below (skip re-INSERT if we already saw this candidate
            # in this run).
            existing_ids: set = (
                set(candidate_index["by_candidate_id"].keys())
                | set(candidate_index["by_jcid"].keys())
            )

            # 4. Process candidates.
            # Borrow + commit + release per candidate rather than holding one
            # connection (and one open txn) for the entire streaming search.
            # The pre-fix shape parked a pool slot for 30s–2min while awaiting
            # LLM/JobDiva/Exa between events, accumulated row locks across
            # every ON CONFLICT DO UPDATE, and grew WAL as one giant txn.
            # With per-row commit the slot is held for ms of DB work; the
            # _PooledConnection wrapper makes borrow/release cheap (no
            # reconnect — just a list pop on the warm pool).
            total_assigned = 0
            normalized_candidates: List[Dict[str, Any]] = []
            # Track IDs of rows we INSERTed (not updates) so the caller can
            # auto-launch interviews only for genuinely new applicants. We
            # never want to re-engage someone who's already been engaged.
            newly_inserted_ids: List[str] = []
            async for event in unified_search_service.search_candidates(criteria):
                if event.get("type") == "stage":
                    logger.debug(f"🤖 [AutoAssignService] Sync Stage for {target_job_id}: {event.get('data')}")

                if event.get("type") != "candidate":
                    continue
                cand = event["data"]
                try:
                    candidate_id = str(cand.get("candidate_id") or cand.get("id") or "")
                    existing_row = self._find_in_index(candidate_index, cand, candidate_id)

                    if existing_row:
                        existing_data = existing_row.get("data")
                        if isinstance(existing_data, str):
                            try:
                                existing_data = json.loads(existing_data)
                            except Exception:
                                existing_data = {}
                        merged_data = self._build_candidate_payload(cand, candidate_id, existing_data)
                        with self._get_db_connection() as conn:
                            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
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

                        with self._get_db_connection() as conn:
                            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
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
                        if candidate_id:
                            newly_inserted_ids.append(candidate_id)
                            existing_ids.add(candidate_id)

                    normalized_candidates.append(cand)
                except Exception as row_err:
                    logger.warning(f"[AutoAssignService] Failed upsert for {cand.get('candidate_id')}: {row_err}")

            logger.info(f"✅ [AutoAssignService] Completed. Total assigned: {total_assigned} for job {target_job_id}")

            if normalized_candidates:
                asyncio.create_task(
                    self._defer_normalized_candidate_upserts(
                        target_job_id,
                        normalized_candidates,
                    )
                )

            # 5. Update performance metrics (Time to First Pass, External Subs, etc.)
            await self.refresh_job_performance_metrics(target_job_id)

            # 6. Auto-launch interviews for newly-inserted applicants. Pre-fix,
            #    this step was missing entirely — the cron pulled JobDiva
            #    applicants into sourced_candidates but no code ever pushed
            #    them to pairbot, so they sat with engage_status NULL until a
            #    recruiter manually clicked Engage on each row. The helper
            #    enforces its own guards (job must have been launched once,
            #    outreach not stopped, DNC honored, batch capped).
            #    Late import to avoid circular import at module load:
            #    engagement.py imports auto_assign_service.
            if newly_inserted_ids:
                try:
                    from routers.engagement import auto_launch_for_candidates
                    import asyncio as _asyncio
                    _asyncio.create_task(
                        auto_launch_for_candidates(newly_inserted_ids, target_job_id)
                    )
                except Exception as launch_err:  # noqa: BLE001
                    logger.warning(
                        f"[AutoAssignService] Failed to schedule auto-launch for job "
                        f"{target_job_id}: {launch_err}"
                    )

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
            
            # Refresh recruitment counts (sourced, launched, etc.)
            metrics_service.refresh_job_metrics(target_job_id)
            
            logger.info(f"📊 [AutoAssignService] Metrics refreshed for {target_job_id}: pass_time={time_to_pass}min, ext_subs={ext_subs}, feedback={feedback_count}")
        except Exception as e:
            logger.warning(f"[AutoAssignService] Metrics refresh failed for job {target_job_id}: {e}")


auto_assign_service = AutoAssignService()
