import asyncio
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
        # Resolve both IDs from monitored_jobs first
        cur.execute(
            "SELECT jobdiva_id, job_id FROM monitored_jobs WHERE (jobdiva_id = %s OR job_id = %s) LIMIT 1",
            (target_job_id, target_job_id)
        )
        mj_row = cur.fetchone()
        if mj_row:
            ref_id, num_id = mj_row
        else:
            ref_id, num_id = target_job_id, target_job_id

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
            WHERE (jobdiva_id = %s OR jobdiva_id = %s)
            """,
            (ref_id, num_id),
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

    async def _count_external_curate_submittals(
        self, numeric_job_id, submittals: Optional[List[Dict[str, Any]]] = None
    ) -> int:
        """
        Count external curate submittals for a job.

        Criteria (all three must be satisfied for a submittal to count):
        1. The submittal recipient name matches the job's contact person (from JobDiva BI JobDetail).
        2. The candidate has a PAIR Candidates qualification with value 'Pass'.
        3. The qualification date is within 60 days of the submittal date.

        `submittals` lets refresh_job_performance_metrics share one
        JobSubmittalsDetail fetch between this counter and the raw-record
        persistence; when None the method fetches them itself.

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

        if submittals is None:
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

    def _persist_job_submittals(
        self,
        resolved_job_id: str,
        jobdiva_ref: str,
        submittals: List[Dict[str, Any]],
    ) -> None:
        """Mirror JobDiva BI JobSubmittalsDetail records into
        jobdiva_submittals (created at startup by routers/jobs.py schema
        init). Full DELETE+INSERT per job so the table always reflects the
        latest JobDiva snapshot — callers must NOT invoke this on a failed
        fetch (see refresh_job_performance_metrics)."""
        from services.jobdiva import get_field

        def _parse_dt(raw: Any) -> Optional[datetime]:
            if raw is None or raw == "":
                return None
            if isinstance(raw, datetime):
                return raw
            try:
                return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            except Exception:
                return None

        rows = []
        for sub in submittals or []:
            candidate_id = str(get_field(sub, ["CANDIDATEID", "ID", "candidateId"]) or "")
            recipient = str(get_field(sub, ["RECIPIENTNAME", "RECIPIENT", "recipientName"]) or "")
            submit_date = _parse_dt(get_field(sub, ["SUBMITDATE", "DATE", "submitDate"]))
            try:
                raw_json = json.dumps(sub, default=str)
            except Exception:
                raw_json = "{}"
            rows.append(
                (str(resolved_job_id), jobdiva_ref or "", candidate_id, recipient, submit_date, raw_json)
            )

        with self._get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL lock_timeout = '2000ms'")
                cur.execute("SET LOCAL statement_timeout = '5000ms'")
                cur.execute(
                    "DELETE FROM jobdiva_submittals WHERE job_id = %s",
                    (str(resolved_job_id),),
                )
                if rows:
                    cur.executemany(
                        "INSERT INTO jobdiva_submittals "
                        "(job_id, jobdiva_ref, candidate_id, recipient_name, submit_date, data) "
                        "VALUES (%s, %s, %s, %s, %s, %s)",
                        rows,
                    )
                conn.commit()
        logger.debug(
            f"📥 [AutoAssignService] Persisted {len(rows)} JobDiva submittal(s) for job {resolved_job_id}"
        )

    async def _count_feedback_completed(self, target_job_id: str) -> int:
        """
        Count candidates with recruiter action (submit/reject with reason)
        stored locally in sourced_candidates.data.
        """
        try:
            with self._get_db_connection() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    # Get both IDs first
                    cur.execute(
                        "SELECT jobdiva_id, job_id FROM monitored_jobs WHERE (jobdiva_id = %s OR job_id = %s) LIMIT 1",
                        (target_job_id, target_job_id)
                    )
                    mj_row = cur.fetchone()
                    if not mj_row:
                        return 0
                    ref_id, num_id = mj_row

                    # Count where feedback_type is set and feedback_reason is not null/empty
                    cur.execute(
                        """
                        SELECT COUNT(*)
                        FROM sourced_candidates
                        WHERE (jobdiva_id = %s OR jobdiva_id = %s)
                          AND data->>'feedback_type' IS NOT NULL
                          AND data->>'feedback_reason' IS NOT NULL
                          AND data->>'feedback_reason' != ''
                        """,
                        (ref_id, num_id)
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
                          AND LOWER(data->>'engage_hard_filter_status') IN ('pass', 'passed')
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
            job_location_type = ""

            try:
                with self._get_db_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT resume_match_filters, sourcing_filters, jobdiva_id, status, location_type, city FROM monitored_jobs "
                            "WHERE job_id = %s OR jobdiva_id = %s LIMIT 1",
                            (job_id, job_id)
                        )
                        row = cur.fetchone()
                        if row:
                            resume_match_filters = row[0] if isinstance(row[0], list) else (json.loads(row[0]) if row[0] else [])
                            sourcing_filters = row[1] if isinstance(row[1], dict) else (json.loads(row[1]) if row[1] else {})
                            jobdiva_ref_id = row[2]
                            job_status = row[3]
                            job_location_type = str(row[4] or "").strip()
                            if not job_location_type and str(row[5] or "").strip().upper() == "REMOTE":
                                job_location_type = "Remote"

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
                location_type=job_location_type or "Unspecified",
                # Capped at 100 (was 500). The 15-min auto-sync doesn't need
                # to pull half a thousand applicants per cycle — JobDiva
                # typically returns <50 fresh hits between cycles, and the
                # per-candidate upsert loop below was the dominant source of
                # pool contention and row-lock collisions with concurrent
                # wizard saves. The earlier 100-cap hotfix capped *after*
                # fetch; doing it here at the criteria level avoids the
                # wasted network/LLM work too.
                page_size=100,
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

            # 4. Process candidates in BATCHES.
            # Pre-fix this was a per-candidate borrow loop: for 500 candidates
            # the sync paid ~1000 pool borrows (one for sourced_candidates +
            # one for candidate_profiles_db.upsert_candidate) and acquired
            # ~1000 row locks across two tables — and that ran for every
            # active job every 15min. With a 20-slot pool and 8 workers, that
            # was the dominant trigger of the wizard's "save hangs" symptom:
            # whenever a save tried to UPDATE monitored_jobs WHERE job_id=X,
            # the sync's metrics refresh for X was holding the same row, and
            # the pool was saturated by candidate writes from other jobs.
            #
            # Now we accumulate updates / inserts / profile-upserts into
            # lists and flush in chunks of _CANDIDATE_BATCH_SIZE via
            # execute_values. Cuts pool borrows from ~1000 to ~6 per job,
            # and the rows touched in any one transaction stay bounded so
            # the row-lock window for any single sourced_candidates row is
            # measured in ms, not seconds.
            _CANDIDATE_BATCH_SIZE = 50
            total_assigned = 0
            # Track IDs of rows we INSERTed (not updates) so the caller can
            # auto-launch interviews only for genuinely new applicants. We
            # never want to re-engage someone who's already been engaged.
            newly_inserted_ids: List[str] = []

            update_batch: List[tuple] = []
            insert_batch: List[tuple] = []
            profile_batch: List[Dict[str, Any]] = []

            def _flush_batches() -> None:
                """Flush the accumulated update / insert / profile batches.

                Each flush is one connection borrow per table touched —
                three at most: one for sourced_candidates updates, one for
                sourced_candidates inserts, one for the candidate_profiles
                family. All three carry SET LOCAL timeouts so a single
                slow batch can't pin a pool slot.
                """
                if update_batch:
                    try:
                        with self._get_db_connection() as conn:
                            with conn.cursor() as cur:
                                cur.execute("SET LOCAL lock_timeout = '2000ms'")
                                cur.execute("SET LOCAL statement_timeout = '10000ms'")
                                psycopg2.extras.execute_values(
                                    cur,
                                    """
                                    UPDATE sourced_candidates AS sc SET
                                        email       = COALESCE(NULLIF(sc.email, ''), v.email),
                                        phone       = COALESCE(NULLIF(sc.phone, ''), v.phone),
                                        headline    = COALESCE(NULLIF(sc.headline, ''), v.headline),
                                        location    = COALESCE(NULLIF(sc.location, ''), v.location),
                                        profile_url = COALESCE(NULLIF(sc.profile_url, ''), v.profile_url),
                                        resume_text = COALESCE(NULLIF(sc.resume_text, ''), v.resume_text),
                                        data        = COALESCE(sc.data, '{}'::jsonb) || COALESCE(v.data, '{}'::jsonb) || jsonb_strip_nulls(jsonb_build_object(
                                            'engage_status',       sc.data->>'engage_status',
                                            'engage_interview_id', sc.data->>'engage_interview_id',
                                            'engage_score',        sc.data->'engage_score',
                                            'engage_updated_at',   sc.data->>'engage_updated_at',
                                            'engage_last_response',sc.data->'engage_last_response',
                                            'engage_hard_filter_status', sc.data->>'engage_hard_filter_status',
                                            'engage_hard_filter_reason', sc.data->>'engage_hard_filter_reason',
                                            'engage_passed_email_sent',  sc.data->'engage_passed_email_sent'
                                        )),
                                        updated_at  = CURRENT_TIMESTAMP
                                    FROM (VALUES %s) AS v (
                                        id, email, phone, headline, location,
                                        profile_url, resume_text, data
                                    )
                                    WHERE sc.id = v.id
                                    """,
                                    update_batch,
                                    template="(%s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
                                )
                    except Exception as upd_err:
                        logger.warning(
                            f"[AutoAssignService] Update batch failed for {target_job_id}: {upd_err}"
                        )
                    update_batch.clear()

                if insert_batch:
                    try:
                        with self._get_db_connection() as conn:
                            with conn.cursor() as cur:
                                cur.execute("SET LOCAL lock_timeout = '2000ms'")
                                cur.execute("SET LOCAL statement_timeout = '10000ms'")
                                psycopg2.extras.execute_values(
                                    cur,
                                    """
                                    INSERT INTO sourced_candidates (
                                        jobdiva_id, candidate_id, source, name, email, phone,
                                        headline, location, profile_url, resume_text, data,
                                        status, resume_match_percentage, updated_at
                                    ) VALUES %s
                                    ON CONFLICT (jobdiva_id, candidate_id, source) DO UPDATE SET
                                        name        = EXCLUDED.name,
                                        email       = EXCLUDED.email,
                                        phone       = EXCLUDED.phone,
                                        headline    = EXCLUDED.headline,
                                        location    = EXCLUDED.location,
                                        profile_url = EXCLUDED.profile_url,
                                        resume_text = EXCLUDED.resume_text,
                                        data        = COALESCE(sourced_candidates.data, '{}'::jsonb) || COALESCE(EXCLUDED.data, '{}'::jsonb) || jsonb_strip_nulls(jsonb_build_object(
                                            'engage_status',       sourced_candidates.data->>'engage_status',
                                            'engage_interview_id', sourced_candidates.data->>'engage_interview_id',
                                            'engage_score',        sourced_candidates.data->'engage_score',
                                            'engage_updated_at',   sourced_candidates.data->>'engage_updated_at',
                                            'engage_last_response',sourced_candidates.data->'engage_last_response',
                                            'engage_hard_filter_status', sourced_candidates.data->>'engage_hard_filter_status',
                                            'engage_hard_filter_reason', sourced_candidates.data->>'engage_hard_filter_reason',
                                            'engage_passed_email_sent',  sourced_candidates.data->'engage_passed_email_sent'
                                        )),
                                        status      = EXCLUDED.status,
                                        resume_match_percentage = EXCLUDED.resume_match_percentage,
                                        updated_at  = CURRENT_TIMESTAMP
                                    """,
                                    insert_batch,
                                    template="(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, CURRENT_TIMESTAMP)",
                                )
                    except Exception as ins_err:
                        logger.warning(
                            f"[AutoAssignService] Insert batch failed for {target_job_id}: {ins_err}"
                        )
                    insert_batch.clear()

                if profile_batch:
                    try:
                        # bulk_upsert_candidates now uses one connection per
                        # call (was one per candidate), so passing the full
                        # batch costs a single pool borrow.
                        candidate_profiles_db.bulk_upsert_candidates(
                            target_job_id,
                            list(profile_batch),
                            source="JobDiva-Applicants",
                        )
                    except Exception as norm_err:
                        logger.warning(
                            f"[AutoAssignService] Profile batch failed for {target_job_id}: {norm_err}"
                        )
                    profile_batch.clear()

            # F7: capture the final summary event so we can persist the
            # `jobdiva_criteria_unconfigured` flag for the dashboard. Default
            # to False — only the search emits a truthy value when JobDiva's
            # JobAgent returned "Criteria Not Assigned" for this job.
            jobdiva_criteria_unconfigured = False
            async for event in unified_search_service.search_candidates(criteria):
                if event.get("type") == "stage":
                    logger.debug(f"🤖 [AutoAssignService] Sync Stage for {target_job_id}: {event.get('data')}")

                if event.get("type") == "summary":
                    summary_payload = (event.get("data") or {}).get("summary") or {}
                    jobdiva_criteria_unconfigured = bool(
                        summary_payload.get("jobdiva_criteria_unconfigured")
                    )
                    continue

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
                        update_batch.append((
                            existing_row["id"],
                            cand.get("email"),
                            cand.get("phone"),
                            cand.get("headline") or cand.get("title"),
                            cand.get("location"),
                            cand.get("profile_url"),
                            cand.get("resume_text"),
                            json.dumps(merged_data),
                        ))
                        existing_ids.add(candidate_id)
                    else:
                        if candidate_id in existing_ids:
                            continue

                        candidate_data_json = json.dumps(
                            self._build_candidate_payload(cand, candidate_id)
                        )

                        insert_batch.append((
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

                    # Populate normalized tables in the same batch
                    profile_batch.append(cand)
                except Exception as row_err:
                    logger.warning(f"[AutoAssignService] Failed to stage upsert for {cand.get('candidate_id')}: {row_err}")

                # Flush whenever any single batch fills up. Keeps the
                # transaction working set bounded.
                if (
                    len(update_batch) >= _CANDIDATE_BATCH_SIZE
                    or len(insert_batch) >= _CANDIDATE_BATCH_SIZE
                    or len(profile_batch) >= _CANDIDATE_BATCH_SIZE
                ):
                    _flush_batches()

            # Drain whatever's left after the stream ends.
            _flush_batches()

            logger.info(f"✅ [AutoAssignService] Completed. Total assigned: {total_assigned} for job {target_job_id}")

            # 5. Update performance metrics (Time to First Pass, External Subs, etc.)
            await self.refresh_job_performance_metrics(
                target_job_id,
                jobdiva_criteria_unconfigured=jobdiva_criteria_unconfigured,
            )

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

    async def refresh_job_performance_metrics(
        self,
        target_job_id: str,
        jobdiva_criteria_unconfigured: Optional[bool] = None,
    ):
        """
        Recalculates and persists performance metrics for a specific job:
        - Time to First Pass (minutes)
        - External Curate Submittals (from JobDiva)
        - Feedback Completed (local actions)
        - Candidate counters (sourced/launched/complete_submissions/pass_submissions)
        - JobDiva JobAgent "Criteria Not Assigned" flag (F7) — only written
          when an explicit boolean is passed; left untouched when None so
          ad-hoc callers (e.g. backfills) don't overwrite an existing flag
          with stale data.

        The candidate counters used to be computed live on every dashboard
        load via `_aggregate_candidate_metrics`. That JOIN + JSONB extraction
        was the dominant cause of dashboard slowness on qacurate. Now they
        live as plain INTEGER columns on monitored_jobs, refreshed here, so
        the dashboard becomes a single indexed SELECT.
        """
        try:
            from services.jobdiva import jobdiva_service
            numeric_jd_id = await jobdiva_service._resolve_jobdiva_job_id(str(target_job_id))
            if not numeric_jd_id:
                logger.debug(f"[AutoAssignService] Could not resolve numeric ID for {target_job_id} — skipping metrics refresh")
                return

            # 0. Fetch raw submittals ONCE for this cycle. None means the
            #    JobDiva call failed — keep the previously stored records and
            #    total instead of wiping them with an empty snapshot.
            submittals = await jobdiva_service.get_job_submittals(numeric_jd_id, none_on_error=True)

            # 1. Count and persist external curate submittals (reuses the
            #    fetched records; falls back to its own fetch on None)
            ext_subs = await self._count_external_curate_submittals(numeric_jd_id, submittals=submittals)
            # 2. Count and persist feedback completed (local actions)
            feedback_count = await self._count_feedback_completed(target_job_id)
            # 3. Calculate time to first PASS candidate (in minutes)
            time_to_pass = await self._calculate_time_to_first_pass(target_job_id)
            # 4. Compute candidate counters via the bounded aggregate.
            #    Lives here (background) rather than in the user-facing
            #    dashboard request path.
            counters = await asyncio.to_thread(
                self._compute_candidate_counters, str(target_job_id)
            )

            with self._get_db_connection() as conn:
                with conn.cursor() as cur:
                    # Parity with save_job_draft and monitor_job_locally:
                    # bound this write so a contested monitored_jobs row
                    # can't pin a pool slot indefinitely. Without this,
                    # auto-sync's metrics refresh was the most common
                    # culprit holding the row that save_job_draft tried to
                    # UPDATE on step 3.
                    cur.execute("SET LOCAL lock_timeout = '2000ms'")
                    cur.execute("SET LOCAL statement_timeout = '5000ms'")
                    # Resolve the PK once up front instead of issuing an
                    # `OR jobdiva_id = %s` predicate at write time. The OR
                    # forces Postgres to consider two index paths under the
                    # row lock and (depending on planner choice) can touch
                    # more rows than intended. A single PK lookup followed
                    # by an UPDATE WHERE job_id = %s is one indexed path,
                    # one row, deterministic.
                    cur.execute(
                        "SELECT job_id, jobdiva_id FROM monitored_jobs "
                        "WHERE job_id = %s OR jobdiva_id = %s "
                        "LIMIT 1",
                        (str(target_job_id), str(target_job_id)),
                    )
                    row = cur.fetchone()
                    if not row:
                        logger.debug(
                            f"[AutoAssignService] No monitored_jobs row for {target_job_id} — skipping metrics update"
                        )
                        return
                    resolved_job_id, resolved_jobdiva_ref = row[0], row[1]
                    set_clauses = [
                        "pair_external_subs = %s",
                        "feedback_completed = %s",
                        "time_to_first_pass = %s",
                        "candidates_sourced = %s",
                        "candidates_launched = %s",
                        "complete_submissions = %s",
                        "pass_submissions = %s",
                    ]
                    params: List[Any] = [
                        ext_subs,
                        feedback_count,
                        time_to_pass,
                        counters["candidates_sourced"],
                        counters["candidates_launched"],
                        counters["complete_submissions"],
                        counters["pass_submissions"],
                    ]
                    # Only overwrite jobdiva_total_subs when this cycle got a
                    # real snapshot — a failed fetch (None) keeps the
                    # last-known total instead of zeroing the dashboard.
                    if submittals is not None:
                        set_clauses.append("jobdiva_total_subs = %s")
                        params.append(len(submittals))
                    # F7: only overwrite `jobdiva_criteria_unconfigured` when
                    # we have a fresh boolean from this run's summary. Callers
                    # that don't run a search (manual backfills) pass None and
                    # leave the flag at its last-known value.
                    if jobdiva_criteria_unconfigured is not None:
                        set_clauses.append("jobdiva_criteria_unconfigured = %s")
                        params.append(bool(jobdiva_criteria_unconfigured))
                    set_clauses.append("updated_at = NOW()")
                    params.append(resolved_job_id)
                    cur.execute(
                        "UPDATE monitored_jobs SET " + ", ".join(set_clauses) + " WHERE job_id = %s",
                        params,
                    )
                    conn.commit()

            # 5. Mirror the raw submittal records for date-bucketed analytics
            #    (admin / team-lead dashboards). Full per-job replace; skipped
            #    when the fetch failed so stored history survives outages.
            if submittals is not None:
                try:
                    await asyncio.to_thread(
                        self._persist_job_submittals,
                        str(resolved_job_id),
                        str(resolved_jobdiva_ref or ""),
                        submittals,
                    )
                except Exception as persist_err:  # noqa: BLE001
                    logger.warning(
                        f"[AutoAssignService] Submittal persistence failed for job {target_job_id}: {persist_err}"
                    )
            logger.info(
                f"📊 [AutoAssignService] Metrics refreshed for {target_job_id}: "
                f"pass_time={time_to_pass}min ext_subs={ext_subs} feedback={feedback_count} "
                f"sourced={counters['candidates_sourced']} pass={counters['pass_submissions']} "
                f"jd_total_subs={'n/a (fetch failed)' if submittals is None else len(submittals)} "
                f"jd_unconfigured={jobdiva_criteria_unconfigured}"
            )
        except Exception as e:
            logger.warning(f"[AutoAssignService] Metrics refresh failed for job {target_job_id}: {e}")

    def _compute_candidate_counters(self, target_job_id: str) -> Dict[str, int]:
        """Compute the four candidate counters for a single job.

        Uses the same aggregate logic as `_aggregate_candidate_metrics` in
        routers/jobs.py but scoped to one job and called from a background
        path so a slow query never blocks the dashboard. Bounded with
        statement_timeout=10s — generous because this is async and not in
        the user request path; if 10s still isn't enough on a degenerate
        row count, the warning is logged and we leave the counters as their
        last-known value.
        """
        zero = {
            "candidates_sourced": 0,
            "candidates_launched": 0,
            "complete_submissions": 0,
            "pass_submissions": 0,
        }
        try:
            with self._get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SET LOCAL statement_timeout = '10000ms'")
                    # Resolve both IDs from monitored_jobs first
                    cur.execute(
                        "SELECT jobdiva_id, job_id FROM monitored_jobs WHERE (jobdiva_id = %s OR job_id = %s) LIMIT 1",
                        (target_job_id, target_job_id)
                    )
                    mj_row = cur.fetchone()
                    if not mj_row:
                        return zero
                    ref_id, num_id = mj_row

                    # Match both storage shapes — some rows are keyed by
                    # jobdiva_id (alphanumeric ref), others by job_id::text.
                    cur.execute(
                        """
                        SELECT
                            COUNT(DISTINCT sc.candidate_id)                                 AS candidates_sourced,
                            -- Counts distinct interviews; ≤ candidates where two share an email (shared-email edge case).
                            COUNT(DISTINCT NULLIF(sc.data->>'engage_interview_id', '')) AS candidates_launched,
                            COUNT(DISTINCT CASE
                                WHEN sc.data->>'engage_status' IN
                                    ('completed', 'failed', 'passed', 'rejected', 'pass', 'fail')
                                THEN sc.candidate_id
                            END)                                                          AS complete_submissions,
                            COUNT(DISTINCT CASE
                                WHEN LOWER(sc.data->>'engage_hard_filter_status') IN ('pass', 'passed')
                                THEN sc.candidate_id
                            END)                                                          AS pass_submissions
                        FROM sourced_candidates sc
                        WHERE (sc.jobdiva_id = %s OR sc.jobdiva_id = %s)
                        """,
                        (ref_id, num_id),
                    )
                    row = cur.fetchone()
                    if not row:
                        return zero
                    return {
                        "candidates_sourced": int(row[0] or 0),
                        "candidates_launched": int(row[1] or 0),
                        "complete_submissions": int(row[2] or 0),
                        "pass_submissions": int(row[3] or 0),
                    }
        except Exception as e:
            logger.warning(
                f"[AutoAssignService] _compute_candidate_counters failed for {target_job_id}: {e}"
            )
            return zero


auto_assign_service = AutoAssignService()
