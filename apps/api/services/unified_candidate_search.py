import logging
import asyncio
import json
import math
import os
import re
import time
from typing import List, Dict, Any, Optional, Tuple
from pydantic import BaseModel

from services.jobdiva import JobDivaService
from services.unipile import unipile_service
from services.vetted import vetted_service
from services.exa_service import exa_service, _extract_city_from_highlights
from services.location import normalize_location_string, within_radius
from core.config import (
    SCORING_REQUIRED_WEIGHT,
    SCORING_PREFERRED_WEIGHT,
    SCORING_YEARS_UNKNOWN_MULT,
    SCORING_YEARS_FLOOR,
    SCORING_RECENT_PENALTY,
    SCORING_EXCLUSION_CAP,
    SCORING_EXCLUSION_PER_HIT,
    SCORING_EXCLUSION_HARD_VETO_THRESHOLD,
    SCORING_UNMATCHED_REQUIRED_FLOOR,
    SCORING_UNMATCHED_PREFERRED_FLOOR,
    SCORING_PARSING_GAP_FLOOR,
    SCORING_COVERAGE_BLEND_THRESHOLD,
    SOURCE_TIER_BONUS,
    EMBEDDING_SKILL_MATCH,
    EMBEDDING_MATCH_THRESHOLD,
    scoring_weights_for_family,
    embedding_skill_match_for_family,
)
from services import skill_embeddings
from services import role_taxonomy
from services import contact_enrichment
from services.role_family import detect_role_family


_TITLE_BOOST_BY_RELEVANCE = {"exact": 30, "similar": 20, "related": 10}


_TITLE_SEPARATORS = re.compile(r"[/|·,;]+")


def _candidate_titles(cand: Dict[str, Any]) -> List[str]:
    """Pull every plausible title field off a candidate dict, split on common
    separators ("/", "|", "·", ",", ";") so multi-title strings like
    "BI Developer/SQL Server Developer/Power BI" become three lookups, deduped."""
    raw: List[str] = []
    for key in ("title", "current_title", "most_recent_title", "headline"):
        val = cand.get(key)
        if isinstance(val, str) and val.strip():
            raw.append(val.strip())
    enhanced = cand.get("enhanced_info")
    if isinstance(enhanced, dict):
        for key in ("current_title", "most_recent_title", "headline"):
            val = enhanced.get(key)
            if isinstance(val, str) and val.strip():
                raw.append(val.strip())
    out: List[str] = []
    for combined in raw:
        for part in _TITLE_SEPARATORS.split(combined):
            part = part.strip()
            if part and part not in out:
                out.append(part)
    return out


def _compute_title_boost(cand: Dict[str, Any], title_criteria: List[Dict[str, Any]]) -> int:
    """Score how well a candidate's title matches the searched titles via taxonomy.

    Returns the strongest tier across (searched_title × candidate_title): exact=30,
    similar=20, related=10, none=0. Each searched title contributes its primary
    `value` plus any `similar_terms` already attached upstream.
    """
    candidate_titles = _candidate_titles(cand)
    if not candidate_titles:
        return 0
    best = 0
    best_pair: Optional[Tuple[str, str, str]] = None
    for item in title_criteria:
        searched_values = [str(item.get("value", "")).strip()]
        for t in item.get("similar_terms", []) or []:
            t = str(t).strip()
            if t:
                searched_values.append(t)
        for searched in searched_values:
            if not searched:
                continue
            for cand_title in candidate_titles:
                tier = role_taxonomy.compare(searched, cand_title)
                points = _TITLE_BOOST_BY_RELEVANCE.get(tier, 0)
                if points > best:
                    best = points
                    best_pair = (searched, cand_title, tier)
                    if best >= 30:
                        cand["title_match_source"] = {"searched": searched, "candidate": cand_title, "relevance": tier}
                        return best
    if best > 0 and best_pair:
        cand["title_match_source"] = {"searched": best_pair[0], "candidate": best_pair[1], "relevance": best_pair[2]}
    return best

logger = logging.getLogger(__name__)

# Sentinel distance for candidates with no usable location data. Big enough
# to always exceed the UI's within_miles radius so the BEYOND 25MI badge
# counts these rows instead of silently passing them as in-radius.
_UNKNOWN_DISTANCE_SENTINEL = 9999.0


class SearchCriteria(BaseModel):
    job_id: str
    title_criteria: List[Dict[str, Any]] = []
    skill_criteria: List[Dict[str, Any]] = []
    keywords: List[str] = []
    resume_match_filters: List[Dict[str, Any]] = []
    location: str = ""
    within_miles: int = 25
    # Structured geo for JobDiva talentSearchDef. Optional — backend will
    # derive these from `location` when the frontend doesn't send them.
    countries: List[str] = []
    states: List[str] = []
    page_number: int = 0
    companies: List[str] = []
    page_size: int = 100
    sources: List[str] = ["JobDiva", "LinkedIn", "Exa"]
    open_to_work: bool = True
    boolean_string: str = ""
    bypass_screening: bool = False
    # 5.6: limit JobDiva Talent Search to candidates whose record was touched in
    # the last N days. None / 0 means "no freshness filter".
    recent_days: Optional[int] = None
    # 5.10: by default JobDiva Talent Search drops profile-only candidates
    # (no resume attached). Set false to opt back in to the full signal.
    require_resume: bool = True
    include_relocation_candidates: bool = True
    min_experience_years: Optional[int] = None

    def sourcing_skill_values(self) -> List[str]:
        """Flat skill-like strings for sources that only accept a plain list
        (LinkedIn-Unipile, Exa, Dice, Vetted). Pulls from skill_criteria +
        title_criteria, skipping excludes and empty values."""
        values: List[str] = []
        seen = set()
        for item in (self.skill_criteria or []) + (self.title_criteria or []):
            if not isinstance(item, dict):
                continue
            if item.get("match_type") == "exclude":
                continue
            value = str(item.get("value", "")).strip()
            if not value:
                continue
            key = value.lower()
            if key in seen:
                continue
            seen.add(key)
            values.append(value)
        return values

