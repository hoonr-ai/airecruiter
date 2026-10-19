"""UnifiedCandidateSearch methods: jobdiva talent."""
import asyncio
import time
from typing import List, Dict, Any, Optional
from ..common import (
    LocationEntry,
    SearchCriteria,
    _MAX_LOCATIONS,
    logger,
)


class JobdivaTalentMixin:


    async def _search_jobdiva_talent(
        self,
        criteria: SearchCriteria,
        resume_count_override: Optional[int] = None,
    ) -> Dict[str, Any]:
        """JobDiva talent-pool sourcing via JobAgentSearch.

        JobAgentSearch (JobDiva's AI matcher) is anchored to the job's JobDiva
        ID and returns a per-job ranked candidate set. We then apply a
        client-side state filter to backstop the geo precision JobDiva does
        not give us.

        ``resume_count_override``: request exactly this many resumes instead
        of the offset+batch tranche math — the quick-first phase uses a small
        value here so JobAgentSearch returns in seconds (latency scales with
        resumeCount).

        Surfaces `criteria_unconfigured: True` in the return when JobAgent
        responded with "Criteria Not Assigned" — frontend uses this to nudge
        the recruiter to set search criteria in JobDiva's web UI.
        """
        source_type = "JobDiva-JobAgent"
        try:
            candidates: List[Dict[str, Any]] = []
            criteria_unconfigured = False

            if criteria.job_id:
                # resumeCount drives JobAgentSearch latency. JobAgent has no
                # offset, so Search-more requests offset+batch candidates and
                # slices off the already-shown ranks locally.
                from core import sourcing_config as _sc_rc
                batch_size = max(1, int(getattr(criteria, "jobdiva_batch_size", 150) or 150))
                offset = max(0, int(getattr(criteria, "jobdiva_offset", 0) or 0))
                max_resume_count = max(
                    batch_size,
                    int(getattr(_sc_rc, "JOBAGENT_MAX_RESUME_COUNT", _sc_rc.JOBAGENT_RESUME_COUNT) or batch_size),
                )
                if resume_count_override:
                    resume_count = min(max_resume_count, max(1, int(resume_count_override)))
                else:
                    resume_count = min(max_resume_count, offset + batch_size)
                # Wall-clock as the orchestrator sees it. Comparing this to the
                # service's "JobAgent TIMING total_ms" reveals event-loop
                # contention: if this is much larger, the coroutine was starved
                # by concurrent producers/scoring, not by JobDiva itself.
                _ja_wall_t0 = time.perf_counter()
                # Read-through cache (Fix 7): filled by the job-intake prewarm
                # or an earlier search; invalidated on criteria edits.
                from services import jobdiva_jobagent_cache as _ja_cache
                _req_resume = getattr(criteria, "require_resume", True)
                ja_result = await _ja_cache.get(
                    criteria.job_id, resume_count=resume_count, require_resume=_req_resume,
                )
                if ja_result is None:
                    ja_result = await self.jobdiva_service.search_via_job_agent(
                        job_id=criteria.job_id,
                        resume_count=resume_count,
                        require_resume=_req_resume,
                    )
                    await _ja_cache.put(
                        criteria.job_id, ja_result,
                        resume_count=resume_count, require_resume=_req_resume,
                    )
                else:
                    self._log_stage("TalentSearch", f"JobAgent cache hit (resumeCount={resume_count})")
                self._log_stage(
                    "TalentSearch",
                    f"JobAgent orchestrator wall-clock="
                    f"{(time.perf_counter() - _ja_wall_t0) * 1000.0:.0f}ms",
                )
                # Back-compat: tolerate either list or dict shape.
                if isinstance(ja_result, dict):
                    candidates = ja_result.get("candidates") or []
                    criteria_unconfigured = bool(ja_result.get("criteria_unconfigured"))
                else:
                    candidates = list(ja_result or [])
                self._log_stage(
                    "TalentSearch",
                    f"JobAgent jobId={criteria.job_id} resume_count={resume_count} "
                    f"raw={len(candidates)} offset={offset} batch={batch_size} "
                    f"criteria_unconfigured={criteria_unconfigured}"
                )
                if resume_count_override:
                    candidates = candidates[: max(1, int(resume_count_override))]
                elif offset:
                    candidates = candidates[offset : offset + batch_size]
                else:
                    candidates = candidates[:batch_size]
                self._log_stage(
                    "TalentSearch",
                    f"JobAgent tranche offset={offset} batch={batch_size} "
                    f"override={resume_count_override or 0} kept={len(candidates)}"
                )

            if not candidates:
                return {
                    "candidates": [],
                    "source_type": source_type,
                    "jobdiva_criteria_unconfigured": criteria_unconfigured,
                }

            before = len(candidates)
            candidates = self._filter_by_state(candidates, criteria)
            after_state = len(candidates)
            if after_state != before:
                self._log_stage(
                    "TalentSearch",
                    f"State filter: {before} → {after_state} (dropped {before - after_state})"
                )

            # Preserve JobAgent's API rank order end-to-end. Each candidate
            # carries `api_rank` (0-based position in JobDiva's response);
            # the frontend renders sorted by api_rank so the UI matches the
            # order JobDiva returned, even when LLM scoring later assigns
            # different match_score values.

            token = await self.jobdiva_service.authenticate()
            if token and candidates:
                cids = [str(c.get("candidate_id") or c.get("id") or "") for c in candidates]
                quals_map = await self.jobdiva_service._fetch_candidate_qualifications_batch(token, cids)
                for c in candidates:
                    cid = str(c.get("candidate_id") or c.get("id") or "")
                    quals = quals_map.get(cid, [])
                    if quals:
                        c["qualifications"] = quals
                        for q in quals:
                            if not isinstance(q, dict): continue
                            qval = str(q.get("QUALIFICATIONVALUE") or q.get("qualificationValue") or "").strip()
                            qname = str(q.get("QUALIFICATION") or q.get("qualificationName") or "").strip()
                            if "current employee" in qval.lower() or "current employee" in qname.lower():
                                c["employee_status"] = "Current Employee"

            for c in candidates:
                c.setdefault("source", source_type)

            self._log_stage(
                "TalentSearch",
                f"Proceeding to LLM extraction for {len(candidates)} candidate(s) from {source_type}"
            )
            return {
                "candidates": candidates,
                "source_type": source_type,
                "jobdiva_criteria_unconfigured": criteria_unconfigured,
            }
        except Exception as e:
            logger.error(f"JobDiva talent-pool search failed: {e}")
            return {
                "candidates": [],
                "source_type": "JobDiva-JobAgent",
                "jobdiva_criteria_unconfigured": False,
            }

    async def _search_jobdiva_talent_search(self, criteria: SearchCriteria) -> Dict[str, Any]:
        """Legacy JobDiva TalentSearch boolean path kept as a separate source."""
        source_type = "JobDiva-TalentSearch"
        try:
            from core import sourcing_config as sc
            countries, states, geo_zip = self._resolve_jobdiva_geo(criteria)
            # Remote jobs have no commute constraint — search US-wide
            # instead of anchoring to the office zip/state.
            if str(getattr(criteria, "location_type", "") or "").strip().lower() == "remote":
                geo_zip = ""
                states = []
            batch_size = max(1, int(getattr(criteria, "jobdiva_batch_size", 150) or 150))
            offset = max(0, int(getattr(criteria, "jobdiva_offset", 0) or 0))
            page_size = max(
                1,
                int(getattr(sc, "JOBDIVA_TALENTSEARCH_PAGE_SIZE", 150) or 150),
            )
            max_total = max(
                batch_size,
                int(getattr(sc, "JOBDIVA_TALENTSEARCH_MAX_TOTAL_COUNT", page_size * 2) or batch_size),
            )
            source_cap = getattr(sc, "JOBDIVA_SOURCE_CAP", None)
            if source_cap:
                max_total = min(max_total, int(source_cap))
            if offset >= max_total:
                return {"candidates": [], "source_type": source_type}

            # The v2 TalentSearch contract ignores pageNumber/pageSize and
            # returns the full filtered set in one response (live probe
            # 2026-07-19) — the old page loop just re-fetched identical rows.
            # One fetch capped at max_total; progressive batching slices it.
            # Titles: primary chip + its recruiter-approved similar titles,
            # one titleSearch pull per variant (bounded in jobdiva service).
            title_pull_variants = criteria.title_variants(
                int(getattr(sc, "JOBDIVA_TALENT_TITLE_PULL_MAX_TITLES", 3) or 3)
            )
            # JobDiva is the one consumer that speaks the native dialect, so an
            # auto-built string is built as `jobdiva` here (roles in TITLES=,
            # `IN {US}` for a remote role). The frontend's string still wins when
            # present — it is what the recruiter sees and may have hand-edited.
            all_candidates = await self.jobdiva_service.search_candidates(
                skills=list(criteria.skill_criteria or []),
                location=criteria.location or "",
                page=1,
                limit=max_total,
                job_id=None,
                boolean_string=(
                    criteria.boolean_string
                    or self._build_boolean_string(criteria, dialect="jobdiva")
                ),
                recent_days=getattr(criteria, "recent_days", None),
                require_resume=getattr(criteria, "require_resume", True),
                countries=countries,
                states=states,
                page_number=0,
                zip_code=geo_zip,
                within_miles=getattr(criteria, "within_miles", 25),
                titles=title_pull_variants,
            )

            # Multi-location: also search each additional location and merge.
            # Cap to _MAX_LOCATIONS entries to prevent unbounded fan-out.
            additional_locs = criteria.additional_locations[:_MAX_LOCATIONS]

            async def _search_one_loc(
                loc_entry: LocationEntry,
            ) -> tuple[List[Dict[str, Any]], bool]:
                """Run a single-location TalentSearch; returns (candidates, ok)."""
                loc_value = str(loc_entry.value or "").strip()
                loc_miles = max(1, min(100, int(loc_entry.within_miles)))
                alt_criteria = criteria.model_copy(update={
                    "location": loc_value,
                    "within_miles": loc_miles,
                    "additional_locations": [],
                })
                # Build a per-location boolean WITHOUT the multi-location OR
                # clause so the text filter doesn't conflict with the geo radius.
                loc_boolean = (
                    criteria.boolean_string
                    or self._build_boolean_string(alt_criteria, dialect="jobdiva")
                )
                alt_countries, alt_states, alt_geo_zip = self._resolve_jobdiva_geo(alt_criteria)
                if str(getattr(criteria, "location_type", "") or "").strip().lower() == "remote":
                    alt_geo_zip = ""
                    alt_states = []
                try:
                    cands = await self.jobdiva_service.search_candidates(
                        skills=list(criteria.skill_criteria or []),
                        location=loc_value,
                        page=1,
                        limit=max_total,
                        job_id=None,
                        boolean_string=loc_boolean,
                        recent_days=criteria.recent_days,
                        require_resume=getattr(criteria, "require_resume", True),
                        countries=alt_countries,
                        states=alt_states,
                        page_number=0,
                        zip_code=alt_geo_zip,
                        within_miles=loc_miles,
                        titles=title_pull_variants,
                    )
                    self._log_stage(
                        "TalentSearch",
                        f"Multi-location TalentSearch for '{loc_value}' returned "
                        f"{len(cands)} candidate(s)",
                    )
                    return cands, True
                except Exception as loc_err:
                    self._log_stage(
                        "TalentSearch",
                        f"Multi-location search failed for '{loc_value}': {loc_err}",
                    )
                    return [], False

            if additional_locs:
                # Bounded concurrency: at most 3 parallel location calls.
                _loc_sem = asyncio.Semaphore(3)

                async def _guarded(loc: LocationEntry):
                    async with _loc_sem:
                        return await _search_one_loc(loc)

                loc_results = await asyncio.gather(
                    *[_guarded(loc) for loc in additional_locs]
                )
                partial_failures = 0
                for loc_cands, ok in loc_results:
                    all_candidates.extend(loc_cands)
                    if not ok:
                        partial_failures += 1
                if partial_failures:
                    self._log_stage(
                        "TalentSearch",
                        f"{partial_failures}/{len(additional_locs)} additional-location "
                        "searches failed (partial results returned)",
                    )

            self._log_stage(
                "TalentSearch",
                f"TalentSearch returned {len(all_candidates)} candidate(s) "
                f"(offset={offset} batch={batch_size})",
            )

            candidates: List[Dict[str, Any]] = []
            seen_ids = set()
            for cand in all_candidates:
                cid = str(cand.get("candidate_id") or cand.get("id") or "").strip()
                dedupe_key = cid or f"{cand.get('email') or ''}:{cand.get('name') or ''}".lower()
                if dedupe_key in seen_ids:
                    continue
                seen_ids.add(dedupe_key)
                candidates.append(cand)
            candidates = candidates[offset:offset + batch_size]

            if not candidates:
                return {"candidates": [], "source_type": source_type}

            before = len(candidates)
            candidates = self._filter_by_state(candidates, criteria)
            after_state = len(candidates)
            if after_state != before:
                self._log_stage(
                    "TalentSearch",
                    f"State filter: {before} → {after_state} (dropped {before - after_state})",
                )

            for idx, c in enumerate(candidates):
                c.setdefault("source", source_type)
                c.setdefault("api_rank", idx + 1)

            self._log_stage(
                "TalentSearch",
                f"Proceeding to LLM extraction for {len(candidates)} candidate(s) from {source_type}",
            )
            return {"candidates": candidates, "source_type": source_type}
        except Exception as e:
            logger.error(f"JobDiva TalentSearch failed: {e}")
            return {"candidates": [], "source_type": source_type}
