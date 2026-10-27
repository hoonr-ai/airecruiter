"""Orchestrating UnifiedCandidateSearch class and its streaming search entry point."""
import logging
import asyncio
import time
from typing import List, Dict, Any, Optional
from services import jobdiva_rate_limit as _bi_rate_limit
from services import jobdiva_activity as _jobdiva_activity
from core import pipeline_trace as _pipeline_trace
from services.jobdiva import JobDivaService
from services.unipile import unipile_service
from services.vetted import vetted_service
from services.exa_service import exa_service
from services import exa_telemetry
from core.config import embedding_skill_match_for_family
from services import skill_embeddings
from services import contact_enrichment
from services.role_family import detect_role_family
from .common import (
    DISABLED_EXTERNAL_SOURCES,
    LINKEDIN_UNIPILE_SEARCH_ENABLED,
    SearchCriteria,
    _UNSET_FAMILY,
    _off_search_family,
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
from . import search_emitters as _search_emitters
from . import search_jobdiva_producers as _search_jobdiva_producers
from . import search_external_producers as _search_external_producers


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
            return await _search_emitters.emit_candidate(cand=cand, assessment=assessment, qualified_counter_key=qualified_counter_key, build_screening=build_screening, criteria=criteria, dedup_owner=dedup_owner, exa_pass_b_should_run=exa_pass_b_should_run, exa_yielded_candidates=exa_yielded_candidates, finalize_candidate=finalize_candidate, queue=queue, seen_ids=seen_ids, self=self, summary=summary)

        async def enrich_shown_row(cand: Dict[str, Any], *, overwrite: bool):
            return await _search_emitters.enrich_shown_row(cand=cand, overwrite=overwrite, criteria=criteria, dedup_owner=dedup_owner, queue=queue, self=self, summary=summary)

        async def emit_jobdiva_agent_result(cand, source_label):
            return await _search_emitters.emit_jobdiva_agent_result(cand=cand, source_label=source_label, criteria=criteria, queue=queue, seen_ids=seen_ids, self=self, summary=summary)

        async def emit_source_status(source: str, *, count: int, error: Optional[BaseException]=None, empty_reason: str='', skip_empty: bool=False, **extra: Any):
            return await _search_emitters.emit_source_status(source=source, count=count, error=error, empty_reason=empty_reason, skip_empty=skip_empty, **extra, queue=queue)

        async def _emit_sample_best(rows, emit_fn, stage_name: str):
            return await _search_emitters._emit_sample_best(rows=rows, emit_fn=emit_fn, stage_name=stage_name, finalize_candidate=finalize_candidate, sample_cap=sample_cap, self=self)

        async def emit_jobdiva_scored(cand, assessment, qualified_counter_key=None, min_score=None, as_full_row=False):
            return await _search_emitters.emit_jobdiva_scored(cand=cand, assessment=assessment, qualified_counter_key=qualified_counter_key, min_score=min_score, as_full_row=as_full_row, build_screening=build_screening, criteria=criteria, dedup_owner=dedup_owner, finalize_candidate=finalize_candidate, jobagent_matched_ids=jobagent_matched_ids, queue=queue, seen_ids=seen_ids, self=self, summary=summary)

        async def produce_jobdiva_applicants():
            return await _search_jobdiva_producers.produce_jobdiva_applicants(SENTINEL=SENTINEL, applicants_selected=applicants_selected, criteria=criteria, emit_candidate=emit_candidate, emit_jobdiva_agent_result=emit_jobdiva_agent_result, emit_jobdiva_scored=emit_jobdiva_scored, emit_source_status=emit_source_status, queue=queue, self=self, summary=summary)

        async def produce_jobdiva_talent():
            return await _search_jobdiva_producers.produce_jobdiva_talent(SENTINEL=SENTINEL, _emit_sample_best=_emit_sample_best, criteria=criteria, emit_jobdiva_agent_result=emit_jobdiva_agent_result, emit_jobdiva_scored=emit_jobdiva_scored, emit_source_status=emit_source_status, jobagent_matched_ids=jobagent_matched_ids, jobagent_selected=jobagent_selected, queue=queue, sample_mode=sample_mode, sample_pool=sample_pool, seen_ids=seen_ids, self=self, summary=summary, talent_selected=talent_selected, talentsearch_selected=talentsearch_selected)

        async def produce_external(name, search_method):
            return await _search_external_producers.produce_external(name=name, search_method=search_method, SENTINEL=SENTINEL, _emit_sample_best=_emit_sample_best, criteria=criteria, emit_candidate=emit_candidate, emit_source_status=emit_source_status, enrich_shown_row=enrich_shown_row, exa_pass_a_done=exa_pass_a_done, exa_pass_b_should_run=exa_pass_b_should_run, queue=queue, sample_mode=sample_mode, sample_pool=sample_pool, self=self, summary=summary)

        async def produce_exa_agent():
            return await _search_external_producers.produce_exa_agent(SENTINEL=SENTINEL, criteria=criteria, dedup_owner=dedup_owner, emit_candidate=emit_candidate, enrich_shown_row=enrich_shown_row, exa_pass_a_done=exa_pass_a_done, exa_yielded_candidates=exa_yielded_candidates, queue=queue, self=self)

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
