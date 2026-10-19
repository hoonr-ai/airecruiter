"""Orchestrating UnifiedCandidateSearch class and its streaming search entry point."""
import logging
import asyncio
import time
from typing import List, Dict, Any, Optional, Tuple
from services import jobdiva_rate_limit as _bi_rate_limit
from services import jobdiva_activity as _jobdiva_activity
from core import pipeline_trace as _pipeline_trace
from services.jobdiva import JobDivaService
from utils.email_utils import is_placeholder_email
from services.unipile import unipile_service
from services.vetted import vetted_service
from services.exa_service import exa_service, _extract_city_from_highlights
from services import exa_telemetry
from services.exa_errors import ExaOutOfCreditsError
from services.location import sanitize_candidate_location
from services.no_contact import apply_no_contact_flag
from core.config import embedding_skill_match_for_family
from services import skill_embeddings
from services import contact_enrichment
from services.profile_resume import deep_search_profile, normalize_linkedin_profile
from services.role_family import detect_role_family
from .common import (
    DISABLED_EXTERNAL_SOURCES,
    LINKEDIN_UNIPILE_SEARCH_ENABLED,
    SearchCriteria,
    _UNSET_FAMILY,
    _off_search_family,
    _paid_contact_deferred,
    _richer_profile_location,
    _search_job_id,
    _traced,
    logger,
    resolve_jobdiva_sources,
)
from .enrichment import EnrichmentMixin
from .hydration import HydrationMixin
from .producers.jobdiva_talent import JobdivaTalentMixin
from .producers.jobdiva_applicants import JobdivaApplicantsMixin
from .location import LocationMixin
from .search_terms import SearchTermsMixin
from .matching import MatchingMixin
from .scoring import ScoringMixin
from .producers.linkedin import LinkedinMixin
from .producers.dice import DiceMixin
from .producers.exa import ExaMixin
from .dedup import DedupMixin