class UnifiedCandidateSearch:
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


    def _log_stage(self, stage: str, message: str) -> None:
        logger.info("[CandidateSearch] %s | %s", stage, message)

    async def search_candidates(self, criteria: SearchCriteria):
        """
        Orchestrate candidate search across multiple providers with tiered JobDiva logic.
        Yields candidates as they are finalized.
        """
        start_time = time.time()
        self._log_stage("Start", f"job={criteria.job_id} sources={', '.join(criteria.sources or [])}")

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
        # Cross-source dedup keys (email, normalised LinkedIn URL,
        # normalised name+location). The legacy `seen_ids` set keys on
        # the source's native candidate_id and so misses the same person
        # showing up in JobDiva-Applicants AND LinkedIn-Exa with
        # different ids. Both sets are checked in `emit_candidate`.
        seen_dedup_keys: set = set()
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
            """Apply match scoring to a candidate."""
            from core.utils import is_valid_phone
            if not is_valid_phone(cand.get("phone")):
                cand["phone"] = None
                
            # Ensure name is title-cased if it exists
            if cand.get("name"):
                cand["name"] = str(cand["name"]).title()

            if criteria.bypass_screening:
                cand["match_score"] = 0
                cand["missing_skills"] = []
                cand["matched_skills"] = []
                cand["explainability"] = ["Scoring skipped (auto-assignment)"]
                cand["match_score_details"] = {}
                return cand

            score_result = self._score_candidate(cand, criteria)
            base_score = score_result["score"]
            cand["match_score"] = base_score
            cand["missing_skills"] = score_result["missing_skills"]
            cand["matched_skills"] = score_result.get("matched_skills", [])
            cand["explainability"] = score_result["explainability"]
            cand["match_score_details"] = score_result.get("score_details", {})

            # Source-tier bonus: warm leads (recruiter's own applicants,
            # JobDiva talent pool, curated DBs) outrank cold scrapes when
            # raw scores are close. Only applied when base_score > 0 so
            # excluded / hard-vetoed candidates aren't promoted.
            source = str(cand.get("source") or "")
            bonus = SOURCE_TIER_BONUS.get(source, 0)
            if bonus and base_score > 0:
                boosted = min(100, base_score + bonus)
                cand["match_score"] = boosted
                cand["match_score_details"]["source_tier_bonus"] = {
                    "source": source,
                    "bonus": bonus,
                    "base_score": base_score,
                }

            # Title-match boost via role taxonomy. Without this, a SQL dev
            # whose resume happens to mention "program management" can outrank
            # a Senior Program Manager whose title actually matches the search.
            if base_score > 0 and criteria.title_criteria:
                title_boost = _compute_title_boost(cand, criteria.title_criteria)
                if title_boost > 0:
                    prev_score = cand["match_score"]
                    cand["match_score"] = min(100, prev_score + title_boost)
                    cand["match_score_details"]["title_boost"] = title_boost

            return cand

        # JobDiva is split into two explicit sources:
        #   - "JobDiva Applicants": people who applied to this job_id (no boolean)
        #   - "JobDiva": talent-pool boolean search only
        # Product requirement (Apr 2026): Step-5 sourcing must NOT fetch applicants.
        # Applicants are surfaced automatically via sync + rank-list.
        applicants_selected = (
            "JobDiva Applicants" in criteria.sources
            or "JobDiva-Applicants" in criteria.sources
        )
        talent_selected = (
            "JobDiva" in criteria.sources
            or "JobDiva-TalentSearch" in criteria.sources
        )

        queue: asyncio.Queue = asyncio.Queue()
        SENTINEL = object()

        def build_screening(assessment):
            return {
                "matched": assessment["matched"][:10],
                "missing": assessment["missing"][:10],
                "excluded": assessment["excluded"][:10],
                "passes_strict": assessment["passes"],
            }

        async def emit_candidate(cand, assessment, qualified_counter_key=None):
            # Policy 2026-05-13: only hard-drop on definitive non-US evidence.
            # State-mismatch, outside-radius, and relocation-excluded-by-filter
            # are softened to "show + score + let recruiter filter in UI" since
            # the source-side metadata is often stale or the candidate is
            # actively relocating.
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
                return

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
            if cid and cid in seen_ids:
                return
            cross_keys = self._dedup_keys(cand)
            if any(k in seen_dedup_keys for k in cross_keys):
                return
            if cid:
                seen_ids.add(cid)
            for k in cross_keys:
                seen_dedup_keys.add(k)
            if qualified_counter_key and assessment["passes"]:
                summary[qualified_counter_key] += 1
            summary["total_candidates"] += 1
            await queue.put({"type": "candidate", "data": cand})

        async def produce_jobdiva_applicants():
            """
            Fetch every candidate who has applied to this job_id in JobDiva
            (no boolean string). Emitted under source=JobDiva-Applicants.
            Skipped for external jobs (negative job_id / EXT-), which have
            no JobDiva applicants.
            """
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

                await queue.put({"type": "stage", "data": "Searching JobDiva applicants..."})
                applicants_res = await self._search_jobdiva_applicants(criteria)
                applicants = applicants_res.get("candidates", [])
                summary["job_applicants_count"] = len(applicants)

                # HOTFIX: Hard cap at 100 to prevent database locking & latency
                # loops. The downstream enrichment + per-candidate upsert path
                # is the dominant source of pool contention during auto-sync
                # cycles; without a hard cap a single job returning 500+
                # applicants can pin the API for minutes. Applied at the
                # search-service layer so every caller (auto-sync, manual
                # source, UI preview) gets the bound regardless of what
                # criteria.page_size the caller requested.
                #
                # F5: order by application recency before truncating so the
                # freshest 100 applicants survive, not whatever order JobDiva
                # returned them in. Applicants are thin records (no resume
                # title/skill haystack pre-enrichment) so we can't pre-rank by
                # skill match — recency is the next-best signal we have.
                if applicants and len(applicants) > 100:
                    def _applicant_recency_key(a: Dict[str, Any]) -> str:
                        # JobApplicantsDetail.RECEIVED is an ISO-ish date string;
                        # lexicographic sort on the ISO form is reverse-chronological
                        # when reversed. Missing dates sort last.
                        return str(a.get("received") or "")
                    applicants.sort(key=_applicant_recency_key, reverse=True)
                    self._log_stage(
                        "Applicants",
                        f"Capping {len(applicants)} applicants to top-100 by recency.",
                    )
                    applicants = applicants[:100]

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
                if criteria.bypass_screening:
                    self._log_stage("Applicants", f"Bypassing LLM enrichment for {len(applicants)} applicants (instant sync mode).")
                    for cand in applicants:
                        assessment = {"passes": True, "matched": [], "missing": [], "excluded": []}
                        await emit_candidate(cand, assessment, "qualified_applicants")
                    return

                self._attach_cached_enhanced_info(applicants)
                from core import sourcing_config as _sc_applicants
                async for cand in self._enrich_filtered_jobdiva_candidates(applicants, criteria):
                    assessment = self._filter_assessment(cand, criteria, enforce_years=True)
                    if _sc_applicants.JOBDIVA_BYPASS_PASS_GATE:
                        # Match-score and matched/missing are still computed and
                        # ride along in screening_summary as a soft signal, but
                        # the gate stops rejecting — JobDiva native order +
                        # recruiter judgement is the source of truth.
                        assessment["passes"] = True
                    elif not assessment["passes"]:
                        self._log_stage(
                            "Applicants",
                            f"yielding unqualified candidate_id={cand.get('candidate_id')} missing={assessment['missing'][:3]} excluded={assessment['excluded'][:3]}",
                        )
                    await emit_candidate(cand, assessment, "qualified_applicants")
            except Exception as e:
                logger.error(f"JobDiva Applicants stage failed: {e}", exc_info=True)
            finally:
                await queue.put(SENTINEL)

        async def produce_jobdiva_talent():
            """
            Run the boolean-string Talent Search against the JobDiva talent pool.
            Independent of Applicants — runs whenever "JobDiva" is in sources.
            """
            try:
                if not talent_selected:
                    return
                await queue.put({"type": "stage", "data": "Searching JobDiva Talent Search..."})
                self._log_stage("TalentSearch", "Running JobDiva Talent boolean search...")
                talent_res = await self._search_jobdiva_talent(criteria)
                talent_pool = talent_res.get("candidates", [])
                summary["talent_search_count"] = len(talent_pool)
                if talent_res.get("jobdiva_criteria_unconfigured"):
                    summary["jobdiva_criteria_unconfigured"] = True

                # HOTFIX: Hard cap at 100 — see Applicants stage above.
                # JobAgent results arrive in JobDiva's API rank order
                # (preserved end-to-end via `api_rank`); this slice keeps the
                # top-100 by JobDiva's own ranking.
                if talent_pool and len(talent_pool) > 100:
                    self._log_stage(
                        "TalentSearch",
                        f"Capping {len(talent_pool)} talent profiles to top-100 by JobAgent rank.",
                    )
                    talent_pool = talent_pool[:100]

                if not talent_pool:
                    self._log_stage("TalentSearch", "No talent-pool candidates returned.")
                    return
                self._attach_cached_enhanced_info(talent_pool)
                from core import sourcing_config as _sc_talent
                async for cand in self._enrich_filtered_jobdiva_candidates(talent_pool, criteria):
                    assessment = self._filter_assessment(cand, criteria, enforce_years=True)
                    if _sc_talent.JOBDIVA_BYPASS_PASS_GATE:
                        assessment["passes"] = True
                    elif not assessment["passes"]:
                        self._log_stage(
                            "TalentSearch",
                            f"yielding unqualified candidate_id={cand.get('candidate_id')} missing={assessment['missing'][:3]} excluded={assessment['excluded'][:3]}",
                        )
                    await emit_candidate(cand, assessment, "qualified_talent")
            except Exception as e:
                logger.error(f"JobDiva Talent stage failed: {e}", exc_info=True)
            finally:
                await queue.put(SENTINEL)

        async def produce_external(name, search_method):
            try:
                await queue.put({"type": "stage", "data": f"Searching {name}..."})
                res = await search_method(criteria)
                if not res:
                    return
                ext_candidates = res.get("candidates", [])
                source_type = res.get("source_type", name)
                summary[f"{source_type.lower()}_count"] = len(ext_candidates)

                # HOTFIX: Hard cap at 100 — see Applicants stage above.
                # F5: pre-rank by cheap title/skill keyword match before
                # slicing. External sources (Exa/Dice/Unipile) ship enough
                # signal in `title` + highlight text to rank meaningfully,
                # and an unranked FIFO slice can discard the best matches
                # if Exa returns ordered by its own relevance and we ask
                # for 50 results but the cap fires elsewhere.
                if ext_candidates and len(ext_candidates) > 100:
                    before_rank = len(ext_candidates)
                    ext_candidates = self._rank_candidates_by_skill(
                        ext_candidates, criteria, keep_top=100
                    )
                    self._log_stage(
                        source_type,
                        f"Capping {before_rank} {source_type} profiles to "
                        f"top-{len(ext_candidates)} by skill+title rank.",
                    )

                self._log_stage(source_type, f"Found {len(ext_candidates)} profiles; starting streaming enrichment...")

                semaphore = asyncio.Semaphore(5)

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
                                    full_profile = await self.unipile_service.get_candidate_profile(provider_id)
                                    if full_profile:
                                        cand.update(self._extract_linkedin_profile_data(full_profile))
                                except Exception as e:
                                    logger.warning(f"Failed to fetch full profile for LinkedIn candidate {provider_id}: {e}")
                            # After enrichment, run the same role-anchor check.
                            if not self._candidate_title_match(cand, criteria):
                                return {"status": "failed_title_match"}

                        # PR-B: cheap pre-LLM YOE gate for external sources too.
                        # Drops candidates whose headline / abstract / resume
                        # snippet shows fewer years than the configured floor.
                        if self._candidate_below_min_years_pre_llm(cand, criteria):
                            return {"status": "failed_filter"}

                        assessment = self._filter_assessment(cand, criteria, enforce_years=False)
                        if not assessment["passes"]:
                            return {"status": "failed_filter"}

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
                        cand["email"] = cand["enhanced_info"].get("email") or cand.get("email")
                        cand["phone"] = cand["enhanced_info"].get("phone") or cand.get("phone")
                        cand["title"] = cand["enhanced_info"].get("job_title") or cand.get("title")
                        cand["location"] = cand["enhanced_info"].get("current_location") or cand.get("location")
                        if cand["enhanced_info"].get("structured_skills") or cand["enhanced_info"].get("skills"):
                            cand["skills"] = cand["enhanced_info"].get("structured_skills") or cand["enhanced_info"].get("skills")

                        # Exa deep analysis on filter survivors only. Replaces
                        # the 4000-char highlights with the full profile text
                        # plus a per-candidate match summary; preserves the
                        # original highlights in resume_text since downstream
                        # location extractors are tuned to that shape.
                        if (
                            source_type == "LinkedIn-Exa"
                            and os.getenv("EXA_DEEP_ANALYSIS_ENABLED", "true").strip().lower() == "true"
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

                        # In-line ZoomInfo → Apollo enrichment for survivors of
                        # the cheap filter gates that still have neither email
                        # nor phone. Gated by CONTACT_ENRICHMENT_INLINE_ENABLED
                        # inside the helper; capped per-job at
                        # contact_enrichment.PER_JOB_CAP so cost is bounded.
                        if not (str(cand.get("email") or "").strip() or str(cand.get("phone") or "").strip()):
                            profile_url = str(cand.get("profile_url") or "").strip()
                            if "linkedin.com/in/" in profile_url.lower():
                                try:
                                    enrich = await contact_enrichment.enrich_contact_for_sourcing(
                                        profile_url, criteria.job_id
                                    )
                                except Exception as e:
                                    logger.warning("contact_enrichment failed for %s: %s", cand.get("id"), e)
                                    enrich = {}
                                if enrich:
                                    cand["email"] = (
                                        cand.get("email")
                                        or enrich.get("workEmail")
                                        or enrich.get("personalEmail")
                                        or ""
                                    )
                                    cand["phone"] = (
                                        cand.get("phone")
                                        or enrich.get("mobilePhone")
                                        or enrich.get("workPhone")
                                        or ""
                                    )
                                    if cand.get("email") or cand.get("phone"):
                                        cand["enhanced_info"]["contact_enrichment_provider"] = enrich.get("provider_used")
                        return {"status": "success", "candidate": cand}

                process_tasks = [asyncio.create_task(_process_external_single(c)) for c in ext_candidates]
                for task in asyncio.as_completed(process_tasks):
                    result = await task
                    if result["status"] == "success":
                        cand = result["candidate"]
                        assessment = self._filter_assessment(cand, criteria, enforce_years=False)
                        await emit_candidate(cand, assessment)
            except Exception as e:
                logger.error(f"{name} search stage failed: {e}", exc_info=True)
            finally:
                await queue.put(SENTINEL)

        # Build producer tasks for all selected sources — run in parallel.
        # JobDiva Applicants and JobDiva Talent are now independent producers,
        # each with its own SENTINEL, so they stream concurrently alongside Exa/Unipile/Dice.
        producers = []
        if applicants_selected:
            producers.append(asyncio.create_task(produce_jobdiva_applicants()))
        if talent_selected:
            producers.append(asyncio.create_task(produce_jobdiva_talent()))

        external_order = [
            ("LinkedIn", self._search_linkedin),
            ("Dice", self._search_dice),
            ("Exa", self._search_exa),
        ]
        for ext_name, ext_method in external_order:
            if ext_name in criteria.sources:
                producers.append(asyncio.create_task(produce_external(ext_name, ext_method)))

        # Drain the queue until every producer emits its SENTINEL.
        # Accumulate JobDiva-sourced candidates so we can hydrate them in
        # the background after producers finish (fast-path mode only).
        active = len(producers)
        hydration_targets: List[Dict[str, Any]] = []
        hydration_task: Optional[asyncio.Task] = None
        try:
            while active > 0:
                event = await queue.get()
                if event is SENTINEL:
                    active -= 1
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
            if hydration_targets and _sc.FAST_PATH_SKIP_DETAIL_IN_TALENT_SEARCH:
                def _hydration_key(c: Dict[str, Any]) -> int:
                    rank = c.get("api_rank")
                    return rank if isinstance(rank, int) else 10**9
                hydration_targets.sort(key=_hydration_key)
                hydration_targets = hydration_targets[
                    : _sc.FAST_PATH_DETAIL_BACKGROUND_MAX_CANDIDATES
                ]
                hydration_task = asyncio.create_task(
                    self._hydrate_jobdiva_in_background(
                        hydration_targets, queue, SENTINEL
                    )
                )
                # Drain hydration events. Hydration emits SENTINEL when done.
                while True:
                    event = await queue.get()
                    if event is SENTINEL:
                        break
                    yield event
        finally:
            # If the generator is closed (e.g. client disconnect), cancel
            # all background work — producers and hydration alike.
            for task in producers:
                if not task.done():
                    task.cancel()
            if hydration_task and not hydration_task.done():
                hydration_task.cancel()

            pending = list(producers)
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


    async def _hydrate_jobdiva_in_background(
        self,
        candidates: List[Dict[str, Any]],
        queue: asyncio.Queue,
        sentinel: Any,
    ) -> None:
        """Page through CandidatesDetail for the top-scored JobDiva candidates
        and emit one ``candidate_detail`` event per hydrated row.

        Paced to respect JobDiva's rate limit:
        - Pages run serially (size = FAST_PATH_DETAIL_BACKGROUND_PAGE_SIZE).
        - Within a page we let ``_fetch_candidate_details_batch`` use its
          existing batching, which is one request per chunk.
        - On exception we exponential-backoff (5s → 60s cap) and continue
          to the next page rather than failing the whole search.
        - A short sleep between pages spreads the load.

        Emits ``sentinel`` on the queue exactly once when done so the
        orchestrator's drain loop terminates cleanly.
        """
        try:
            from core import sourcing_config as sc
            from services.jobdiva import get_field

            page_size = max(1, int(sc.FAST_PATH_DETAIL_BACKGROUND_PAGE_SIZE))
            page_delay = max(0.0, float(sc.FAST_PATH_DETAIL_BACKGROUND_PAGE_DELAY_S))

            token = await self.jobdiva_service.authenticate()
            if not token:
                logger.warning(
                    "Hydration skipped: JobDiva authentication failed."
                )
                return

            backoff_s = 5.0
            total_pages = (len(candidates) + page_size - 1) // page_size
            for page_idx in range(total_pages):
                page = candidates[page_idx * page_size : (page_idx + 1) * page_size]
                page_ids = [
                    str(c.get("candidate_id") or c.get("id") or "")
                    for c in page
                    if c.get("candidate_id") or c.get("id")
                ]
                if not page_ids:
                    continue

                try:
                    detail_map = await self.jobdiva_service._fetch_candidate_details_batch(
                        token, page_ids
                    )
                    # Reset backoff on success.
                    backoff_s = 5.0
                except Exception as exc:
                    logger.warning(
                        "Hydration page %d/%d failed: %s — backing off %.1fs",
                        page_idx + 1, total_pages, exc, backoff_s,
                    )
                    try:
                        await asyncio.sleep(backoff_s)
                    except asyncio.CancelledError:
                        raise
                    backoff_s = min(60.0, backoff_s * 2.0)
                    continue

                # Convert each detail record into a patch and emit.
                hydrated = 0
                for cand_id, detail in (detail_map or {}).items():
                    if not detail:
                        continue
                    patch: Dict[str, Any] = {}
                    email = (get_field(detail, [
                        "email", "EMAIL", "emailAddress", "EMAILADDRESS",
                        "emails", "EMAILS", "emailId", "EMAILID",
                        "email1", "EMAIL1", "email2", "EMAIL2",
                        "alternateEmail", "ALTERNATEEMAIL",
                    ]) or "")
                    phone = (get_field(detail, [
                        "phone", "PHONE", "phoneNumber", "PHONENUMBER",
                        "mobilePhone", "MOBILEPHONE", "phone1", "PHONE1",
                        "cellPhone", "CELLPHONE",
                    ]) or "")
                    address1 = (get_field(detail, ["address1", "ADDRESS1", "address", "ADDRESS"]) or "")
                    linkedin = (get_field(detail, ["linkedinUrl", "LINKEDINURL", "linkedin", "LINKEDIN", "linkedIn", "LINKEDIN_URL"]) or "")
                    resume_id = (get_field(detail, ["resumeId", "RESUMEID", "resume_id"]) or "")
                    resume_text = self.jobdiva_service._extract_resume_text(detail) or ""
                    city = (get_field(detail, ["city", "CITY", "locationCity", "LOCATIONCITY"]) or "")
                    state = (get_field(detail, ["state", "STATE", "locationState", "LOCATIONSTATE"]) or "")
                    if email:
                        patch["email"] = str(email).strip()
                    if phone:
                        patch["phone"] = str(phone).strip()
                    if address1:
                        patch["address1"] = str(address1).strip()
                    if linkedin:
                        patch["linkedin_url"] = str(linkedin).strip()
                    if resume_id:
                        patch["resume_id"] = str(resume_id).strip()
                    if resume_text:
                        patch["resume_text"] = resume_text
                        patch["abstract"] = resume_text[:240].replace("\n", " ").strip()
                    if city:
                        patch["city"] = str(city).strip()
                    if state:
                        patch["state"] = str(state).strip()

                    if not patch:
                        continue

                    await queue.put({
                        "type": "candidate_detail",
                        "candidate_id": str(cand_id),
                        "patch": patch,
                    })
                    hydrated += 1

                logger.info(
                    "Hydration page %d/%d: %d patches for %d ids",
                    page_idx + 1, total_pages, hydrated, len(page_ids),
                )

                if page_delay > 0 and page_idx + 1 < total_pages:
                    try:
                        await asyncio.sleep(page_delay)
                    except asyncio.CancelledError:
                        raise
        except asyncio.CancelledError:
            # Caller closed the generator. Let the SENTINEL still fire in finally.
            raise
        except Exception as exc:
            logger.exception("Hydration task failed: %s", exc)
        finally:
            try:
                await queue.put(sentinel)
            except Exception:
                pass


    async def _search_jobdiva_talent(self, criteria: SearchCriteria) -> Dict[str, Any]:
        """JobDiva talent-pool sourcing via JobAgentSearch.

        JobAgentSearch (JobDiva's AI matcher) is anchored to the job's JobDiva
        ID and returns a per-job ranked candidate set. We then apply a
        client-side state filter to backstop the geo precision JobDiva does
        not give us.

        Surfaces `criteria_unconfigured: True` in the return when JobAgent
        responded with "Criteria Not Assigned" — frontend uses this to nudge
        the recruiter to set search criteria in JobDiva's web UI.
        """
        source_type = "JobDiva-JobAgent"
        try:
            candidates: List[Dict[str, Any]] = []
            criteria_unconfigured = False

            if criteria.job_id:
                resume_count = max(200, (criteria.page_size or 50) * 4)
                ja_result = await self.jobdiva_service.search_via_job_agent(
                    job_id=criteria.job_id,
                    resume_count=resume_count,
                    require_resume=getattr(criteria, "require_resume", True),
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
                    f"raw={len(candidates)} criteria_unconfigured={criteria_unconfigured}"
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

    async def _search_jobdiva_applicants(self, criteria: SearchCriteria) -> Dict[str, Any]:
        try:
            token = await self.jobdiva_service.authenticate()
            if not token:
                return {"candidates": [], "source_type": "JobDiva-Applicants"}
            candidates = await self.jobdiva_service._get_all_job_applicants(
                criteria.job_id,
                criteria.page_size,
                token
            )
            self._log_stage("Applicants", f"JobDiva returned {len(candidates)} candidate(s)")
            # No pre-screening needed - JobApplicantsDetail has no title/skills data
            # Go directly to LLM extraction
            for c in candidates:
                c["source"] = "JobDiva-Applicants"
            self._log_stage("Applicants", f"Proceeding to LLM extraction for {len(candidates)} candidate(s)")
            return {"candidates": candidates, "source_type": "JobDiva-Applicants"}
        except Exception as e:
            logger.error(f"JobDiva Applicants search failed: {e}")
            return {"candidates": [], "source_type": "JobDiva-Applicants"}

    # 2-letter US state codes for the location heuristic.
    _US_STATE_CODES = frozenset({
        "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
        "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
        "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
        "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
        "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
        "DC",
    })

    # Country aliases → JobDiva expects the 2-letter code in talentSearchDef.
    _COUNTRY_ALIASES = {
        "US": "US", "USA": "US", "U.S.": "US", "U.S.A.": "US",
        "UNITED STATES": "US", "UNITED STATES OF AMERICA": "US",
        "CA": "CA", "CAN": "CA", "CANADA": "CA",
        "UK": "GB", "U.K.": "GB", "UNITED KINGDOM": "GB", "GREAT BRITAIN": "GB", "GB": "GB",
        "IN": "IN", "INDIA": "IN",
        "AU": "AU", "AUS": "AU", "AUSTRALIA": "AU",
        "DE": "DE", "GERMANY": "DE",
        "FR": "FR", "FRANCE": "FR",
        "MX": "MX", "MEXICO": "MX",
        "BR": "BR", "BRAZIL": "BR",
        "JP": "JP", "JAPAN": "JP",
        "CN": "CN", "CHINA": "CN",
        "SG": "SG", "SINGAPORE": "SG",
        "IE": "IE", "IRELAND": "IE",
        "NL": "NL", "NETHERLANDS": "NL",
        "ES": "ES", "SPAIN": "ES",
        "IT": "IT", "ITALY": "IT",
    }

    # Country values we treat as "US" when present on the candidate record.
    _US_COUNTRY_TOKENS = frozenset({
        "us", "usa", "u.s.", "u.s.a.", "united states",
        "united states of america", "america",
    })

    # Substrings that, when present in a candidate's location text, are
    # confident evidence of a non-US location. Each entry is matched as a
    # word-boundary substring against a space-padded version of the location
    # text, so e.g. " india " matches but "indianapolis" does not.
    _NON_US_LOCATION_TOKENS = frozenset({
        "india", "united kingdom", "canada", "australia", "germany",
        "france", "philippines", "pakistan", "china", "ireland", "mexico",
        "brazil", "spain", "italy", "netherlands", "singapore", "uae",
        "dubai", "saudi arabia", "japan", "south korea", "vietnam",
        "indonesia", "malaysia", "thailand", "egypt", "nigeria",
        "south africa", "russia", "ukraine", "poland", "turkey",
        "israel", "argentina", "chile", "colombia", "peru", "venezuela",
        "bangladesh", "sri lanka", "nepal", "kenya", "ghana", "morocco",
        "switzerland", "sweden", "norway", "denmark", "finland", "belgium",
        "austria", "portugal", "greece", "hungary", "romania",
        "bulgaria", "iran", "iraq", "afghanistan", "qatar", "kuwait",
        "bahrain", "oman", "jordan", "lebanon", "ethiopia", "tanzania",
        "uganda", "zimbabwe", "new zealand", "taiwan", "hong kong",
        "u.k.", "england", "scotland",
    })

    def _resolve_jobdiva_geo(self, criteria: SearchCriteria) -> tuple[List[str], List[str]]:
        """
        Produce (countries, states) for JobDiva's talentSearchDef.

        Priority: explicit `criteria.countries` / `criteria.states` if set;
        otherwise heuristically split `criteria.location` by comma and pick
        out a US state code and/or a country. Always defaults to ``["US"]``
        when no country can be resolved — searches are US-only by policy.
        """
        countries = [c.strip() for c in (criteria.countries or []) if c and c.strip()]
        states = [s.strip() for s in (criteria.states or []) if s and s.strip()]
        if countries or states:
            return countries, states

        loc = (criteria.location or "").strip()
        if not loc:
            # No location criteria at all → still scope to US-only.
            return ["US"], []

        tokens = [t.strip() for t in loc.split(",") if t.strip()]
        if not tokens:
            return ["US"], []

        # Walk tokens right-to-left: first match country, then state.
        consumed: set = set()
        for idx in range(len(tokens) - 1, -1, -1):
            token_upper = tokens[idx].upper()
            if token_upper in self._COUNTRY_ALIASES:
                countries.append(self._COUNTRY_ALIASES[token_upper])
                consumed.add(idx)
                break

        for idx in range(len(tokens) - 1, -1, -1):
            if idx in consumed:
                continue
            token_upper = tokens[idx].upper()
            if len(token_upper) == 2 and token_upper in self._US_STATE_CODES:
                states.append(token_upper)
                if not countries:
                    countries.append("US")
                break

        if not countries:
            countries.append("US")

        return countries, states

    # Adjacent-state map for the "within N miles spills across state lines"
    # case (Charlotte NC ↔ Fort Mill SC, NYC ↔ Jersey City NJ, etc.). Hand-
    # written, conservative — only the metros recruiters actually ask about.
    # If a state isn't here, _filter_by_state behaves as strict-state.
    _ADJACENT_STATES = {
        "NC": {"SC", "VA", "TN", "GA"},
        "SC": {"NC", "GA"},
        "VA": {"NC", "WV", "MD", "DC", "TN", "KY"},
        "NY": {"NJ", "CT", "PA", "MA", "VT"},
        "NJ": {"NY", "PA", "DE"},
        "PA": {"NY", "NJ", "OH", "WV", "MD", "DE"},
        "CT": {"NY", "MA", "RI"},
        "MA": {"NY", "CT", "RI", "NH", "VT"},
        "MD": {"VA", "PA", "DE", "WV", "DC"},
        "DC": {"MD", "VA"},
        "DE": {"MD", "PA", "NJ"},
        "CA": {"NV", "OR", "AZ"},
        "TX": {"OK", "LA", "AR", "NM"},
        "WA": {"OR", "ID"},
        "OR": {"WA", "CA", "ID", "NV"},
        "IL": {"IN", "WI", "IA", "MO", "KY"},
        "IN": {"IL", "OH", "KY", "MI"},
        "OH": {"PA", "WV", "KY", "IN", "MI"},
        "FL": {"GA", "AL"},
        "GA": {"FL", "AL", "TN", "NC", "SC"},
        "AZ": {"CA", "NV", "UT", "NM"},
        "MI": {"IN", "OH", "WI"},
        "MN": {"WI", "IA", "ND", "SD"},
        "WI": {"IL", "IA", "MN", "MI"},
        "CO": {"WY", "NM", "UT", "KS", "NE", "OK"},
    }

    # Wider regional clusters when within_miles is large (≥200 mi).
    _REGIONAL_CLUSTERS = [
        {"NY", "NJ", "PA", "CT", "MA", "RI"},
        {"NC", "SC", "VA", "TN", "GA"},
        {"TX", "OK", "LA", "AR", "NM"},
        {"CA", "NV", "OR", "AZ", "WA"},
        {"IL", "IN", "OH", "MI", "WI", "MN", "IA", "KY", "MO"},
        {"FL", "GA", "AL", "MS", "LA"},
        {"MD", "DC", "VA", "DE", "PA"},
    ]

    def _filter_by_state(
        self,
        candidates: List[Dict[str, Any]],
        criteria: SearchCriteria,
    ) -> List[Dict[str, Any]]:
        """Location + US-only gate.

        US-only is applied unconditionally; only candidates with positive
        evidence of a non-US location are dropped. The radius/state check
        runs only when ``criteria.location`` is set, and soft-keeps any
        candidate whose location can't be resolved.
        """
        if not candidates:
            return candidates

        enforce_location = self._should_enforce_location(criteria)

        kept: List[Dict[str, Any]] = []
        non_us_dropped = 0
        filtered = 0
        geocode_failed = 0

        for c in candidates:
            if self._is_likely_non_us(c):
                non_us_dropped += 1
                continue

            if not enforce_location:
                kept.append(c)
                continue

            is_match, reason, distance = self._location_match_verdict(c, criteria)
            if distance is not None:
                c["distance_miles"] = round(float(distance), 1)
            if reason:
                c["location_match_reason"] = reason
            if is_match:
                kept.append(c)
            else:
                filtered += 1
                if reason in {"candidate_ungeocodable", "target_ungeocodable"}:
                    geocode_failed += 1

        self._log_stage(
            "LocationGate",
            f"pre-filter kept {len(kept)}/{len(candidates)} candidates"
            f" (non_us_dropped={non_us_dropped}, filtered={filtered},"
            f" geocode_failures={geocode_failed})",
        )
        return kept

    @staticmethod
    def _candidate_haystack(c: Dict[str, Any]) -> str:
        """Concatenated lowercase text used for skill keyword matching."""
        parts = [
            str(c.get("title") or ""),
            str(c.get("abstract") or ""),
            str(c.get("resume_text") or ""),
            " ".join(str(s) for s in (c.get("skills") or [])),
        ]
        return " ".join(p for p in parts if p).lower()

    def _rank_candidates_by_skill(
        self,
        candidates: List[Dict[str, Any]],
        criteria: SearchCriteria,
        keep_top: int,
    ) -> List[Dict[str, Any]]:
        """Pre-rank candidates by skill keyword match against title/abstract/resume.

        Used on the TalentSearch fallback path where JobDiva returns its broken
        fixed pool — without this, the downstream LLM ranker sees ~280 mostly-
        irrelevant candidates and surfaces poor matches. Scoring:

        - +3 per `must` skill or title term hit (case-insensitive substring).
        - +1 per `can` (preferred) hit.
        - +1 per plain `keywords[]` hit.
        - -10 per `exclude` term hit (effectively drops the candidate).
        - Drop candidates whose `must` hit-count is 0 when `must` terms exist.

        Returns at most `keep_top` candidates, sorted by score desc, ties
        broken by JobDiva's original ordering.
        """
        must_terms: List[str] = []
        can_terms: List[str] = []
        exclude_terms: List[str] = []
        for item in (criteria.title_criteria or []) + (criteria.skill_criteria or []):
            value = str(item.get("value", "")).strip().lower()
            if not value:
                continue
            mt = item.get("match_type", "must")
            if mt == "exclude":
                exclude_terms.append(value)
            elif mt == "can":
                can_terms.append(value)
            else:
                must_terms.append(value)
        keyword_terms = [str(k).strip().lower() for k in (criteria.keywords or []) if str(k).strip()]

        # If we have no skill signal at all, leave the order alone.
        if not (must_terms or can_terms or keyword_terms or exclude_terms):
            return candidates[:keep_top]

        # Match a term against haystack: full-phrase match counts 1.0;
        # otherwise count significant tokens (len>=4) and award a partial
        # match scaled to coverage. This matters because users type long
        # phrases ("Java Full Stack Development") but resumes have shorter
        # canonical forms ("Java"). Without partial match we would over-drop.
        def term_score(term: str, hay_text: str) -> float:
            if not term or not hay_text:
                return 0.0
            if term in hay_text:
                return 1.0
            tokens = [t for t in term.split() if len(t) >= 4]
            if not tokens:
                return 0.0
            hits = sum(1 for t in tokens if t in hay_text)
            cov = hits / len(tokens)
            # Require at least one significant token; reward higher coverage.
            return cov if cov >= 1.0 / max(1, len(tokens)) else 0.0

        # First pass — strict scoring. Drop candidates that miss every must
        # term or trigger any exclude term, score the rest.
        strict: List[tuple] = []
        soft: List[tuple] = []  # parallel weak-signal pool used only if strict is empty
        any_haystack = 0
        for idx, c in enumerate(candidates):
            hay = self._candidate_haystack(c)
            if hay:
                any_haystack += 1
            must_score = sum(term_score(t, hay) for t in must_terms) if hay else 0
            must_hits = 1 if must_score > 0 else 0
            can_score = sum(term_score(t, hay) for t in can_terms) if hay else 0
            kw_score = sum(term_score(t, hay) for t in keyword_terms) if hay else 0
            exc_hits = sum(1 for t in exclude_terms if t in hay) if hay else 0
            soft_score = can_score + kw_score - 10.0 * exc_hits
            strict_score = 3.0 * must_score + soft_score
            if exc_hits:
                continue  # always drop exclude-hits regardless of mode
            if must_terms:
                if must_hits > 0 and strict_score > 0:
                    strict.append((strict_score, idx, c))
                else:
                    soft.append((soft_score, idx, c))
            else:
                strict.append((strict_score, idx, c))

        # Soft-mode fallback: when haystacks are mostly empty (TalentSearch
        # often returns thin records) or no strict matches survive, fall back
        # to "any signal beats nothing" so we don't return 0.
        sparse = any_haystack < max(3, len(candidates) // 3)
        if not strict or sparse:
            if sparse:
                logger.debug(
                    "_rank_candidates_by_skill: sparse haystacks "
                    f"({any_haystack}/{len(candidates)}); using soft mode"
                )
            else:
                logger.debug(
                    "_rank_candidates_by_skill: no strict matches; using soft mode"
                )
            scored = strict + soft
            # Original-order tiebreak preserves JobDiva's ranking when scores tie.
            scored.sort(key=lambda t: (-t[0], t[1]))
            return [c for _, _, c in scored[:keep_top]]

        strict.sort(key=lambda t: (-t[0], t[1]))
        return [c for _, _, c in strict[:keep_top]]

    @staticmethod
    def _strip_location_from_boolean(boolean: str, location: str) -> str:
        """Remove the auto-appended `"<location>"` term from a boolean string.

        Conservative: only strips an exact quoted match (case-insensitive)
        with adjacent ` AND ` glue. If the location can't be found cleanly,
        returns the input unchanged so user-typed booleans aren't mangled.
        """
        if not boolean:
            return boolean
        loc = (location or "").strip()
        if not loc:
            return boolean

        quoted = re.escape(f'"{loc}"')
        patterns = [
            rf'\s+AND\s+{quoted}(?=\s|$|\))',  # mid/tail: " AND \"X\""
            rf'(?<=^){quoted}\s+AND\s+',       # leading: "\"X\" AND "
            rf'(?<=\()\s*{quoted}\s+AND\s+',   # inside group: "(\"X\" AND ..."
            rf'\s+AND\s+{quoted}(?=\))',       # before group close
        ]
        out = boolean
        for pat in patterns:
            new_out = re.sub(pat, lambda m: ' ' if m.group(0).startswith(' AND ') else '', out, flags=re.IGNORECASE)
            if new_out != out:
                out = new_out
                break
        return re.sub(r'\s+', ' ', out).strip()

    def _jobdiva_search_terms(self, criteria: SearchCriteria) -> List[Dict[str, Any]]:
        terms: List[Dict[str, Any]] = []
        for item in criteria.title_criteria + criteria.skill_criteria:
            value = str(item.get("value", "")).strip()
            if not value:
                continue
            terms.append({
                "value": value,
                "match_type": item.get("match_type", "must"),
                "years": item.get("years", 0),
                "recent": item.get("recent", False),
            })
        for value in criteria.keywords:
            if value and value.strip():
                terms.append({"value": value.strip(), "match_type": "must"})
        return terms

    def _build_boolean_string(self, criteria: SearchCriteria) -> str:
        def quote(value: str) -> str:
            return f'"{value.strip()}"'

        def normalize_term(value: str) -> str:
            value = str(value or "").lower().strip()
            value = value.replace('"', "").replace("(", "").replace(")", "")
            value = re.sub(r"^must be local to\s*", "", value)
            value = re.sub(r"\s*metro$", "", value)
            value = re.sub(r"^must not be employed by:\s*", "", value)
            value = re.sub(r"\s+within\s+\d+\s+mi$", "", value)
            value = re.sub(r"\s+recent$", "", value)
            value = re.sub(r"\s+over\s+\d+\s+years?$", "", value)
            return re.sub(r"\s+", " ", value).strip()

        def add_unique(bucket: List[str], seen: set, clause: str, key_value: str = "") -> None:
            key = normalize_term(key_value or clause)
            if not clause or not key or key in seen:
                return
            seen.add(key)
            bucket.append(clause)

        must_groups = []
        can_terms = []
        exclude_terms = []
        seen_must = set()
        seen_can = set()
        seen_exclude = set()
        source_keys = set()

        for item in criteria.title_criteria + criteria.skill_criteria:
            value = str(item.get("value", "")).strip()
            if not value:
                continue
            source_keys.add(normalize_term(value))
            variants = [quote(value)]
            for similar in item.get("similar_terms", []) or []:
                if str(similar).strip():
                    source_keys.add(normalize_term(str(similar)))
                    variants.append(quote(str(similar)))
            group = variants[0] if len(variants) == 1 else f"({' OR '.join(variants)})"
            match_type = item.get("match_type", "must")
            if match_type == "exclude":
                add_unique(exclude_terms, seen_exclude, group, value)
            elif match_type == "can":
                add_unique(can_terms, seen_can, group, value)
            else:
                add_unique(must_groups, seen_must, group, value)

        for keyword in criteria.keywords:
            if keyword and keyword.strip():
                add_unique(must_groups, seen_must, quote(keyword), keyword)
        for company in criteria.companies:
            if company and company.strip():
                source_keys.add(normalize_term(company))
                add_unique(must_groups, seen_must, quote(company), company)

        parts = must_groups[:]
        if can_terms:
            parts.append(f"({' OR '.join(can_terms)})")
        if criteria.location:
            add_unique(parts, seen_must, quote(criteria.location), criteria.location)

        boolean_string = " AND ".join(part for part in parts if part and part != "()") or "*"
        if exclude_terms:
            boolean_string = f"{boolean_string} NOT ({' OR '.join(exclude_terms)})"
        
        logger.info(f"Boolean string built from Page 5 sourcing filters only: {boolean_string[:150]}...")
        return boolean_string

    def _filter_candidates(
        self,
        candidates: List[Dict[str, Any]],
        criteria: SearchCriteria,
        source_type: str = "applicants",
    ) -> List[Dict[str, Any]]:
        dimensions = self._collect_sourcing_dimensions(criteria)  # Use sourcing dimensions for pre-screening
        enforce_location = self._should_enforce_location(criteria)
        filterable_dimensions = [
            dimension for dimension in dimensions
            if dimension["label"] in {"Titles", "Skills", "Location", "Company Experience", "Keywords"}
        ]
        if not any(
            dimension["required"] or dimension["preferred"] or dimension["excluded"]
            for dimension in filterable_dimensions
        ):
            return candidates

        title_groups: List[List[str]] = []
        skill_groups: List[List[str]] = []
        company_groups: List[List[str]] = []
        keyword_groups: List[List[str]] = []
        exclude_groups: List[List[str]] = []

        for dimension in filterable_dimensions:
            exclude_groups.extend(dimension.get("excluded_groups", []))
            if dimension["label"] == "Titles":
                title_groups.extend(dimension.get("required_groups", []))
            elif dimension["label"] == "Skills":
                skill_groups.extend(dimension.get("required_groups", []))
            elif dimension["label"] == "Company Experience":
                company_groups.extend(dimension.get("required_groups", []))
            elif dimension["label"] == "Keywords":
                keyword_groups.extend(dimension.get("required_groups", []))

        def group_matches(haystack: str, group: List[str]) -> bool:
            return any(term and term in haystack for term in self._dedupe_terms(group))

        filtered = []
        non_us_dropped = 0
        for candidate in candidates:
            haystack = self._candidate_summary_text(candidate)

            # US-only scope: drop only when the candidate's country/location
            # text is positive evidence of a non-US location. Silent records
            # are treated as US (kept).
            if self._is_likely_non_us(candidate):
                non_us_dropped += 1
                continue

            if any(group_matches(haystack, group) for group in exclude_groups):
                continue

            # Location is no longer a hard pre-enrichment drop. Policy update
            # 2026-05-13: when in doubt, surface the candidate with a score
            # signal instead of silently rejecting. Recruiter's UI filter
            # handles final location selection. Distance / state-mismatch
            # still flow into screening_summary and the score dimension.

            # Titles are alternative labels for the role, so one matching title is
            # enough at the pre-enrichment stage.
            if title_groups and self._has_visible_field(candidate, ["title", "headline"]):
                if not any(group_matches(haystack, group) for group in title_groups):
                    continue

            # Skills can be sparse in applicant/TalentSearch summaries. If JobDiva
            # returned skill text, use it now; otherwise defer skill proof to the
            # LLM-enriched resume screen.
            if skill_groups and self._candidate_has_visible_skills(candidate):
                if not all(group_matches(haystack, group) for group in skill_groups):
                    continue

            if company_groups and self._has_visible_field(candidate, ["company", "employer", "current_company"]):
                if not all(group_matches(haystack, group) for group in company_groups):
                    continue

            if keyword_groups and source_type == "talent_search":
                if not all(group_matches(haystack, group) for group in keyword_groups):
                    continue

            filtered.append(candidate)

        self._log_stage(
            "SummaryScreen",
            f"{source_type}: kept {len(filtered)} of {len(candidates)} candidate(s)"
            f" (non_us_dropped={non_us_dropped})",
        )
        return filtered

    def _candidate_summary_text(self, candidate: Dict[str, Any]) -> str:
        skills = candidate.get("skills", []) or []
        skill_text = json.dumps(skills) if not isinstance(skills, str) else skills
        pieces = [
            candidate.get("name", ""),
            candidate.get("firstName", ""),
            candidate.get("lastName", ""),
            candidate.get("title", ""),
            candidate.get("headline", ""),
            candidate.get("city", ""),
            candidate.get("state", ""),
            candidate.get("location", ""),
            candidate.get("company", ""),
            candidate.get("employer", ""),
            candidate.get("current_company", ""),
            skill_text,
        ]
        return self._normalize_search_text(" ".join(str(piece) for piece in pieces if piece))

    def _normalize_search_text(self, value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "").lower()).strip()

    def _has_visible_field(self, candidate: Dict[str, Any], field_names: List[str]) -> bool:
        return any(str(candidate.get(field) or "").strip() for field in field_names)

    def _candidate_has_visible_skills(self, candidate: Dict[str, Any]) -> bool:
        skills = candidate.get("skills")
        if isinstance(skills, list) and len(skills) > 0:
            return True
        return bool(str(skills or "").strip())

    def _location_matches(self, candidate: Dict[str, Any], criteria: SearchCriteria) -> bool:
        is_match, _reason, distance = self._location_match_verdict(candidate, criteria)
        if distance is not None:
            candidate["distance_miles"] = round(float(distance), 1)
        return is_match

    def _candidate_structured_locations(self, candidate: Dict[str, Any]) -> List[str]:
        """Return the candidate's current residence locations only.

        Only "where the candidate is now" signals are used — never historical
        job locations, resume free-text locations, or company HQ addresses —
        so the radius filter doesn't accidentally match a candidate to a city
        they worked in five years ago.

        Order of preference:
        1. ``enhanced_info.current_location`` (LLM-extracted from resume header)
        2. ``candidate.location`` (live source field)
        3. ``candidate.city + ", " + candidate.state`` (live source field)
        """
        enhanced = candidate.get("enhanced_info") or {}
        enhanced_dict = enhanced if isinstance(enhanced, dict) else {}

        city = str(candidate.get("city") or "").strip()
        state = str(candidate.get("state") or "").strip()
        city_state = f"{city}, {state}".strip(", ") if (city or state) else ""

        location_values = [
            enhanced_dict.get("current_location"),
            candidate.get("location"),
            city_state,
        ]
        cleaned: List[str] = []
        seen = set()
        for value in location_values:
            loc = normalize_location_string(str(value or ""))
            key = loc.lower()
            if not loc or key in seen:
                continue
            seen.add(key)
            cleaned.append(loc)
        return cleaned

    def _scope_location_to_us(self, location: Any) -> str:
        """Append ", United States" to a location string when no country is
        already named, so downstream services (Exa, Dice, Vetted, Unipile)
        scope their lookups to the US.

        Returns ``"United States"`` when the input is empty.
        """
        text = str(location or "").strip().strip(",")
        if not text:
            return "United States"

        upper_tokens = {t.strip().upper() for t in text.split(",")}
        if upper_tokens & set(self._COUNTRY_ALIASES.keys()):
            return text
        return f"{text}, United States"

    def _scope_boolean_to_us(self, boolean_string: Any) -> str:
        """Ensure the boolean keyword string carries a US-scope hint.

        The downstream Exa free-text query and Unipile's keyword fallback both
        consume this string verbatim, so the cheapest way to bias them toward
        US results is to append a literal country phrase when no country is
        already mentioned.
        """
        text = str(boolean_string or "").strip()
        if not text:
            return '"United States"'

        upper = text.upper()
        if any(
            token in upper
            for token in (
                "UNITED STATES",
                "UNITED STATES OF AMERICA",
                '"USA"',
                " USA ",
                " USA,",
                "(USA)",
                "U.S.",
                "U.S.A.",
            )
        ):
            return text
        return f'({text}) AND "United States"'

    def _is_likely_non_us(self, candidate: Dict[str, Any]) -> bool:
        """Return True only when the candidate is clearly outside the US.

        Used to enforce an unconditional US-only scope on every search.
        Defaults to False (treat as US) when the candidate's country and
        location text are silent — we want to keep observed candidates
        unless we have positive evidence they're abroad.
        """
        country = str(candidate.get("country") or "").strip().lower()
        if country and country not in self._US_COUNTRY_TOKENS:
            return True

        enhanced = candidate.get("enhanced_info") or {}
        if isinstance(enhanced, dict):
            enhanced_country = str(enhanced.get("current_country") or "").strip().lower()
            if enhanced_country and enhanced_country not in self._US_COUNTRY_TOKENS:
                return True

        locs = self._candidate_structured_locations(candidate)
        if locs:
            padded = " " + " | ".join(loc.lower() for loc in locs) + " "
            for token in self._NON_US_LOCATION_TOKENS:
                # Match as bounded substring to avoid false positives
                # (e.g. "india" must not hit "indianapolis").
                if f" {token} " in padded or f" {token}," in padded:
                    return True
        return False

    def _location_match_verdict(
        self,
        candidate: Dict[str, Any],
        criteria: SearchCriteria,
    ) -> Tuple[bool, str, Optional[float]]:
        if not criteria.location:
            return True, "no_location_requirement", None

        required = self._parse_location(criteria.location)
        if not required["city"] and not required["state"]:
            return True, "empty_location_requirement", None

        # B1: opt-out for "open to relocation" candidates whose actual location
        # is unknown or outside the radius. Default keeps them (soft-keep).
        relocation_flag = bool(candidate.get("open_to_relocation"))
        include_relocation = bool(getattr(criteria, "include_relocation_candidates", True))

        candidate_locs = self._candidate_structured_locations(candidate)
        if not candidate_locs:
            if relocation_flag and not include_relocation:
                return False, "relocation_excluded_by_filter", None
            # Soft-keep with sentinel distance so the UI counts these under
            # BEYOND 25MI instead of silently treating them as in-radius.
            # Common with JobDiva-JobAgent applicants whose city/state field
            # is blank in the API response (data quality, not a remote signal).
            return True, "candidate_location_missing_keep", _UNKNOWN_DISTANCE_SENTINEL

        # If search is state-only, enforce state equality without geocoding.
        if required["state"] and not required["city"]:
            seen_states: List[str] = []
            for value in candidate_locs:
                parsed = self._parse_location(value)
                state = parsed.get("state")
                if state:
                    seen_states.append(state)
                    if state == required["state"]:
                        return True, "state_match", None
            if not seen_states:
                # Candidate has location text but no parseable state →
                # soft-keep and let enrichment decide.
                return True, "candidate_state_unknown_keep", None
            return False, "state_mismatch", None

        # Hard cap 100 mi everywhere (defense-in-depth — UI also caps).
        miles = min(100, int(getattr(criteria, "within_miles", 25) or 25))
        target = normalize_location_string(criteria.location)
        geocode_failure = False
        closest_distance: Optional[float] = None

        # OFFLINE FAST-PATH: zip / city+state normalized match. JobAgent
        # locations are noisy ("PLANO, TX 75024" vs "Plano, TX" vs "Plano TX")
        # and Nominatim is rate-limited, so doing a string-level normalized
        # match catches the obvious in-radius cases without an HTTP call.
        # Falls through to Nominatim for anything not directly resolvable.
        try:
            from services.us_state_index import state_centroid_distance_miles
        except Exception:
            state_centroid_distance_miles = None  # type: ignore[assignment]

        offline_state_mismatch_distance: Optional[float] = None
        for candidate_loc in candidate_locs:
            parsed = self._parse_location(candidate_loc)
            cand_city = parsed.get("city", "")
            cand_state = parsed.get("state", "")
            cand_zip = parsed.get("zip", "")

            # Exact zip match → same point, ~0 mi.
            if cand_zip and required.get("zip") and cand_zip == required["zip"]:
                return True, "zip_match", 0.0

            # Exact city + state match → same metro, treat as 0 mi.
            if (
                cand_city and cand_state
                and required.get("city") and required.get("state")
                and cand_city == required["city"]
                and cand_state == required["state"]
            ):
                return True, "city_state_match", 0.0

            # State-centroid distance as a cheap upper-bound check. If the
            # candidate's state centroid is already far beyond the radius,
            # we can skip the Nominatim call and report the cross-state
            # distance as the candidate's offline-estimated distance. (We
            # only short-circuit reject when the gap is large enough that
            # any in-state metro pair would also be outside the radius.)
            if (
                state_centroid_distance_miles is not None
                and cand_state and required.get("state")
                and cand_state.upper() != required["state"].upper()
            ):
                centroid_d = state_centroid_distance_miles(
                    cand_state.upper(), required["state"].upper()
                )
                if centroid_d is not None:
                    if offline_state_mismatch_distance is None or centroid_d < offline_state_mismatch_distance:
                        offline_state_mismatch_distance = float(centroid_d)

        # Network path: defer to Nominatim for any candidate we couldn't
        # resolve offline.
        for candidate_loc in candidate_locs:
            is_within, reason, distance = within_radius(candidate_loc, target, miles)
            if isinstance(distance, (int, float)) and distance >= 0:
                if closest_distance is None or distance < closest_distance:
                    closest_distance = float(distance)
            if is_within:
                return True, "within_radius", closest_distance
            if reason in {"candidate_ungeocodable", "target_ungeocodable"}:
                geocode_failure = True

        # Prefer offline cross-state estimate when Nominatim couldn't pin a
        # distance — at least the UI can render a real number under BEYOND.
        if closest_distance is None and offline_state_mismatch_distance is not None:
            closest_distance = offline_state_mismatch_distance

        if geocode_failure:
            # Nominatim is best-effort and rate-limited. A geocode miss is
            # not evidence the candidate is outside the radius — soft-keep
            # but report the sentinel distance so BEYOND 25MI counts them.
            return True, "geocode_unavailable_keep", closest_distance if closest_distance is not None else _UNKNOWN_DISTANCE_SENTINEL
        if relocation_flag and not include_relocation:
            return False, "relocation_excluded_by_filter", closest_distance
        # No hard filter: a candidate geocoded as outside the radius is
        # still kept and shown under BEYOND 25MI with the real distance.
        # The recruiter decides whether to widen radius or skip them.
        return True, "outside_radius_soft_keep", closest_distance if closest_distance is not None else _UNKNOWN_DISTANCE_SENTINEL

    def _should_enforce_location(self, criteria: SearchCriteria) -> bool:
        normalized_location = self._normalize_term(criteria.location)
        return bool(normalized_location)

    def _parse_location(self, value: Any) -> Dict[str, str]:
        state_aliases = {
            "alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar",
            "california": "ca", "colorado": "co", "connecticut": "ct", "delaware": "de",
            "florida": "fl", "georgia": "ga", "hawaii": "hi", "idaho": "id",
            "illinois": "il", "indiana": "in", "iowa": "ia", "kansas": "ks",
            "kentucky": "ky", "louisiana": "la", "maine": "me", "maryland": "md",
            "massachusetts": "ma", "michigan": "mi", "minnesota": "mn", "mississippi": "ms",
            "missouri": "mo", "montana": "mt", "nebraska": "ne", "nevada": "nv",
            "new hampshire": "nh", "new jersey": "nj", "new mexico": "nm", "new york": "ny",
            "north carolina": "nc", "north dakota": "nd", "ohio": "oh", "oklahoma": "ok",
            "oregon": "or", "pennsylvania": "pa", "rhode island": "ri", "south carolina": "sc",
            "south dakota": "sd", "tennessee": "tn", "texas": "tx", "utah": "ut",
            "vermont": "vt", "virginia": "va", "washington": "wa", "west virginia": "wv",
            "wisconsin": "wi", "wyoming": "wy", "district of columbia": "dc",
        }
        from services.us_state_index import resolve_state_code

        text = self._normalize_search_text(value)
        text = re.sub(r"\bwithin\s+\d+\s+mi\b", "", text)
        text = re.sub(r"\bmetro\b", "", text)
        text = re.sub(r"^must be local to\s+", "", text).strip(" ,")

        # Extract a 5-digit US zip (with optional ZIP+4 suffix) before
        # splitting on comma. This is a normalization fix for inputs like
        # "Plano, TX 75024" / "PLANO TX 75024-1234" / "75024" — zip stays
        # accessible for downstream offline match instead of being stripped.
        zip_match = re.search(r"\b(\d{5})(?:-\d{4})?\b", text)
        zip_code = zip_match.group(1) if zip_match else ""
        if zip_code:
            text = (text[:zip_match.start()] + text[zip_match.end():]).strip(" ,")

        parts = [part.strip() for part in re.split(r",|\\|/", text) if part.strip()]

        city = parts[0] if parts else ""
        state = ""
        if len(parts) > 1:
            state = parts[1].split()[0]
        elif len(parts) == 1:
            tokens = parts[0].split()
            # Bare state input ("CA", "California", "Calif") — recognise via
            # the local us_state_index so we hit the state-only fast path in
            # _location_match_verdict instead of soft-keeping on a Nominatim
            # miss. Bug F: production used to silently accept NC candidates
            # for a "CA" job because geocoding "CA" was ambiguous.
            bare_state = resolve_state_code(parts[0])
            if bare_state:
                city = ""
                state = bare_state
            elif len(tokens) > 1 and len(tokens[-1]) == 2:
                city = " ".join(tokens[:-1])
                state = tokens[-1]
            elif len(tokens) > 1:
                # "City State" (no comma, full or abbreviated). Try resolving
                # the trailing token(s) as a state name.
                for n in (2, 1):
                    if len(tokens) > n:
                        candidate_state = resolve_state_code(" ".join(tokens[-n:]))
                        if candidate_state:
                            city = " ".join(tokens[:-n])
                            state = candidate_state
                            break

        return {
            "city": self._normalize_term(city),
            "state": state_aliases.get(self._normalize_term(state), self._normalize_term(state)),
            "zip": zip_code,
        }

    def _resume_filter_term(self, filter_item: Dict[str, Any]) -> str:
        raw_value = str(filter_item.get("value", "")).strip()
        if not raw_value:
            return ""
        value = raw_value.split("—")[0].strip()
        value = value.replace("Must be local to ", "").replace(" metro", "").strip()
        value = value.replace("Must not be employed by:", "").strip()
        value = re.sub(r"^(must have|must include|must be|can have|preferred|nice to have)\s*:?\s*", "", value, flags=re.IGNORECASE)
        return value

    def _candidate_match_text(self, candidate: Dict[str, Any]) -> str:
        enhanced = candidate.get("enhanced_info") or {}
        pieces = [
            candidate.get("name", ""),
            candidate.get("title", ""),
            candidate.get("headline", ""),
            candidate.get("location", ""),
            candidate.get("city", ""),
            candidate.get("state", ""),
            candidate.get("resume_text", ""),
            enhanced.get("candidate_name", ""),
            enhanced.get("job_title", ""),
            enhanced.get("current_location", ""),
            json.dumps(enhanced.get("key_skills", [])),
            json.dumps(enhanced.get("company_experience", [])),
            json.dumps(enhanced.get("candidate_education", [])),
            json.dumps(enhanced.get("candidate_certification", [])),
        ]
        if isinstance(candidate.get("skills"), list):
            pieces.append(json.dumps(candidate.get("skills")))
        return " ".join(str(piece) for piece in pieces if piece).lower()

    def _normalize_term(self, value: Any) -> str:
        value = str(value or "").lower().strip()
        value = value.replace('"', "").replace("(", "").replace(")", "")
        value = re.sub(r"^must be local to\s*", "", value)
        value = re.sub(r"\s*metro$", "", value)
        value = re.sub(r"^must not be employed by:\s*", "", value)
        value = re.sub(r"\s+within\s+\d+\s+mi$", "", value)
        value = re.sub(r"\s+recent$", "", value)
        value = re.sub(r"\s+over\s+\d+\s+years?$", "", value)
        return re.sub(r"\s+", " ", value).strip()

    def _candidate_profile(self, candidate: Dict[str, Any]) -> Dict[str, Any]:
        enhanced = candidate.get("enhanced_info") or {}

        def unique_terms(values: List[str]) -> List[str]:
            ordered: List[str] = []
            seen = set()
            for value in values:
                normalized = self._normalize_term(value)
                if not normalized or normalized in seen:
                    continue
                seen.add(normalized)
                ordered.append(normalized)
            return ordered

        skill_terms: List[str] = []
        for source in [enhanced.get("key_skills", []), enhanced.get("structured_skills", []), enhanced.get("skills", []), candidate.get("skills", [])]:
            if not isinstance(source, list):
                continue
            for item in source:
                if isinstance(item, dict):
                    if item.get("skill"):
                        skill_terms.append(str(item.get("skill")))
                    if item.get("name"):
                        skill_terms.append(str(item.get("name")))
                elif isinstance(item, str):
                    skill_terms.append(item)

        title_terms = unique_terms([
            enhanced.get("job_title", ""),
            candidate.get("title", ""),
            candidate.get("headline", ""),
        ])

        # Fix 1 (Path A / A′): when enhanced_info is missing or the LLM
        # extraction returned an empty shell (error case), fall back to
        # source-native candidate fields. LinkedIn/Unipile and certain JobDiva
        # paths already populate these at the candidate root, so we give them a
        # non-zero profile even when LLM extraction silently degraded.
        company_sources: List[Any] = [
            enhanced.get("company_experience") or [],
            candidate.get("company_experience") or [],
            candidate.get("experience") or [],
            candidate.get("work_experience") or [],
        ]
        company_terms: List[str] = []
        for source in company_sources:
            if not isinstance(source, list):
                continue
            for item in source:
                if isinstance(item, dict):
                    for key in ["company", "company_name", "employer", "name"]:
                        if item.get(key):
                            company_terms.append(str(item.get(key)))
                elif isinstance(item, str):
                    company_terms.append(item)

        education_sources: List[Any] = [
            enhanced.get("candidate_education") or [],
            candidate.get("education") or [],
        ]
        education_terms: List[str] = []
        for source in education_sources:
            if not isinstance(source, list):
                continue
            for item in source:
                if isinstance(item, dict):
                    for key in ["degree", "field", "institution", "school", "specialization"]:
                        if item.get(key):
                            education_terms.append(str(item.get(key)))
                elif isinstance(item, str):
                    education_terms.append(item)

        certification_sources: List[Any] = [
            enhanced.get("candidate_certification") or [],
            candidate.get("certifications") or [],
        ]
        certification_terms: List[str] = []
        for source in certification_sources:
            if not isinstance(source, list):
                continue
            for item in source:
                if isinstance(item, dict):
                    for key in ["name", "certification", "title", "issuer"]:
                        if item.get(key):
                            certification_terms.append(str(item.get(key)))
                elif isinstance(item, str):
                    certification_terms.append(item)

        location_terms = unique_terms([
            enhanced.get("current_location", ""),
            candidate.get("location", ""),
            f"{candidate.get('city', '')}, {candidate.get('state', '')}".strip(", "),
        ])

        resume_years = 0
        raw_years = enhanced.get("years_of_experience") or candidate.get("experience_years")
        if raw_years is not None:
            try:
                match = re.search(r"\d+(?:\.\d+)?", str(raw_years))
                if match:
                    resume_years = float(match.group(0))
            except Exception:
                resume_years = 0

        return {
            "titles": title_terms,
            "skills": unique_terms(skill_terms),
            "companies": unique_terms(company_terms),
            "education": unique_terms(education_terms),
            "certifications": unique_terms(certification_terms),
            "locations": location_terms,
            "years_of_experience": resume_years,
            "text": self._candidate_match_text(candidate),
            "recent_text": self._candidate_match_text(candidate)[:3000],
        }

    def _contains_term(self, profile: Dict[str, Any], term: str, *collections: str) -> bool:
        normalized = self._normalize_term(term)
        if not normalized:
            return False

        for collection_name in collections:
            for item in profile.get(collection_name, []):
                norm_item = self._normalize_term(item)
                if normalized == norm_item or normalized in norm_item or norm_item in normalized:
                    return True

        # Critical: location matching must not fall back to generic resume text,
        # otherwise stale historical locations can satisfy current-location checks.
        is_location_only = len(collections) == 1 and collections[0] == "locations"
        if is_location_only:
            return False

        return normalized in profile.get("text", "")

    def _fuzzy_term_score(self, profile: Dict[str, Any], term: str, *collections: str) -> float:
        """Calculate a similarity score between 0.0 and 1.0 for a term and candidate profile."""
        normalized_term = self._normalize_term(term)
        if not normalized_term:
            return 0.0

        # Check for strict match first (100%)
        if self._contains_term(profile, term, *collections):
            return 1.0

        # Keyword-based partial matching
        term_words = [w for w in normalized_term.split() if len(w) > 2] # ignore tiny words
        if not term_words:
            return 0.0

        best_overlap_score = 0.0

        # Check against structured collections (higher weight)
        for coll in collections:
            for item in profile.get(coll, []):
                item_clean = self._normalize_term(item)
                item_words = set(item_clean.split())
                if not item_words:
                    continue
                intersection = [w for w in term_words if w in item_words]
                overlap = len(intersection) / len(term_words)
                if overlap > best_overlap_score:
                    best_overlap_score = overlap

        # Check against full text (broad keyword match, lower weight)
        profile_text = profile.get("text", "")
        is_location_only = len(collections) == 1 and collections[0] == "locations"
        if is_location_only:
            text_score = 0.0
        else:
            text_matches = sum(1 for word in term_words if word in profile_text)
            text_score = (text_matches / len(term_words)) * 0.35

        # Embedding-cosine augmentation. The effective flag honors both
        # the global EMBEDDING_SKILL_MATCH env var (legacy / IT path)
        # and the per-family override (non-IT families default on),
        # resolved through `embedding_skill_match_for_family`. When on
        # and the embeddings have been pre-warmed (in `search_candidates`
        # for the query side and `emit_candidate` for the candidate
        # side), take the max of keyword score and cosine similarity.
        # Below the configured threshold the cosine score is treated as
        # 0 so noisy near-matches don't promote weak candidates.
        # Locations and other non-skill collections are excluded —
        # embeddings make sense only for free-form skill / title text.
        embedding_score = 0.0
        embedding_active = embedding_skill_match_for_family(self._current_family)
        if embedding_active and not is_location_only:
            candidate_terms: List[str] = []
            for coll in collections:
                for item in profile.get(coll, []) or []:
                    candidate_terms.append(str(item))
            if candidate_terms:
                cosine = skill_embeddings.best_cosine(term, candidate_terms)
                if cosine >= EMBEDDING_MATCH_THRESHOLD:
                    embedding_score = cosine

        return max(best_overlap_score, text_score, embedding_score)

    def _criteria_query_terms(self, criteria: SearchCriteria) -> List[str]:
        """Flat list of every skill / title / company / keyword term in
        the criteria, used to pre-warm query-side embeddings once per
        search."""
        terms: List[str] = []
        for source in (
            criteria.title_criteria,
            criteria.skill_criteria,
            criteria.resume_match_filters,
        ):
            for item in source or []:
                if isinstance(item, dict):
                    val = item.get("value")
                    if val:
                        terms.append(str(val))
        for kw in criteria.keywords or []:
            if kw:
                terms.append(str(kw))
        for company in criteria.companies or []:
            if company:
                terms.append(str(company))
        return terms

    def _candidate_skill_terms(self, candidate: Dict[str, Any]) -> List[str]:
        """Flat list of skill / title / company / cert / education
        strings on a candidate, used to warm candidate-side embeddings
        before scoring."""
        terms: List[str] = []
        for key in ("title", "headline", "company", "current_company"):
            val = candidate.get(key)
            if val:
                terms.append(str(val))
        for key in ("skills", "titles", "companies", "education", "certifications"):
            val = candidate.get(key) or []
            if isinstance(val, list):
                for item in val:
                    if isinstance(item, dict):
                        for k in ("skill", "name", "value"):
                            if item.get(k):
                                terms.append(str(item[k]))
                                break
                    elif item:
                        terms.append(str(item))
            elif isinstance(val, str) and val:
                terms.append(val)

        enhanced = candidate.get("enhanced_info") or {}
        if isinstance(enhanced, dict):
            for key in ("key_skills", "skills"):
                val = enhanced.get(key) or []
                if isinstance(val, list):
                    for item in val:
                        if item:
                            terms.append(str(item))
        return terms

    def _dedupe_terms(self, terms: List[str]) -> List[str]:
        ordered: List[str] = []
        seen = set()
        for term in terms:
            normalized = self._normalize_term(term)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            ordered.append(normalized)
        return ordered

    def _score_ratio(self, matched: List[str], total: List[str]) -> float:
        if not total:
            return 1.0
        return len(self._dedupe_terms(matched)) / len(self._dedupe_terms(total))

    def _group_terms(self, group: Any) -> List[str]:
        if isinstance(group, dict):
            return self._dedupe_terms(group.get("terms", []))
        return self._dedupe_terms(group if isinstance(group, list) else [group])

    def _group_label(self, group: Any) -> str:
        if isinstance(group, dict):
            return str(group.get("label") or (self._group_terms(group) or [""])[0])
        return (self._group_terms(group) or [""])[0]

    def _group_min_years(self, group: Any) -> int:
        if isinstance(group, dict):
            try:
                return int(group.get("years") or 0)
            except Exception:
                return 0
        return 0

    def _group_recent(self, group: Any) -> bool:
        return bool(group.get("recent")) if isinstance(group, dict) else False

    def _term_group_matches(self, profile: Dict[str, Any], group: Any, collections: List[str]) -> bool:
        terms = self._group_terms(group)
        # Any match above 0.5 is considered a "Pass" for pre-screening/filtering
        return any(self._fuzzy_term_score(profile, term, *collections) > 0.5 for term in terms)

    def _term_group_fuzzy_score(self, profile: Dict[str, Any], group: Any, collections: List[str]) -> float:
        """Returns the best fuzzy score found for any term in the group."""
        terms = self._group_terms(group)
        if not terms:
            return 0.0
        return max(self._fuzzy_term_score(profile, term, *collections) for term in terms)

    def _term_group_fully_matches(
        self,
        profile: Dict[str, Any],
        group: Any,
        collections: List[str],
        enforce_years: bool = False,
    ) -> bool:
        if not self._term_group_matches(profile, group, collections):
            return False

        min_years = self._group_min_years(group)
        if enforce_years and min_years > 0:
            years = float(profile.get("years_of_experience") or 0)
            if years and years < min_years:
                return False

        return True

    def _term_group_score(self, profile: Dict[str, Any], group: Any, collections: List[str]) -> float:
        fuzzy_base = self._term_group_fuzzy_score(profile, group, collections)
        if fuzzy_base <= 0:
            return 0.0

        score = fuzzy_base
        min_years = self._group_min_years(group)
        if min_years > 0:
            years = float(profile.get("years_of_experience") or 0)
            if years <= 0:
                # T2: years-unknown multiplier (was 0.75, softened via env).
                score *= SCORING_YEARS_UNKNOWN_MULT
            elif years < min_years:
                # T2: years-below-min floor (was 0.35, softened via env).
                score *= max(SCORING_YEARS_FLOOR, years / min_years)

        if self._group_recent(group):
            terms = self._group_terms(group)
            recent_text = profile.get("recent_text", "")
            if terms and not any(term in recent_text for term in terms):
                # T2: not-recent penalty (was 0.85, softened via env).
                score *= SCORING_RECENT_PENALTY

        return score

    def _matched_term_groups(
        self,
        profile: Dict[str, Any],
        groups: List[Any],
        collections: List[str],
    ) -> List[str]:
        matched = []
        for group in groups:
            if not self._group_terms(group):
                continue
            if self._term_group_matches(profile, group, collections):
                matched.append(self._group_label(group))
        return matched

    def _filter_assessment(
        self,
        candidate: Dict[str, Any],
        criteria: SearchCriteria,
        enforce_years: bool = False,
    ) -> Dict[str, Any]:
        """
        Heuristic assessment of whether a candidate profile matches sourcing criteria.
        Used for early pre-screening of JobDiva/LinkedIn profiles before full LLM enrichment.
        """
        profile = self._candidate_profile(candidate)
        missing: List[str] = []
        matched: List[str] = []
        excluded: List[str] = []

        # US-only scope is enforced at every stage (see `_filter_candidates`
        # and `_filter_by_state`). Soft-fail: only drop on positive evidence
        # of a non-US location.
        if self._is_likely_non_us(candidate):
            return {
                "passes": False,
                "missing": ["Location: outside US"],
                "matched": self._dedupe_terms(matched),
                "excluded": self._dedupe_terms(excluded),
                "location_failure_reason": "non_us_candidate",
            }

        # Minimum-years-of-experience floor. Only enforced when
        # `enforce_years=True` (post-LLM stage), where `years_of_experience`
        # reflects the LLM's resume parse rather than the source's
        # heuristic default (which JobDiva can populate as a constant
        # like 4 from title alone, killing real candidates pre-LLM).
        min_years = int(getattr(criteria, "min_experience_years", 0) or 0)
        if enforce_years and min_years > 0:
            years = float(profile.get("years_of_experience") or 0)
            if 0 < years < min_years:
                return {
                    "passes": False,
                    "missing": [f"YOE: needs {min_years}+ years (resume shows {int(years)})"],
                    "matched": self._dedupe_terms(matched),
                    "excluded": self._dedupe_terms(excluded),
                    "min_years_failure": True,
                }

        if self._should_enforce_location(criteria):
            location_ok, reason, distance = self._location_match_verdict(candidate, criteria)
            if distance is not None:
                candidate["distance_miles"] = round(float(distance), 1)
            # Policy 2026-05-13: location mismatches no longer fail the
            # assessment outright. They surface as a soft signal via
            # `location_failure_reason` so emit_candidate / the UI filter
            # can decide. Only `non_us_candidate` (handled above as a
            # top-level check) is still a hard reject.
            if not location_ok and reason == "non_us_candidate":
                return {
                    "passes": False,
                    "missing": [f"Location: {criteria.location}"],
                    "matched": self._dedupe_terms(matched),
                    "excluded": self._dedupe_terms(excluded),
                    "location_failure_reason": reason,
                }
            if not location_ok:
                # Record the soft reason so the UI/score knows about the
                # mismatch but does not drop the candidate here.
                missing.append(f"Location: {criteria.location}")

        total_required = 0
        matched_required = 0
        for dimension in self._collect_sourcing_dimensions(criteria):
            collections = dimension["collections"]
            # Check exclusions - these are ALWAYS hard filters
            for group in dimension.get("excluded_groups", []):
                if self._term_group_matches(profile, group, collections):
                    excluded.append(f"{dimension['label']}: {self._group_label(group)}")

            # Count required matches/misses for the threshold gate below.
            required_groups = dimension.get("required_groups", [])
            preferred_groups = dimension.get("preferred_groups", [])
            for group in required_groups:
                total_required += 1
                if self._term_group_matches(profile, group, collections):
                    matched_required += 1
                    matched.append(f"{dimension['label']}: {self._group_label(group)}")
                else:
                    missing.append(f"{dimension['label']}: {self._group_label(group)}")
            for group in preferred_groups:
                if self._term_group_matches(profile, group, collections):
                    matched.append(f"{dimension['label']}: {self._group_label(group)}")

        # Enforce hard exclusions
        if excluded:
            return {
                "passes": False,
                "missing": self._dedupe_terms(missing),
                "matched": self._dedupe_terms(matched),
                "excluded": self._dedupe_terms(excluded)
            }

        # DETERMINING PASS STATUS
        # If enforce_years=False, we are in the "Discovery/Pre-screen" phase.
        # Discovery phases should be LENIENT because shallow metadata (LinkedIn headline, JobDiva title)
        # is often incomplete. We only fail if there's a hard exclusion.
        if not enforce_years:
            return {
                "passes": True,
                "missing": self._dedupe_terms(missing),
                "matched": self._dedupe_terms(matched),
                "excluded": [],
                "location_failure_reason": None,
            }

        # Stage-5 gate: replaced legacy `passes = not missing` (which required
        # 100% of required groups to match — kills real candidates whose resume
        # text doesn't enumerate every keyword). Pass when at least
        # `sourcing_config.REQUIRED_MATCH_RATIO` of required groups are matched,
        # or when there are no required groups. Default 0.5.
        # Rationale: a 35-year senior with a matching title and 2 of 6
        # required skills should not be rejected by a binary gate.
        from core import sourcing_config
        required_ratio = float(sourcing_config.REQUIRED_MATCH_RATIO)
        threshold = math.ceil(total_required * required_ratio)
        if total_required == 0:
            passes = True
        else:
            passes = matched_required >= threshold
        return {
            "passes": passes,
            "missing": self._dedupe_terms(missing),
            "matched": self._dedupe_terms(matched),
            "excluded": self._dedupe_terms(excluded),
            "location_failure_reason": None,
            "matched_required": matched_required,
            "total_required": total_required,
        }

    def _score_candidate(self, candidate: Dict[str, Any], criteria: SearchCriteria) -> Dict[str, Any]:
        profile = self._candidate_profile(candidate)
        dimensions = self._collect_scoring_dimensions(criteria)  # Use scoring dimensions for evaluation

        weighted_scores: List[float] = []
        weighted_max = 0.0
        explainability: List[str] = []
        missing_required: List[str] = []
        matched_required_skills: List[str] = []
        score_details: Dict[str, Any] = {}
        # Hard-veto trigger: any excluded group whose match strength meets
        # SCORING_EXCLUSION_HARD_VETO_THRESHOLD forces score → 0 regardless
        # of how strong the rest of the candidate looks. The soft penalty
        # below still applies for borderline matches.
        hard_veto_hits: List[str] = []

        for dimension in dimensions:
            total_weight = float(dimension["weight"])
            required_groups = [
                group for group in dimension.get("required_groups", [])
                if self._group_terms(group)
            ]
            preferred_groups = [
                group for group in dimension.get("preferred_groups", [])
                if self._group_terms(group)
            ]
            excluded_groups = [
                group for group in dimension.get("excluded_groups", [])
                if self._group_terms(group)
            ]

            if not required_groups and not preferred_groups and not excluded_groups:
                continue

            required_matches = self._matched_term_groups(profile, required_groups, dimension["collections"])
            preferred_matches = self._matched_term_groups(profile, preferred_groups, dimension["collections"])
            excluded_matches = self._matched_term_groups(profile, excluded_groups, dimension["collections"])

            # Part 3: weighted mean across groups so recruiters can flag
            # individual filters as 2x or 0.5x in the UI. Default weight is
            # 1.0, which reproduces the previous arithmetic-mean formula.
            # L4: unmatched groups floor at SCORING_UNMATCHED_*_FLOOR instead
            # of 0 — many "misses" are really synonym/parsing artifacts (the
            # rubric may list "CS degree" and "Engineering degree" as two
            # groups even though either satisfies the recruiter), and strong
            # candidates shouldn't be zeroed on every such item.
            # L5: Coverage-based quality lift. When the candidate hits at
            # least SCORING_COVERAGE_BLEND_THRESHOLD of the groups at decent
            # quality, blend toward the hits-only mean. A candidate who
            # nails 7 of 10 required items at ~0.9 each shouldn't be
            # dragged to ~0.7 by three misses — especially when those
            # misses are rubric redundancies. Weak coverage (< threshold)
            # falls through to the ordinary floored mean, preserving
            # "weak fit = weak score" ordering.
            def _weighted_ratio(groups, is_required: bool):
                if not groups:
                    return 1.0
                floor = (
                    SCORING_UNMATCHED_REQUIRED_FLOOR if is_required
                    else SCORING_UNMATCHED_PREFERRED_FLOOR
                )
                collections_for_group = dimension["collections"]
                tuples: List[tuple] = []
                for g in groups:
                    raw = self._term_group_score(profile, g, collections_for_group)
                    w = float(g.get("weight") or 1.0)
                    tuples.append((raw, w))

                total_w = sum(w for _, w in tuples) or float(len(tuples))
                floored_mean = sum(
                    (raw if raw > 0 else floor) * w for raw, w in tuples
                ) / total_w

                # Hits = groups that match decently (matches the
                # _term_group_matches threshold of > 0.5).
                hits = [(raw, w) for raw, w in tuples if raw > 0.5]
                if hits and len(hits) / len(tuples) >= SCORING_COVERAGE_BLEND_THRESHOLD:
                    hit_w = sum(w for _, w in hits) or 1.0
                    hit_mean = sum(raw * w for raw, w in hits) / hit_w
                    coverage = len(hits) / len(tuples)
                    # Higher coverage → lean harder on hit_mean. Full
                    # coverage collapses to hit_mean; half coverage is a
                    # 50/50 blend. Never drop below floored_mean.
                    blend = (1.0 - coverage) * floored_mean + coverage * hit_mean
                    return max(floored_mean, blend)
                return floored_mean

            required_ratio = _weighted_ratio(required_groups, True) if required_groups else 1.0
            preferred_ratio = _weighted_ratio(preferred_groups, False) if preferred_groups else 1.0
            base_ratio = 0.0
            if required_groups and preferred_groups:
                # T1: rebalance required-vs-preferred (was 0.75/0.25, softened via env).
                base_ratio = (required_ratio * SCORING_REQUIRED_WEIGHT) + (preferred_ratio * SCORING_PREFERRED_WEIGHT)
            elif required_groups:
                base_ratio = required_ratio
            elif preferred_groups:
                base_ratio = preferred_ratio

            # L4: Parsing-gap rescue. If the structured collections this
            # dimension scores against are entirely empty on the profile
            # (classic LinkedIn/JobDiva parsing gap — e.g. no `companies`
            # extracted), but the candidate does have resume_text, floor
            # base_ratio so a single missing field can't torpedo the score.
            # Clear non-fits (candidate with real data that still doesn't
            # match) are unaffected because they'll have something in the
            # collection even if it's wrong.
            collections = dimension["collections"]
            has_structured_data = any(profile.get(c) for c in collections)
            if not has_structured_data and profile.get("text"):
                base_ratio = max(base_ratio, SCORING_PARSING_GAP_FLOOR)

            dimension_score = total_weight * base_ratio
            weighted_scores.append(dimension_score)
            weighted_max += total_weight
            score_details[dimension["label"]] = {
                "weight": total_weight,
                "score": round(dimension_score, 2),
                "required_matched": len(required_matches),
                "required_total": len(required_groups),
                "preferred_matched": len(preferred_matches),
                "preferred_total": len(preferred_groups),
            }

            if excluded_matches:
                # T3: cap exclusion penalty (was 0.6 / 0.25, softened via env).
                penalty = min(
                    total_weight * SCORING_EXCLUSION_CAP,
                    len(excluded_matches) * max(4.0, total_weight * SCORING_EXCLUSION_PER_HIT),
                )
                weighted_scores.append(-penalty)
                score_details[dimension["label"]]["exclusion_penalty"] = round(penalty, 2)
                explainability.append(
                    f"{dimension['label']}: conflicting match on {', '.join(excluded_matches[:2])}"
                )

                # Hard-veto check: if any excluded group scored above the
                # configured threshold, mark the candidate for forced-zero
                # after the dimension loop. We probe per-group rather than
                # relying on `excluded_matches` (which only reports >0.5
                # hits) because the threshold may be tighter or looser.
                if SCORING_EXCLUSION_HARD_VETO_THRESHOLD <= 1.0:
                    for group in excluded_groups:
                        strength = self._term_group_score(
                            profile, group, dimension["collections"]
                        )
                        if strength >= SCORING_EXCLUSION_HARD_VETO_THRESHOLD:
                            hard_veto_hits.append(
                                f"{dimension['label']}: {self._group_label(group)}"
                            )
                            break

            if required_groups:
                missing = [
                    self._group_label(group) for group in required_groups
                    if not self._term_group_matches(profile, group, dimension["collections"])
                ]
                partial = []
                for group in required_groups:
                    if self._term_group_matches(profile, group, dimension["collections"]):
                        min_years = self._group_min_years(group)
                        years = float(profile.get("years_of_experience") or 0)
                        if min_years > 0 and years and years < min_years:
                            partial.append(f"{self._group_label(group)} needs {min_years}+ years")
                        elif min_years > 0 and years <= 0:
                            partial.append(f"{self._group_label(group)} years not proven")
                if missing:
                    missing_required.extend(missing)
                if partial:
                    missing_required.extend(partial)
                if required_matches:
                    explainability.append(
                        f"{dimension['label']}: matched {len(required_matches)}/{len(required_groups)} required"
                    )
            elif preferred_groups and preferred_matches:
                explainability.append(
                    f"{dimension['label']}: matched {len(preferred_matches)}/{len(preferred_groups)} preferred"
                )

            dim_label = dimension["label"]
            for item in required_matches + preferred_matches:
                matched_required_skills.append(
                    item if dim_label == "Skills" else f"{dim_label}: {item}"
                )

        score = 0
        if weighted_max > 0:
            score = round(max(0.0, min(100.0, (sum(weighted_scores) / weighted_max) * 100)))

        score_details["hard_veto"] = {
            "triggered": bool(hard_veto_hits),
            "reasons": hard_veto_hits[:3],
        }

        if hard_veto_hits:
            score = 0
            explainability.insert(
                0,
                f"Hard exclusion: matches recruiter exclusion rule ({hard_veto_hits[0]})",
            )
        elif score >= 85:
            explainability.insert(0, "Excellent rubric and sourcing alignment")
        elif score >= 70:
            explainability.insert(0, "Strong overall fit across active filters")
        elif score >= 50:
            explainability.insert(0, "Partial fit; review missing rubric requirements")
        else:
            explainability.insert(0, "Limited fit against active rubric and sourcing filters")

        if not explainability:
            explainability = ["No active resume-match filters were available for scoring"]

        return {
            "score": score,
            "missing_skills": self._dedupe_terms(missing_required),
            "explainability": explainability[:6],
            "matched_skills": self._dedupe_terms(matched_required_skills),
            "score_details": score_details,
        }

    def _candidate_satisfies_required_filters(self, candidate: Dict[str, Any], criteria: SearchCriteria) -> bool:
        return self._filter_assessment(candidate, criteria, enforce_years=True)["passes"]

    # PR-B: cheap regex used by the pre-LLM YOE gate. Looks for forms
    # like "8+ years", "5 years of experience", "10 yrs". Returns the
    # first integer match, or 0 when nothing parses (caller treats 0 as
    # "unknown → keep" to avoid penalising candidates with non-standard
    # phrasing).
    _YEARS_REGEX = re.compile(
        r"\b(\d{1,2})\s*\+?\s*(?:years?|yrs?)\b", re.IGNORECASE
    )

    def _heuristic_years_from_text(self, text: str) -> int:
        if not text:
            return 0
        # Cap input — only the first chunk is searched. Most resumes /
        # headlines / abstracts surface the years number near the top.
        sample = str(text)[:1500]
        best = 0
        for match in self._YEARS_REGEX.finditer(sample):
            try:
                value = int(match.group(1))
            except ValueError:
                continue
            if 0 < value <= 50 and value > best:
                best = value
        return best

    def _candidate_below_min_years_pre_llm(
        self,
        candidate: Dict[str, Any],
        criteria: SearchCriteria,
    ) -> bool:
        """Cheap regex check before LLM enrichment runs.

        True only when the candidate's headline / abstract / resume_text
        head contains a parseable years number AND that number is below
        `criteria.min_experience_years`. Returns False when no number is
        found (deferred to the post-LLM gate via `_filter_assessment`).

        core.sourcing_config.SKIP_JOBDIVA_YOE_PRECHECK: when set, skip the
        heuristic entirely for JobDiva-sourced candidates. JobDiva's
        experience_years can be a constant default per the comment at line
        ~1849, and the regex pulls numbers out of "5+ years" copy that may
        not reflect the actual resume. Defer YOE to the post-LLM gate
        (Stage 5).
        """
        from core import sourcing_config
        if sourcing_config.SKIP_JOBDIVA_YOE_PRECHECK:
            source = str(candidate.get("source") or "").lower()
            if source.startswith("jobdiva"):
                return False

        min_years = int(getattr(criteria, "min_experience_years", 0) or 0)
        if min_years <= 0:
            return False
        haystack = " ".join([
            str(candidate.get("headline") or ""),
            str(candidate.get("title") or ""),
            str(candidate.get("abstract") or ""),
            str(candidate.get("resume_text") or "")[:1500],
        ])
        years = self._heuristic_years_from_text(haystack)
        return 0 < years < min_years

    def _collect_sourcing_dimensions(self, criteria: SearchCriteria) -> List[Dict[str, Any]]:
        """Collect match dimensions for PRE-SCREENING.

        Uses Page-5 sourcing filters PLUS the non-overlapping subset of
        Page-4 `resume_match_filters` (Certifications and Education).
        Lifting cert/edu into pre-screen lets us drop candidates that
        will fail the rubric anyway *before* paying for LLM enrichment;
        the audit estimated 30-50% of enrichment cost on cert-heavy
        roles was wasted on candidates that would later get filtered.

        Skill / title / company / domain / location filters are NOT
        lifted — they overlap with the existing pre-screen dimensions
        and would double-count.
        """
        dimensions = {
            "titles": {
                "label": "Titles",
                "weight": 15.0,
                "collections": ["titles"],
                "required": [],
                "preferred": [],
                "excluded": [],
            },
            "skills": {
                "label": "Skills",
                "weight": 45.0,
                "collections": ["skills"],
                "required": [],
                "preferred": [],
                "excluded": [],
            },
            "location": {
                "label": "Location",
                "weight": 4.0,
                "collections": ["locations"],
                "required": [],
                "preferred": [],
                "excluded": [],
            },
            "companies": {
                "label": "Company Experience",
                "weight": 5.0,
                "collections": ["companies"],
                "required": [],
                "preferred": [],
                "excluded": [],
            },
            "keywords": {
                "label": "Keywords",
                "weight": 5.0,
                "collections": ["skills", "titles", "companies", "locations"],
                "required": [],
                "preferred": [],
                "excluded": [],
            },
            # PR-B: pre-screen rubric dimensions lifted from Step-4
            # `resume_match_filters` so cert/edu requirements gate the
            # LLM enrichment instead of being applied only after.
            "certifications": {
                "label": "Certifications",
                "weight": 8.0,
                "collections": ["certifications", "skills"],
                "required": [],
                "preferred": [],
                "excluded": [],
            },
            "education": {
                "label": "Education",
                "weight": 6.0,
                "collections": ["education"],
                "required": [],
                "preferred": [],
                "excluded": [],
            },
        }

        for dimension in dimensions.values():
            dimension["required_groups"] = []
            dimension["preferred_groups"] = []
            dimension["excluded_groups"] = []

        def add_terms(
            bucket: str,
            match_type: str,
            values: List[str],
            label: str = "",
            years: int = 0,
            recent: bool = False,
        ) -> None:
            clean_values = [value for value in values if str(value).strip()]
            if not clean_values:
                return
            match_type = str(match_type or "must").lower().replace("_", " ").strip()
            target = "required"
            if match_type in {"exclude", "must not", "must-not", "must_not"}:
                target = "excluded"
            elif match_type in {"can", "preferred", "nice to have", "nice-to-have"}:
                target = "preferred"
            dimensions[bucket][target].extend(clean_values)
            dimensions[bucket][f"{target}_groups"].append({
                "terms": clean_values,
                "label": label or clean_values[0],
                "years": years or 0,
                "recent": recent,
            })

        # ONLY use Page 5 sourcing filters
        for item in criteria.title_criteria:
            value = str(item.get("value", "")).strip()
            variants = [value] + [str(similar).strip() for similar in item.get("similar_terms", []) or [] if str(similar).strip()]
            add_terms(
                "titles",
                item.get("match_type", "must"),
                [variant for variant in variants if variant],
                label=value,
                years=int(item.get("years") or 0),
                recent=bool(item.get("recent")),
            )

        for item in criteria.skill_criteria:
            value = str(item.get("value", "")).strip()
            variants = [value] + [str(similar).strip() for similar in item.get("similar_terms", []) or [] if str(similar).strip()]
            add_terms(
                "skills",
                item.get("match_type", "must"),
                [variant for variant in variants if variant],
                label=value,
                years=int(item.get("years") or 0),
                recent=bool(item.get("recent")),
            )

        # Filter out keywords that are actually Page 4 filters to prevent leak
        match_filter_values = {str(f.get("value", "")).strip().lower() for f in (criteria.resume_match_filters or [])}
        
        for keyword in criteria.keywords:
            kw_clean = str(keyword).strip()
            if kw_clean.lower() not in match_filter_values:
                add_terms("keywords", "must", [kw_clean])
                
        for company in criteria.companies:
            add_terms("companies", "must", [company])
        if self._should_enforce_location(criteria):
            add_terms("location", "must", [criteria.location])

        # PR-B: lift Certifications + Education filters from the Step-4
        # rubric into pre-screen. Other categories (skill / title /
        # company / domain / location) overlap with the dimensions
        # already populated above and would double-count, so they're
        # left to the post-enrichment scorer.
        for filter_item in criteria.resume_match_filters or []:
            if not filter_item.get("active", True):
                continue

            category = str(filter_item.get("category", "")).lower()
            if not ("cert" in category or "license" in category or "edu" in category):
                continue

            term = self._resume_filter_term(filter_item)
            if not term:
                continue

            raw_value = str(filter_item.get("value", "")).strip().lower()
            target_match_type = (
                "can"
                if "preferred" in category or raw_value.startswith("can ")
                else "must"
            )
            target_bucket = "education" if "edu" in category else "certifications"
            add_terms(
                target_bucket,
                target_match_type,
                [term],
                label=term,
            )

        return list(dimensions.values())

    def _collect_scoring_dimensions(self, criteria: SearchCriteria) -> List[Dict[str, Any]]:
        """Collect match dimensions for SCORING using Page 3 rubrics + Page 4 resume match filters.

        Weights are looked up from `core.config.scoring_weights_for_family`
        keyed by the search's detected role family (set on the instance
        in `search_candidates` via `_resolve_search_family`). IT and
        unknown family resolve to the legacy default weight set, so IT
        scoring is byte-identical to pre-fix behavior.
        """
        weights = scoring_weights_for_family(self._current_family)
        dimensions = {
            "titles": {
                "label": "Titles",
                "weight": weights["titles"],
                "collections": ["titles"],
                "required": [],
                "preferred": [],
                "excluded": [],
            },
            "skills": {
                "label": "Skills",
                "weight": weights["skills"],
                "collections": ["skills"],
                "required": [],
                "preferred": [],
                "excluded": [],
            },
            "location": {
                "label": "Location",
                "weight": weights["location"],
                "collections": ["locations"],
                "required": [],
                "preferred": [],
                "excluded": [],
            },
            "companies": {
                "label": "Company Experience",
                "weight": weights["companies"],
                "collections": ["companies"],
                "required": [],
                "preferred": [],
                "excluded": [],
            },
            "education": {
                "label": "Education",
                "weight": weights["education"],
                "collections": ["education"],
                "required": [],
                "preferred": [],
                "excluded": [],
            },
            "certifications": {
                "label": "Certifications",
                "weight": weights["certifications"],
                "collections": ["certifications"],
                "required": [],
                "preferred": [],
                "excluded": [],
            },
            "keywords": {
                "label": "Keywords",
                "weight": weights["keywords"],
                "collections": ["skills", "titles", "companies", "education", "certifications", "locations"],
                "required": [],
                "preferred": [],
                "excluded": [],
            },
        }

        for dimension in dimensions.values():
            dimension["required_groups"] = []
            dimension["preferred_groups"] = []
            dimension["excluded_groups"] = []

        def add_terms(
            bucket: str,
            match_type: str,
            values: List[str],
            label: str = "",
            years: int = 0,
            recent: bool = False,
            weight: float = 1.0,
        ) -> None:
            clean_values = [value for value in values if str(value).strip()]
            if not clean_values:
                return
            match_type = str(match_type or "must").lower().replace("_", " ").strip()
            target = "required"
            if match_type in {"exclude", "must not", "must-not", "must_not"}:
                target = "excluded"
            elif match_type in {"can", "preferred", "nice to have", "nice-to-have"}:
                target = "preferred"

            # Clamp weight to the same [0.1, 5] band the UI enforces, with a
            # 1.0 default so legacy payloads behave identically to before.
            try:
                w = float(weight)
            except (TypeError, ValueError):
                w = 1.0
            w = max(0.1, min(5.0, w)) if w > 0 else 1.0

            # Avoid duplicate identical groups in the same dimension. If the
            # same term re-enters with a different weight (e.g. once via the
            # sourcing criteria at weight 1.0, once via a resume-match filter
            # at weight 2.0), keep the higher weight — recruiter intent.
            existing_groups = dimensions[bucket][f"{target}_groups"]
            for g in existing_groups:
                if set(g["terms"]) == set(clean_values) and g["label"] == (label or clean_values[0]):
                    if w > float(g.get("weight") or 1.0):
                        g["weight"] = w
                    return

            dimensions[bucket][f"{target}_groups"].append({
                "terms": clean_values,
                "label": label or clean_values[0],
                "years": years or 0,
                "recent": recent,
                "weight": w,
            })

        # 1. Include Page 5 Sourcing Criteria (as baseline relevance)
        for item in criteria.title_criteria:
            value = str(item.get("value", "")).strip()
            variants = [value] + [str(s).strip() for s in item.get("similar_terms", []) if str(s).strip()]
            add_terms("titles", item.get("match_type", "must"), variants, label=value, years=int(item.get("years") or 0))

        for item in criteria.skill_criteria:
            value = str(item.get("value", "")).strip()
            variants = [value] + [str(s).strip() for s in item.get("similar_terms", []) if str(s).strip()]
            add_terms("skills", item.get("match_type", "must"), variants, label=value, years=int(item.get("years") or 0))

        # 2. Add Page 4 Resume Match Filters (specific preferences)
        for filter_item in criteria.resume_match_filters:
            if not filter_item.get("active", True):
                continue

            category = str(filter_item.get("category", "")).lower()
            raw_value = str(filter_item.get("value", "")).strip()
            if not raw_value:
                continue

            term = self._resume_filter_term(filter_item)
            if not term:
                continue

            # Part 3: per-filter weight from the Step-5 UI. Default 1.0 preserves
            # the pre-weight behaviour for legacy payloads.
            try:
                fw = float(filter_item.get("weight") if filter_item.get("weight") is not None else 1.0)
            except (TypeError, ValueError):
                fw = 1.0

            if "customer" in category or raw_value.lower().startswith("must not"):
                add_terms("companies", "exclude", [term], weight=fw)
            elif "title" in category:
                add_terms("titles", "can" if "preferred" in category or raw_value.lower().startswith("can ") else "must", [term], weight=fw)
            elif "skill" in category:
                add_terms("skills", "can" if "preferred" in category or raw_value.lower().startswith("can ") else "must", [term], weight=fw)
            elif "edu" in category:
                add_terms("education", "can" if "preferred" in category or raw_value.lower().startswith("can ") else "must", [term], weight=fw)
            elif "cert" in category or "license" in category:
                add_terms("certifications", "can" if "preferred" in category or raw_value.lower().startswith("can ") else "must", [term], weight=fw)
            elif "domain" in category:
                add_terms("companies", "can", [term], weight=fw)
                add_terms("keywords", "can", [term], weight=fw)
            elif "local" in term.lower() or "location" in category:
                add_terms("location", "must", [term], weight=fw)
            else:
                add_terms("keywords", "must", [term], weight=fw)

        return list(dimensions.values())

    def _collect_match_dimensions(self, criteria: SearchCriteria) -> List[Dict[str, Any]]:
        """Legacy method - redirects to _collect_scoring_dimensions for backward compatibility."""
        return self._collect_scoring_dimensions(criteria)

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
                    if self._candidate_below_min_years_pre_llm(candidate, criteria):
                        counters["pre_llm_skipped_min_years"] += 1
                        self._log_stage(
                            "LLMGate",
                            "skipping LLM for candidate_id=%s reason=below_min_years_pre_llm threshold=%s" % (
                                candidate_id,
                                int(criteria.min_experience_years or 0),
                            ),
                        )
                        return {"status": "failed_filter", "candidate": None}

                    if criteria.bypass_screening:
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
                    candidate["location"] = candidate["enhanced_info"].get("current_location") or candidate.get("location")
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
                candidate["location"] = candidate["enhanced_info"].get("current_location") or candidate.get("location")
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
                candidate["enhanced_info"] = enhanced
                candidate["enhanced_info_status"] = enhanced.get("resume_extraction_status") or "cached"
                candidate["name"] = enhanced.get("candidate_name") or candidate.get("name")
                candidate["email"] = enhanced.get("email") or candidate.get("email")
                candidate["phone"] = enhanced.get("phone") or candidate.get("phone")
                candidate["title"] = enhanced.get("job_title") or candidate.get("title")
                candidate["location"] = enhanced.get("current_location") or candidate.get("location")
                candidate["education"] = enhanced.get("candidate_education", [])
                candidate["certifications"] = enhanced.get("candidate_certification", [])
                candidate["urls"] = enhanced.get("urls", {})
                candidate["experience_years"] = enhanced.get("years_of_experience") or candidate.get("experience_years")
                if enhanced.get("key_skills"):
                    candidate["skills"] = enhanced.get("key_skills")

            logger.info(f"📦 Attached cached enhanced info for {len(rows)} candidates")
        except Exception as e:
            logger.debug(f"Cached enhanced-info lookup skipped: {e}")

    async def _search_linkedin(self, criteria: SearchCriteria) -> Dict[str, Any]:
        try:
            # Unipile expects skills as a list of dicts or strings. Derive from
            # skill_criteria + title_criteria so callers don't have to send a
            # redundant flat list.
            skill_values = criteria.sourcing_skill_values()
            skills = [{"value": s, "priority": "Must Have"} for s in skill_values]
            candidates = await self.unipile_service.search_candidates(
                skills=skills,
                location=self._scope_location_to_us(criteria.location),
                open_to_work=criteria.open_to_work,
                limit=criteria.page_size,
                boolean_string=self._scope_boolean_to_us(
                    criteria.boolean_string or self._build_boolean_string(criteria)
                ),
            )

            return {"candidates": candidates, "source_type": "LinkedIn-Unipile"}
        except Exception as e:
            logger.error(f"LinkedIn search failed: {e}")
            return {"candidates": [], "source_type": "LinkedIn-Unipile"}
    
    def _extract_linkedin_profile_data(self, profile: Dict[str, Any]) -> Dict[str, Any]:
        """Extract detailed data from full LinkedIn profile for enrichment."""
        extracted = {}
        
        # Extract experience
        experience = profile.get("experience", []) or profile.get("work_history", [])
        if experience:
            company_exp = []
            for exp in experience[:10]:  # Limit to last 10 positions
                company_exp.append({
                    "company": exp.get("company", exp.get("company_name", "")),
                    "title": exp.get("title", exp.get("job_title", "")),
                    "start_date": exp.get("start_date", exp.get("start", "")),
                    "end_date": exp.get("end_date", exp.get("end", "Present"))
                })
            extracted["company_experience"] = company_exp
        
        # Extract education
        education = profile.get("education", [])
        if education:
            edu_list = []
            for edu in education:
                edu_list.append({
                    "degree": edu.get("degree", edu.get("degree_name", "")),
                    "institution": edu.get("school", edu.get("institution", "")),
                    "year": edu.get("end_date", edu.get("year", ""))
                })
            extracted["candidate_education"] = edu_list
        
        # Extract skills
        skills = profile.get("skills", [])
        if skills:
            extracted["skills"] = [{"name": s} if isinstance(s, str) else s for s in skills[:20]]
        
        # Extract certifications
        certifications = profile.get("certifications", []) or profile.get("licenses", [])
        if certifications:
            cert_list = []
            for cert in certifications:
                cert_list.append({
                    "name": cert.get("name", cert.get("certification_name", "")),
                    "issuer": cert.get("authority", cert.get("issuer", "")),
                    "year": cert.get("issue_date", cert.get("year", ""))
                })
            extracted["candidate_certification"] = cert_list
        
        # Extract additional fields
        if profile.get("summary"):
            extracted["summary"] = profile.get("summary")
        
        return extracted

    @staticmethod
    def _candidate_title_match(cand: Dict[str, Any], criteria: SearchCriteria) -> bool:
        """Cheap title sanity check for external candidates (Exa, Dice, Unipile).

        Drops candidates whose `title`/`headline` show no overlap with the
        job's must-titles. Conservative — only fires when (a) the job has
        must-title criteria and (b) the candidate has some non-empty title
        signal to match against. Without this check, an Exa search for a
        Program Manager job can surface "Senior Software Engineer" profiles
        whose only connection to "program manager" is a stray word in their
        about section. Local scoring catches this later, but at higher cost
        and after it's already taken a 100-cap slot.

        Returns True (pass) unless we have strong reason to believe the
        title is mismatched. Multi-word must-titles count as a hit when
        either (a) the full phrase appears, or (b) every significant token
        (>=4 chars) of the phrase appears in the candidate's title field.
        """
        must_titles: List[str] = []
        for item in criteria.title_criteria or []:
            if not isinstance(item, dict):
                continue
            if item.get("match_type", "must") != "must":
                continue
            value = str(item.get("value", "")).strip().lower()
            if value:
                must_titles.append(value)
        if not must_titles:
            return True

        title_text = str(cand.get("title") or "").lower()
        headline_text = str(cand.get("headline") or "").lower()
        if not (title_text or headline_text):
            # No title signal yet (e.g. Unipile pre-enrichment). Defer to
            # downstream filters rather than dropping blind.
            return True

        snippet = str(cand.get("resume_text") or "")[:500].lower()
        hay = " ".join(p for p in (title_text, headline_text, snippet) if p)

        for title in must_titles:
            if title in hay:
                return True
            tokens = [t for t in title.split() if len(t) >= 4]
            if not tokens:
                # All tokens too short to gate on (e.g. "BA" / "QA") — pass.
                return True
            if all(t in title_text or t in headline_text for t in tokens):
                return True
        return False

    @staticmethod
    def _role_hint_from_criteria(criteria: SearchCriteria) -> str:
        """Pick up to two `must` titles from the criteria and join with OR.

        Used to anchor Exa/Dice queries when the boolean string is empty.
        Falls back to "" so the caller can use its own default — Exa uses
        "candidate", Dice uses "resume profile". Never hardcodes a role
        family (the previous "software engineer OR developer" default
        biased non-tech searches into engineering candidates).
        """
        titles: List[str] = []
        for item in criteria.title_criteria or []:
            if not isinstance(item, dict):
                continue
            if item.get("match_type", "must") == "exclude":
                continue
            value = str(item.get("value", "")).strip()
            if value:
                titles.append(value)
            if len(titles) >= 2:
                break
        if not titles:
            return ""
        if len(titles) == 1:
            return f'"{titles[0]}"'
        return " OR ".join(f'"{t}"' for t in titles)

    async def _search_dice(self, criteria: SearchCriteria) -> Dict[str, Any]:
        try:
            skills_values = criteria.sourcing_skill_values()
            boolean_string = criteria.boolean_string or self._build_boolean_string(criteria)
            candidates = await self.exa_service.search_dice_candidates(
                skills=skills_values,
                location=self._scope_location_to_us(criteria.location),
                limit=min(criteria.page_size, 50),
                boolean_string=self._scope_boolean_to_us(boolean_string),
                role_hint=self._role_hint_from_criteria(criteria),
            )
            return {"candidates": candidates, "source_type": "Dice"}
        except Exception as e:
            logger.error(f"Dice search failed: {e}")
            return {"candidates": [], "source_type": "Dice"}

    async def _search_vetted(self, criteria: SearchCriteria) -> Dict[str, Any]:
        try:
            candidates = await self.vetted_service.search_candidates(
                skills=criteria.sourcing_skill_values(),
                location=self._scope_location_to_us(criteria.location),
                limit=criteria.page_size
            )
            return {"candidates": candidates, "source_type": "VettedDB"}
        except Exception as e:
            logger.error(f"VettedDB search failed: {e}")
            return {"candidates": [], "source_type": "VettedDB"}

    async def _search_exa(self, criteria: SearchCriteria) -> Dict[str, Any]:
        try:
            skills_values = criteria.sourcing_skill_values()
            boolean_string = criteria.boolean_string or self._build_boolean_string(criteria)
            candidates = await self.exa_service.search_candidates(
                skills=skills_values,
                location=self._scope_location_to_us(criteria.location),
                limit=min(criteria.page_size, 50),
                boolean_string=self._scope_boolean_to_us(boolean_string),
                role_hint=self._role_hint_from_criteria(criteria),
            )
            return {"candidates": candidates, "source_type": "LinkedIn-Exa"}
        except Exception as e:
            logger.error(f"Exa search failed: {e}")
            return {"candidates": [], "source_type": "LinkedIn-Exa"}

    def _dedup_keys(self, candidate: Dict[str, Any]) -> List[str]:
        """Cross-source dedup keys for one candidate.

        Each key is namespaced (`email:`, `linkedin:`, `name_loc:`) so two
        candidates only collide when *one* of them genuinely overlaps —
        sharing a normalised LinkedIn URL is sufficient even if names
        differ slightly, and an email-with-`@` gates the email key
        against catastrophic empty-string collisions.
        """
        keys: List[str] = []

        email = str(candidate.get("email") or "").strip().lower()
        if email and "@" in email:
            keys.append(f"email:{email}")

        profile_url = str(candidate.get("profile_url") or "").strip().lower()
        if profile_url and "linkedin.com" in profile_url:
            normalized = profile_url.split("?", 1)[0].rstrip("/")
            keys.append(f"linkedin:{normalized}")

        first = str(candidate.get("firstName") or "").strip().lower()
        last = str(candidate.get("lastName") or "").strip().lower()
        full_name = f"{first} {last}".strip()
        if not full_name:
            full_name = str(candidate.get("name") or "").strip().lower()
        location_raw = (
            str(candidate.get("city") or "").strip().lower()
            or str(candidate.get("location") or "").strip().lower()
        )
        if full_name and location_raw and " " in full_name:
            keys.append(f"name_loc:{full_name}|{location_raw}")

        return keys

    def _deduplicate_candidates(self, candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        seen = {}
        unique_results = []
        
        for cand in candidates:
            # Use email or combined name+city as key
            email = cand.get("email", "").lower().strip()
            name = f"{cand.get('firstName', '')} {cand.get('lastName', '')}".lower().strip()
            city = cand.get("city", "").lower().strip()
            
            key = email if email else f"{name}|{city}"
            
            if not key or key == "|":
                unique_results.append(cand)
                continue
                
            if key not in seen:
                seen[key] = cand
                unique_results.append(cand)
            else:
                # If we have a duplicate, prioritize JobDiva-Applicants over others
                existing = seen[key]
                if cand.get("source") == "JobDiva-Applicants" and existing.get("source") != "JobDiva-Applicants":
                    # Replace existing with current
                    for i, r in enumerate(unique_results):
                        if r == existing:
                            unique_results[i] = cand
                            break
                    seen[key] = cand
                    
        return unique_results

unified_search_service = UnifiedCandidateSearch()
