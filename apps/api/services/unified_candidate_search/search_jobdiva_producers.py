"""Closures of UnifiedCandidateSearch._search_candidates_impl, moved out verbatim.

Each takes the search run's shared state (the names it used to close over)
as keyword arguments; the method keeps a thin wrapper that passes them.
"""
import asyncio
from typing import List, Dict, Any, Optional, Tuple
from .common import (
    _traced,
    logger,
)




async def produce_jobdiva_applicants(*, SENTINEL, applicants_selected, criteria, emit_candidate, emit_jobdiva_agent_result, emit_jobdiva_scored, emit_source_status, queue, self, summary):
    """
            Fetch every candidate who has applied to this job_id in JobDiva
            (no boolean string). Emitted under source=JobDiva-Applicants.
            Skipped for external jobs (negative job_id / EXT-), which have
            no JobDiva applicants.
            """
    _ap_emitted = 0
    _ap_raw = 0
    _ap_error: Optional[BaseException] = None
    _ap_ran = False
    try:
        if not applicants_selected:
            return
        job_id_str = str(criteria.job_id or "")
        is_external_job = job_id_str.startswith("-") or job_id_str.startswith("EXT-")
        if is_external_job:
            self._log_stage(
                "Applicants",
                f"External job {job_id_str} — no JobDiva applicants to fetch.",
            )
            return

        _ap_ran = True
        await queue.put({"type": "stage", "data": "Searching JobDiva applicants..."})
        applicants_res = await self._search_jobdiva_applicants(criteria)
        applicants = applicants_res.get("candidates", [])
        _ap_raw = len(applicants)
        summary["job_applicants_count"] = len(applicants)

        # Source cap (JOBDIVA_SOURCE_CAP) to prevent database locking &
        # latency loops. The downstream enrichment + per-candidate upsert
        # path is the dominant source of pool contention during auto-sync
        # cycles; without a cap a single job returning many hundreds of
        # applicants can pin the API for minutes. Applied at the
        # search-service layer so every caller (auto-sync, manual
        # source, UI preview) gets the bound regardless of what
        # criteria.page_size the caller requested. Originally a hard 100;
        # raised to JOBDIVA_SOURCE_CAP so Step-5 surfaces more results.
        #
        # F5: order by application recency before truncating so the
        # freshest applicants survive, not whatever order JobDiva
        # returned them in. Applicants are thin records (no resume
        # title/skill haystack pre-enrichment) so we can't pre-rank by
        # skill match — recency is the next-best signal we have.
        from core import sourcing_config as _sc_cap
        _applicant_cap = _sc_cap.JOBDIVA_SOURCE_CAP
        if applicants and _applicant_cap and len(applicants) > _applicant_cap:
            def _applicant_recency_key(a: Dict[str, Any]) -> str:
                # JobApplicantsDetail.RECEIVED is an ISO-ish date string;
                # lexicographic sort on the ISO form is reverse-chronological
                # when reversed. Missing dates sort last.
                return str(a.get("received") or "")
            applicants.sort(key=_applicant_recency_key, reverse=True)
            self._log_stage(
                "Applicants",
                f"Capping {len(applicants)} applicants to top-{_applicant_cap} by recency.",
            )
            applicants = applicants[:_applicant_cap]

        if not applicants:
            self._log_stage("Applicants", "No applicants found.")
            return

        # Stamp api_rank by final list position so the Step-5 UI sorts
        # by JobDiva's native order (recency, when sorted above; or
        # whatever order JobDiva returned, otherwise). The frontend
        # comparator prefers api_rank over match_score when present.
        for _idx, _a in enumerate(applicants):
            _a["api_rank"] = _idx + 1

        self._log_stage(
            "Applicants",
            f"Found {len(applicants)} applicants; starting resume screen...",
        )
        if criteria.bypass_screening and not self._has_experience_years_range(criteria):
            self._log_stage("Applicants", f"Bypassing LLM enrichment for {len(applicants)} applicants (instant sync mode).")
            for cand in applicants:
                assessment = {"passes": True, "matched": [], "missing": [], "excluded": []}
                if await emit_candidate(cand, assessment, "qualified_applicants"):
                    _ap_emitted += 1
            return

        self._attach_cached_enhanced_info(applicants)

        # Stage 1: emit a minimal agent_result row for every fresh
        # applicant before any resume / LLM work. The frontend renders
        # these immediately (in api_rank order) with shimmer cells for
        # the columns still loading.
        fresh_applicants: List[Dict[str, Any]] = []
        for _cand in applicants:
            _cand["source"] = _cand.get("source") or "JobDiva-Applicants"
            if await emit_jobdiva_agent_result(_cand, "JobDiva-Applicants"):
                fresh_applicants.append(_cand)

        if not fresh_applicants:
            return

        # Stages 2-3: progressive enrichment forwards detail patches as
        # they land; on each candidate_enriched terminal we score +
        # cross-source-dedup and emit the scored patch.
        from core import sourcing_config as _sc_applicants
        async for event in self._enrich_filtered_jobdiva_progressive(fresh_applicants, criteria):
            ev_type = event.get("type")
            if ev_type == "candidate_detail":
                await queue.put(event)
                continue
            if ev_type == "candidate_enriched":
                cand = event["candidate"]
                assessment = self._filter_assessment(cand, criteria, enforce_years=True)
                if _sc_applicants.JOBDIVA_BYPASS_PASS_GATE:
                    # Match-score and matched/missing are still computed
                    # and ride along in screening_summary as a soft
                    # signal, but the gate stops rejecting — JobDiva
                    # native order + recruiter judgement is the source
                    # of truth.
                    assessment["passes"] = True
                elif not assessment["passes"]:
                    self._log_stage(
                        "Applicants",
                        f"yielding unqualified candidate_id={cand.get('candidate_id')} missing={assessment['missing'][:3]} excluded={assessment['excluded'][:3]}",
                    )
                if await emit_jobdiva_scored(cand, assessment, "qualified_applicants"):
                    _ap_emitted += 1
    except Exception as e:
        _ap_error = e
        logger.error(f"JobDiva Applicants stage failed: {e}", exc_info=True)
    finally:
        if _ap_ran or _ap_error is not None:
            _ap_offset = max(0, int(getattr(criteria, "jobdiva_offset", 0) or 0))
            await emit_source_status(
                "JobDiva-Applicants",
                count=_ap_emitted,
                error=_ap_error,
                empty_reason=(
                    f"All {_ap_raw} applicants were already listed under "
                    "another source or dropped by filters."
                    if _ap_raw > 0
                    else "JobDiva returned no applicants for this job."
                ),
                skip_empty=_ap_offset > 0,
            )
        await queue.put(SENTINEL)