class UnifiedCandidateSearch(EnrichmentMixin, HydrationMixin, JobdivaTalentMixin, JobdivaApplicantsMixin, LocationMixin, SearchTermsMixin, MatchingMixin, ScoringMixin, LinkedinMixin, DiceMixin, ExaMixin, DedupMixin):
    def __init__(self):
        self.jobdiva_service = JobDivaService()
        self.unipile_service = unipile_service
        self.vetted_service = vetted_service
        self.exa_service = exa_service
        # Set per-search by `_resolve_search_family`; read by
        # `_fuzzy_term_score` to choose between the global embedding
        # flag and the per-family override. None means "no active
        # family override" (use the global flag).
        self._current_family: Optional[str] = None
        # Bounded concurrency for Exa Research API (1/5 account QPS). Lazy
        # init in `search_candidates` so the env value is honoured at request
        # time, not import time.
        self._exa_agent_semaphore: Optional[asyncio.Semaphore] = None

    @property
    def _active_family(self) -> Optional[str]:
        """The role family the CURRENT scoring pass should use.

        An off-search scorer sets `_off_search_family` for its own
        task/thread; everything else reads the live search's instance state.
        """
        scoped = _off_search_family.get()
        return self._current_family if scoped == _UNSET_FAMILY else scoped

    def _resolve_search_family(self, criteria: "SearchCriteria") -> Optional[str]:
        """Detect role family from the criteria's title + skill hints.

        Returns None when there's no signal to classify (treated as IT
        downstream — preserves legacy behavior on empty criteria).
        """
        title_hint = ""
        for item in criteria.title_criteria or []:
            if isinstance(item, dict):
                v = str(item.get("value") or "").strip()
                if v:
                    title_hint = v
                    break
        if not title_hint:
            return None
        return detect_role_family(title_hint, "", criteria.skill_criteria or [])

    @staticmethod
    def _has_experience_years_range(criteria: "SearchCriteria") -> bool:
        """Whether an upper YOE cap makes the range a strict hard filter."""
        try:
            return int(criteria.max_experience_years or 0) > 0
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _experience_range_failure(criteria: "SearchCriteria", assessment: Any) -> bool:
        """Whether Min YOE or Max YOE rejected this candidate."""
        if not isinstance(assessment, dict):
            return False
        try:
            min_set = int(getattr(criteria, "min_experience_years", 0) or 0) > 0
        except (TypeError, ValueError):
            min_set = False
        max_set = UnifiedCandidateSearch._has_experience_years_range(criteria)
        return bool(
            (min_set and assessment.get("min_years_failure"))
            or (max_set and assessment.get("max_years_failure"))
        )


    def _log_stage(self, stage: str, message: str) -> None:
        # AutoSync runs a search per monitored job every cycle; its stage
        # lines were ~10k INFO lines per few hours. Keep them for interactive
        # searches (support greps them), DEBUG for background sync.
        from services.jobdiva_rate_limit import in_background

        level = logging.DEBUG if in_background() else logging.INFO
        logger.log(level, "[CandidateSearch] %s | %s", stage, message)

    async def search_candidates(self, criteria: SearchCriteria):
        """Run ``_search_candidates_impl`` with a pipeline run id (added to the
        first SSE event as ``run_id``) and, for interactive searches, the
        ``jd:active:{job_id}`` marker that makes background JobDiva work
        (other jobs' hydration, AutoSync) back off. Background (AutoSync)
        searches don't mark: they are the work that should yield."""
        job_id = str(getattr(criteria, "job_id", "") or "")
        run_id = _pipeline_trace.new_run(job_id or "unknown")
        _search_job_id.set(job_id or None)
        # Who is paying for this search's Exa calls (ExaCall `trigger`; the
        # job comes from the pipeline run above). A bare set, like the two
        # above: this is an async generator. A launch session's S1 has
        # already bound launch_s1 and keeps it.
        exa_telemetry.bind_if_unbound(
            trigger="autosync" if _bi_rate_limit.in_background()
            else f"step5_{str(getattr(criteria, 'search_mode', 'full') or 'full').lower()}",
            # A search about to be launched (its paid contact put off to
            # Launch PAIR): ExaCall `defer`.
            defer=True if getattr(criteria, "defer_paid_contact", False) else None,
        )
        mark = bool(job_id) and not _bi_rate_limit.in_background()
        marker = _jobdiva_activity.active_job(job_id if mark else None)
        first = True
        async with marker:
            inner = self._search_candidates_impl(criteria)
            try:
                async for event in inner:
                    if first and isinstance(event, dict):
                        first = False
                        event = {**event, "run_id": run_id}
                    yield event
            finally:
                await inner.aclose()

    async def _search_candidates_impl(self, criteria: SearchCriteria):
        """
        Orchestrate candidate search across multiple providers with tiered JobDiva logic.
        Yields candidates as they are finalized.
        """
        start_time = time.time()
        self._log_stage("Start", f"job={criteria.job_id} sources={', '.join(criteria.sources or [])}")
        if "Exa" in (criteria.sources or []) or "Dice" in (criteria.sources or []):
            exa_telemetry.record_config_once()

        # Fresh per-run enrichment budget. The provider caps
        # (PER_JOB_CAP / EXA_SOURCING_CONTACT_CAP) are in-process per-worker
        # counters that nothing else resets — without this, a job whose
        # counter filled up once would silently skip enrichment on every
        # re-run for the rest of the worker's lifetime.
        try:
            contact_enrichment.reset_job_counter(str(criteria.job_id or ""))
        except Exception:
            pass

        # Detect role family once per search. Stored on the instance so
        # `_fuzzy_term_score` can swap between the global EMBEDDING_SKILL_MATCH
        # flag (legacy IT behavior) and the per-family override
        # (`embedding_skill_match_for_family`). None family → IT path.
        self._current_family = self._resolve_search_family(criteria)
        self._log_stage("Family", f"detected={self._current_family or 'unknown (IT path)'}")

        # Pre-warm embeddings for all query-side terms once per search.
        # No-op when both the global flag is off and the family override
        # is off. Per-candidate skill embeddings are warmed lazily
        # inside emit_candidate; this batches the (small) query side
        # once so we don't pay an embedding round-trip per candidate.
        embedding_active = embedding_skill_match_for_family(self._current_family)
        if embedding_active:
            try:
                query_terms = self._criteria_query_terms(criteria)
                if query_terms:
                    await skill_embeddings.warm_terms(query_terms)
            except Exception as exc:  # never let embedding warm break a search
                logger.warning(f"query-term embedding warm failed: {exc}")

        seen_ids = set()
        # candidate_ids matched by the JobDiva JobAgent (recruiter-authored
        # criteria). The TalentSearch min-score gate exempts these: when the
        # same person surfaces via both pools, the surviving row may carry the
        # TalentSearch label, but a JobAgent match must never be dropped.
        jobagent_matched_ids: set = set()
        # Cross-source dedup ownership map: dedup key (strong identity —
        # email, phone+name, normalised LinkedIn URL) -> the already-emitted
        # "owner" candidate dict that claimed it. The legacy `seen_ids` set
        # keys on the source's native candidate_id and so misses the same
        # person showing up in JobDiva-Applicants AND LinkedIn-Exa with
        # different ids. On a collision we MERGE best-of into one surviving
        # row (never silently drop a JobDiva row): JobDiva wins the survivor
        # slot over any non-JobDiva source so it stays Launch-PAIR-actionable.
        dedup_owner: Dict[str, Dict[str, Any]] = {}
        summary = {
            "total_candidates": 0,
            "job_applicants_count": 0,
            "linkedin_count": 0,
            "dice_count": 0,
            "vetted_count": 0,
            "exa_count": 0,
            "talent_search_count": 0,
            "new_extractions": 0,
            "qualified_applicants": 0,
            "qualified_talent": 0,
            # True iff JobDiva's JobAgent returned "Criteria Not Assigned" for
            # this job — frontend uses this to render a one-time nudge banner
            # asking the recruiter to set search agent criteria in JobDiva's
            # web UI for sharper matching.
            "jobdiva_criteria_unconfigured": False,
        }

        def finalize_candidate(cand):
            """Apply match scoring to a candidate — see apply_scoring_policy."""
            self._hydrate_candidate_location_from_zip(cand)
            return self.apply_scoring_policy(cand, criteria)

        # Which JobDiva producers this request selects — see
        # resolve_jobdiva_sources for the source-name contract.
        # Product requirement (Apr 2026): Step-5 sourcing must NOT fetch
        # applicants; they're surfaced automatically via sync + rank-list.
        _jobdiva_selection = resolve_jobdiva_sources(criteria.sources)
        applicants_selected = _jobdiva_selection["applicants"]
        jobagent_selected = _jobdiva_selection["jobagent"]
        talentsearch_selected = _jobdiva_selection["talentsearch"]
        talent_selected = jobagent_selected or talentsearch_selected

        # Sample-first flow: probe each source with a small ranked pool and
        # emit only the first `sample_cap` rows per source that clear the
        # normal gates. Everything latency- or cost-heavy that doesn't change
        # what a sample row looks like (Exa Pass B, background hydration,
        # contact enrichment, the JobAgent full tranche) is skipped.
        from core import sourcing_config as _sc_sample
        sample_mode = str(getattr(criteria, "search_mode", "full")).lower() == "sample"
        sample_cap = max(1, int(getattr(criteria, "sample_per_source", 2) or 2))
        sample_pool = max(
            sample_cap,
            int(getattr(_sc_sample, "SAMPLE_MODE_POOL_SIZE", 8) or 8),
        )
        if sample_mode:
            self._log_stage(
                "SampleMode",
                f"sample search: cap={sample_cap}/source pool={sample_pool}",
            )

        queue: asyncio.Queue = asyncio.Queue()
        SENTINEL = object()

        # Exa Agent (Research API) Pass B coordination — Pass B waits for
        # Pass A's external producer to finish, then snapshots the URLs it
        # yielded to use as seeds for the research run. `_exa_yielded_candidates`
        # holds the *live* candidate dicts so the deep-search patches can be
        # merged onto the existing rows when URLs overlap.
        from core import sourcing_config as _sc
        exa_pass_a_done: asyncio.Event = asyncio.Event()
        exa_yielded_candidates: List[Dict[str, Any]] = []
        exa_pass_b_should_run = (
            _sc.EXA_AGENT_ENABLED
            and ("Exa" in criteria.sources)
            # Sample mode: Pass B is a multi-minute deep-search sweep whose
            # whole point is breadth — the sample preview never needs it.
            and not sample_mode
        )
        # Visible diagnostic so support can grep one line and confirm Pass B
        # is gated correctly for this request — without this the only signal
        # that Pass B was skipped was the absence of "Exa Agent create" logs,
        # which is easy to misread as a silent failure.
        self._log_stage(
            "ExaAgent",
            f"pass_b_should_run={exa_pass_b_should_run} "
            f"(EXA_AGENT_ENABLED={_sc.EXA_AGENT_ENABLED}, "
            f"'Exa' in sources={'Exa' in criteria.sources}, "
            f"sources={list(criteria.sources)})",
        )
        if exa_pass_b_should_run and self._exa_agent_semaphore is None:
            self._exa_agent_semaphore = asyncio.Semaphore(max(1, _sc.EXA_AGENT_CONCURRENCY))

        def build_screening(assessment):
            return {
                "matched": assessment["matched"][:10],
                "missing": assessment["missing"][:10],
                "excluded": assessment["excluded"][:10],
                "passes_strict": assessment["passes"],
            }

        async def emit_candidate(cand, assessment, qualified_counter_key=None):
            # Keep the early source assessment permissive so candidates can
            # finish enrichment. The final location gate below enforces every
            # configured radius consistently before a row reaches Step 5.
            location_reason = assessment.get("location_failure_reason") if isinstance(assessment, dict) else None
            if not assessment.get("passes") and location_reason in {
                "non_us_candidate",
            }:
                distance = cand.get("distance_miles")
                self._log_stage(
                    "LocationGate",
                    f"dropping candidate_id={cand.get('candidate_id') or cand.get('id')} "
                    f"reason={location_reason} distance={distance}",
                )
                return False

            cand["screening_summary"] = build_screening(assessment)

            # Warm candidate-side skill embeddings before scoring so the
            # sync `_fuzzy_term_score` path can read them from the
            # in-process cache. Honors both the global flag and the
            # per-family override (non-IT families default-on).
            if embedding_skill_match_for_family(self._current_family):
                try:
                    cand_terms = self._candidate_skill_terms(cand)
                    if cand_terms:
                        await skill_embeddings.warm_terms(cand_terms)
                except Exception as exc:
                    logger.warning(
                        f"candidate-skill embedding warm failed: {exc}"
                    )

            cand = finalize_candidate(cand)
            cid = str(cand.get("candidate_id") or cand.get("id"))

            # Step 5 has a strict radius contract across every source. Check
            # again after finalization because résumé extraction may replace
            # a stale profile location or recover a previously blank one.
            self._hydrate_candidate_location_from_zip(cand)
            final_location_veto = self._location_hard_gate(cand, criteria)
            if final_location_veto:
                summary["location_mismatch_dropped"] = summary.get("location_mismatch_dropped", 0) + 1
                self._log_stage(
                    "LocationGate",
                    f"dropping candidate_id={cid} source={cand.get('source')} "
                    f"reason={cand.get('location_veto_reason') or final_location_veto} "
                    f"distance={cand.get('distance_miles')}",
                )
                return False

            # Relevance gate for machine-queried sources (Exa / Unipile /
            # DeepSearch / Dice / Vetted). JobDiva-JobAgent (recruiter-authored
            # criteria) and JobDiva-Applicants (real applicants) are exempt.
            # Mirrors the TalentSearch ScoreGate: rows the recruiter must not
            # launch (confirmed wrong location, or below the score floor) are
            # never emitted. Unscoreable rows (match_score None) stay visible.
            from core import sourcing_config as _sc_gate
            _src = str(cand.get("source") or "")
            _exempt = set(getattr(_sc_gate, "EXTERNAL_MIN_SCORE_EXEMPT_SOURCES",
                                  ("JobDiva-JobAgent", "JobDiva-Applicants")))
            if _src not in _exempt:
                _min_score = getattr(_sc_gate, "EXTERNAL_SOURCE_MIN_SCORE", None)
                if (
                    _min_score is not None
                    and cand.get("match_score") is not None
                    and int(cand.get("match_score") or 0) < int(_min_score)
                ):
                    summary["below_min_score_dropped"] = summary.get("below_min_score_dropped", 0) + 1
                    self._log_stage(
                        "ScoreGate",
                        f"dropping candidate_id={cid} source={_src} "
                        f"match_score={cand.get('match_score')} < min_score={_min_score}",
                    )
                    return False

            if cid and cid in seen_ids:
                return False
            cross_keys = self._dedup_keys(cand)
            owner = next((dedup_owner[k] for k in cross_keys if k in dedup_owner), None)
            if owner is not None:
                # Same person already shown (from this or another source).
                # Merge best-of into the surviving row instead of showing a
                # duplicate — and never emit a second row. If the survivor is
                # a JobDiva row it absorbs this source's contact info.
                changed = self._merge_candidate_best_of(owner, cand)
                owner_id = str(owner.get("candidate_id") or owner.get("id") or "")
                self._log_stage(
                    "Dedup",
                    f"merge-into-owner kept={owner_id}({owner.get('source')}) "
                    f"dropped={cid}({cand.get('source')}) keys={cross_keys}",
                )
                if changed and owner_id:
                    await queue.put({
                        "type": "candidate_detail",
                        "candidate_id": owner_id,
                        "patch": dict(changed),
                    })
                return False
            if cid:
                seen_ids.add(cid)
            for k in cross_keys:
                dedup_owner[k] = cand
            if qualified_counter_key and assessment["passes"]:
                summary[qualified_counter_key] += 1
            summary["total_candidates"] += 1
            # Track Pass A's LinkedIn-Exa candidates so Pass B (Exa Research
            # API) can seed its instructions with their URLs and merge
            # enrichment patches back onto these exact dicts on overlap.
            if exa_pass_b_should_run and str(cand.get("source") or "") == "LinkedIn-Exa":
                exa_yielded_candidates.append(cand)
            await queue.put({"type": "candidate", "data": cand})
            return True

        async def enrich_shown_row(cand: Dict[str, Any], *, overwrite: bool) -> None:
            """Contact lookup (Apollo → paid Exa on a miss, emails only; see
            enrich_contact_for_sourcing) for a row that is ALREADY on screen;
            whatever it finds streams as a `contact_enrichment` patch.

            Running after emit means the score/location/dedup gates decide who
            gets a paid lookup, and the row paints without waiting on a lookup
            that can take most of a minute (Exa phone runs). On a search about
            to be launched the paid Exa step is left to Launch PAIR
            (``paid_deferred`` on the span, ``exa_contact_deferred`` in the
            summary)."""
            # Spend policy: LinkedIn sources at/above the score floor only.
            # emit_candidate's gate lets UNSCORED rows through for display, and
            # Unipile/Dice rows reach here too — neither may buy a lookup.
            blocked = contact_enrichment.contact_lookup_block_reason(
                cand.get("source"), cand.get("match_score")
            )
            if blocked:
                summary["contact_lookup_skipped"] = summary.get("contact_lookup_skipped", 0) + 1
                logger.debug("contact lookup skipped for %s: %s", cand.get("candidate_id") or cand.get("id"), blocked)
                return
            summary["contact_lookup_attempted"] = summary.get("contact_lookup_attempted", 0) + 1
            deferred = _paid_contact_deferred(cand, criteria)
            if deferred:
                summary["exa_contact_deferred"] = summary.get("exa_contact_deferred", 0) + 1
            before = {k: str(cand.get(k) or "").strip() for k in ("email", "phone")}
            async with _pipeline_trace.span(
                "contact_enrichment", source=str(cand.get("source") or ""), paid_deferred=deferred,
            ):
                await self._apply_contact_enrichment(cand, criteria, overwrite=overwrite)
            patch = {
                k: cand[k] for k in ("email", "phone")
                if str(cand.get(k) or "").strip() and str(cand.get(k)).strip() != before[k]
            }
            if not patch:
                return
            # The row was keyed for cross-source dedup before its contact
            # existed; later rows for the same person must still merge into it.
            for key in self._dedup_keys(cand):
                dedup_owner.setdefault(key, cand)
            await queue.put({
                "type": "candidate_detail",
                "candidate_id": str(cand.get("candidate_id") or cand.get("id") or ""),
                "stage": "contact_enrichment",
                "patch": patch,
            })

        async def emit_jobdiva_agent_result(cand, source_label):
            """Stage 1 of progressive JobDiva flow: emit a minimal row from the
            agent search result so the UI can render an api_rank-ordered shimmer
            row before resume fetch / LLM extraction / scoring complete.

            Applies in-source candidate_id dedup only — cross-source dedup
            (email / linkedin / name+location) requires fields we don't have
            yet, so it runs at the ``scored`` stage instead. Returns True if
            the agent_result was emitted (caller should enrich); False if
            the candidate is a same-source duplicate.
            """
            cid = str(cand.get("candidate_id") or cand.get("id") or "")
            if cid and cid in seen_ids:
                return False

            self._hydrate_candidate_location_from_zip(cand)
            final_location_veto = self._location_hard_gate(cand, criteria)
            if final_location_veto:
                self._log_stage(
                    "LocationGate",
                    f"deferring JobDiva candidate_id={cid} source={source_label} "
                    f"reason={cand.get('location_veto_reason') or final_location_veto} "
                    f"distance={cand.get('distance_miles')}",
                )
                # The CRM location may be absent or stale; résumé enrichment
                # can supply the authoritative current residence. Keep it out
                # of Step 5 until the post-enrichment gate has a final verdict.
                cand["_defer_initial_emit"] = True

            if cid:
                seen_ids.add(cid)

            agent_payload: Dict[str, Any] = {
                "candidate_id": cid or cand.get("id"),
                "id": cand.get("id") or cid,
                "name": cand.get("name") or "",
                "source": cand.get("source") or source_label,
                "_stage": "agent_result",
            }
            # Pass through fields that the JobDiva agent typically populates
            # before enrichment so the row isn't fully empty on first paint.
            for key in (
                "api_rank", "location", "title", "headline", "email", "phone",
                "resume_id", "resume_text", "experience_years", "skills",
                "education", "certifications", "enhanced_info",
                "enhanced_info_status", "received", "city", "state",
                "linkedin_url", "profile_url", "image_url", "data",
                "zipcode", "distance_miles", "location_out_of_radius",
                "location_match_reason",
                "qualifications", "employee_status", "available",
                "availability_status", "current_company", "scoring_mode",
                "no_contact", "no_contact_reason", "no_contact_company",
                # Where the row's email came from when a merge gave it
                # another row's (_merge_candidate_best_of): Launch PAIR's
                # getCandidateSeedEmail reads it.
                "email_source",
            ):
                v = cand.get(key)
                if v not in (None, "", [], {}):
                    agent_payload[key] = v

            if not cand.get("_defer_initial_emit", False):
                summary["total_candidates"] += 1
                await queue.put({"type": "candidate", "data": agent_payload})
            return True

        async def emit_source_status(
            source: str,
            *,
            count: int,
            error: Optional[BaseException] = None,
            empty_reason: str = "",
            skip_empty: bool = False,
            **extra: Any,
        ) -> None:
            """Report how a source pool ended. Every pool fails open to zero
            rows with only a log line, so without this event the browser
            cannot tell a dead pool from a legitimately empty one. The
            exception is sanitized to its class name — raw error text can
            carry internal URLs/hosts and this reason renders verbatim in
            the recruiter-facing banner."""
            if isinstance(error, ExaOutOfCreditsError):
                # Not "no matches": the source could not be searched at all.
                status = "failed"
                reason = (
                    f"Exa is out of credits — {source} results are missing "
                    "until it is topped up."
                )
            elif error is not None:
                status = "failed"
                if count > 0:
                    reason = (
                        f"{source} search failed ({type(error).__name__}) after "
                        f"{count} result(s) arrived — this list may be incomplete."
                    )
                else:
                    reason = (
                        f"{source} search failed ({type(error).__name__}) — "
                        "check the API logs."
                    )
            elif count > 0:
                status, reason = "ok", ""
            elif skip_empty:
                # An exhausted follow-up tranche ("Search more") is not a source
                # problem — stay quiet rather than contradict rows already shown.
                return
            else:
                status = "empty"
                reason = empty_reason or f"{source} returned no results for this job."
            await queue.put({
                "type": "source_status",
                "data": {
                    "source": source,
                    "status": status,
                    "count": count,
                    "reason": reason,
                    **extra,
                },
            })

        async def _emit_sample_best(rows, emit_fn, stage_name: str) -> int:
            """Sample mode: the whole probe pool is scored first, then the
            best rows are emitted — always the top SAMPLE_MIN_ROWS_PER_SOURCE
            (a weak source still shows what it has), beyond that only rows
            at/above SAMPLE_QUALITY_FLOOR, never more than `sample_cap`.
            Hard-filter fails / no-contact / client-conflict rows never take
            a slot (the full run still shows them at 0%). `emit_fn` is the
            source's normal emitter (dedup, min-score gate, queue) and may
            decline a row, in which case the next-best one is tried.

            `rows` are (candidate, assessment) pairs collected from the
            source's enrichment stream instead of being emitted on arrival.
            """
            from core import sourcing_config as _sc_smp
            min_rows = max(0, int(getattr(_sc_smp, "SAMPLE_MIN_ROWS_PER_SOURCE", 2) or 0))
            floor = getattr(_sc_smp, "SAMPLE_QUALITY_FLOOR", None)
            scored: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
            for cand, assessment in rows:
                try:
                    finalize_candidate(cand)
                except Exception as exc:  # scoring must never kill the stream
                    logger.warning(
                        f"sample pre-score failed for candidate_id={cand.get('candidate_id')}: {exc}"
                    )
                scored.append((cand, assessment))

            def _sort_key(item):
                s = item[0].get("match_score")
                return -1.0 if s is None else float(s)

            scored.sort(key=_sort_key, reverse=True)
            emitted = 0
            skipped_hard = 0
            for cand, assessment in scored:
                if emitted >= sample_cap:
                    break
                if self._sample_row_hard_failed(cand):
                    skipped_hard += 1
                    continue
                s = cand.get("match_score")
                if (
                    emitted >= min_rows
                    and floor is not None
                    and (s is None or float(s) < float(floor))
                ):
                    break
                if await emit_fn(cand, assessment):
                    emitted += 1
            self._log_stage(
                "SampleMode",
                f"{stage_name}: pool={len(rows)} emitted={emitted} "
                f"hard_fails_skipped={skipped_hard} cap={sample_cap} min={min_rows} floor={floor}",
            )
            return emitted

        async def emit_jobdiva_scored(cand, assessment, qualified_counter_key=None, min_score=None, as_full_row=False):
            """Stage 3 of progressive JobDiva flow: score the (now-enriched)
            candidate and emit a ``candidate_detail`` patch with the scored
            payload. Mirrors :py:func:`emit_candidate` for dedup +
            non-US drop semantics, but emits a patch (the row already
            exists from emit_jobdiva_agent_result) instead of a fresh
            ``candidate`` event.

            ``min_score``: when set, rows scoring strictly below it are
            removed via a ``dropped`` patch instead of shown. Used for
            JobDiva-TalentSearch (machine-generated query → only surface
            >JOBDIVA_TALENTSEARCH_MIN_SCORE matches). Unscoreable rows
            (``detail_failed`` → match_score None) are always kept.

            ``as_full_row``: sample-mode variant. The agent_result skeleton
            was never emitted, so instead of patching a (nonexistent) row this
            emits a complete ``candidate`` event on success and stays silent on
            drops — no drop patch is sent for a row the client never saw.
            """
            cid = str(cand.get("candidate_id") or cand.get("id") or "")
            deferred_initial = bool(cand.pop("_defer_initial_emit", False))
            emit_full_row = as_full_row or deferred_initial
            location_reason = assessment.get("location_failure_reason") if isinstance(assessment, dict) else None
            # A selected experience range is an explicit recruiter constraint,
            # unlike the normal JobDiva relevance gate (which intentionally
            # remains advisory).  Do not let JOBDIVA_BYPASS_PASS_GATE leak an
            # enriched candidate outside that range back into Step 5.
            range_failure = self._experience_range_failure(criteria, assessment)
            if range_failure:
                self._log_stage(
                    "ExperienceGate",
                    f"dropping candidate_id={cid} outside configured experience range",
                )
                if not emit_full_row:
                    await queue.put({
                        "type": "candidate_detail",
                        "candidate_id": cid,
                        "stage": "dropped",
                        "patch": {"_stage": "dropped", "_drop_reason": "experience_years_out_of_range"},
                    })
                return False
            if not assessment.get("passes") and location_reason in {"non_us_candidate"}:
                distance = cand.get("distance_miles")
                self._log_stage(
                    "LocationGate",
                    f"dropping candidate_id={cid} reason={location_reason} distance={distance}",
                )
                if not emit_full_row:
                    await queue.put({
                        "type": "candidate_detail",
                        "candidate_id": cid,
                        "stage": "dropped",
                        "patch": {"_stage": "dropped", "_drop_reason": "non_us_candidate"},
                    })
                return False

            # Résumé enrichment (_apply_resume_location) can overwrite
            # cand["location"] with a value the first-paint gate in
            # emit_jobdiva_agent_result never saw — re-check here so a
            # résumé-recovered location outside every configured radius
            # still gets dropped instead of reaching Step 5.
            self._hydrate_candidate_location_from_zip(cand)
            scored_location_veto = self._location_hard_gate(cand, criteria)
            if scored_location_veto:
                summary["location_mismatch_dropped"] = summary.get("location_mismatch_dropped", 0) + 1
                self._log_stage(
                    "LocationGate",
                    f"dropping candidate_id={cid} reason={cand.get('location_veto_reason') or scored_location_veto} "
                    f"distance={cand.get('distance_miles')}",
                )
                if not emit_full_row:
                    await queue.put({
                        "type": "candidate_detail",
                        "candidate_id": cid,
                        "stage": "dropped",
                        "patch": {"_stage": "dropped", "_drop_reason": "location_mismatch"},
                    })
                return False

            cand["screening_summary"] = build_screening(assessment)

            if embedding_skill_match_for_family(self._current_family):
                try:
                    cand_terms = self._candidate_skill_terms(cand)
                    if cand_terms:
                        await skill_embeddings.warm_terms(cand_terms)
                except Exception as exc:
                    logger.warning(f"candidate-skill embedding warm failed: {exc}")

            cand = finalize_candidate(cand)

            # Min-score gate (TalentSearch only). Runs after finalize so the
            # score already includes the source-tier bonus / title boost.
            # Unscoreable rows (detail_failed → None) stay visible as
            # "Limited data" — we can't fairly judge them.
            if (
                min_score is not None
                and cand.get("match_score") is not None
                and int(cand.get("match_score") or 0) < int(min_score)
                and cid not in jobagent_matched_ids
            ):
                self._log_stage(
                    "ScoreGate",
                    f"dropping candidate_id={cid} source={cand.get('source')} "
                    f"match_score={cand.get('match_score')} < min_score={min_score}",
                )
                # Release the id: the JobAgent pool shares seen_ids, and its
                # copy of this person may have been suppressed as a duplicate
                # when this row painted first. Freeing the id lets the
                # never-dropped JobAgent copy re-emit if it arrives later.
                if cid:
                    seen_ids.discard(cid)
                if not emit_full_row:
                    await queue.put({
                        "type": "candidate_detail",
                        "candidate_id": cid,
                        "stage": "dropped",
                        "patch": {"_stage": "dropped", "_drop_reason": "below_min_score"},
                    })
                return False

            # Cross-source dedup runs here (not at agent_result) since the
            # keys depend on email / phone / linkedin URL, which are only
            # reliably populated after enrichment ("the candidate details API").
            # POLICY: a JobDiva row, once shown, is never the dropped side of a
            # JobDiva-vs-other collision — it takes over the survivor slot and
            # absorbs the other source's best fields. Genuine intra-JobDiva
            # duplicates (Applicants vs Talent) merge into one surviving row.
            cross_keys = self._dedup_keys(cand)
            owner = next((dedup_owner[k] for k in cross_keys if k in dedup_owner), None)
            if owner is not None:
                owner_id = str(owner.get("candidate_id") or owner.get("id") or "")
                if not self._cand_is_jobdiva(owner):
                    # JobDiva takes over from the non-JobDiva owner: absorb the
                    # owner's best fields, remove the (already-shown) owner row,
                    # and re-point the dedup keys to this JobDiva candidate.
                    self._merge_candidate_best_of(cand, owner)
                    for k in [k for k, v in dedup_owner.items() if v is owner]:
                        del dedup_owner[k]
                    for k in cross_keys:
                        dedup_owner[k] = cand
                    self._log_stage(
                        "Dedup",
                        f"cross_source merge: kept={cid}(JobDiva) "
                        f"dropped={owner_id}({owner.get('source')}) keys={cross_keys}",
                    )
                    await queue.put({
                        "type": "candidate_detail",
                        "candidate_id": owner_id,
                        "stage": "dropped",
                        "patch": {"_stage": "dropped", "_drop_reason": "merged_into_jobdiva"},
                    })
                    # fall through: emit this JobDiva candidate's scored patch
                else:
                    # Owner is also JobDiva (e.g. Applicants vs Talent for the
                    # same person): a genuine intra-JobDiva duplicate. Merge
                    # best-of into the surviving JobDiva row and drop this one.
                    changed = self._merge_candidate_best_of(owner, cand)
                    self._log_stage(
                        "Dedup",
                        f"cross_source merge: kept={owner_id}(JobDiva) "
                        f"dropped={cid}(JobDiva) keys={cross_keys}",
                    )
                    if changed and owner_id:
                        await queue.put({
                            "type": "candidate_detail",
                            "candidate_id": owner_id,
                            "patch": dict(changed),
                        })
                    if not emit_full_row:
                        await queue.put({
                            "type": "candidate_detail",
                            "candidate_id": cid,
                            "stage": "dropped",
                            "patch": {"_stage": "dropped", "_drop_reason": "cross_source_duplicate"},
                        })
                    return False
            else:
                for k in cross_keys:
                    dedup_owner[k] = cand

            if qualified_counter_key and assessment["passes"]:
                summary[qualified_counter_key] += 1

            if emit_full_row:
                # Sample mode: no skeleton row exists client-side — emit the
                # fully-enriched, scored candidate as a complete row. The
                # total_candidates counter normally increments at the
                # agent_result emission, which this path skipped.
                summary["total_candidates"] += 1
                await queue.put({"type": "candidate", "data": cand})
                return True

            scored_patch: Dict[str, Any] = {"_stage": "scored"}
            for key in (
                "match_score", "matched_skills", "missing_skills",
                "explainability", "match_score_details", "screening_summary",
                "enhanced_info", "enhanced_info_status", "education",
                "certifications", "skills", "urls", "experience_years",
                "name", "title", "location", "email", "phone",
                # Forward merged identity so a take-over reflects the unioned
                # sources + absorbed contact/profile on the surviving row, and
                # where an absorbed email came from (email_source: Launch
                # PAIR's getCandidateSeedEmail never seeds another source's).
                "sources", "profile_url", "linkedin_url", "image_url",
                "resume_id", "city", "state", "email_source",
                # Geo verdict → UI "~N mi away" badge on the location cell.
                "zipcode", "distance_miles", "location_out_of_radius",
                "location_match_reason",
                # Résumé-is-final residence: which side won and, when the
                # résumé overrode the profile, what the profile said → UI
                # "résumé" badge + tooltip on the location cell.
                "location_source", "profile_location", "location_conflict",
                "qualifications", "employee_status", "available",
                "availability_status", "current_company",
                # Candidate-details failure flag → UI renders "N/A" + keeps the
                # row launchable (vs. a 0% drop).
                "detail_failed",
                # "high_level" for JobDiva-JobAgent rows (LLM skills-match
                # skipped) → popup labels the score "JobDiva agent search".
                "scoring_mode",
                # No-contact company flag → UI greys the row out and disables
                # every action. False rides along too (v is not None) so a
                # stale flag clears client-side.
                "no_contact", "no_contact_reason", "no_contact_company",
            ):
                v = cand.get(key)
                if v is not None:
                    scored_patch[key] = v
            await queue.put({
                "type": "candidate_detail",
                "candidate_id": cid,
                "stage": "scored",
                "patch": scored_patch,
            })
            return True

        async def produce_jobdiva_applicants():
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

        async def produce_jobdiva_talent():
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

        async def produce_external(name, search_method):
            # Status label must match the source string stamped on rows, which
            # is only known after the search returns — fall back to the static
            # mapping when the search fails or yields nothing.
            _ext_label = {"LinkedIn": "LinkedIn-Unipile", "Exa": "LinkedIn-Exa"}.get(name, name)
            _ext_raw = 0
            _ext_emitted = 0
            _ext_error: Optional[BaseException] = None
            # Post-emit contact lookups; drained before this producer's SENTINEL
            # so their patches land inside the stream.
            ext_contact_tasks: List[asyncio.Task] = []
            try:
                await queue.put({"type": "stage", "data": f"Searching {name}..."})
                res = await search_method(criteria)
                if not res:
                    return
                ext_candidates = res.get("candidates", [])
                source_type = res.get("source_type", name)
                _ext_label = source_type
                _ext_raw = len(ext_candidates)
                summary[f"{source_type.lower()}_count"] = len(ext_candidates)

                # HOTFIX: Hard cap at 100 — see Applicants stage above.
                # F5: pre-rank by cheap title/skill keyword match before
                # slicing. External sources (Exa/Dice/Unipile) ship enough
                # signal in `title` + highlight text to rank meaningfully,
                # and an unranked FIFO slice can discard the best matches
                # if Exa returns ordered by its own relevance and we ask
                # for 50 results but the cap fires elsewhere.
                _ext_keep_top = min(100, sample_pool) if sample_mode else 100
                if ext_candidates and len(ext_candidates) > _ext_keep_top:
                    before_rank = len(ext_candidates)
                    ext_candidates = self._rank_candidates_by_skill(
                        ext_candidates, criteria, keep_top=_ext_keep_top
                    )
                    self._log_stage(
                        source_type,
                        f"Capping {before_rank} {source_type} profiles to "
                        f"top-{len(ext_candidates)} by skill+title rank.",
                    )

                self._log_stage(source_type, f"Found {len(ext_candidates)} profiles; starting streaming enrichment...")

                from core import sourcing_config as _sc_ext
                semaphore = asyncio.Semaphore(
                    max(1, int(getattr(_sc_ext, "EXTERNAL_PROCESS_CONCURRENCY", 12) or 12))
                )

                async def _process_external_single(cand):
                    async with semaphore:
                        cand["source"] = source_type

                        # F3: cheap role-anchor check on external candidates.
                        # Exa has no real query filter — surfaced profiles can
                        # be completely off-role (e.g. software engineers when
                        # the job is "Program Manager"). Drop those before
                        # they consume a 100-cap slot and a downstream LLM call.
                        # Unipile is checked AFTER profile enrichment fills its
                        # title field; Exa/Dice carry a title from the start.
                        if source_type != "LinkedIn-Unipile" and not self._candidate_title_match(cand, criteria):
                            return {"status": "failed_title_match"}

                        is_linkedin = source_type.startswith("LinkedIn")
                        if source_type == "LinkedIn-Unipile":
                            provider_id = cand.get("provider_id")
                            if provider_id:
                                try:
                                    full_profile = await self.unipile_service.get_candidate_profile(
                                        provider_id, account_id=cand.get("unipile_account_id")
                                    )
                                    if full_profile:
                                        # The search row's `title` is the
                                        # LinkedIn headline; keep it reachable
                                        # for the role-anchor check when the
                                        # profile supplies a real job title.
                                        _headline = str(cand.get("title") or "")
                                        cand.update(self._extract_linkedin_profile_data(full_profile))
                                        if _headline and not cand.get("headline"):
                                            cand["headline"] = _headline
                                        # The WHOLE profile (role descriptions,
                                        # summary, languages ...) rides to
                                        # /candidates/save so Launch PAIR can give
                                        # JobDiva a complete résumé; the fields
                                        # above are only what scoring reads.
                                        _linkedin_profile = normalize_linkedin_profile(full_profile)
                                        if _linkedin_profile:
                                            cand["linkedin_profile"] = _linkedin_profile
                                            # Contact the member shares on LinkedIn
                                            # is free -- take it before any paid
                                            # ZoomInfo / Apollo / Exa lookup runs.
                                            _li_email, _li_phone = contact_enrichment.sanitize_agent_contact(
                                                (_linkedin_profile.get("emails") or [""])[0],
                                                (_linkedin_profile.get("phones") or [""])[0],
                                            )
                                            if _li_email and not is_placeholder_email(_li_email) and not str(cand.get("email") or "").strip():
                                                cand["email"] = _li_email
                                            if _li_phone and not str(cand.get("phone") or "").strip():
                                                cand["phone"] = _li_phone
                                except Exception as e:
                                    logger.warning(f"Failed to fetch full profile for LinkedIn candidate {provider_id}: {e}")
                            # After enrichment, run the same role-anchor check.
                            if not self._candidate_title_match(cand, criteria):
                                return {"status": "failed_title_match"}

                        # PR-B: cheap pre-LLM YOE gate for external sources too.
                        # Drops candidates whose headline / abstract / resume
                        # snippet shows fewer years than the configured floor.
                        if self._candidate_outside_years_range_pre_llm(cand, criteria):
                            return {"status": "failed_filter"}
                        assessment = self._filter_assessment(cand, criteria, enforce_years=False)
                        if not assessment["passes"]:
                            return {"status": "failed_filter"}

                        # No-contact company (checked after the Unipile profile
                        # fetch fills company_experience): emit the row so Step 5
                        # renders it greyed out, but spend nothing more on it —
                        # no LLM extraction (which is also the enhanced-info
                        # persistence path), no deep analysis, no paid contact
                        # enrichment.
                        if apply_no_contact_flag(cand):
                            cand["enhanced_info"] = cand.get("enhanced_info") or {}
                            cand["enhanced_info_status"] = "no_contact"
                            return {"status": "no_contact_shown", "candidate": cand}

                        from services.sourced_candidates_storage import process_linkedin_candidate, process_dice_candidate
                        if is_linkedin:
                            enhanced = await process_linkedin_candidate(cand)
                        elif source_type == "Dice":
                            enhanced = await process_dice_candidate(cand)
                        else:
                            enhanced = cand

                        if isinstance(enhanced, dict) and enhanced is not cand:
                            cand["enhanced_info"] = enhanced.get("raw", enhanced)
                        else:
                            cand["enhanced_info"] = cand.get("enhanced_info") or {}

                        cand["enhanced_info_status"] = "completed"
                        cand["name"] = cand["enhanced_info"].get("candidate_name") or cand.get("name")
                        # For LinkedIn-Exa, ZoomInfo→Apollo is the sole source of
                        # truth for email/phone — enhanced_info values here are
                        # LLM-extracted from profile text and frequently empty or
                        # hallucinated. Skipping the assignment lets the inline
                        # enrichment block below populate contact info cleanly.
                        if source_type != "LinkedIn-Exa":
                            cand["email"] = cand["enhanced_info"].get("email") or cand.get("email")
                            cand["phone"] = cand["enhanced_info"].get("phone") or cand.get("phone")
                        cand["title"] = cand["enhanced_info"].get("job_title") or cand.get("title")
                        cand["experience_years"] = (
                            cand["enhanced_info"].get("years_of_experience")
                            or cand.get("experience_years")
                        )
                        # Résumé is final for residence (see
                        # _apply_resume_location): an explicitly stated
                        # résumé location replaces the profile's; the profile
                        # value only stands when the résumé is silent.
                        self._apply_resume_location(cand)
                        # The heuristic gate above is only an early cost saver.
                        # Once enrichment has extracted total YOE, enforce the
                        # requested range against that authoritative value.
                        range_assessment = self._filter_assessment(
                            cand, criteria, enforce_years=True
                        )
                        if (
                            range_assessment.get("min_years_failure")
                            or range_assessment.get("max_years_failure")
                        ):
                            self._log_stage(
                                "ExperienceGate",
                                "dropping candidate_id=%s outside configured experience range" % (
                                    cand.get("candidate_id") or cand.get("id"),
                                ),
                            )
                            return {"status": "failed_experience_range"}
                        if cand["enhanced_info"].get("structured_skills") or cand["enhanced_info"].get("skills"):
                            cand["skills"] = cand["enhanced_info"].get("structured_skills") or cand["enhanced_info"].get("skills")

                        # Exa deep analysis on filter survivors only. Replaces
                        # the 4000-char highlights with the full profile text
                        # plus a per-candidate match summary; preserves the
                        # original highlights in resume_text since downstream
                        # location extractors are tuned to that shape.
                        # When Pass B (the Exa Agent run) is on it supersedes
                        # this per-URL get_contents() summary. Both are paid
                        # and both default off (sourcing_config): turning Pass B
                        # off must not silently turn this on.
                        from core import sourcing_config as _sc_deep
                        if (
                            source_type == "LinkedIn-Exa"
                            and not sample_mode
                            and not exa_pass_b_should_run
                            and _sc_deep.exa_deep_analysis_enabled()
                        ):
                            try:
                                deep = await self.exa_service.deep_analyze_candidate(
                                    str(cand.get("profile_url") or ""),
                                    criteria.sourcing_skill_values(),
                                    criteria.location or "",
                                )
                            except Exception as e:
                                logger.warning("Exa deep_analyze raised for %s: %s", cand.get("id"), e)
                                deep = {}
                            if deep:
                                cand["deep_text"] = deep.get("text", "")
                                cand["exa_deep_summary"] = deep.get("summary", "")
                                if (not cand.get("city") or not cand.get("state")) and deep.get("text"):
                                    head = deep["text"][:600]
                                    c2, s2 = _extract_city_from_highlights(head)
                                    if c2 and not cand.get("city"):
                                        cand["city"] = c2
                                    if s2 and not cand.get("state"):
                                        cand["state"] = s2

                        # In-line ZoomInfo → Apollo (→ Exa Agent for LinkedIn
                        # sources) enrichment.
                        #   - LinkedIn-Exa: ALWAYS run; the result overwrites any
                        #     pre-existing email/phone. The enrichment chain is
                        #     the source of truth for Exa-sourced contact info.
                        #   - Other sources (Dice/Unipile): run as a backfill
                        #     whenever email OR phone is missing — Unipile never
                        #     supplies contact itself, so ZoomInfo/Apollo (and
                        #     the Exa fallback for LinkedIn-*) fill the gaps;
                        #     only empty fields are written (overwrite=False).
                        # Gated by CONTACT_ENRICHMENT_INLINE_ENABLED inside the
                        # helper; capped per-job at contact_enrichment.PER_JOB_CAP
                        # (+ EXA_SOURCING_CONTACT_CAP for the Exa fallback).
                        #
                        # `full_name` is required for the ZoomInfo path — the
                        # new Data API doesn't accept linkedinUrl as a match
                        # input, so we need firstName + lastName for
                        # ContactSearch. Without a name we skip ZoomInfo and
                        # go straight to Apollo (which does accept a URL).
                        is_exa_source = source_type == "LinkedIn-Exa"
                        # EMAIL is the only field sourcing spends on. 436 widened
                        # this to "either field missing", which pulled every
                        # phone-less candidate into the provider chain — and since
                        # phones are the costly half (Apollo per-record reveal, Exa
                        # per run), that was the bulk of sourcing enrichment spend,
                        # incurred for candidates the recruiter had not shortlisted
                        # yet. Phone acquisition now happens at Launch PAIR or on
                        # the Step 5 phone button. The helper is also told
                        # want_phone=False, so this is belt-and-braces.
                        has_email = bool(str(cand.get("email") or "").strip())
                        # Sample mode: never spend paid enrichment credits on a
                        # preview row — contact info is acquired in the full
                        # run / at Launch PAIR for the rows that matter.
                        # The lookup itself runs AFTER the row is emitted (see
                        # enrich_shown_row below): inline, it held every Exa row
                        # off screen for the whole ZoomInfo → Apollo → Exa chain
                        # and bought contacts for rows the score gate then dropped.
                        needs_contact = (is_exa_source or not has_email) and not sample_mode
                        return {"status": "success", "candidate": cand, "needs_contact": needs_contact}

                process_tasks = [asyncio.create_task(_process_external_single(c)) for c in ext_candidates]
                # Per-status drop accounting: these gates used to discard rows
                # with no counter or log line, so "Exa found 50, UI shows 1"
                # was undiagnosable from the stream log.
                _status_counts: Dict[str, int] = {}
                # Sample mode: hold rows until the whole probe pool is
                # scored, then emit the best ones (_emit_sample_best).
                _ext_sample_rows: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
                try:
                    for task in asyncio.as_completed(process_tasks):
                        result = await task
                        _status_counts[result["status"]] = _status_counts.get(result["status"], 0) + 1
                        if result["status"] in ("success", "no_contact_shown"):
                            cand = result["candidate"]
                            assessment = self._filter_assessment(cand, criteria, enforce_years=False)
                            if sample_mode:
                                _ext_sample_rows.append((cand, assessment))
                                continue
                            if await emit_candidate(cand, assessment):
                                _ext_emitted += 1
                                if result.get("needs_contact"):
                                    ext_contact_tasks.append(asyncio.create_task(
                                        enrich_shown_row(cand, overwrite=source_type == "LinkedIn-Exa")
                                    ))
                finally:
                    # An exception mid-loop can leave pool tasks in flight —
                    # cancel and drain so no work leaks past the producer.
                    # No-op when the loop ran to completion.
                    for _t in process_tasks:
                        if not _t.done():
                            _t.cancel()
                    await asyncio.gather(*process_tasks, return_exceptions=True)
                if sample_mode:
                    _ext_emitted = await _emit_sample_best(
                        _ext_sample_rows, emit_candidate, source_type
                    )
                if _status_counts:
                    self._log_stage(
                        name,
                        f"processed {len(ext_candidates)} {source_type} candidates: "
                        + ", ".join(f"{k}={v}" for k, v in sorted(_status_counts.items())),
                    )
            except ExaOutOfCreditsError as e:
                # Already logged once at ERROR by the breaker; no traceback.
                _ext_error = e
                logger.warning(f"{name} search skipped: {e}")
            except Exception as e:
                _ext_error = e
                logger.error(f"{name} search stage failed: {e}", exc_info=True)
            finally:
                await emit_source_status(
                    _ext_label,
                    count=_ext_emitted,
                    error=_ext_error,
                    empty_reason=(
                        f"{_ext_label} returned {_ext_raw} profiles, but none cleared "
                        "the relevance/score gates or all were already listed."
                        if _ext_raw > 0
                        else f"{_ext_label} returned no matching profiles — the source "
                        "may have no matches, be disabled, or be out of quota."
                    ),
                )
                # Signal Pass A completion so the Exa Research producer can
                # seed its run with the URLs we just yielded — before waiting
                # on contact lookups, which Pass B does not need.
                if name == "Exa" and exa_pass_b_should_run:
                    exa_pass_a_done.set()
                if ext_contact_tasks:
                    try:
                        await asyncio.gather(*ext_contact_tasks, return_exceptions=True)
                    finally:
                        for _t in ext_contact_tasks:
                            if not _t.done():
                                _t.cancel()
                await queue.put(SENTINEL)

        async def produce_exa_agent():
            """Pass B: Exa Agent API (Websets 2.0) deep-search.

            Waits for Pass A (LinkedIn-Exa producer) to finish, snapshots the
            URLs it yielded, runs one Agent call that BOTH enriches those
            URLs with structured fields (last_activity, follower_count,
            recent_companies, fit_rationale) AND discovers additional
            candidates. URL-overlapping results become `candidate_detail`
            patches with stage `exa_deep_search`; new ones become fresh
            `candidate` events tagged `LinkedIn-DeepSearch`.

            Emits stage events at start / completion / error so the Step-5 UI
            can show "Exa deep-search ran (N enriched, M new)" or "Exa
            deep-search failed — falling back to keyword results" in the
            status bar without the recruiter having to inspect API logs.

            Contact info is NOT requested from the agent (see
            EXA_AGENT_CONTACT_FIELDS): its contact tool billed a lookup for
            every returned profile before Apollo was ever tried. New rows get
            the regular Apollo-first chain instead, after they are shown.
            """
            enriched = 0
            new_found = 0
            failure_reason = ""
            contact_tasks: List[asyncio.Task] = []

            try:
                await queue.put({"type": "stage", "data": "Exa deep-search warming up..."})
                # Bounded wait so a hung Pass A can't stall Pass B forever.
                try:
                    await asyncio.wait_for(exa_pass_a_done.wait(), timeout=120.0)
                except asyncio.TimeoutError:
                    logger.warning("Exa Pass A did not signal within 120s — running deep-research with no seeds")

                seed_urls: List[str] = []
                for c in exa_yielded_candidates:
                    u = str(c.get("profile_url") or "").strip()
                    if u:
                        seed_urls.append(u)

                await queue.put({
                    "type": "stage",
                    "data": f"Exa deep-search running ({len(seed_urls)} seeds)...",
                })

                async with self._exa_agent_semaphore:
                    try:
                        # Plain title text — the agent query is natural
                        # language, so no quoted-boolean role hints here.
                        # jd_role joins the top titles with " or " and is the
                        # string the agent prompt prefers. (criteria has no
                        # `role_hint` field; the old `criteria.role_hint`
                        # fallback raised AttributeError whenever
                        # title_criteria was empty and silently killed Pass B.)
                        agent_titles = criteria.sourcing_titles()
                        results = await self.exa_service.deep_research_candidates(
                            jd_title=agent_titles[0] if agent_titles else "",
                            jd_role=" or ".join(agent_titles[:2]),
                            skills=criteria.skill_only_values(),
                            # Remote-aware: US-wide for remote jobs; else all
                            # configured locations "or"-joined (Pass B is one
                            # call per request, so additional locations ride
                            # in the same prompt rather than a fan-out).
                            location=self._search_location_for_deep_research(criteria),
                            seed_urls=seed_urls,
                            # Exa accepts one radius for this OR query, so
                            # request the broadest configured search envelope.
                            # emit_candidate enforces each chip's exact radius
                            # before a candidate reaches Step 5.
                            within_miles=max(
                                [getattr(criteria, "within_miles", 25) or 25]
                                + [entry.within_miles for entry in (criteria.additional_locations or [])]
                            ),
                            exclude_company=str(getattr(criteria, "client_name", "") or ""),
                        )
                    except Exception as e:
                        logger.warning("deep_research_candidates raised: %s", e)
                        failure_reason = f"agent call raised: {type(e).__name__}"
                        results = []

                if not results:
                    if not failure_reason:
                        failure_reason = "agent returned no candidates"
                    return

                def _norm_url(u: Any) -> str:
                    s = str(u or "").strip().lower()
                    if not s:
                        return ""
                    return s.split("?", 1)[0].rstrip("/")

                pass_a_by_url: Dict[str, Dict[str, Any]] = {
                    _norm_url(c.get("profile_url")): c
                    for c in exa_yielded_candidates
                    if c.get("profile_url")
                }

                for idx, entry in enumerate(results):
                    if not isinstance(entry, dict):
                        continue
                    url = entry.get("linkedin_url") or ""
                    nurl = _norm_url(url)
                    patch_fields: Dict[str, Any] = {
                        "exa_last_activity": entry.get("last_activity"),
                        "exa_follower_count": entry.get("follower_count"),
                        "exa_recent_companies": entry.get("recent_companies") or [],
                        "exa_fit_rationale": entry.get("fit_rationale") or "",
                    }
                    # Contact fields from the agent's enrichment tool — only
                    # present when EXA_AGENT_CONTACT_FIELDS re-enables them
                    # (off by default: Apollo gets first refusal). Sanity-gate
                    # before use; only backfill — never overwrite ZoomInfo/Apollo
                    # data already on the row.
                    agent_email, agent_phone = contact_enrichment.sanitize_agent_contact(
                        entry.get("email"), entry.get("phone")
                    )

                    if nurl and nurl in pass_a_by_url:
                        existing = pass_a_by_url[nurl]
                        cid = str(existing.get("candidate_id") or existing.get("id") or "")
                        if not cid:
                            continue
                        # Pass A can store a blank line or the wrong city.
                        # The agent reads the profile line. Write that place
                        # onto the row. If it is outside every circle or
                        # outside the country, remove the card: Step 5 already
                        # drops a row when this event arrives with
                        # stage "dropped". A place inside a circle, or a vague
                        # label, stays and the badge updates.
                        recovered_location = sanitize_candidate_location(entry.get("location"))
                        current_location = sanitize_candidate_location(existing.get("location"))
                        # A later read of the profile can replace a bare city
                        # ("Edison") with the line LinkedIn actually shows
                        # ("Edison, New Jersey, United States").
                        upgraded = _richer_profile_location(current_location, recovered_location)
                        if upgraded:
                            existing["location"] = upgraded
                            recovered_location = upgraded
                            outside_country = self._is_likely_outside_country(
                                existing, self._target_country(criteria)
                            )
                            recovered_veto = None if outside_country else self._location_hard_gate(
                                existing, criteria
                            )
                            if outside_country or recovered_veto:
                                self._log_stage(
                                    "LocationGate",
                                    f"dropping candidate_id={cid} after Exa location recovery "
                                    f"location={recovered_location!r} "
                                    f"reason={existing.get('location_veto_reason') or 'non_us_candidate'}",
                                )
                                await queue.put({
                                    "type": "candidate_detail",
                                    "candidate_id": cid,
                                    "stage": "dropped",
                                    "patch": {
                                        "_stage": "dropped",
                                        "_drop_reason": (
                                            "non_us_candidate" if outside_country else "location_mismatch"
                                        ),
                                    },
                                })
                                continue
                            patch_fields["location"] = recovered_location
                            # A confirmed in-radius place clears the
                            # unverified badge the blank row was showing.
                            patch_fields["location_match_reason"] = (
                                existing.get("location_match_reason") or ""
                            )
                            if existing.get("distance_miles") is not None:
                                patch_fields["distance_miles"] = existing["distance_miles"]
                        prior_sources = existing.get("sources") or [existing.get("source") or "LinkedIn-Exa"]
                        if not isinstance(prior_sources, list):
                            prior_sources = [str(prior_sources)]
                        merged = list(dict.fromkeys([*prior_sources, "LinkedIn-DeepSearch"]))
                        patch_fields["sources"] = merged
                        patch_fields["_stage"] = "exa_deep_search"
                        if agent_email and not str(existing.get("email") or "").strip():
                            patch_fields["email"] = agent_email
                        if agent_phone and not str(existing.get("phone") or "").strip():
                            patch_fields["phone"] = agent_phone
                        existing["sources"] = merged
                        existing.update({k: v for k, v in patch_fields.items() if v is not None})
                        # Deep-search may reveal the employer (recent_companies)
                        # only now, after the row was emitted unflagged —
                        # re-check and ride the flag on this same patch.
                        if apply_no_contact_flag(existing):
                            for _nc_key in ("no_contact", "no_contact_reason", "no_contact_company"):
                                if existing.get(_nc_key) is not None:
                                    patch_fields[_nc_key] = existing[_nc_key]
                        await queue.put({
                            "type": "candidate_detail",
                            "candidate_id": cid,
                            "stage": "exa_deep_search",
                            "patch": patch_fields,
                        })
                        enriched += 1
                    else:
                        new_id = f"exadeep_{idx}_{nurl[-32:] or 'unknown'}"
                        # Pull skills out of recent_companies / fit_rationale
                        # so the scorer has something to compare against the
                        # JD's required skills. Without this, every deep-only
                        # candidate scored 0 and sank to the bottom of the
                        # sorted list — invisible to the recruiter.
                        rationale_blob = " ".join([
                            str(entry.get("fit_rationale") or ""),
                            *[
                                f"{rc.get('title', '')} {rc.get('company', '')}"
                                for rc in (entry.get("recent_companies") or [])
                                if isinstance(rc, dict)
                            ],
                        ])
                        new_cand: Dict[str, Any] = {
                            "id": new_id,
                            "candidate_id": new_id,
                            "provider_id": new_id,
                            "name": entry.get("name") or "",
                            "firstName": (str(entry.get("name") or "").split(" ", 1) + [""])[0],
                            "lastName": (str(entry.get("name") or "").split(" ", 1) + [""])[1],
                            "title": entry.get("current_title") or "",
                            "headline": entry.get("current_title") or "",
                            "location": entry.get("location") or "",
                            "email": agent_email,
                            "phone": agent_phone,
                            "profile_url": url,
                            "source": "LinkedIn-DeepSearch",
                            "sources": ["LinkedIn-DeepSearch"],
                            "skills": [],
                            "resume_text": rationale_blob,  # feeds the scorer
                            # The person's own profile facts (title, location,
                            # recent roles) for the JobDiva résumé -- the
                            # rationale above is PAIR's words and never goes there.
                            "linkedin_profile": deep_search_profile(entry),
                            "_stage": "exa_deep_search",
                            **{k: v for k, v in patch_fields.items() if v is not None},
                        }
                        # Hard filter: never surface someone currently employed
                        # by the hiring client. The agent prompt already asks
                        # for the exclusion, but its recent_companies output is
                        # the ground truth — belt and suspenders.
                        try:
                            from services.company_match import currently_employed_by_client
                            _client_match = currently_employed_by_client(
                                new_cand, str(getattr(criteria, "client_name", "") or "")
                            )
                            if _client_match:
                                logger.info(
                                    "Exa deep-search dropping %s — currently at hiring client (%s)",
                                    new_cand.get("name"), _client_match,
                                )
                                continue
                        except Exception:
                            pass

                        # Cross-source dedup so a deep-search hit doesn't
                        # duplicate a row that some other producer (Unipile,
                        # JobDiva) already emitted under a different id. On a
                        # collision, fold this hit's best-of into the owner
                        # rather than dropping it on the floor.
                        keys = self._dedup_keys(new_cand)
                        _deep_owner = next((dedup_owner[k] for k in keys if k in dedup_owner), None)
                        if _deep_owner is not None:
                            self._merge_candidate_best_of(_deep_owner, new_cand)
                            continue
                        # Route through emit_candidate so the deep-only
                        # candidate gets the same scoring + finalize_candidate
                        # treatment as Pass A — without this its match_score
                        # stayed at 0 and it sorted to the bottom of the list.
                        # The closure already handles seen_ids / dedup_owner
                        # bookkeeping internally; we don't pre-add here.
                        try:
                            assessment = self._filter_assessment(
                                new_cand, criteria, enforce_years=False
                            )
                        except Exception as e:
                            logger.warning("filter_assessment failed for deep candidate %s: %s", new_id, e)
                            assessment = {
                                "passes": True, "matched": [], "missing": [],
                                "excluded": [], "score": 0,
                            }
                        # Deep-only candidates never pass through the Pass A
                        # enrichment block, so they get the same ZoomInfo →
                        # Apollo → (Exa on a miss) chain here — but only once
                        # emit_candidate has shown them. Rows the score/location
                        # gates drop, and no-contact-company rows, cost nothing.
                        if not await emit_candidate(new_cand, assessment):
                            continue
                        new_found += 1
                        if not new_cand.get("no_contact") and not (
                            str(new_cand.get("email") or "").strip()
                            and str(new_cand.get("phone") or "").strip()
                        ):
                            contact_tasks.append(
                                asyncio.create_task(enrich_shown_row(new_cand, overwrite=True))
                            )
                # Patches must land before this producer's SENTINEL closes
                # its share of the stream.
                if contact_tasks:
                    await asyncio.gather(*contact_tasks, return_exceptions=True)
            except Exception as e:
                logger.error(f"Exa Agent producer failed: {e}", exc_info=True)
                failure_reason = failure_reason or f"producer crashed: {type(e).__name__}"
            finally:
                for task in contact_tasks:
                    if not task.done():
                        task.cancel()
                # Final status emit so the UI status bar reflects what
                # happened, even when no candidate_detail patches landed.
                if failure_reason and (enriched + new_found) == 0:
                    await queue.put({
                        "type": "stage",
                        "data": f"Exa deep-search skipped: {failure_reason}",
                    })
                else:
                    await queue.put({
                        "type": "stage",
                        "data": f"Exa deep-search done — {enriched} enriched, {new_found} new",
                    })
                self._log_stage(
                    "ExaAgent",
                    f"pass_b finished enriched={enriched} new_found={new_found} "
                    f"failure_reason={failure_reason or '(none)'}",
                )
                await queue.put(SENTINEL)

        # Build producer tasks for all selected sources — run in parallel.
        # JobDiva Applicants and JobDiva Talent are now independent producers,
        # each with its own SENTINEL, so they stream concurrently alongside Exa/Dice.
        producers = []
        if applicants_selected:
            producers.append(asyncio.create_task(_traced("produce_jobdiva_applicants", produce_jobdiva_applicants())))
        if talent_selected:
            producers.append(asyncio.create_task(_traced("produce_jobdiva_talent", produce_jobdiva_talent())))

        external_order = [
            ("LinkedIn", self._search_linkedin),
            ("Dice", self._search_dice),
            ("Exa", self._search_exa),
        ]
        disabled_source_events = []
        for ext_name, ext_method in external_order:
            # LinkedIn sourcing is currently paused because this path uses
            # Unipile. Ignore stale clients and saved criteria that still
            # submit LinkedIn; LinkedIn-Exa remains a separate active source.
            if (
                ext_name in criteria.sources
                and ext_name in DISABLED_EXTERNAL_SOURCES
                and not LINKEDIN_UNIPILE_SEARCH_ENABLED
            ):
                label = {"LinkedIn": "LinkedIn-Unipile"}.get(ext_name, ext_name)
                disabled_source_events.append({
                    "type": "source_status",
                    "data": {
                        "source": label,
                        "status": "disabled",
                        "count": 0,
                        "reason": f"{label} sourcing is temporarily disabled.",
                    },
                })
                continue
            if ext_name in criteria.sources:
                producers.append(asyncio.create_task(_traced("produce_external", produce_external(ext_name, ext_method), source=ext_name)))

        # Surface disabled sources even when no provider producers remain
        # (for example, a stale client submitting only LinkedIn).
        for event in disabled_source_events:
            await queue.put(event)

        # Exa Research API Pass B — depends on Pass A (`produce_external("Exa")`),
        # so only schedule when both the agent is enabled AND Exa is selected.
        if exa_pass_b_should_run:
            producers.append(asyncio.create_task(_traced("produce_exa_agent", produce_exa_agent())))

        # Keepalive producer — emits {"type": "keepalive"} every 20 s while any
        # producer is still running. Without this, the HTTP/2 stream is silent
        # for the entire duration of slow upstream calls (e.g. JobDiva's
        # JobAgentSearch which can take 3-4 minutes), and Chrome kills the
        # stream with ERR_HTTP2_PROTOCOL_ERROR after ~90 s of inactivity.
        _active_ref: List[int] = [len(producers)]  # mutable box so the coroutine sees updates

        async def produce_keepalive():
            while _active_ref[0] > 0:
                await asyncio.sleep(20)
                if _active_ref[0] > 0:
                    await queue.put({"type": "keepalive"})

        keepalive_task = asyncio.create_task(produce_keepalive())

        # Drain the queue until every producer emits its SENTINEL.
        # Accumulate JobDiva-sourced candidates so we can hydrate them in
        # the background after producers finish (fast-path mode only).
        active = len(producers)
        hydration_targets: List[Dict[str, Any]] = []
        hydration_task: Optional[asyncio.Task] = None
        try:
            while active > 0 or not queue.empty():
                event = await queue.get()
                if event is SENTINEL:
                    active -= 1
                    _active_ref[0] = active
                    continue
                if event.get("type") == "keepalive":
                    yield event 
                    continue
                if event.get("type") == "candidate":
                    cand_payload = event.get("data") or {}
                    src = str(cand_payload.get("source") or "")
                    if src.startswith("JobDiva"):
                        hydration_targets.append(cand_payload)
                yield event

            # Phase 4 of fast-path: now that the foreground list is streamed,
            # hydrate the top of the JobDiva pool with CandidatesDetail. Each
            # page emits per-candidate "candidate_detail" events into the same
            # SSE stream so the UI can merge in place. Order targets by
            # api_rank ASC so top JobAgent results hydrate first — matches
            # the UI display order. Records without api_rank (other sources)
            # sort last.
            from core import sourcing_config as _sc
            # Sample mode: rows were emitted fully enriched (as_full_row) and
            # the preview doesn't warrant a background CandidatesDetail sweep.
            if hydration_targets and _sc.FAST_PATH_SKIP_DETAIL_IN_TALENT_SEARCH and not sample_mode:
                def _hydration_key(c: Dict[str, Any]) -> int:
                    rank = c.get("api_rank")
                    return rank if isinstance(rank, int) else 10**9
                hydration_targets.sort(key=_hydration_key)
                hydration_targets = hydration_targets[
                    : _sc.FAST_PATH_DETAIL_BACKGROUND_MAX_CANDIDATES
                ]
                hydration_task = asyncio.create_task(_traced(
                    "hydrate_jobdiva",
                    self._hydrate_jobdiva_in_background(
                        hydration_targets, queue, SENTINEL
                    ),
                    candidates=len(hydration_targets),
                ))
                # Drain hydration events. Hydration emits SENTINEL when done.
                while True:
                    event = await queue.get()
                    if event is SENTINEL:
                        break
                    yield event
        finally:
            # Stop the keepalive heartbeat.
            _active_ref[0] = 0
            if not keepalive_task.done():
                keepalive_task.cancel()
            # If the generator is closed (e.g. client disconnect), cancel
            # all background work — producers and hydration alike.
            for task in producers:
                if not task.done():
                    task.cancel()
            if hydration_task and not hydration_task.done():
                hydration_task.cancel()

            pending = [keepalive_task] + list(producers)
            if hydration_task:
                pending.append(hydration_task)
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        duration = time.time() - start_time
        yield {
            "type": "summary",
            "data": {
                "summary": summary,
                "search_criteria": criteria.dict(),
                "extraction_time_seconds": round(duration, 1)
            }
        }
        self._log_stage("Done", f"Search complete for job {criteria.job_id} in {int(duration)}s. Streamed {summary['total_candidates']} candidates.")

unified_search_service = UnifiedCandidateSearch()
