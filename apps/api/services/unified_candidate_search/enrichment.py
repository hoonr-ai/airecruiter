"""UnifiedCandidateSearch methods: enrichment."""
import asyncio
from typing import List, Dict, Any
from services.location import sanitize_candidate_location
from services.no_contact import apply_no_contact_flag
from services import contact_enrichment
from .common import (
    SearchCriteria,
    _below_contact_score_floor,
    _paid_contact_deferred,
    logger,
)


class EnrichmentMixin:

    async def _apply_contact_enrichment(
        self,
        cand: Dict[str, Any],
        criteria: "SearchCriteria",
        *,
        overwrite: bool,
    ) -> None:
        """In-line contact enrichment for one candidate (Apollo → Exa by
        default; the providers and their order live inside
        ``contact_enrichment.enrich_contact_for_sourcing``). Gated by
        CONTACT_ENRICHMENT_INLINE_ENABLED and capped at PER_JOB_CAP inside the
        helper.

        - ``overwrite=True`` (Exa / deep-search): the enrichment result REPLACES
          any pre-existing email/phone — those sources are the authority.
        - ``overwrite=False`` (JobDiva/Dice/Unipile): only backfill when a field
          is empty, to preserve provider-supplied contact info and bound cost.

        ``full_name`` is required for the ZoomInfo path (the new Data API doesn't
        accept linkedinUrl as a match input — it needs firstName + lastName for
        ContactSearch). Without a name we skip ZoomInfo and go straight to
        Apollo (which does accept a URL). Mutates ``cand`` in place; never raises.

        LinkedIn-sourced candidates additionally fall through to the Exa Agent
        contact run when ZoomInfo + Apollo both miss (``include_exa``): ZoomInfo
        can't match by LinkedIn URL and Apollo credits run dry, so URL-only
        profiles would otherwise stream in with no contact at all. Budgeted
        separately inside the helper (EXA_SOURCING_CONTACT_CAP per job). Not
        on a search about to be launched (``_paid_contact_deferred``): Launch
        PAIR buys the email together with the phone; Apollo still runs here.
        """
        profile_url = str(cand.get("profile_url") or "").strip()
        if "linkedin.com/in/" not in profile_url.lower():
            return
        # Score floor (Fix 8): never spend a lookup on a scored row below
        # CONTACT_ENRICH_MIN_SCORE. Unscored rows are left to the caller's
        # policy (contact_lookup_block_reason).
        if _below_contact_score_floor(cand):
            return
        source = str(cand.get("source") or "")
        # Outside the try below: a mistake here must not turn into a row
        # silently enriched by nobody.
        include_exa = source.startswith("LinkedIn") and not _paid_contact_deferred(cand, criteria)
        # Scopes the ZoomInfo name search (an unscoped name is ambiguous) and
        # sharpens the Exa query. The LLM extraction has no `current_company`
        # key, so this reads the stored roles too.
        company = contact_enrichment.current_company_of(cand)
        try:
            enrich = await contact_enrichment.enrich_contact_for_sourcing(
                profile_url,
                criteria.job_id,
                full_name=str(cand.get("name") or "").strip() or None,
                include_exa=include_exa,
                company=company,
                # What the candidate already has. Seeds the ZoomInfo
                # match-by-email lookup (the cheap way to fill a missing phone)
                # and gates the paid Exa fallback, which is reserved for
                # candidates we cannot otherwise reach at all.
                seed_email=str(cand.get("email") or "").strip(),
                seed_phone=str(cand.get("phone") or "").strip(),
                # Sourcing never buys phone numbers. An email alone makes a
                # candidate launchable (the PAIR gate is phone OR email) and
                # renders on Step 5, while phones are the expensive half of every
                # provider — Apollo gates them behind a per-record reveal, Exa
                # charges per run. Hunting one for a candidate the recruiter may
                # never shortlist is speculative spend, so it is deferred to
                # Launch PAIR and to the per-candidate phone button on Step 5,
                # both of which carry real intent.
                want_phone=False,
            )
        except Exception as e:
            logger.warning("contact_enrichment failed for %s: %s", cand.get("id"), e)
            enrich = {}
        if not enrich:
            return

        # Only a personal number becomes the candidate's phone; a work line
        # (workPhone) is never used for outreach.
        if overwrite:
            new_email = enrich.get("workEmail") or enrich.get("personalEmail") or ""
            new_phone = enrich.get("mobilePhone") or ""
            if new_email:
                cand["email"] = new_email
            if new_phone:
                cand["phone"] = new_phone
        else:
            cand["email"] = (
                cand.get("email")
                or enrich.get("workEmail")
                or enrich.get("personalEmail")
                or ""
            )
            cand["phone"] = (
                cand.get("phone")
                or enrich.get("mobilePhone")
                or ""
            )
        if cand.get("email") or cand.get("phone"):
            if not isinstance(cand.get("enhanced_info"), dict):
                cand["enhanced_info"] = cand.get("enhanced_info") or {}
            cand["enhanced_info"]["contact_enrichment_provider"] = enrich.get("provider_used")

    async def _enrich_filtered_jobdiva_candidates(self, candidates: List[Dict[str, Any]], criteria: SearchCriteria):
        """
        Enriches JobDiva candidates with full resumes and LLM assessment.
        Yields enriched candidates concurrently as they complete.
        """
        from services.sourced_candidates_storage import process_jobdiva_candidate
        
        jobdiva_candidates = [
            candidate for candidate in candidates
            if str(candidate.get("source", "")).startswith("JobDiva")
        ]
        self._log_stage("ResumeScreen", f"checking {len(jobdiva_candidates)} JobDiva candidate resume(s) before LLM")

        semaphore = asyncio.Semaphore(5)
        counters = {
            "screened": 0,
            "skipped": 0,
            "no_resume": 0,
            "failed_filter": 0,
            "failed_location": 0,
            "failed_location_geocode": 0,
            "llm_extraction_errors": 0,
            "pre_llm_skipped_low_score": 0,
            "pre_llm_skipped_no_required_hit": 0,
            "pre_llm_skipped_min_years": 0,
        }

        async def _process_single(candidate, index):
            async with semaphore:
                if candidate.get("enhanced_info"):
                    self._log_stage("ResumeScreen", f"candidate_id={candidate.get('candidate_id')} already has enhanced_info")
                    return {"status": "success", "candidate": candidate}

                candidate_id = str(candidate.get("candidate_id") or candidate.get("id") or "")
                if not candidate_id:
                    self._log_stage("ResumeScreen", f"skipped candidate at index {index}; no candidate_id")
                    return {"status": "skipped", "candidate": None}

                try:
                    resume_text = candidate.get("resume_text") or ""
                    if not resume_text or "Resume content unavailable" in resume_text:
                        self._log_stage("ResumeScreen", f"fetching resume for candidate_id={candidate_id}")
                        resume_data = await self.jobdiva_service.get_candidate_resume(
                            candidate_id,
                            resume_id=candidate.get("resume_id"),
                        )
                        resume_text = (resume_data or {}).get("resume_text", "")
                        if resume_text and "Resume content unavailable" not in resume_text:
                            candidate["resume_text"] = resume_text
                            candidate["resume_id"] = (resume_data or {}).get("resume_id") or candidate.get("resume_id")
                            candidate["email"] = candidate.get("email") or (resume_data or {}).get("email")
                            candidate["phone"] = candidate.get("phone") or (resume_data or {}).get("phone")
                            candidate["title"] = candidate.get("title") or (resume_data or {}).get("title")
                            candidate["location"] = candidate.get("location") or (resume_data or {}).get("location")
                            self._log_stage("ResumeScreen", f"successfully fetched resume for candidate_id={candidate_id} ({len(resume_text)} chars)")

                    if not candidate.get("resume_text"):
                        self._log_stage("ResumeScreen", f"skipped candidate_id={candidate_id}; no resume text available")
                        return {"status": "no_resume", "candidate": None}

                    # PR-B: cheap pre-LLM YOE gate. Drops candidates whose
                    # resume text confidently shows fewer years than the
                    # configured floor, before paying for LLM enrichment.
                    # Soft-keep if no number is parseable.
                    if self._candidate_outside_years_range_pre_llm(candidate, criteria):
                        counters["pre_llm_skipped_min_years"] += 1
                        self._log_stage(
                            "LLMGate",
                            "skipping LLM for candidate_id=%s reason=outside_years_range_pre_llm threshold=%s" % (
                                candidate_id,
                                int(criteria.min_experience_years or 0),
                            ),
                        )
                        return {"status": "failed_filter", "candidate": None}

                    if criteria.bypass_screening and not self._has_experience_years_range(criteria):
                        self._log_stage("ResumeScreen", f"Bypassing LLM extraction for candidate_id={candidate_id} (auto-sync mode)")
                        # In bypass mode, we still ensure name/title/location are basic-hydrated
                        # even without LLM if JobDiva already has them.
                        candidate["enhanced_info"] = {}
                        candidate["enhanced_info_status"] = "skipped"
                        return {"status": "success", "candidate": candidate}

                    self._log_stage("ResumeScreen", f"running quick filter for candidate_id={candidate_id}")
                    assessment = self._filter_assessment(candidate, criteria, enforce_years=False)
                    if not assessment["passes"]:
                        location_reason = assessment.get("location_failure_reason")
                        self._log_stage(
                            "ResumeScreen",
                            "FAILED FILTER candidate_id=%s matched=%s missing=%s excluded=%s location_reason=%s" % (
                                candidate_id,
                                assessment["matched"][:5],
                                assessment["missing"][:5],
                                assessment["excluded"][:5],
                                location_reason,
                            ),
                        )
                        if location_reason:
                            if location_reason in {"candidate_ungeocodable", "target_ungeocodable"}:
                                return {"status": "failed_location_geocode", "candidate": None}
                            return {"status": "failed_location", "candidate": None}
                        return {"status": "failed_filter", "candidate": None}

                    self._log_stage(
                        "ResumeScreen",
                        "PASSED FILTER candidate_id=%s matched=%s - proceeding to LLM" % (
                            candidate_id,
                            assessment["matched"][:5],
                        ),
                    )

                    # Pre-LLM gate to reduce expensive extraction calls on
                    # obvious low-fit profiles while protecting borderline
                    # candidates that already show required-term evidence.
                    pre_score_result = self._score_candidate(candidate, criteria)
                    pre_score = float(pre_score_result.get("score") or 0)
                    pre_score_details = pre_score_result.get("score_details") or {}
                    has_required_hit = any(
                        isinstance(dim, dict)
                        and int(dim.get("required_total") or 0) > 0
                        and int(dim.get("required_matched") or 0) > 0
                        for dim in pre_score_details.values()
                    )

                    skip_reason = None
                    if pre_score < 25:
                        skip_reason = "low_score"
                        counters["pre_llm_skipped_low_score"] += 1
                    elif pre_score < 40 and not has_required_hit:
                        skip_reason = "no_required_hit"
                        counters["pre_llm_skipped_no_required_hit"] += 1

                    if skip_reason:
                        self._log_stage(
                            "LLMGate",
                            "skipping LLM for candidate_id=%s pre_score=%.1f reason=%s required_hit=%s" % (
                                candidate_id,
                                pre_score,
                                skip_reason,
                                has_required_hit,
                            ),
                        )
                        candidate["enhanced_info"] = candidate.get("enhanced_info") or {}
                        candidate["enhanced_info_status"] = "skipped_pre_llm_gate"
                        return {"status": "success", "candidate": candidate}

                    self._log_stage("LLM", f"STARTING LLM extraction for candidate_id={candidate_id}, resume_id={candidate.get('resume_id') or 'unknown'}")
                    
                    enhanced = await process_jobdiva_candidate(candidate)
                    # Fix 2: detect silent LLM extraction failures and surface
                    # them via a dedicated counter + stage log, so operators can
                    # tell "LLM failed on N candidates" from "valid empty
                    # profile".
                    extraction_error = (
                        enhanced.get("_extraction_error")
                        if isinstance(enhanced, dict) else None
                    )
                    if extraction_error:
                        logger.warning(
                            "LLM extraction degraded for candidate_id=%s (%s); scoring from resume_text + source-native fields only",
                            candidate_id, extraction_error,
                        )
                    if isinstance(enhanced, dict) and enhanced is not candidate:
                        candidate["enhanced_info"] = enhanced.get("raw", enhanced)
                    else:
                        candidate["enhanced_info"] = {}
                    if extraction_error and isinstance(candidate.get("enhanced_info"), dict):
                        candidate["enhanced_info"]["_extraction_error"] = extraction_error
                        
                    candidate["enhanced_info_status"] = "completed"
                    candidate["name"] = candidate["enhanced_info"].get("candidate_name") or candidate.get("name")
                    candidate["email"] = candidate["enhanced_info"].get("email") or candidate.get("email")
                    candidate["phone"] = candidate["enhanced_info"].get("phone") or candidate.get("phone")
                    candidate["title"] = candidate["enhanced_info"].get("job_title") or candidate.get("title")
                    # Résumé is final for residence (see _apply_resume_location):
                    # the résumé's explicitly stated location replaces
                    # JobDiva's CRM city/state; the CRM value stands only when
                    # the résumé is silent.
                    self._apply_resume_location(candidate)
                    candidate["education"] = candidate["enhanced_info"].get("candidate_education", [])
                    candidate["certifications"] = candidate["enhanced_info"].get("candidate_certification", [])
                    candidate["urls"] = candidate["enhanced_info"].get("urls", {})
                    candidate["experience_years"] = candidate["enhanced_info"].get("years_of_experience") or candidate.get("experience_years")
                    if candidate["enhanced_info"].get("structured_skills") or candidate["enhanced_info"].get("skills"):
                        candidate["skills"] = candidate["enhanced_info"].get("structured_skills") or candidate["enhanced_info"].get("skills")

                    self._log_stage("LLM", f"COMPLETED LLM extraction for candidate_id={candidate_id}")
                    return {"status": "success", "candidate": candidate}
                except Exception as e:
                    logger.error(f"❌ Filtered JobDiva enhancement FAILED for {candidate_id}: {e}", exc_info=True)
                    return {"status": "skipped", "candidate": None}

        # Fire off all processing tasks concurrently
        tasks = [_process_single(candidate, i) for i, candidate in enumerate(jobdiva_candidates, 1)]
        
        # Yield results exactly as soon as they complete
        for task in asyncio.as_completed(tasks):
            result = await task
            status = result["status"]
            if status == "success":
                counters["screened"] += 1
                cand = result["candidate"]
                if isinstance(cand, dict) and isinstance(cand.get("enhanced_info"), dict) \
                        and cand["enhanced_info"].get("_extraction_error"):
                    counters["llm_extraction_errors"] += 1
                yield cand
            elif status == "no_resume":
                counters["no_resume"] += 1
                counters["skipped"] += 1
            elif status == "failed_filter":
                counters["failed_filter"] += 1
                counters["skipped"] += 1
            elif status == "failed_location":
                counters["failed_location"] += 1
                counters["failed_filter"] += 1
                counters["skipped"] += 1
            elif status == "failed_location_geocode":
                counters["failed_location_geocode"] += 1
                counters["failed_location"] += 1
                counters["failed_filter"] += 1
                counters["skipped"] += 1
            elif status == "skipped":
                counters["skipped"] += 1

        self._log_stage(
            "ResumeScreen",
            "RESULTS: kept %s of %s JobDiva candidate(s); skipped %s total (no_resume=%s, failed_filter=%s, failed_location=%s, geocode_failures=%s, llm_extraction_errors=%s)" % (
                counters["screened"],
                len(jobdiva_candidates),
                counters["skipped"],
                counters["no_resume"],
                counters["failed_filter"],
                counters["failed_location"],
                counters["failed_location_geocode"],
                counters["llm_extraction_errors"],
            ),
        )
        self._log_stage(
            "LLMGate",
            "pre-LLM skips: low_score=%s no_required_hit=%s min_years=%s" % (
                counters["pre_llm_skipped_low_score"],
                counters["pre_llm_skipped_no_required_hit"],
                counters["pre_llm_skipped_min_years"],
            ),
        )

    async def _enrich_filtered_jobdiva_progressive(
        self,
        candidates: List[Dict[str, Any]],
        criteria: SearchCriteria,
        skip_llm: bool = False,
    ):
        """Progressive variant of :py:meth:`_enrich_filtered_jobdiva_candidates`.

        Instead of buffering enrichment + scoring per candidate and yielding the
        finished record at the end, this yields multiple events per candidate so
        the UI can paint rows with shimmer placeholders that fill in as data
        lands:

        - ``{"type": "candidate_detail", "candidate_id", "stage": "jobdiva_details",
          "patch": {...}}`` once the resume + JobDiva profile fields land.
        - ``{"type": "candidate_enriched", "candidate": cand}`` (internal) after
          LLM extraction; the caller scores + dedups + emits the ``scored``
          stage patch so cross-source dedup state stays in one place.

        Policy: this enricher never drops a JobDiva candidate. Gate failures
        (no_resume / failed_filter / failed_location / below_min_years / error)
        skip the LLM step but still emit ``candidate_enriched`` so the caller
        scores + shows the row — hard-filter-fails surface at 0% and are
        excluded only at Launch PAIR, never hidden on Step 5. The only row
        removal left is cross-source dedup, handled in ``emit_jobdiva_scored``.

        ``skip_llm=True`` (JobDiva-JobAgent high-level scoring): the résumé +
        profile fields are still fetched and streamed, but the per-candidate
        LLM extraction is skipped for EVERY candidate — JobDiva's recruiter-
        configured agent already vetted them, so they get the cheap
        deterministic score only. Rows are tagged ``scoring_mode="high_level"``
        so the UI can label the score "JobDiva agent search".
        """
        from services.sourced_candidates_storage import process_jobdiva_candidate

        jobdiva_candidates = [
            candidate for candidate in candidates
            if str(candidate.get("source", "")).startswith("JobDiva")
        ]
        self._log_stage(
            "ResumeScreen",
            f"checking {len(jobdiva_candidates)} JobDiva candidate resume(s) before LLM (progressive)",
        )

        out_queue: asyncio.Queue = asyncio.Queue()
        SENTINEL = object()
        from core import sourcing_config as _sc_conc
        # Outer width: how many candidates are in enrichment flight at once.
        # The JobDiva-facing resume fetch keeps its own tighter bound below so
        # widening the LLM fan-out can't burst JobDiva's rate limiter.
        semaphore = asyncio.Semaphore(
            max(1, int(getattr(_sc_conc, "JOBDIVA_ENRICH_CONCURRENCY", 12) or 12))
        )
        resume_fetch_semaphore = asyncio.Semaphore(
            max(1, int(getattr(_sc_conc, "JOBDIVA_RESUME_FETCH_CONCURRENCY", 5) or 5))
        )
        counters = {
            "screened": 0,
            "skipped": 0,
            "no_resume": 0,
            "failed_filter": 0,
            "failed_location": 0,
            "failed_location_geocode": 0,
            "llm_extraction_errors": 0,
            "pre_llm_skipped_low_score": 0,
            "pre_llm_skipped_no_required_hit": 0,
            "pre_llm_skipped_min_years": 0,
            "no_contact": 0,
        }

        async def _process(candidate: Dict[str, Any]):
            async with semaphore:
                cid = str(candidate.get("candidate_id") or candidate.get("id") or "")
                if not cid:
                    return
                if skip_llm:
                    # Tag before any keep-path so even no-resume/error rows
                    # carry the provenance label for the UI popup.
                    candidate["scoring_mode"] = "high_level"

                async def _keep(status: str, *, detail_failed: bool = False):
                    # Policy: JobDiva (agentsearch) candidates are never removed
                    # from Step 5. Instead of dropping on a gate failure, skip
                    # the expensive LLM step (these score low/0 anyway via the
                    # hard-veto path) and emit so the caller scores + shows them.
                    # 0% hard-filter-fails are excluded only at Launch PAIR.
                    #
                    # detail_failed=True marks a *candidate-details* failure (no
                    # résumé / detail-API error) — not a genuine policy fail. Those
                    # are surfaced as "N/A" (no score) instead of a misleading 0%
                    # and stay launchable; see finalize_candidate.
                    candidate["enhanced_info"] = candidate.get("enhanced_info") or {}
                    candidate["enhanced_info_status"] = status
                    if detail_failed:
                        candidate["detail_failed"] = True
                    counters["screened"] += 1
                    await out_queue.put({
                        "type": "candidate_enriched",
                        "candidate": candidate,
                    })

                try:
                    pre_enriched = bool(candidate.get("enhanced_info"))

                    # ── Stage 2: resume + JobDiva profile ──────────────────
                    resume_text = candidate.get("resume_text") or ""
                    if not resume_text or "Resume content unavailable" in resume_text:
                        self._log_stage("ResumeScreen", f"fetching resume for candidate_id={cid}")
                        async with resume_fetch_semaphore:
                            resume_data = await self.jobdiva_service.get_candidate_resume(
                                cid,
                                resume_id=candidate.get("resume_id"),
                            )
                        rt = (resume_data or {}).get("resume_text", "")
                        if rt and "Resume content unavailable" not in rt:
                            candidate["resume_text"] = rt
                            candidate["resume_id"] = (resume_data or {}).get("resume_id") or candidate.get("resume_id")
                            candidate["email"] = candidate.get("email") or (resume_data or {}).get("email")
                            candidate["phone"] = candidate.get("phone") or (resume_data or {}).get("phone")
                            candidate["title"] = candidate.get("title") or (resume_data or {}).get("title")
                            candidate["location"] = candidate.get("location") or (resume_data or {}).get("location")
                            self._log_stage(
                                "ResumeScreen",
                                f"successfully fetched resume for candidate_id={cid} ({len(rt)} chars)",
                            )

                    if not candidate.get("resume_text") and not pre_enriched:
                        self._log_stage("ResumeScreen", f"kept candidate_id={cid}; no resume text available (N/A — detail lookup failed)")
                        counters["no_resume"] += 1
                        await _keep("kept_no_resume", detail_failed=True)
                        return

                    # No-contact check now that resume/profile fields (and any
                    # cached enhanced_info with company history, attached by
                    # _attach_cached_enhanced_info) are on the row, so the
                    # details patch below streams the flag with this paint.
                    # Fresh JobDiva rows usually carry no employer signal yet;
                    # those get caught post-LLM at the finalize_candidate
                    # choke-point instead.
                    apply_no_contact_flag(candidate)

                    # Emit the jobdiva_details patch as soon as resume + profile
                    # fields are in hand. UI clears the shimmer on these cells.
                    details_patch: Dict[str, Any] = {"_stage": "details_loaded"}
                    for k in (
                        "resume_text", "resume_id", "email", "phone",
                        "title", "location", "city", "state", "zipcode",
                        "experience_years", "headline",
                        "qualifications", "employee_status", "available",
                        "availability_status", "current_company",
                        "no_contact", "no_contact_reason", "no_contact_company",
                    ):
                        v = candidate.get(k)
                        if v not in (None, "", [], {}):
                            details_patch[k] = v
                    await out_queue.put({
                        "type": "candidate_detail",
                        "candidate_id": cid,
                        "stage": "jobdiva_details",
                        "patch": details_patch,
                    })

                    # No-contact company: everything the row needs for its
                    # greyed-out display is in hand — stop here. No LLM
                    # extraction (which is also the enhanced-info persistence
                    # path), no pre-LLM scoring spend; finalize_candidate
                    # leaves the row unscored.
                    if candidate.get("no_contact"):
                        counters["no_contact"] += 1
                        self._log_stage(
                            "NoContact",
                            f"candidate_id={cid} kept display-only: "
                            f"{candidate.get('no_contact_reason')} — "
                            "skipping LLM + persistence",
                        )
                        await _keep("no_contact")
                        return

                    # High-level scoring (JobDiva-JobAgent): résumé + profile
                    # fields are in hand — stop here. No pre-LLM gates, no LLM
                    # extraction; the caller scores on the cheap deterministic
                    # signals (rank floor keeps JobDiva's ordering honored).
                    if skip_llm:
                        # Ground the Skills dimension (45% of the score) and its
                        # exclusions in the résumé we just fetched. Without this
                        # the only skills on the row are whatever
                        # `_extract_candidate_skills` produced — JobDiva's own
                        # field when it exists, else a guess from the job title,
                        # else the literal ["Communication", "Problem Solving"]
                        # placeholder. The LLM pass that normally overwrites
                        # those is exactly what we're skipping here, so scoring
                        # and "must not have X" would otherwise be judged
                        # against fiction. Literal scan — no LLM, no extra I/O.
                        evidenced = self._skills_evidenced_in_text(
                            candidate.get("resume_text") or "", criteria
                        )
                        if evidenced:
                            candidate["skills"] = evidenced
                        await _keep("high_level")
                        return

                    # PR-B: cheap pre-LLM YOE gate.
                    if self._candidate_outside_years_range_pre_llm(candidate, criteria):
                        counters["pre_llm_skipped_min_years"] += 1
                        self._log_stage(
                            "LLMGate",
                            "skipping LLM for candidate_id=%s reason=outside_years_range_pre_llm threshold=%s (kept, scored)" % (
                                cid,
                                int(criteria.min_experience_years or 0),
                            ),
                        )
                        await _keep("kept_min_years")
                        return

                    if criteria.bypass_screening and not self._has_experience_years_range(criteria):
                        self._log_stage(
                            "ResumeScreen",
                            f"Bypassing LLM extraction for candidate_id={cid} (auto-sync mode)",
                        )
                        candidate["enhanced_info"] = candidate.get("enhanced_info") or {}
                        candidate["enhanced_info_status"] = "skipped"
                        counters["screened"] += 1
                        await out_queue.put({
                            "type": "candidate_enriched",
                            "candidate": candidate,
                        })
                        return

                    self._log_stage("ResumeScreen", f"running quick filter for candidate_id={cid}")
                    assessment = self._filter_assessment(candidate, criteria, enforce_years=False)
                    if not assessment["passes"]:
                        location_reason = assessment.get("location_failure_reason")
                        self._log_stage(
                            "ResumeScreen",
                            "FAILED FILTER (kept, scored) candidate_id=%s matched=%s missing=%s excluded=%s location_reason=%s" % (
                                cid,
                                assessment["matched"][:5],
                                assessment["missing"][:5],
                                assessment["excluded"][:5],
                                location_reason,
                            ),
                        )
                        # JobDiva candidates are never dropped here. Skip the LLM
                        # step (hard-filter / location fails score low/0 anyway
                        # via the hard-veto path) but keep + score them so the
                        # row stays visible. The 0% hard-filter-fails (e.g. last
                        # company == the company asked for) are excluded only at
                        # Launch PAIR, never hidden on Step 5.
                        if location_reason:
                            if location_reason in {"candidate_ungeocodable", "target_ungeocodable"}:
                                counters["failed_location_geocode"] += 1
                                counters["failed_location"] += 1
                                await _keep("kept_location_geocode")
                                return
                            counters["failed_location"] += 1
                            await _keep("kept_location")
                            return
                        counters["failed_filter"] += 1
                        await _keep("kept_hard_filter")
                        return

                    self._log_stage(
                        "ResumeScreen",
                        "PASSED FILTER candidate_id=%s matched=%s - proceeding to LLM" % (
                            cid,
                            assessment["matched"][:5],
                        ),
                    )

                    # Pre-LLM cost gate.
                    pre_score_result = self._score_candidate(candidate, criteria)
                    pre_score = float(pre_score_result.get("score") or 0)
                    pre_score_details = pre_score_result.get("score_details") or {}
                    has_required_hit = any(
                        isinstance(dim, dict)
                        and int(dim.get("required_total") or 0) > 0
                        and int(dim.get("required_matched") or 0) > 0
                        for dim in pre_score_details.values()
                    )

                    skip_reason = None
                    if pre_score < 25:
                        skip_reason = "low_score"
                        counters["pre_llm_skipped_low_score"] += 1
                    elif pre_score < 40 and not has_required_hit:
                        skip_reason = "no_required_hit"
                        counters["pre_llm_skipped_no_required_hit"] += 1

                    if skip_reason:
                        self._log_stage(
                            "LLMGate",
                            "skipping LLM for candidate_id=%s pre_score=%.1f reason=%s required_hit=%s" % (
                                cid,
                                pre_score,
                                skip_reason,
                                has_required_hit,
                            ),
                        )
                        candidate["enhanced_info"] = candidate.get("enhanced_info") or {}
                        candidate["enhanced_info_status"] = "skipped_pre_llm_gate"
                        counters["screened"] += 1
                        await out_queue.put({
                            "type": "candidate_enriched",
                            "candidate": candidate,
                        })
                        return

                    self._log_stage(
                        "LLM",
                        f"STARTING LLM extraction for candidate_id={cid}, resume_id={candidate.get('resume_id') or 'unknown'}",
                    )

                    enhanced = await process_jobdiva_candidate(candidate)
                    extraction_error = (
                        enhanced.get("_extraction_error")
                        if isinstance(enhanced, dict) else None
                    )
                    # process_jobdiva_candidate returns skipped=True when the
                    # résumé is missing or a synthetic placeholder ("available
                    # upon request" etc., caught by _has_real_resume_text) — i.e.
                    # the candidate-details API gave us nothing real to extract.
                    extraction_skipped = (
                        bool(enhanced.get("skipped")) if isinstance(enhanced, dict) else False
                    )
                    if extraction_error:
                        logger.warning(
                            "LLM extraction degraded for candidate_id=%s (%s); marking N/A (detail failed)",
                            cid, extraction_error,
                        )
                        counters["llm_extraction_errors"] += 1
                    if isinstance(enhanced, dict) and enhanced is not candidate:
                        candidate["enhanced_info"] = enhanced.get("raw", enhanced)
                    else:
                        candidate["enhanced_info"] = {}
                    if extraction_error and isinstance(candidate.get("enhanced_info"), dict):
                        candidate["enhanced_info"]["_extraction_error"] = extraction_error

                    # Candidate-details failure: a skipped/placeholder résumé or a
                    # degraded LLM extraction means there's no real data to score.
                    # Flag so the row shows "N/A" (not a misleading 0% / floored
                    # score) and stays launchable instead of being dropped.
                    if extraction_skipped or extraction_error:
                        candidate["detail_failed"] = True

                    candidate["enhanced_info_status"] = "completed"
                    candidate["name"] = candidate["enhanced_info"].get("candidate_name") or candidate.get("name")
                    candidate["email"] = candidate["enhanced_info"].get("email") or candidate.get("email")
                    candidate["phone"] = candidate["enhanced_info"].get("phone") or candidate.get("phone")
                    candidate["title"] = candidate["enhanced_info"].get("job_title") or candidate.get("title")
                    # Résumé is final for residence (see _apply_resume_location).
                    self._apply_resume_location(candidate)
                    candidate["education"] = candidate["enhanced_info"].get("candidate_education", [])
                    candidate["certifications"] = candidate["enhanced_info"].get("candidate_certification", [])
                    candidate["urls"] = candidate["enhanced_info"].get("urls", {})
                    candidate["experience_years"] = candidate["enhanced_info"].get("years_of_experience") or candidate.get("experience_years")
                    if candidate["enhanced_info"].get("structured_skills") or candidate["enhanced_info"].get("skills"):
                        candidate["skills"] = candidate["enhanced_info"].get("structured_skills") or candidate["enhanced_info"].get("skills")

                    self._log_stage("LLM", f"COMPLETED LLM extraction for candidate_id={cid}")
                    counters["screened"] += 1
                    await out_queue.put({
                        "type": "candidate_enriched",
                        "candidate": candidate,
                    })
                except Exception as e:
                    logger.error(
                        f"❌ Progressive JobDiva enrichment FAILED for {cid}: {e}; keeping candidate (N/A — detail lookup failed)",
                        exc_info=True,
                    )
                    counters["llm_extraction_errors"] += 1
                    await _keep("error", detail_failed=True)

        tasks = [asyncio.create_task(_process(c)) for c in jobdiva_candidates]

        async def _signal_done():
            await asyncio.gather(*tasks, return_exceptions=True)
            await out_queue.put(SENTINEL)

        signal_task = asyncio.create_task(_signal_done())

        try:
            while True:
                ev = await out_queue.get()
                if ev is SENTINEL:
                    break
                yield ev
        finally:
            # Caller closed the generator (client disconnect / abort). Cancel
            # outstanding per-candidate tasks so we don't leak work.
            for t in tasks:
                if not t.done():
                    t.cancel()
            if not signal_task.done():
                signal_task.cancel()
            await asyncio.gather(signal_task, *tasks, return_exceptions=True)

        self._log_stage(
            "ResumeScreen",
            "RESULTS (progressive): kept %s of %s JobDiva candidate(s); none dropped — kept-without-LLM (no_resume=%s, failed_filter=%s, failed_location=%s, geocode_failures=%s, llm_extraction_errors=%s)" % (
                counters["screened"],
                len(jobdiva_candidates),
                counters["no_resume"],
                counters["failed_filter"],
                counters["failed_location"],
                counters["failed_location_geocode"],
                counters["llm_extraction_errors"],
            ),
        )
        self._log_stage(
            "LLMGate",
            "pre-LLM skips: low_score=%s no_required_hit=%s min_years=%s" % (
                counters["pre_llm_skipped_low_score"],
                counters["pre_llm_skipped_no_required_hit"],
                counters["pre_llm_skipped_min_years"],
            ),
        )

    async def _enrich_linkedin_candidates(self, candidates: List[Dict[str, Any]], criteria: SearchCriteria) -> int:
        """Enrich LinkedIn candidates with LLM extraction and save to candidate_enhanced_info."""
        from services.sourced_candidates_storage import process_linkedin_candidate

        enriched_count = 0
        linkedin_candidates = [
            candidate for candidate in candidates
            if str(candidate.get("source", "")) == "LinkedIn"
        ]
        self._log_stage("LinkedIn Enrichment", f"processing {len(linkedin_candidates)} LinkedIn candidate(s)")

        for index, candidate in enumerate(linkedin_candidates, 1):
            candidate_id = str(candidate.get("candidate_id") or candidate.get("id") or "")
            if not candidate_id:
                self._log_stage("LinkedIn Enrichment", f"skipped candidate at index {index}; no candidate_id")
                continue

            # Skip if already enriched
            if candidate.get("enhanced_info"):
                self._log_stage("LinkedIn Enrichment", f"candidate_id={candidate_id} already has enhanced_info, skipping")
                enriched_count += 1
                continue

            try:
                self._log_stage(
                    "LLM",
                    "STARTING LLM extraction for LinkedIn candidate %s of %s (candidate_id=%s)" % (
                        enriched_count + 1,
                        len(linkedin_candidates),
                        candidate_id,
                    ),
                )
                
                enhanced = await process_linkedin_candidate(candidate)
                candidate["enhanced_info"] = enhanced.get("raw", enhanced) if isinstance(enhanced, dict) else {}
                candidate["enhanced_info_status"] = "completed"
                candidate["name"] = candidate["enhanced_info"].get("candidate_name") or candidate.get("name")
                candidate["email"] = candidate["enhanced_info"].get("email") or candidate.get("email")
                candidate["phone"] = candidate["enhanced_info"].get("phone") or candidate.get("phone")
                candidate["title"] = candidate["enhanced_info"].get("job_title") or candidate.get("title")
                # Résumé/profile-text location is final when explicitly
                # stated (see _apply_resume_location); the LinkedIn area
                # stands only when the extraction found none. Both sides
                # sanitized: LinkedIn areas can literally read "Remote".
                self._apply_resume_location(candidate)
                candidate["education"] = candidate["enhanced_info"].get("candidate_education", [])
                candidate["certifications"] = candidate["enhanced_info"].get("candidate_certification", [])
                candidate["urls"] = candidate["enhanced_info"].get("urls", {})
                candidate["experience_years"] = candidate["enhanced_info"].get("years_of_experience") or candidate.get("experience_years")
                if candidate["enhanced_info"].get("structured_skills") or candidate["enhanced_info"].get("skills"):
                    candidate["skills"] = candidate["enhanced_info"].get("structured_skills") or candidate["enhanced_info"].get("skills")
                
                enriched_count += 1
                self._log_stage("LLM", f"COMPLETED LLM extraction for LinkedIn candidate_id={candidate_id}, enriched_count={enriched_count}")
            except Exception as e:
                logger.error(f"❌ LinkedIn enhancement FAILED for {candidate_id}: {e}", exc_info=True)

        self._log_stage("LinkedIn Enrichment", f"completed {enriched_count} LinkedIn candidate(s)")
        return enriched_count

    def _attach_cached_enhanced_info(self, candidates: List[Dict[str, Any]]) -> None:
        candidate_ids = [str(c.get("candidate_id") or c.get("id")) for c in candidates if c.get("candidate_id") or c.get("id")]
        if not candidate_ids:
            return

        try:
            import psycopg2.extras
            from core.db import get_db_connection

            with get_db_connection() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute("""
                        SELECT candidate_id, candidate_name, email, phone, job_title,
                               years_of_experience, current_location, key_skills,
                               company_experience, candidate_education,
                               candidate_certification, urls, resume_extraction_status
                        FROM candidate_enhanced_info
                        WHERE candidate_id = ANY(%s)
                    """, (candidate_ids,))
                    rows = {str(row["candidate_id"]): dict(row) for row in cur.fetchall()}

            for candidate in candidates:
                candidate_id = str(candidate.get("candidate_id") or candidate.get("id"))
                enhanced = rows.get(candidate_id)
                if not enhanced:
                    continue
                # Sanitize the cached value in the dict itself — this blob
                # rides to the UI as enhanced_info, and rows written before
                # the work-arrangement guard can carry "Remote"/"Hybrid".
                enhanced["current_location"] = sanitize_candidate_location(
                    enhanced.get("current_location")
                )
                candidate["enhanced_info"] = enhanced
                candidate["enhanced_info_status"] = enhanced.get("resume_extraction_status") or "cached"
                candidate["name"] = enhanced.get("candidate_name") or candidate.get("name")
                candidate["email"] = enhanced.get("email") or candidate.get("email")
                candidate["phone"] = enhanced.get("phone") or candidate.get("phone")
                candidate["title"] = enhanced.get("job_title") or candidate.get("title")
                # The cached current_location was LLM-extracted from the resume
                # on some PRIOR run (any job/source) — it must never clobber the
                # live source-native location. This exact overwrite put a stale
                # "Hyderabad, India" on a JobAgent row whose JobDiva record
                # said "Ajax, ON" (Job 26-22448).
                candidate["location"] = (
                    sanitize_candidate_location(candidate.get("location"))
                    or enhanced.get("current_location")
                )
                candidate["education"] = enhanced.get("candidate_education", [])
                candidate["certifications"] = enhanced.get("candidate_certification", [])
                candidate["urls"] = enhanced.get("urls", {})
                candidate["experience_years"] = enhanced.get("years_of_experience") or candidate.get("experience_years")
                if enhanced.get("key_skills"):
                    candidate["skills"] = enhanced.get("key_skills")

            logger.info(f"📦 Attached cached enhanced info for {len(rows)} candidates")
        except Exception as e:
            logger.debug(f"Cached enhanced-info lookup skipped: {e}")

    def _drop_client_employees(
        self,
        candidates: List[Dict[str, Any]],
        criteria: SearchCriteria,
        source_label: str,
    ) -> List[Dict[str, Any]]:
        """Search-time hard filter for EXTERNAL sources (Exa/Unipile): drop
        rows whose CURRENT company is the hiring client (criteria.client_name).

        We can never submit a client's own employees, so external-source rows
        are filtered before they ever reach Step 5 — the query side can't
        express the negation (Exa's neural search would be ATTRACTED by the
        company name; Unipile keyword NOT would over-exclude past employees).
        JobDiva rows are deliberately NOT touched here (Step-5 policy: JobDiva
        candidates are never dropped) — they're caught by the launch gate.
        """
        client_name = str(getattr(criteria, "client_name", "") or "")
        if not candidates or not client_name:
            return candidates
        try:
            from services.company_match import (
                currently_employed_by_client,
                is_placeholder_client,
            )
            if is_placeholder_client(client_name):
                return candidates
            kept: List[Dict[str, Any]] = []
            dropped: List[str] = []
            for cand in candidates:
                match = currently_employed_by_client(cand, client_name)
                if match:
                    dropped.append(f"{cand.get('name') or cand.get('id')} ({match})")
                else:
                    kept.append(cand)
            if dropped:
                self._log_stage(
                    source_label,
                    f"Dropped {len(dropped)} candidate(s) currently employed by "
                    f"hiring client {client_name!r}: {', '.join(dropped[:10])}"
                    f"{' …' if len(dropped) > 10 else ''}",
                )
            return kept
        except Exception as exc:
            logger.warning(f"client-employee filter skipped ({source_label}): {exc}")
            return candidates