async def produce_jobdiva_talent(*, SENTINEL, _emit_sample_best, criteria, emit_jobdiva_agent_result, emit_jobdiva_scored, emit_source_status, jobagent_matched_ids, jobagent_selected, queue, sample_mode, sample_pool, seen_ids, self, summary, talent_selected, talentsearch_selected):
    """
            Run the JobDiva talent-pool sources.
            Independent of Applicants — runs whenever JobAgent and/or
            TalentSearch is in sources. Each pool is gated separately so
            selecting only one halves the JobDiva work for the request.
            """
    try:
        if not talent_selected:
            return
        pool_label = " + ".join(
            label
            for label, on in (
                ("JobAgent", jobagent_selected),
                ("Talent Search", talentsearch_selected),
            )
            if on
        )
        await queue.put(
            {"type": "stage", "data": f"Searching JobDiva ({pool_label})..."}
        )

        async def _process_talent_pool(
            talent_res: Dict[str, Any],
            *,
            stage_name: str,
            source_label: str,
            cap_label: str,
            min_score: Optional[int] = None,
            high_level_scoring: bool = False,
        ) -> int:
            """Returns how many rows survived to a scored emission —
                    the min-score gate and cross-source dedup can drop every
                    row of a pool AFTER the raw fetch looked healthy, and the
                    pool's source_status must reflect what the UI shows."""
            emitted = 0
            talent_pool = talent_res.get("candidates", [])

            # Source cap (JOBDIVA_SOURCE_CAP) — see Applicants stage above.
            from core import sourcing_config as _sc_cap
            _talent_cap = _sc_cap.JOBDIVA_SOURCE_CAP
            if sample_mode:
                # Sample probe: only the top-of-rank pool is worth
                # enriching — we stop after `sample_cap` emissions.
                _talent_cap = min(_talent_cap or sample_pool, sample_pool)
            if talent_pool and _talent_cap and len(talent_pool) > _talent_cap:
                self._log_stage(
                    stage_name,
                    f"Capping {len(talent_pool)} talent profiles to top-{_talent_cap} by {cap_label}.",
                )
                talent_pool = talent_pool[:_talent_cap]

            if not talent_pool:
                self._log_stage(stage_name, "No talent-pool candidates returned.")
                return 0
            self._attach_cached_enhanced_info(talent_pool)

            fresh_talent: List[Dict[str, Any]] = []
            for _cand in talent_pool:
                _cand["source"] = _cand.get("source") or source_label
                if sample_mode:
                    # No skeleton emission in sample mode (rows arrive
                    # complete via as_full_row), but the same-source
                    # dedup that emit_jobdiva_agent_result performs is
                    # still required.
                    _scid = str(_cand.get("candidate_id") or _cand.get("id") or "")
                    if _scid and _scid in seen_ids:
                        continue
                    if _scid:
                        seen_ids.add(_scid)
                    fresh_talent.append(_cand)
                elif await emit_jobdiva_agent_result(_cand, source_label):
                    fresh_talent.append(_cand)

            if not fresh_talent:
                return 0

            from core import sourcing_config as _sc_talent
            enrich_gen = self._enrich_filtered_jobdiva_progressive(
                fresh_talent, criteria, skip_llm=high_level_scoring
            )
            # Sample mode: rows are held back until the whole probe
            # pool is scored, then the best ones are emitted
            # (_emit_sample_best) — "the 2-5 best per source", not
            # "the first 2-5 to finish enriching".
            sample_rows: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
            try:
                async for event in enrich_gen:
                    ev_type = event.get("type")
                    if ev_type == "candidate_detail":
                        # Sample mode paints no skeleton rows, so there is
                        # nothing client-side for these patches to target.
                        if not sample_mode:
                            await queue.put(event)
                        continue
                    if ev_type == "candidate_enriched":
                        cand = event["candidate"]
                        assessment = self._filter_assessment(cand, criteria, enforce_years=True)
                        if _sc_talent.JOBDIVA_BYPASS_PASS_GATE:
                            assessment["passes"] = True
                        elif not assessment["passes"]:
                            self._log_stage(
                                stage_name,
                                f"yielding unqualified candidate_id={cand.get('candidate_id')} missing={assessment['missing'][:3]} excluded={assessment['excluded'][:3]}",
                            )
                        if sample_mode:
                            sample_rows.append((cand, assessment))
                            continue
                        if await emit_jobdiva_scored(
                            cand, assessment, "qualified_talent",
                            min_score=min_score,
                        ):
                            emitted += 1
            finally:
                # Deterministically close the generator: an exception
                # mid-stream would otherwise leave its in-flight
                # enrichment tasks (resume fetches / LLM calls) running
                # until GC finalization. aclose() raises GeneratorExit
                # inside it, whose finally cancels those tasks.
                await enrich_gen.aclose()
            if sample_mode:
                emitted = await _emit_sample_best(
                    sample_rows,
                    lambda c, a: emit_jobdiva_scored(
                        c, a, "qualified_talent",
                        min_score=min_score, as_full_row=True,
                    ),
                    stage_name,
                )
            return emitted

        async def _run_jobagent_pool():
            from core import sourcing_config as _sc_pool

            async def _consume_jobagent(jobagent_res: Dict[str, Any]) -> None:
                if jobagent_res.get("jobdiva_criteria_unconfigured"):
                    summary["jobdiva_criteria_unconfigured"] = True
                for _c in jobagent_res.get("candidates") or []:
                    _jc = str(_c.get("candidate_id") or _c.get("id") or "")
                    if _jc:
                        jobagent_matched_ids.add(_jc)
                if self._has_experience_years_range(criteria):
                    self._log_stage(
                        "ExperienceGate",
                        "JobAgent range filter enabled; extracting résumés before emitting candidates",
                    )
                await _process_talent_pool(
                    jobagent_res,
                    stage_name="JobDiva",
                    source_label="JobDiva-JobAgent",
                    cap_label="JobAgent rank",
                    # Recruiter-authored criteria in JobDiva + its own
                    # ranking normally use a high-level score. A selected
                    # YOE range is an explicit cap, however, and requires
                    # résumé extraction before a row can be emitted.
                    high_level_scoring=(
                        bool(getattr(_sc_pool, "JOBAGENT_HIGH_LEVEL_SCORING", True))
                        and not criteria.assess_all_sources
                        and not self._has_experience_years_range(criteria)
                    ),
                )

            # Two-phase fetch: JobAgentSearch latency scales with
            # resumeCount, so a small quick call paints the top-N rows
            # (resume text rides in the agent response) in seconds
            # while the full tranche is still in flight. The full call
            # re-returns those ranks; seen_ids dedup in
            # emit_jobdiva_agent_result keeps the overlap from
            # re-emitting or re-enriching. Initial search only —
            # "Search more" tranches (offset>0) and headless runs
            # (bypass_screening) go straight to the full call.
            _quick_n = max(
                0, int(getattr(_sc_pool, "JOBAGENT_QUICK_FIRST_COUNT", 0) or 0)
            )
            _offset = max(0, int(getattr(criteria, "jobdiva_offset", 0) or 0))
            _batch = max(1, int(getattr(criteria, "jobdiva_batch_size", 150) or 150))
            two_phase = (
                _quick_n > 0
                and _offset == 0
                and _batch > _quick_n
                and not getattr(criteria, "bypass_screening", False)
                and not sample_mode
            )
            pool_exc: Optional[BaseException] = None
            try:
                if sample_mode:
                    # Sample probe: JobAgentSearch latency scales with
                    # resumeCount, so ask for exactly the probe pool —
                    # returns in seconds. No full-tranche follow-up.
                    self._log_stage(
                        "JobDiva",
                        f"Running JobDiva JobAgent sample probe (resumeCount={sample_pool})...",
                    )
                    await _consume_jobagent(
                        await self._search_jobdiva_talent(
                            criteria, resume_count_override=sample_pool
                        )
                    )
                elif not two_phase:
                    self._log_stage("JobDiva", "Running JobDiva JobAgent search...")
                    await _consume_jobagent(await self._search_jobdiva_talent(criteria))
                else:
                    self._log_stage(
                        "JobDiva",
                        f"Running JobDiva JobAgent search (two-phase: quick {_quick_n} "
                        f"+ full {_batch})...",
                    )

                    async def _quick_phase():
                        try:
                            res = await self._search_jobdiva_talent(
                                criteria, resume_count_override=_quick_n
                            )
                            await _consume_jobagent(res)
                        except Exception as e:
                            # Quick phase is purely a first-paint accelerator —
                            # the full phase covers its ranks, so never let it
                            # fail the pool.
                            logger.warning(
                                f"JobAgent quick-first phase failed (full phase still covers it): {e}"
                            )

                    async def _full_phase():
                        await _consume_jobagent(await self._search_jobdiva_talent(criteria))

                    # return_exceptions: a full-phase failure must still
                    # WAIT for the quick phase — raising here would orphan
                    # it mid-emit and snapshot the count too early.
                    results = await asyncio.gather(
                        _quick_phase(), _full_phase(), return_exceptions=True
                    )
                    pool_exc = next(
                        (r for r in results if isinstance(r, Exception)), None
                    )
            except Exception as e:
                # Contain the failure: both JobDiva pools share one outer
                # gather, so an uncaught JobAgent error would abandon the
                # TalentSearch pool mid-flight too.
                pool_exc = e
            if pool_exc is not None:
                # Keep the legacy "JobDiva Talent stage failed" signature —
                # incident greps and alerting key on it.
                logger.error(
                    f"JobDiva Talent stage failed (JobAgent pool): {pool_exc}",
                    exc_info=pool_exc,
                )
            _criteria_unset = bool(summary.get("jobdiva_criteria_unconfigured"))
            await emit_source_status(
                "JobDiva-JobAgent",
                count=len(jobagent_matched_ids),
                error=pool_exc,
                empty_reason=(
                    "No Search Agent criteria are configured for this "
                    "job in JobDiva, so the AI matcher returned nothing."
                    if _criteria_unset
                    else "JobDiva returned no Job Agent matches for this job."
                ),
                skip_empty=_offset > 0,
                criteria_unconfigured=_criteria_unset,
            )

        async def _run_talent_search_pool():
            from core import sourcing_config as _sc_pool
            self._log_stage("TalentSearch", "Running JobDiva Talent boolean search...")
            pool_exc: Optional[BaseException] = None
            raw_count = 0
            emitted_count = 0
            try:
                talent_res = await self._search_jobdiva_talent_search(criteria)
                raw_count = len(talent_res.get("candidates", []))
                _min_score = getattr(_sc_pool, "JOBDIVA_TALENTSEARCH_MIN_SCORE", None)
                emitted_count = await _process_talent_pool(
                    talent_res,
                    stage_name="TalentSearch",
                    source_label="JobDiva-TalentSearch",
                    cap_label="TalentSearch rank",
                    # Machine-generated query → only surface strong
                    # matches; the sub-threshold tail is noise.
                    min_score=int(_min_score) if _min_score else None,
                )
                # Committed only after the pool fully processed: a
                # mid-processing failure must not leave a healthy-looking
                # count in the summary next to a "failed" source_status.
                summary["talent_search_count"] = raw_count
            except Exception as e:
                # Contain the failure so it can't abandon the JobAgent
                # pool sharing the outer gather.
                pool_exc = e
                # Legacy signature — incident greps and alerting key on it.
                logger.error(
                    f"JobDiva Talent stage failed (TalentSearch pool): {e}",
                    exc_info=True,
                )
            _ts_offset = max(0, int(getattr(criteria, "jobdiva_offset", 0) or 0))
            await emit_source_status(
                "JobDiva-TalentSearch",
                count=emitted_count,
                error=pool_exc,
                empty_reason=(
                    f"JobDiva returned {raw_count} boolean matches, but none "
                    "cleared the quality bar or all were already listed "
                    "under another source."
                    if raw_count > 0
                    else "The boolean Talent Search returned no matches for this job."
                ),
                skip_empty=_ts_offset > 0,
            )

        # Overlap the selected JobDiva talent searches to halve
        # wall-clock latency when both are on.
        pools = []
        if jobagent_selected:
            pools.append(_traced("jobagent_pool", _run_jobagent_pool()))
        if talentsearch_selected:
            pools.append(_run_talent_search_pool())
        self._log_stage(
            "JobDiva",
            f"talent pools selected: jobagent={jobagent_selected} "
            f"talent_search={talentsearch_selected}",
        )
        if pools:
            await asyncio.gather(*pools)
    except Exception as e:
        logger.error(f"JobDiva Talent stage failed: {e}", exc_info=True)
    finally:
        await queue.put(SENTINEL)
