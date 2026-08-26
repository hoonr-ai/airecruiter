import logging
import asyncio
import json
import math
import os
import re
import time
from typing import List, Dict, Any, Optional, Sequence, Tuple
from pydantic import BaseModel

from services.jobdiva import JobDivaService
from utils.email_utils import is_placeholder_email
from services.unipile import unipile_service
from services.vetted import vetted_service
from services.exa_service import exa_service, _extract_city_from_highlights
from services.location import (
    haversine_miles,
    normalize_location_string,
    sanitize_candidate_location,
    within_radius,
)
from services.jobdiva_boolean_translator import strip_jobdiva_dialect
from services.no_contact import apply_no_contact_flag
from core.config import (
    SCORING_REQUIRED_WEIGHT,
    SCORING_PREFERRED_WEIGHT,
    SCORING_YEARS_UNKNOWN_MULT,
    SCORING_YEARS_FLOOR,
    SCORING_RECENT_PENALTY,
    SCORING_RECENCY_DECAY,
    SCORING_EXCLUSION_CAP,
    SCORING_EXCLUSION_PER_HIT,
    SCORING_EXCLUSION_HARD_VETO_THRESHOLD,
    SCORING_UNMATCHED_REQUIRED_FLOOR,
    SCORING_UNMATCHED_PREFERRED_FLOOR,
    SCORING_PARSING_GAP_FLOOR,
    SCORING_COVERAGE_BLEND_THRESHOLD,
    SOURCE_TIER_BONUS,
    OPEN_TO_WORK_SCORE_BONUS,
    JOBAGENT_RANK_SCORE_FLOOR,
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


def _is_excluded_criterion(item: Dict[str, Any]) -> bool:
    """Whether a rubric chip is an EXCLUDE, tolerant of spelling/casing.

    The wizard emits lowercase "exclude", but other writers produce "must_not"
    and capitalised forms, and an exact `== "exclude"` compare silently treats
    those as INCLUDE — which is worse than ignoring them: the term becomes
    something we actively search FOR. Mirrors the normalisation used by
    sourcing_skills_with_priority and the boolean builder.
    """
    match_type = str(item.get("match_type", "must") or "must").lower()
    return match_type.replace("_", " ").strip() in {"exclude", "must not"}


def resolve_jobdiva_sources(sources: Sequence[str]) -> Dict[str, bool]:
    """Which JobDiva producers a request's `sources` list selects.

    JobDiva is three independent producers, each with its own Step-5
    checkbox (or, for Applicants, its own auto-sync path):

      - Applicants:   people who applied to this job_id (no boolean)
      - JobAgent:     JobDiva's AI matcher, driven by the criteria the
                      recruiter configured on the req inside JobDiva
      - TalentSearch: the boolean query PAIR generates

    Bare ``"JobDiva"`` is the legacy combined value — older saved drafts
    and non-wizard callers still send it, and it keeps meaning "run both
    talent pools". Separating them lets a recruiter spend the search
    budget on whichever pool is actually productive for the req.
    """
    selected = set(sources or ())
    legacy_both = "JobDiva" in selected
    return {
        "applicants": bool(
            {"JobDiva Applicants", "JobDiva-Applicants"} & selected
        ),
        "jobagent": legacy_both or "JobDiva-JobAgent" in selected,
        "talentsearch": legacy_both or "JobDiva-TalentSearch" in selected,
    }


class SearchCriteria(BaseModel):
    job_id: str
    title_criteria: List[Dict[str, Any]] = []
    skill_criteria: List[Dict[str, Any]] = []
    keywords: List[str] = []
    resume_match_filters: List[Dict[str, Any]] = []
    location: str = ""
    within_miles: int = 25
    # Job work arrangement ("Remote" | "Hybrid" | "Onsite" | "Unspecified").
    # Remote jobs skip the commute-radius constraint entirely (any US
    # location passes; non-US is still dropped).
    location_type: str = "Unspecified"
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
    # JobDiva tranche controls. Initial search uses offset=0/batch=150; the
    # "Search more" action uses offset=150/batch=150 to append the next page.
    jobdiva_offset: int = 0
    jobdiva_batch_size: int = 150
    # Hiring client / account name. Drives two rubric signals: the
    # "currently employed by client" hard gate (via current-employer
    # exclusion) and the positive "Same client / industry experience"
    # dimension. Optional — when blank both signals degrade gracefully
    # (the same-client dimension drops out and redistributes).
    client_name: str = ""

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

    def sourcing_skills_with_priority(self) -> List[Dict[str, str]]:
        """Like sourcing_skill_values, but each term keeps a coarse priority
        ('Must Have' | 'Preferred') derived from its rubric match_type.

        Lets the Unipile layer map required-vs-optional onto LinkedIn
        MUST_HAVE / CAN_HAVE instead of ANDing every term together — sending
        everything as MUST_HAVE collapsed searches to ~1 result. Skills come
        before titles so the downstream MUST_HAVE cap fills from the more
        reliably-resolvable skill terms first. Excludes/empties dropped;
        deduped by lowercased value (first occurrence wins)."""
        out: List[Dict[str, str]] = []
        seen = set()
        for item in (self.skill_criteria or []) + (self.title_criteria or []):
            if not isinstance(item, dict):
                continue
            match_type = str(item.get("match_type", "must") or "must").lower().replace("_", " ").strip()
            if match_type in {"exclude", "must not"}:
                continue
            value = str(item.get("value", "")).strip()
            if not value:
                continue
            key = value.lower()
            if key in seen:
                continue
            seen.add(key)
            priority = "Preferred" if match_type in {"can", "preferred", "nice to have"} else "Must Have"
            out.append({"value": value, "priority": priority})
        return out

    def skill_only_values(self) -> List[str]:
        """Plain skill strings from skill_criteria ONLY (titles excluded).

        Used for the natural-language Exa/Dice queries where titles and
        skills occupy different slots of the sentence — mixing titles into
        the skill list (as sourcing_skill_values does) reads as
        '<title> with <title>, <skill> experience'."""
        values: List[str] = []
        seen = set()
        for item in self.skill_criteria or []:
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

    def sourcing_titles(self) -> List[str]:
        """Plain non-exclude title strings, in criteria order, deduped.

        Feeds the one-role-per-search Exa/Dice fan-out and the Exa Agent
        deep-search prompt."""
        titles: List[str] = []
        seen = set()
        for item in self.title_criteria or []:
            if not isinstance(item, dict):
                continue
            if _is_excluded_criterion(item):
                continue
            value = str(item.get("value", "")).strip()
            key = value.lower()
            if not value or key in seen:
                continue
            seen.add(key)
            titles.append(value)
        return titles

    def title_variants(self, max_titles: int = 3) -> List[str]:
        """Role variants for multi-pull title searches, most important first.

        Order: primary title chip, then ITS selected similar titles
        (`similar_terms`, the recruiter-approved grounded variants), then the
        next title chip and its variants. Deduped case-insensitively and
        capped at `max_titles` so the per-variant TalentSearch pulls stay
        bounded."""
        variants: List[str] = []
        seen = set()

        def _add(value: Any) -> None:
            v = str(value or "").strip()
            if v and v.lower() not in seen:
                seen.add(v.lower())
                variants.append(v)

        for item in self.title_criteria or []:
            if not isinstance(item, dict):
                continue
            if _is_excluded_criterion(item):
                continue
            _add(item.get("value"))
            for similar in item.get("similar_terms") or []:
                _add(similar)
        return variants[: max(1, int(max_titles or 1))]

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
        # Bounded concurrency for Exa Research API (1/5 account QPS). Lazy
        # init in `search_candidates` so the env value is honoured at request
        # time, not import time.
        self._exa_agent_semaphore: Optional[asyncio.Semaphore] = None

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

    async def _apply_contact_enrichment(
        self,
        cand: Dict[str, Any],
        criteria: "SearchCriteria",
        *,
        overwrite: bool,
    ) -> None:
        """In-line ZoomInfo → Apollo contact enrichment for one candidate.

        ZoomInfo→Apollo is the source of truth for sourced contact info; the
        precedence (ZoomInfo first, Apollo fallback) lives inside
        ``contact_enrichment.enrich_contact_for_sourcing``. Gated by
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
        separately inside the helper (EXA_SOURCING_CONTACT_CAP per job).
        """
        profile_url = str(cand.get("profile_url") or "").strip()
        if "linkedin.com/in/" not in profile_url.lower():
            return
        source = str(cand.get("source") or "")
        enhanced = cand.get("enhanced_info") if isinstance(cand.get("enhanced_info"), dict) else {}
        company = str(
            cand.get("current_company")
            or enhanced.get("current_company")
            or enhanced.get("company")
            or ""
        ).strip()
        try:
            enrich = await contact_enrichment.enrich_contact_for_sourcing(
                profile_url,
                criteria.job_id,
                full_name=str(cand.get("name") or "").strip() or None,
                include_exa=source.startswith("LinkedIn"),
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

        if overwrite:
            new_email = enrich.get("workEmail") or enrich.get("personalEmail") or ""
            new_phone = enrich.get("mobilePhone") or enrich.get("workPhone") or ""
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
                or enrich.get("workPhone")
                or ""
            )
        if cand.get("email") or cand.get("phone"):
            if not isinstance(cand.get("enhanced_info"), dict):
                cand["enhanced_info"] = cand.get("enhanced_info") or {}
            cand["enhanced_info"]["contact_enrichment_provider"] = enrich.get("provider_used")

    async def search_candidates(self, criteria: SearchCriteria):
        """
        Orchestrate candidate search across multiple providers with tiered JobDiva logic.
        Yields candidates as they are finalized.
        """
        start_time = time.time()
        self._log_stage("Start", f"job={criteria.job_id} sources={', '.join(criteria.sources or [])}")

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
            """Apply match scoring to a candidate."""
            # Ensure name is title-cased if it exists
            if cand.get("name"):
                cand["name"] = str(cand["name"]).title()

            # Location hygiene at the emit choke-point: no path may display or
            # persist a work-arrangement string ("Remote"/"Hybrid"/"WFH") as
            # the candidate's location — it isn't a place, it dodges the
            # radius gate (can't geocode → unknown → soft-keep), and it hides
            # the CRM's real city/state. Blank it and rebuild from the
            # structured fields; an empty location is honest.
            loc = sanitize_candidate_location(cand.get("location"))
            if not loc:
                # CRM city fields can literally say "REMOTE" ("REMOTE, GA"),
                # so the rebuild is sanitized too.
                loc = sanitize_candidate_location(", ".join(
                    p for p in [
                        str(cand.get("city") or "").strip(),
                        str(cand.get("state") or "").strip(),
                    ] if p
                ))
            cand["location"] = loc

            # No-contact companies (services/no_contact.py): flag at the emit
            # choke-point so every source is covered — external rows carry
            # employer fields pre-LLM, JobDiva rows only after enhancement.
            # Flagged rows are display-only: never scored (match_score None
            # renders as the grey "N/A" pill, and the None-safe score gates
            # keep the row visible), greyed out client-side, and blocked
            # server-side at /candidates/save and the launch gate.
            if apply_no_contact_flag(cand):
                cand["match_score"] = None
                cand["missing_skills"] = []
                cand["matched_skills"] = []
                cand["explainability"] = [
                    cand.get("no_contact_reason") or "No-contact company"
                ]
                cand["match_score_details"] = {}
                return cand

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

            if cand.get("scoring_mode") == "high_level":
                # No % is shown for agent rows (see the match_score=None stamp
                # below), so drop the rubric tier-judgment line — it reads as
                # a verdict on a score the recruiter never sees. Concrete
                # lines (matched dimensions, location note, hard exclusions)
                # stay: those are the legitimate reasons the popup surfaces.
                _tier_lines = {
                    "Excellent rubric and sourcing alignment",
                    "Strong overall fit across active filters",
                    "Partial fit; review missing rubric requirements",
                    "Limited fit against active rubric and sourcing filters",
                }
                _expl = [
                    line for line in (cand["explainability"] or [])
                    if line not in _tier_lines
                ]
                cand["explainability"] = [
                    "Matched by JobDiva agent search — detailed AI skills "
                    "analysis skipped"
                ] + _expl[:5]

            # JobAgent-rank floor: JobDiva's JobAgent endpoint pre-ranks
            # candidates by their own relevance matcher. After refactor
            # `b5a6aaa` (JobAgent-only sourcing), every JobDiva candidate
            # that reaches this code has already been vetted for the job
            # by JobDiva's matcher — recruiters trust that signal more
            # than our rubric-literal-match score, which can crater on
            # phrasing variants (resume "Microsoft Office" vs rubric
            # "MS Office Suite"). Apply a tiered floor so JobDiva's top
            # picks never score below what their rank implies; rubric
            # match still wins when it's *higher* than the floor, so
            # rubric-strong candidates aren't artificially capped.
            #
            # Skipped when hard-veto fired (base_score == 0): exclusion
            # rules always trump rank trust.
            source = str(cand.get("source") or "")
            api_rank = cand.get("api_rank")
            if (
                source == "JobDiva-JobAgent"
                and base_score > 0
                and isinstance(api_rank, int)
            ):
                floor = 0
                for rank_cutoff, floor_value in JOBAGENT_RANK_SCORE_FLOOR:
                    if api_rank < rank_cutoff:
                        floor = floor_value
                        break
                if floor and cand["match_score"] < floor:
                    cand["match_score_details"]["jobagent_rank_floor"] = {
                        "api_rank": api_rank,
                        "floor": floor,
                        "rubric_score": base_score,
                    }
                    cand["match_score"] = floor
                    # base_score is what the source-tier bonus stacks on
                    # below — re-anchor to the floored value so the bonus
                    # math reflects the post-floor baseline.
                    base_score = floor

            # Source-tier bonus: warm leads (recruiter's own applicants,
            # JobDiva talent pool, curated DBs) outrank cold scrapes when
            # raw scores are close. Only applied when base_score > 0 so
            # excluded / hard-vetoed candidates aren't promoted.
            bonus = SOURCE_TIER_BONUS.get(source, 0)
            if bonus and base_score > 0:
                boosted = min(100, cand["match_score"] + bonus)
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

            # Open-to-Work boost. Candidates confirmed open to work (the real
            # Apify #OpenToWork signal, resolved for LinkedIn sources) get a
            # small tie-breaker bump — an actively-job-seeking match is more
            # actionable than an identical passive one. Only when the signal is
            # explicitly True (not "checking"/unknown) and base_score > 0, so a
            # hard-vetoed candidate is never promoted. For candidates whose
            # status resolves asynchronously after this scoring pass (cold Apify
            # cache), the UI still shows the badge via polling; the score bump
            # lands on the warm path / subsequent searches.
            if base_score > 0 and OPEN_TO_WORK_SCORE_BONUS and cand.get("open_to_work") is True:
                prev_score = cand["match_score"]
                cand["match_score"] = min(100, prev_score + OPEN_TO_WORK_SCORE_BONUS)
                cand["match_score_details"]["open_to_work_bonus"] = OPEN_TO_WORK_SCORE_BONUS

            # Candidate-details failure: when the JobDiva detail/résumé fetch or
            # LLM extraction yielded no real data (detail_failed), we can't fairly
            # score the candidate. Surface "N/A" (match_score=None) instead of a
            # misleading 0% / floored score so they aren't dropped at Launch PAIR.
            # A genuine hard-veto (exclusion rule / out-of-radius) always takes
            # precedence — those keep their 0% and are skipped at launch.
            if cand.get("detail_failed"):
                hard_veto = (cand.get("match_score_details") or {}).get("hard_veto") or {}
                if not hard_veto.get("triggered"):
                    cand["match_score"] = None

            # JobDiva-JobAgent rows are never presented as a percentage
            # (2026-08-25 policy): they follow the criteria the recruiter
            # authored inside JobDiva and JobDiva's own ranking, so a rubric %
            # misleads. The scoring pass above still runs — matched/missing
            # skills, explainability, and the location badge feed the row and
            # its popup — only the number is withheld. NULL match_score is
            # already the storage/UI "unscored" sentinel (kept by every
            # min-score gate, NULLS LAST in rank-list sorting).
            if str(cand.get("source") or "") == "JobDiva-JobAgent":
                cand["match_score"] = None

            return cand

        # Which JobDiva producers this request selects — see
        # resolve_jobdiva_sources for the source-name contract.
        # Product requirement (Apr 2026): Step-5 sourcing must NOT fetch
        # applicants; they're surfaced automatically via sync + rank-list.
        _jobdiva_selection = resolve_jobdiva_sources(criteria.sources)
        applicants_selected = _jobdiva_selection["applicants"]
        jobagent_selected = _jobdiva_selection["jobagent"]
        talentsearch_selected = _jobdiva_selection["talentsearch"]
        talent_selected = jobagent_selected or talentsearch_selected

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
                if (
                    bool(getattr(_sc_gate, "EXTERNAL_LOCATION_CONFIRMED_MISMATCH_DROP", True))
                    and cand.get("location_veto_reason")
                ):
                    summary["location_mismatch_dropped"] = summary.get("location_mismatch_dropped", 0) + 1
                    self._log_stage(
                        "LocationGate",
                        f"dropping candidate_id={cid} source={_src} "
                        f"reason={cand.get('location_veto_reason')} "
                        f"distance={cand.get('distance_miles')}",
                    )
                    return False
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
            ):
                v = cand.get(key)
                if v not in (None, "", [], {}):
                    agent_payload[key] = v

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
            if error is not None:
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

        async def emit_jobdiva_scored(cand, assessment, qualified_counter_key=None, min_score=None):
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
            """
            cid = str(cand.get("candidate_id") or cand.get("id") or "")
            location_reason = assessment.get("location_failure_reason") if isinstance(assessment, dict) else None
            if not assessment.get("passes") and location_reason in {"non_us_candidate"}:
                distance = cand.get("distance_miles")
                self._log_stage(
                    "LocationGate",
                    f"dropping candidate_id={cid} reason={location_reason} distance={distance}",
                )
                await queue.put({
                    "type": "candidate_detail",
                    "candidate_id": cid,
                    "stage": "dropped",
                    "patch": {"_stage": "dropped", "_drop_reason": "non_us_candidate"},
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

            scored_patch: Dict[str, Any] = {"_stage": "scored"}
            for key in (
                "match_score", "matched_skills", "missing_skills",
                "explainability", "match_score_details", "screening_summary",
                "enhanced_info", "enhanced_info_status", "education",
                "certifications", "skills", "urls", "experience_years",
                "name", "title", "location", "email", "phone",
                # Forward merged identity so a take-over reflects the unioned
                # sources + absorbed contact/profile on the surviving row.
                "sources", "profile_url", "linkedin_url", "image_url",
                "resume_id", "city", "state",
                # Geo verdict → UI "~N mi away" badge on the location cell.
                "zipcode", "distance_miles", "location_out_of_radius",
                "location_match_reason",
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
                if criteria.bypass_screening:
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
                        if await emit_jobdiva_agent_result(_cand, source_label):
                            fresh_talent.append(_cand)

                    if not fresh_talent:
                        return 0

                    from core import sourcing_config as _sc_talent
                    async for event in self._enrich_filtered_jobdiva_progressive(
                        fresh_talent, criteria, skip_llm=high_level_scoring
                    ):
                        ev_type = event.get("type")
                        if ev_type == "candidate_detail":
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
                            if await emit_jobdiva_scored(
                                cand, assessment, "qualified_talent", min_score=min_score
                            ):
                                emitted += 1
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
                        await _process_talent_pool(
                            jobagent_res,
                            stage_name="JobDiva",
                            source_label="JobDiva-JobAgent",
                            cap_label="JobAgent rank",
                            # Recruiter-authored criteria in JobDiva + its own
                            # ranking → trust the results: high-level score only
                            # (no per-candidate LLM skills-match), never dropped.
                            high_level_scoring=bool(
                                getattr(_sc_pool, "JOBAGENT_HIGH_LEVEL_SCORING", True)
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
                    )
                    pool_exc: Optional[BaseException] = None
                    try:
                        if not two_phase:
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
                    pools.append(_run_jobagent_pool())
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
                        # Source-native location is authoritative; the LLM's
                        # resume-parsed current_location only fills a blank
                        # (it can latch onto a past employer / education city).
                        # Both sides sanitized: "Remote"/"Hybrid" is a work
                        # arrangement, never a place.
                        cand["location"] = (
                            sanitize_candidate_location(cand.get("location"))
                            or sanitize_candidate_location(cand["enhanced_info"].get("current_location"))
                        )
                        if cand["enhanced_info"].get("structured_skills") or cand["enhanced_info"].get("skills"):
                            cand["skills"] = cand["enhanced_info"].get("structured_skills") or cand["enhanced_info"].get("skills")

                        # Exa deep analysis on filter survivors only. Replaces
                        # the 4000-char highlights with the full profile text
                        # plus a per-candidate match summary; preserves the
                        # original highlights in resume_text since downstream
                        # location extractors are tuned to that shape.
                        # When EXA_AGENT_ENABLED, Pass B (Research API) is the
                        # canonical enrichment path and supersedes this per-URL
                        # get_contents() summary — keep this block as the
                        # fallback when the agent path is disabled.
                        if (
                            source_type == "LinkedIn-Exa"
                            and not exa_pass_b_should_run
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
                        if is_exa_source or not has_email:
                            await self._apply_contact_enrichment(
                                cand, criteria, overwrite=is_exa_source
                            )
                        return {"status": "success", "candidate": cand}

                process_tasks = [asyncio.create_task(_process_external_single(c)) for c in ext_candidates]
                # Per-status drop accounting: these gates used to discard rows
                # with no counter or log line, so "Exa found 50, UI shows 1"
                # was undiagnosable from the stream log.
                _status_counts: Dict[str, int] = {}
                for task in asyncio.as_completed(process_tasks):
                    result = await task
                    _status_counts[result["status"]] = _status_counts.get(result["status"], 0) + 1
                    if result["status"] in ("success", "no_contact_shown"):
                        cand = result["candidate"]
                        assessment = self._filter_assessment(cand, criteria, enforce_years=False)
                        if await emit_candidate(cand, assessment):
                            _ext_emitted += 1
                if _status_counts:
                    self._log_stage(
                        name,
                        f"processed {len(ext_candidates)} {source_type} candidates: "
                        + ", ".join(f"{k}={v}" for k, v in sorted(_status_counts.items())),
                    )
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
                # seed its run with the URLs we just yielded.
                if name == "Exa" and exa_pass_b_should_run:
                    exa_pass_a_done.set()
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
            """
            enriched = 0
            new_found = 0
            failure_reason = ""
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
                            # Remote-aware: US-wide for remote jobs, else the
                            # US-scoped job location.
                            location=self._search_location_for_source(criteria),
                            seed_urls=seed_urls,
                            within_miles=getattr(criteria, "within_miles", 25),
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
                    # Contact fields from the agent's enrichment tool (schema
                    # requests them with descriptions when the enrich flag is
                    # on). Sanity-gate before use; only backfill — never
                    # overwrite ZoomInfo/Apollo data already on the row.
                    agent_email, agent_phone = contact_enrichment.sanitize_agent_contact(
                        entry.get("email"), entry.get("phone")
                    )

                    if nurl and nurl in pass_a_by_url:
                        existing = pass_a_by_url[nurl]
                        cid = str(existing.get("candidate_id") or existing.get("id") or "")
                        if not cid:
                            continue
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
                        # enrichment block. The agent run may have supplied
                        # email/phone already (contact fields in the output
                        # schema); only fall through to ZoomInfo→Apollo when
                        # something is still missing — mirrors the launch-time
                        # chain's first-hit-wins short-circuit and preserves
                        # the per-job enrichment budget. overwrite=True keeps
                        # ZoomInfo/Apollo as the source of truth for whatever
                        # fields they do return.
                        if not (
                            str(new_cand.get("email") or "").strip()
                            and str(new_cand.get("phone") or "").strip()
                        ):
                            await self._apply_contact_enrichment(
                                new_cand, criteria, overwrite=True
                            )
                        await emit_candidate(new_cand, assessment)
                        new_found += 1
            except Exception as e:
                logger.error(f"Exa Agent producer failed: {e}", exc_info=True)
                failure_reason = failure_reason or f"producer crashed: {type(e).__name__}"
            finally:
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

        # Exa Research API Pass B — depends on Pass A (`produce_external("Exa")`),
        # so only schedule when both the agent is enabled AND Exa is selected.
        if exa_pass_b_should_run:
            producers.append(asyncio.create_task(produce_exa_agent()))

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
            while active > 0:
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
            from services.jobdiva import get_field, _select_better_phone

            page_size = max(1, int(sc.FAST_PATH_DETAIL_BACKGROUND_PAGE_SIZE))
            page_delay = max(0.0, float(sc.FAST_PATH_DETAIL_BACKGROUND_PAGE_DELAY_S))

            # Snapshot each candidate's current phone so the détail patch can be
            # upgrade-only (never downgrade a mobile to a home/work number).
            existing_phone_by_id = {
                str(c.get("candidate_id") or c.get("id") or ""): str(c.get("phone") or "")
                for c in candidates
                if c.get("candidate_id") or c.get("id")
            }

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
                    notes_actions_map = await self.jobdiva_service._fetch_candidate_notes_action_types_batch(
                        token, page_ids
                    )
                    quals_map = await self.jobdiva_service._fetch_candidate_qualifications_batch(
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
                    # Upgrade-only phone: pick the détail's best (mobile-typed)
                    # number and only patch it when it beats what we already
                    # have — never downgrade a JobAgent mobile to a home/work #.
                    better_phone = _select_better_phone(
                        existing_phone_by_id.get(str(cand_id), ""), detail
                    )
                    address1 = (get_field(detail, ["address1", "ADDRESS1", "address", "ADDRESS"]) or "")
                    linkedin = (get_field(detail, ["linkedinUrl", "LINKEDINURL", "linkedin", "LINKEDIN", "linkedIn", "LINKEDIN_URL"]) or "")
                    resume_id = (get_field(detail, ["resumeId", "RESUMEID", "resume_id"]) or "")
                    resume_text = self.jobdiva_service._extract_resume_text(detail) or ""
                    city = (get_field(detail, ["city", "CITY", "locationCity", "LOCATIONCITY"]) or "")
                    state = (get_field(detail, ["state", "STATE", "locationState", "LOCATIONSTATE"]) or "")
                    if email:
                        patch["email"] = str(email).strip()
                    if better_phone and better_phone != existing_phone_by_id.get(str(cand_id), "").strip():
                        patch["phone"] = better_phone
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
                    # Rebuild the display string too — the UI reads `location`
                    # first, so patching only city/state leaves a stale (or
                    # LLM-corrupted) `location` on screen forever. Sanitized:
                    # CRM city fields can literally say "REMOTE", and this
                    # patch bypasses the finalize_candidate choke-point.
                    if city or state:
                        rebuilt_loc = sanitize_candidate_location(", ".join(
                            p for p in [str(city).strip(), str(state).strip()] if p
                        ))
                        if rebuilt_loc:
                            patch["location"] = rebuilt_loc

                    quals = detail.get("qualifications") or detail.get("QUALIFICATIONS") or quals_map.get(str(cand_id)) or []
                    if quals:
                        patch["qualifications"] = quals
                        for q in quals:
                            if not isinstance(q, dict): continue
                            qval = str(q.get("QUALIFICATIONVALUE") or q.get("qualificationValue") or "").strip()
                            qname = str(q.get("QUALIFICATION") or q.get("qualificationName") or "").strip()
                            if "current employee" in qval.lower() or "current employee" in qname.lower():
                                patch["employee_status"] = "Current Employee"
                    emp_status = get_field(detail, ["EMPLOYEESTATUS", "employeeStatus", "CURRENTEMPLOYEE", "currentEmployee", "ASSIGNMENTSTATUS", "assignmentStatus"])
                    if emp_status:
                        patch["employee_status"] = str(emp_status).strip()
                    avail_val = get_field(detail, ["AVAILABLE", "available", "STATUS", "status"])
                    if avail_val is not None and avail_val != "":
                        patch["available"] = avail_val

                    action_types = notes_actions_map.get(str(cand_id), [])
                    if action_types:
                        patch["action_types"] = action_types
                        for at in action_types:
                            at_low = at.lower()
                            if "offer accepted" in at_low or "placed" in at_low or "hired" in at_low:
                                patch["offer_status"] = "Offer Accepted"
                                break
                            elif "offer extended" in at_low:
                                patch["offer_status"] = "Offer Extended"

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
                ja_result = await self.jobdiva_service.search_via_job_agent(
                    job_id=criteria.job_id,
                    resume_count=resume_count,
                    require_resume=getattr(criteria, "require_resume", True),
                )
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

    # Canadian province/territory codes. Disjoint from _US_STATE_CODES, so a
    # 2-letter token identifies its country unambiguously — EXCEPT the codes
    # that collide with _COUNTRY_ALIASES ("CA", "IN", "DE", "NL"): a token in
    # the state slot always resolves as a state/province, never a country.
    _CA_PROVINCE_CODES = frozenset({
        "AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE",
        "QC", "SK", "YT",
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

    def _resolve_jobdiva_geo(self, criteria: SearchCriteria) -> tuple[List[str], List[str], str]:
        """
        Produce (countries, states, zip_code) for JobDiva's talentSearchDef.

        Priority: explicit `criteria.countries` / `criteria.states` if set;
        otherwise parse `criteria.location`. The Step-5 seed is
        "City, ST ZIP" — the zip must be extracted BEFORE tokenizing
        (it used to ride along as "AZ 85281", fail the len==2 state-code
        check, and silently drop the state from the payload). Always
        defaults to ``["US"]`` when no country can be resolved — searches
        are US-only by policy.
        """
        from services import zip_index

        loc = (criteria.location or "").strip()
        parsed = self._parse_location(loc) if loc else {"city": "", "state": "", "zip": ""}
        zip_code = parsed.get("zip") or ""
        if zip_code and not zip_index.is_known_zip(zip_code):
            zip_code = ""
        # Job has no zip but a resolvable city → use the zip nearest the
        # city centroid so zip+radius search still has something to anchor.
        if not zip_code and parsed.get("city") and parsed.get("state"):
            zip_code = zip_index.city_state_default_zip(parsed["city"], parsed["state"]) or ""

        countries = [c.strip() for c in (criteria.countries or []) if c and c.strip()]
        states = [s.strip() for s in (criteria.states or []) if s and s.strip()]
        if countries or states:
            return countries, states, zip_code

        if not loc:
            # No location criteria at all → still scope to US-only.
            return ["US"], [], ""

        # Country comes from the parser, whose state-slot rule disambiguates
        # "CA": "Los Angeles, CA" is California ⇒ US (the old right-to-left
        # token walk here matched CA-the-country first and scoped every
        # California job to Canada), while "Ajax, ON CA" / "Toronto, ON" ⇒
        # Canada via the trailing token / the ON province code.
        parsed_country = (parsed.get("country") or "").upper()
        if parsed_country:
            countries.append(parsed_country)

        # State: prefer the robust parser (handles "AZ", "Arizona",
        # "AZ 85281", and Canadian provinces like "ON"/"Ontario"), fall back
        # to the raw token walk.
        loc_no_zip = re.sub(r"\b\d{5}(?:-\d{4})?\b", " ", loc)
        tokens = [t.strip() for t in loc_no_zip.split(",") if t.strip()]
        parsed_state = (parsed.get("state") or "").upper()
        if parsed_state and parsed_state in self._US_STATE_CODES:
            states.append(parsed_state)
        elif parsed_state and parsed_state in self._CA_PROVINCE_CODES:
            # JobDiva's talentSearchDef `states` carries provinces for
            # country=CA searches (its own UI labels the field
            # "State/Province").
            states.append(parsed_state)
        else:
            for idx in range(len(tokens) - 1, -1, -1):
                token_upper = tokens[idx].upper()
                if len(token_upper) == 2 and token_upper in self._US_STATE_CODES:
                    states.append(token_upper)
                    break

        # Bare-zip input ("85281") → backfill the state from the zip index.
        if not states and zip_code:
            zip_entry = zip_index.lookup_zip(zip_code)
            if zip_entry and zip_entry["state"].upper() in self._US_STATE_CODES:
                states.append(zip_entry["state"].upper())

        if not countries:
            countries.append("US")

        return countries, states, zip_code

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

        POLICY (JOBDIVA_LOCATION_SOFT_KEEP, default True): out-of-radius /
        state-mismatch JobDiva candidates are NOT dropped here — they are kept
        and flagged (`location_out_of_radius`, with `distance_miles`) so the
        location rubric dimension scores them down and the recruiter can narrow
        via the location chip / MIN MATCH. This mirrors `emit_candidate`'s
        "soften everything except positive non-US evidence" policy so a JobDiva
        row is never removed before it renders. Set the flag False to restore
        the old hard radius filter.
        """
        if not candidates:
            return candidates

        from core import sourcing_config as _sc_loc
        soft_keep = bool(getattr(_sc_loc, "JOBDIVA_LOCATION_SOFT_KEEP", True))
        enforce_location = self._should_enforce_location(criteria)
        job_country = self._target_country(criteria)

        kept: List[Dict[str, Any]] = []
        non_us_dropped = 0
        out_of_radius_kept = 0
        filtered = 0
        geocode_failed = 0

        for c in candidates:
            if self._is_likely_outside_country(c, job_country):
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
            elif soft_keep:
                # Outside radius / different state — keep it visible (scored
                # down on location), never hard-drop a JobDiva candidate here.
                c["location_out_of_radius"] = True
                out_of_radius_kept += 1
                kept.append(c)
            else:
                filtered += 1
                if reason in {"candidate_ungeocodable", "target_ungeocodable"}:
                    geocode_failed += 1

        self._log_stage(
            "LocationGate",
            f"pre-filter kept {len(kept)}/{len(candidates)} candidates"
            f" (non_us_dropped={non_us_dropped}, out_of_radius_kept={out_of_radius_kept},"
            f" filtered={filtered}, geocode_failures={geocode_failed})",
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

    def _build_boolean_string(
        self, criteria: SearchCriteria, dialect: str = "generic"
    ) -> str:
        """Build the sourcing boolean from the Step 5 filters.

        ``dialect="jobdiva"`` emits JobDiva's native shape — roles in the
        dedicated ``TITLES= (...)`` field and ``IN {US}`` for a remote role's
        geo, i.e. what a recruiter hand-writes into JobAgent criteria.
        ``"generic"`` (the default) keeps a plain boolean expression, because
        Unipile and Exa parse neither of those constructs.

        AND/OR *allocation* matters far more here than term count, because
        every AND multiplies the constraint while every OR widens it:

          - **Roles are alternatives** → one OR group, AND'ed in once. Nobody
            is a "Senior Data Engineer" and an "ETL Developer" at the same
            time, so ANDing title clauses matched ~nobody.
          - **Skills are requirements** → they AND, but only the most
            important `JOBDIVA_BOOLEAN_MUST_SKILL_CAP` of them. The ranked
            overflow is demoted into the preferred OR group instead of being
            dropped, so it still lifts ranking without gating the search.
          - **Companies are alternatives** ("worked at any of these") → OR.
          - **Excludes** → a single trailing `NOT (...)` group.

        Importance ranking for the capped skill ANDs matches the structured
        TalentSearch term selection (role-named skills first, then ones
        carrying explicit years/recency), so the boolean a recruiter reads and
        the query we actually run agree on what the core requirements are.
        """
        from services.jobdiva_boolean_translator import term_appears_as_token
        from services.rubric_grounding import is_industry_term as term_is_industry
        from core import sourcing_config as _sc_bool

        must_skill_cap = max(
            1, int(getattr(_sc_bool, "JOBDIVA_BOOLEAN_MUST_SKILL_CAP", 4) or 4)
        )
        # The role the skill ranking is measured against — the first included
        # title chip. With no title chips the ranking simply falls back to
        # years/recency then chip order.
        primary_role = ""
        for _t in criteria.title_criteria or []:
            _mt = str(_t.get("match_type", "must") or "must").lower().replace("_", " ").strip()
            if _mt in {"exclude", "must not"}:
                continue
            primary_role = str(_t.get("value", "")).strip()
            if primary_role:
                break

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

        def match_type_of(item: Dict[str, Any]) -> str:
            """Normalized rubric match type.

            The wizard emits must/can/exclude, but casing and `must_not`
            spellings turn up from other writers. The old exact `== "exclude"`
            compare fell through to the else-branch, promoting an EXCLUDED term
            into the REQUIRED AND chain — the worst possible misread.
            """
            mt = str(item.get("match_type", "must") or "must").lower()
            mt = mt.replace("_", " ").strip()
            if mt in {"exclude", "must not"}:
                return "exclude"
            if mt in {"can", "preferred", "nice to have"}:
                return "can"
            return "must"

        def variants_of(item: Dict[str, Any]) -> List[str]:
            """Term + its recruiter-approved similar terms, registered for
            dedup and returned quoted."""
            out: List[str] = []
            value = str(item.get("value", "")).strip()
            if value:
                source_keys.add(normalize_term(value))
                out.append(value)
            for similar in item.get("similar_terms", []) or []:
                s = str(similar).strip()
                if s:
                    source_keys.add(normalize_term(s))
                    out.append(s)
            return out

        must_groups: List[str] = []
        can_terms: List[str] = []
        exclude_terms: List[str] = []
        seen_must = set()
        seen_can = set()
        seen_exclude = set()
        source_keys = set()

        # ── Roles: alternatives, so ONE OR group ─────────────────────────
        # A candidate is a "Senior Data Engineer" OR an "ETL Developer" —
        # essentially never both. ANDing title clauses together was the single
        # biggest recall killer in this builder: two ANDed titles already
        # matched ~nobody, and the old workaround (truncate to the first two)
        # both over-constrained AND discarded the remaining role variants.
        # Collapsing every included title plus its similar titles into one OR
        # group means extra variants now *widen* recall instead of destroying
        # it, so none have to be thrown away.
        role_terms: List[str] = []
        seen_roles = set()
        for item in criteria.title_criteria or []:
            mt = match_type_of(item)
            terms = variants_of(item)
            if not terms:
                continue
            if mt == "exclude":
                group = (
                    quote(terms[0]) if len(terms) == 1
                    else f"({' OR '.join(quote(t) for t in terms)})"
                )
                add_unique(exclude_terms, seen_exclude, group, terms[0])
                continue
            for t in terms:
                key = normalize_term(t)
                if key and key not in seen_roles:
                    seen_roles.add(key)
                    role_terms.append(t)

        # ── Skills: requirements, so they AND — but only the important few ──
        # Rank by the same rule the TalentSearch term selection uses: skills
        # named in the primary role first (the core competency), then those
        # carrying an explicit years/recency requirement, then wizard chip
        # order. Keep the top N as hard ANDs and demote the overflow into the
        # preferred OR group rather than dropping it — it still lifts ranking
        # without gating the search.
        # Industry chips get their own AND'ed cluster and skip the must-skill cap:
        # industry and capability are different axes. Folding a preferred industry
        # into the shared OR bucket would emit
        # `(Articulate OR Captivate OR Healthcare)`, i.e. a healthcare candidate
        # with no eLearning tool satisfies the eLearning requirement — the merge
        # weakens the query instead of sharpening it. Recruiters write them as
        # separate groups for the same reason.
        industry_groups: List[str] = []
        seen_industry = set()

        must_skill_items: List[Dict[str, Any]] = []
        for item in criteria.skill_criteria or []:
            mt = match_type_of(item)
            terms = variants_of(item)
            if not terms:
                continue
            group = (
                quote(terms[0]) if len(terms) == 1
                else f"({' OR '.join(quote(t) for t in terms)})"
            )
            if mt == "exclude":
                add_unique(exclude_terms, seen_exclude, group, terms[0])
            elif term_is_industry(terms[0]):
                add_unique(industry_groups, seen_industry, group, terms[0])
            elif mt == "can":
                add_unique(can_terms, seen_can, group, terms[0])
            else:
                try:
                    has_years = int(item.get("years") or 0) > 0
                except (TypeError, ValueError):
                    has_years = False
                must_skill_items.append({
                    "group": group,
                    "value": terms[0],
                    "in_role": term_appears_as_token(terms[0], primary_role),
                    "weighted": has_years or bool(item.get("recent")),
                })

        must_skill_items.sort(
            key=lambda s: (0 if s["in_role"] else 1, 0 if s["weighted"] else 1)
        )
        for idx, s in enumerate(must_skill_items):
            if idx < must_skill_cap:
                add_unique(must_groups, seen_must, s["group"], s["value"])
            else:
                add_unique(can_terms, seen_can, s["group"], s["value"])

        for keyword in criteria.keywords:
            if keyword and keyword.strip():
                add_unique(must_groups, seen_must, quote(keyword), keyword)

        # ── Companies: alternatives too ("worked at any of these") ────────
        # These were ANDed, which asked for someone employed by every listed
        # company simultaneously.
        company_terms: List[str] = []
        seen_companies = set()
        for company in criteria.companies:
            c = str(company or "").strip()
            key = normalize_term(c)
            if c and key and key not in seen_companies:
                seen_companies.add(key)
                source_keys.add(key)
                company_terms.append(c)

        parts: List[str] = []
        parts.extend(must_groups)
        # Own AND'ed clause(s), never merged into the preferred bucket.
        parts.extend(industry_groups)
        if company_terms:
            parts.append(
                quote(company_terms[0]) if len(company_terms) == 1
                else f"({' OR '.join(quote(c) for c in company_terms)})"
            )
        if can_terms:
            # Singletons are flattened — `(A)` and `A` are equivalent, and the
            # bare form reads better in the string the recruiter copies.
            parts.append(
                can_terms[0] if len(can_terms) == 1
                else f"({' OR '.join(can_terms)})"
            )

        is_jobdiva = str(dialect or "generic").strip().lower() == "jobdiva"

        # Roles. In JobDiva's dialect they ride in the dedicated TITLES= field,
        # which matches the candidate's job title rather than anywhere in the
        # résumé body — so a keyword-chain role group both over-matches (someone
        # who merely mentions the title) and competes with the skill ANDs. Every
        # other consumer (Unipile, Exa) only understands a plain expression, so
        # there the role group is AND'ed into the chain as before.
        titles_suffix = ""
        if role_terms:
            role_clause = " OR ".join(quote(t) for t in role_terms)
            if is_jobdiva:
                titles_suffix = f" , TITLES= ({role_clause})"
            else:
                parts.insert(
                    0,
                    quote(role_terms[0]) if len(role_terms) == 1 else f"({role_clause})",
                )

        # Geo. A remote role must NOT carry a city keyword: `AND "Dallas, TX"` is
        # a literal résumé-text match, so on a 100%-remote job it rejects every
        # candidate who doesn't happen to name that city — the opposite of the
        # intent. JobDiva has a structured country filter for this (`IN {US}`,
        # what recruiters hand-write); other dialects have no equivalent, so we
        # simply omit the constraint and let the caller's own location argument
        # and the client-side location scorer handle it.
        location_type = str(getattr(criteria, "location_type", "") or "").strip().lower()
        if location_type == "remote":
            if is_jobdiva:
                parts.append("IN {US}")
        elif criteria.location:
            add_unique(parts, seen_must, quote(criteria.location), criteria.location)

        boolean_string = " AND ".join(part for part in parts if part and part != "()") or "*"
        if exclude_terms:
            boolean_string = f"{boolean_string} NOT ({' OR '.join(exclude_terms)})"

        logger.info(f"Boolean string built from Page 5 sourcing filters only: {boolean_string[:150]}...")
        return boolean_string + titles_suffix

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
        job_country = self._target_country(criteria)
        for candidate in candidates:
            haystack = self._candidate_summary_text(candidate)

            # Country scope: drop only when the candidate's country/location
            # text is positive evidence of a location outside the job's
            # country. Silent records are treated as in-country (kept).
            if self._is_likely_outside_country(candidate, job_country):
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
        1. ``candidate.city + ", " + candidate.state`` (live source field —
           for JobDiva rows this is the CRM record; background detail
           hydration refreshes it)
        2. ``candidate.location`` (live source field)
        3. ``enhanced_info.current_location`` (LLM-extracted from resume) —
           ONLY consulted when the source fields are blank. The LLM value can
           latch onto a past-employer/education city, and because this list
           feeds ``_is_likely_outside_country`` / radius verdicts, a wrong
           entry here can veto a genuinely local candidate (or pass a remote
           one), so it must never ride alongside authoritative source data.
        """
        enhanced = candidate.get("enhanced_info") or {}
        enhanced_dict = enhanced if isinstance(enhanced, dict) else {}

        city = str(candidate.get("city") or "").strip()
        state = str(candidate.get("state") or "").strip()
        city_state = f"{city}, {state}".strip(", ") if (city or state) else ""

        # Work-arrangement strings ("Remote"/"Hybrid") are stripped before the
        # values feed the geo verdict — they can't geocode, and their presence
        # here used to mask the real residence signal.
        source_native = [
            sanitize_candidate_location(city_state),
            sanitize_candidate_location(candidate.get("location")),
        ]
        location_values = list(source_native)
        if not any(str(v or "").strip() for v in source_native):
            location_values.append(
                sanitize_candidate_location(enhanced_dict.get("current_location"))
            )
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

    # Display names for the countries the sourcing pipeline can scope to.
    _COUNTRY_DISPLAY_NAMES = {"US": "United States", "CA": "Canada"}

    def _target_country(self, criteria: SearchCriteria) -> str:
        """The job's country code ("US" default, "CA" for Canadian jobs).

        Resolved from explicit ``criteria.countries`` when the frontend sent
        them, else parsed from ``criteria.location`` ("Toronto, ON" ⇒ CA via
        the province code; "Los Angeles, CA" ⇒ US via the state-slot rule).
        Every non-JobDiva query scope and the outside-country candidate gate
        key off this, so a Canadian job stops searching (and keeping) US-only
        candidates and vice versa.
        """
        for c in (criteria.countries or []):
            code = self._COUNTRY_ALIASES.get(str(c).strip().upper())
            if code:
                return code
        parsed = self._parse_location(criteria.location or "")
        return (parsed.get("country") or "US").upper()

    def _country_display_name(self, country_code: str) -> str:
        return self._COUNTRY_DISPLAY_NAMES.get(
            (country_code or "US").upper(), "United States"
        )

    def _search_location_for_source(self, criteria: SearchCriteria) -> str:
        """Location string to hand external sources (Exa, Dice, Unipile).

        Remote jobs search country-wide — anchoring a remote search to the
        office city just biases discovery toward one metro for no reason.
        Onsite/hybrid keep the job location scoped to the job's country.
        """
        country = self._target_country(criteria)
        if str(getattr(criteria, "location_type", "") or "").strip().lower() == "remote":
            return self._country_display_name(country)
        return self._scope_location_to_country(criteria.location, country)

    def _scope_location_to_country(self, location: Any, country_code: str) -> str:
        """Append the job's country name to a location string when no country
        is already named, so downstream services (Exa, Dice, Vetted, Unipile)
        scope their lookups correctly. A Canadian job used to get ", United
        States" appended here, which made Exa/Unipile search the wrong
        country entirely.

        Returns the bare country name when the input is empty.
        """
        country_name = self._country_display_name(country_code)
        text = str(location or "").strip().strip(",")
        if not text:
            return country_name

        upper_tokens = {t.strip().upper() for t in text.split(",")}
        if upper_tokens & set(self._COUNTRY_ALIASES.keys()):
            return text
        return f"{text}, {country_name}"

    def _scope_location_to_us(self, location: Any) -> str:
        """Back-compat wrapper — US-scoped variant of
        :py:meth:`_scope_location_to_country`."""
        return self._scope_location_to_country(location, "US")

    # Country-field spellings that positively identify Canada.
    _CA_COUNTRY_TOKENS = frozenset({"ca", "can", "canada"})

    def _candidate_country_code(self, candidate: Dict[str, Any]) -> str:
        """Best-effort country code ("US"/"CA"/…) from the candidate record.

        Returns "" when the field is absent or unrecognizable — an
        unparseable value (JobDiva can return ids or free text) is treated
        as "unknown", never as evidence of being abroad.
        """
        enhanced = candidate.get("enhanced_info")
        enhanced_country = (
            enhanced.get("current_country") if isinstance(enhanced, dict) else None
        )
        for raw in (candidate.get("country"), enhanced_country):
            text = str(raw or "").strip().upper()
            if not text:
                continue
            code = self._COUNTRY_ALIASES.get(text)
            if code:
                return code
        return ""

    def _is_likely_outside_country(
        self, candidate: Dict[str, Any], country_code: str = "US"
    ) -> bool:
        """Return True only when the candidate is clearly outside the job's
        country (default US; "CA" for Canadian jobs).

        Defaults to False (treat as in-country) when the candidate's country
        and location text are silent — we want to keep observed candidates
        unless we have positive evidence they're abroad. A Canadian job must
        NOT drop "Toronto, Ontario, Canada" candidates (the old US-only gate
        did exactly that, leaving near-empty external results for CA jobs).
        """
        country_code = (country_code or "US").upper()

        known_code = self._candidate_country_code(candidate)
        if known_code and known_code != country_code:
            return True

        if country_code == "CA":
            foreign_tokens = (self._NON_US_LOCATION_TOKENS - {"canada"}) | {
                "united states", "usa", "u.s.", "u.s.a.",
            }
        else:
            foreign_tokens = self._NON_US_LOCATION_TOKENS

        locs = self._candidate_structured_locations(candidate)
        if locs:
            padded = " " + " | ".join(loc.lower() for loc in locs) + " "
            for token in foreign_tokens:
                # Match as bounded substring to avoid false positives
                # (e.g. "india" must not hit "indianapolis").
                if f" {token} " in padded or f" {token}," in padded:
                    return True
        return False

    def _is_likely_non_us(self, candidate: Dict[str, Any]) -> bool:
        """Back-compat wrapper: outside-US check (US jobs)."""
        return self._is_likely_outside_country(candidate, "US")

    def _location_match_verdict(
        self,
        candidate: Dict[str, Any],
        criteria: SearchCriteria,
    ) -> Tuple[bool, str, Optional[float]]:
        if not criteria.location:
            return True, "no_location_requirement", None

        # Remote jobs have no commute constraint — any US location passes.
        # (Non-US candidates are still dropped by the _is_likely_non_us gate.)
        if str(getattr(criteria, "location_type", "") or "").strip().lower() == "remote":
            return True, "remote_job_no_location_constraint", None

        from services import zip_index

        required = self._parse_location(criteria.location)

        # Bare-zip input ("85281") or noisy city text: backfill city/state
        # from the offline zip index so every check below still works.
        if required.get("zip"):
            zip_entry = zip_index.lookup_zip(required["zip"])
            if zip_entry:
                if not required["city"]:
                    required["city"] = self._normalize_term(zip_entry["city"])
                if not required["state"]:
                    required["state"] = self._normalize_term(zip_entry["state"])

        if not required["city"] and not required["state"] and not required.get("zip"):
            return True, "empty_location_requirement", None

        # B1: opt-out for "open to relocation" candidates whose actual location
        # is unknown or outside the radius. Default keeps them (soft-keep).
        relocation_flag = bool(candidate.get("open_to_relocation"))
        include_relocation = bool(getattr(criteria, "include_relocation_candidates", True))

        candidate_locs = self._candidate_structured_locations(candidate)

        # Direct zip from the source payload (JobDiva JobAgent) — usable even
        # when the candidate's location strings are blank or noisy, and more
        # precise than city-level matching when present.
        direct_zip = ""
        direct_zip_entry = zip_index.lookup_zip(str(candidate.get("zipcode") or "").strip())
        if direct_zip_entry:
            direct_zip = direct_zip_entry["zip"]

        if not candidate_locs and not direct_zip:
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
            if direct_zip_entry:
                zip_state = self._normalize_term(direct_zip_entry["state"])
                seen_states.append(zip_state)
                if zip_state == required["state"]:
                    return True, "state_match", None
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
        geocode_failure = False
        closest_distance: Optional[float] = None

        # OFFLINE FAST-PATH: exact zip / city+state match, then real centroid
        # distances from the offline zip index. JobAgent locations are noisy
        # ("PLANO, TX 75024" vs "Plano, TX" vs "Plano TX") and Nominatim is
        # rate-limited/best-effort, so anything resolvable against the local
        # index gets a deterministic distance with no HTTP call. Nominatim
        # remains only for strings the index can't place (misspellings,
        # neighborhoods, non-standard formats).
        try:
            from services.us_state_index import state_centroid_distance_miles
        except Exception:
            state_centroid_distance_miles = None  # type: ignore[assignment]

        # Resolve the search target to coordinates offline: zip centroid
        # first (most precise), else the city's averaged centroid.
        required_point: Optional[Tuple[float, float]] = None
        if required.get("zip"):
            required_point = zip_index.zip_centroid(required["zip"])
        if required_point is None and required.get("city") and required.get("state"):
            required_point = zip_index.city_state_centroid(required["city"], required["state"])

        offline_state_mismatch_distance: Optional[float] = None
        best_offline_distance: Optional[float] = None
        unresolved_locs: List[str] = []

        parse_targets = list(candidate_locs)
        if direct_zip:
            parse_targets.append(direct_zip)

        for candidate_loc in parse_targets:
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

            # Offline centroid distance — candidate zip beats city+state
            # for precision. Deterministic, offline, and immune to geocoder
            # flakiness whenever both sides resolve against the index.
            cand_point = zip_index.zip_centroid(cand_zip) if cand_zip else None
            if cand_point is None and cand_city and cand_state:
                cand_point = zip_index.city_state_centroid(cand_city, cand_state)
            if cand_point is not None and required_point is not None:
                d = haversine_miles(cand_point[0], cand_point[1], required_point[0], required_point[1])
                if best_offline_distance is None or d < best_offline_distance:
                    best_offline_distance = float(d)
                continue

            # This signal couldn't be placed offline ("Greater Phoenix
            # Area", neighborhoods, misspellings) — it still deserves a
            # Nominatim attempt below. A stale-but-resolvable signal must
            # not confirm the candidate outside while a fresher unresolved
            # one would have geocoded in-radius.
            unresolved_locs.append(candidate_loc)

            # State-centroid distance as a cheap upper-bound estimate when
            # the exact locality can't be resolved — at least the UI can
            # render a real number under BEYOND instead of the sentinel.
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

        if best_offline_distance is not None:
            offline_d = round(best_offline_distance, 1)
            if offline_d <= miles:
                return True, "within_radius", offline_d
            # Outside per the offline signals. Only confirmed if EVERY
            # location signal was offline-resolvable; otherwise fall through
            # and let Nominatim try the unresolved strings, seeding the
            # closest-distance with the offline estimate.
            closest_distance = offline_d
            if not unresolved_locs:
                if relocation_flag and not include_relocation:
                    return False, "relocation_excluded_by_filter", offline_d
                return True, "outside_radius_soft_keep", offline_d

        # Network path: Nominatim for the strings the offline index couldn't
        # place. Geocode the cleaned "City, ST" reconstruction as target —
        # raw strings with zips/suffixes ("Plano, TX 75024") routinely miss.
        if required.get("city") and required.get("state"):
            target = f"{required['city']}, {required['state']}"
        else:
            target = normalize_location_string(criteria.location)
        for candidate_loc in unresolved_locs:
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
            # Canadian provinces/territories — full names map to their
            # 2-letter codes (disjoint from every US state code), so
            # "Toronto, Ontario" and "Toronto, ON" compare equal.
            "alberta": "ab", "british columbia": "bc", "manitoba": "mb",
            "new brunswick": "nb", "newfoundland and labrador": "nl",
            "newfoundland": "nl", "nova scotia": "ns",
            "northwest territories": "nt", "nunavut": "nu", "ontario": "on",
            "prince edward island": "pe", "quebec": "qc", "québec": "qc",
            "saskatchewan": "sk", "yukon": "yt",
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
        # Take the LAST match: in address-style strings ("10001 W Main St,
        # Mesa, AZ 85201") the leading 5-digit number is a street number,
        # and the zip — when present — trails.
        zip_matches = list(re.finditer(r"\b(\d{5})(?:-\d{4})?\b", text))
        zip_match = zip_matches[-1] if zip_matches else None
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

        normalized_state = state_aliases.get(self._normalize_term(state), self._normalize_term(state))

        # ---- country detection -------------------------------------------
        # Rule: a token in the STATE SLOT (right after the city) is always a
        # state/province, never a country — "Los Angeles, CA" is California,
        # not Canada. Countries are read from the tokens AFTER the state
        # ("Ajax, ON CA", "Toronto, Ontario, Canada"), or inferred from the
        # state code itself (ON ⇒ Canada, TX ⇒ US). A state-slot token that
        # is ONLY a country name ("Toronto, Canada") moves to country.
        country = ""
        after_state_tokens: List[str] = []
        if len(parts) > 1:
            after_state_tokens.extend(parts[1].split()[1:])
        for extra_part in parts[2:]:
            after_state_tokens.extend(extra_part.split())
        for tok in reversed([t.strip() for t in after_state_tokens if t.strip()]):
            tok_upper = tok.upper()
            if tok_upper in self._COUNTRY_ALIASES:
                country = self._COUNTRY_ALIASES[tok_upper]
                break
        # Multi-word country names split across tokens ("United States").
        if not country and len(parts) > 1:
            tail_text = " ".join(after_state_tokens).upper().strip(" .")
            if tail_text in self._COUNTRY_ALIASES:
                country = self._COUNTRY_ALIASES[tail_text]
        state_upper = normalized_state.upper()
        if not country and state_upper:
            if state_upper in self._CA_PROVINCE_CODES:
                country = "CA"
            elif state_upper in self._US_STATE_CODES:
                country = "US"
            elif state_upper in self._COUNTRY_ALIASES:
                # "Toronto, Canada" — the state slot held a country name.
                country = self._COUNTRY_ALIASES[state_upper]
                normalized_state = ""

        # Cross-validate the zip against the state: a street number in an
        # address-style string ("10001 W Main St, Mesa, AZ") can collide
        # with a real zip on the other side of the country. When any part
        # of the string resolves to a US state and the zip's own state
        # disagrees, the 5-digit number wasn't a zip — drop it. Unknown
        # zips (not in the index) are kept for the exact-equality match.
        if zip_code:
            from services import zip_index
            zip_entry = zip_index.lookup_zip(zip_code)
            if zip_entry:
                state_hint = resolve_state_code(normalized_state) if normalized_state else None
                if not state_hint:
                    for part in reversed(parts[1:]):
                        first_token = part.split()[0] if part.split() else ""
                        state_hint = resolve_state_code(first_token)
                        if state_hint:
                            break
                if state_hint and zip_entry["state"].upper() != state_hint.upper():
                    zip_code = ""
            # Foreign postal codes collide with US zips ("Paris, 75001,
            # France" — 75001 is Addison, TX). A named non-US country in
            # the string disqualifies the number as a US anchor.
            if zip_code and any(
                p.strip().lower() in self._NON_US_LOCATION_TOKENS for p in parts
            ):
                zip_code = ""

        return {
            "city": self._normalize_term(city),
            "state": normalized_state,
            "zip": zip_code,
            "country": country,
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
            # Exa deep-analysis full profile text — richer than the 4k-char
            # highlights in resume_text; without it the fetched profile never
            # reaches skill matching.
            candidate.get("deep_text", ""),
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

    def _skills_evidenced_in_text(self, text: str, criteria: SearchCriteria) -> List[str]:
        """Rubric skill terms that literally appear in `text`, whole-token.

        A no-LLM stand-in for résumé skill extraction, used on the JobAgent
        high-level scoring path where the per-candidate LLM step is skipped.

        Safer than the fallback it replaces: it can only ever return terms the
        rubric already asked about, so unlike `_extract_candidate_skills`' title
        inference it cannot invent skills (that path ends at a literal
        ["Communication", "Problem Solving"] placeholder, which the Skills
        dimension — 45% of the score — would otherwise be judged against).
        Whole-token matching keeps "R" from matching "senior".
        """
        haystack = str(text or "")
        if not haystack:
            return []
        from services.jobdiva_boolean_translator import term_appears_as_token

        found: List[str] = []
        seen = set()
        for term in criteria.skill_only_values():
            value = str(term or "").strip()
            key = value.lower()
            if not value or key in seen:
                continue
            if term_appears_as_token(value, haystack):
                seen.add(key)
                found.append(value)
        return found

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
        # Companies the candidate is *currently* at — used to scope the
        # "must not company" exclusion to present employment only. A role is
        # current when end_date is empty / null / "Present" / "Current".
        # String-only entries (sparse fallbacks) are treated as not-current so
        # we err toward not over-blocking on missing tenure data.
        current_company_terms: List[str] = []
        def _is_current_role(item: Dict[str, Any]) -> bool:
            end = item.get("end_date") if isinstance(item, dict) else None
            if end is None:
                return True
            end_str = str(end).strip().lower()
            return end_str in ("", "present", "current", "now")
        for source in company_sources:
            if not isinstance(source, list):
                continue
            for item in source:
                if isinstance(item, dict):
                    is_current = _is_current_role(item)
                    for key in ["company", "company_name", "employer", "name"]:
                        if item.get(key):
                            name = str(item.get(key))
                            company_terms.append(name)
                            if is_current:
                                current_company_terms.append(name)
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

        # Source-native location first; LLM-extracted current_location is a
        # last-resort fallback (it can name a past-employer/education city).
        # Arrangement strings are stripped — "Remote" must not be scored as a
        # place.
        location_terms = unique_terms([
            sanitize_candidate_location(
                f"{candidate.get('city', '')}, {candidate.get('state', '')}".strip(", ")
            ),
            sanitize_candidate_location(candidate.get("location", "")),
            sanitize_candidate_location(enhanced.get("current_location", "")),
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
            "current_companies": unique_terms(current_company_terms),
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

        # Critical: some collections must NOT fall back to generic resume text.
        #  - locations: stale historical locations can't satisfy a current-location check.
        #  - current_companies: the "currently employed by client" veto must match
        #    the present employer only; the resume text lists past employers too.
        if len(collections) == 1 and collections[0] in ("locations", "current_companies"):
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
        # Structured-only collections (location, current employer) never use the
        # resume-text fallback — see _contains_term for the rationale.
        is_location_only = len(collections) == 1 and collections[0] in ("locations", "current_companies")
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
                # Recency decay: the term matches the candidate, but not within
                # their most-recent roles (recent_text). The "Recent (3y) Title
                # Relevance" dimension cares specifically about recent usage, so
                # this is a firmer cut than the legacy flat penalty.
                score *= SCORING_RECENCY_DECAY

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

        # Country scope is enforced at every stage (see `_filter_candidates`
        # and `_filter_by_state`). Soft-fail: only drop on positive evidence
        # of a location outside the job's country. (The reason key stays
        # "non_us_candidate" for wire-compat with the UI drop vocabulary.)
        job_country = self._target_country(criteria)
        if self._is_likely_outside_country(candidate, job_country):
            return {
                "passes": False,
                "missing": [f"Location: outside {self._country_display_name(job_country)}"],
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
            excluded_collections = dimension.get("excluded_collections", collections)
            # Check exclusions - these are ALWAYS hard filters
            for group in dimension.get("excluded_groups", []):
                if self._term_group_matches(profile, group, excluded_collections):
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

    # ---- Synthetic scoring dimensions (2026-06 rubric rework) ----
    # Each returns a float in [0, 1], or None when the candidate has no data
    # for that dimension. A None result makes _score_candidate DROP the
    # dimension from the weighted denominator, so its weight is redistributed
    # proportionally across the dimensions that do have data.

    @staticmethod
    def _year_from_date(value: Any) -> Optional[int]:
        """Best-effort 4-digit year out of a free-text date like 'Jan 2020'."""
        if value is None:
            return None
        text = str(value).strip().lower()
        if text in ("", "present", "current", "now", "ongoing"):
            return None
        m = re.search(r"(19|20)\d{2}", text)
        return int(m.group(0)) if m else None

    def _role_duration_years(self, item: Dict[str, Any]) -> Optional[float]:
        """Tenure of a single company-experience entry, in years. Returns None
        when neither bound is parseable. Open-ended ('Present') roles use the
        current year as the end bound."""
        if not isinstance(item, dict):
            return None
        start = self._year_from_date(item.get("start_date") or item.get("start") or item.get("from"))
        if start is None:
            return None
        end = self._year_from_date(item.get("end_date") or item.get("end") or item.get("to"))
        if end is None:
            try:
                from datetime import datetime, timezone
                end = datetime.now(timezone.utc).year
            except Exception:
                return None
        return max(0.0, float(end - start))

    def _score_yoe(self, profile: Dict[str, Any], criteria: SearchCriteria) -> Optional[float]:
        """Total relevant years of experience vs the recruiter's minimum.
        None when no target is set or years are unknown (→ redistribute)."""
        target = int(getattr(criteria, "min_experience_years", 0) or 0)
        if target <= 0:
            return None
        years = float(profile.get("years_of_experience") or 0)
        if years <= 0:
            return None
        return max(0.0, min(1.0, years / float(target)))

    def _score_same_client(self, profile: Dict[str, Any], criteria: SearchCriteria) -> Optional[float]:
        """Prior experience at the hiring client (or recruiter-named target
        companies). None when we have no client/company reference."""
        refs: List[str] = []
        cn = str(getattr(criteria, "client_name", "") or "").strip()
        if cn:
            refs.append(cn)
        for c in (getattr(criteria, "companies", []) or []):
            if str(c).strip():
                refs.append(str(c).strip())
        if not refs:
            return None
        # No parsed employment history → can't assess; redistribute rather than
        # penalise a parsing gap (consistent with the rest of the scorer).
        if not profile.get("companies"):
            return None
        best = 0.0
        for ref in refs:
            best = max(best, self._fuzzy_term_score(profile, ref, "companies"))
        return best

    def _score_career_stability(self, candidate: Dict[str, Any]) -> Optional[float]:
        """Average tenure across dated roles. Job-hoppers score low; stable
        tenures score high. None when fewer than 2 dated roles (→ redistribute)."""
        enhanced = candidate.get("enhanced_info") or {}
        sources = [
            enhanced.get("company_experience") or [],
            candidate.get("company_experience") or [],
            candidate.get("experience") or [],
        ]
        durations: List[float] = []
        for source in sources:
            if not isinstance(source, list):
                continue
            for item in source:
                d = self._role_duration_years(item)
                if d is not None:
                    durations.append(d)
            if durations:
                break
        if len(durations) < 2:
            return None
        avg = sum(durations) / len(durations)
        if avg >= 2.5:
            return 1.0
        if avg >= 1.5:
            return 0.75
        if avg >= 1.0:
            return 0.5
        return 0.3

    def _score_profile(self, candidate: Dict[str, Any]) -> Optional[float]:
        """Profile completeness / external signal. LinkedIn present → full
        credit; any other URL → partial. None when no URL data at all."""
        urls = candidate.get("urls") or (candidate.get("enhanced_info") or {}).get("urls") or {}
        if not isinstance(urls, dict) or not urls:
            return None
        present = [k for k, v in urls.items() if str(v or "").strip()]
        if not present:
            return None
        if str(urls.get("linkedin") or "").strip():
            return 1.0
        return 0.6

    def _score_availability(self, candidate: Dict[str, Any], criteria: SearchCriteria) -> Optional[float]:
        """Candidate availability / record freshness. None when no signal
        is present (→ redistribute)."""
        enhanced = candidate.get("enhanced_info") or {}
        avail = candidate.get("availability") or enhanced.get("availability")
        if isinstance(avail, str) and avail.strip():
            a = avail.strip().lower()
            if any(k in a for k in ("immediate", "now", "available", "ready", "2 week", "two week")):
                return 1.0
            if any(k in a for k in ("unavailable", "not available", "month", "4 week", "8 week")):
                return 0.4
            return 0.6
        for key in ("last_active", "last_activity_date", "last_updated", "updated_at", "date_modified"):
            days = self._days_since(candidate.get(key) or enhanced.get(key))
            if days is not None:
                if days <= 30:
                    return 1.0
                if days <= 90:
                    return 0.7
                if days <= 180:
                    return 0.5
                return 0.3
        if candidate.get("open_to_work") or candidate.get("open_to_relocation"):
            return 0.8
        return None

    @staticmethod
    def _days_since(value: Any) -> Optional[float]:
        if value is None or str(value).strip() == "":
            return None
        try:
            from datetime import datetime, timezone
            text = str(value).strip().replace("Z", "+00:00")
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0)
        except Exception:
            return None

    def _location_hard_gate(self, candidate: Dict[str, Any], criteria: SearchCriteria) -> Optional[str]:
        """Evidence-based location gate. Returns a veto reason string ONLY when
        the candidate's location is KNOWN and confirmed outside the radius;
        returns None (no veto) when location is unknown/unparseable — those
        candidates are kept (soft) per the existing location policy."""
        if not self._should_enforce_location(criteria):
            return None
        ok, reason, distance = self._location_match_verdict(candidate, criteria)
        if distance is not None:
            candidate["distance_miles"] = (
                None if distance == _UNKNOWN_DISTANCE_SENTINEL else round(float(distance), 1)
            )
        # Confirmed-outside reasons. "outside_radius_soft_keep" only counts as
        # confirmed when we have a real (non-sentinel) distance beyond the radius.
        miles = min(100, int(getattr(criteria, "within_miles", 25) or 25))
        # Stamp the badge fields for every scored candidate — previously only
        # the JobDiva LocationGate set them, so Exa/LinkedIn rows never showed
        # the out-of-radius badge even with a confirmed distance.
        if (
            isinstance(distance, (int, float))
            and distance != _UNKNOWN_DISTANCE_SENTINEL
            and distance > miles
        ):
            candidate["location_out_of_radius"] = True
            candidate.setdefault("location_match_reason", reason)
        # JobDiva-JobAgent exemption: these rows follow the criteria the
        # recruiter authored inside JobDiva (which may deliberately reach
        # beyond the job's radius), so a confirmed mismatch never zeroes
        # their score — the badge fields stamped above still render the
        # distance and the recruiter filters via the UI chips. Every other
        # source falls through to the hard veto below.
        from core import sourcing_config as _sc_gate
        if (
            str(candidate.get("source") or "") == "JobDiva-JobAgent"
            and not getattr(_sc_gate, "JOBAGENT_LOCATION_HARD_VETO", False)
        ):
            return None
        if reason in ("state_mismatch", "relocation_excluded_by_filter"):
            # Machine-readable veto marker for the emit-time drop gates and
            # telemetry (the human string below feeds explainability only).
            candidate["location_veto_reason"] = reason
            return f"location outside {criteria.location}"
        if (
            reason == "outside_radius_soft_keep"
            and isinstance(distance, (int, float))
            and distance != _UNKNOWN_DISTANCE_SENTINEL
            and distance > miles
        ):
            candidate["location_veto_reason"] = "outside_radius_confirmed"
            return f"location {round(float(distance))}mi outside {criteria.location} ({miles}mi radius)"
        return None

    def _score_candidate(self, candidate: Dict[str, Any], criteria: SearchCriteria) -> Dict[str, Any]:
        profile = self._candidate_profile(candidate)
        dimensions = self._collect_scoring_dimensions(criteria)  # Use scoring dimensions for evaluation
        weights = scoring_weights_for_family(self._current_family)

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

            excluded_collections = dimension.get("excluded_collections", dimension["collections"])
            required_matches = self._matched_term_groups(profile, required_groups, dimension["collections"])
            preferred_matches = self._matched_term_groups(profile, preferred_groups, dimension["collections"])
            excluded_matches = self._matched_term_groups(profile, excluded_groups, excluded_collections)

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
                            profile, group, excluded_collections
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
                    item if dim_label == "Skills Match" else f"{dim_label}: {item}"
                )

        # ---- Synthetic dimensions (availability, yoe, same_client,
        # career_stability, profile). Each is appended ONLY when its scorer
        # returns a value; a None result drops the dimension from weighted_max
        # so its weight is redistributed across the dimensions that have data. ----
        synthetic = [
            ("Availability", weights.get("availability", 0.0), self._score_availability(candidate, criteria)),
            ("Total Relevant YOE", weights.get("yoe", 0.0), self._score_yoe(profile, criteria)),
            ("Same Client / Industry", weights.get("same_client", 0.0), self._score_same_client(profile, criteria)),
            ("Career Stability & Progression", weights.get("career_stability", 0.0), self._score_career_stability(candidate)),
            ("Profile Completeness", weights.get("profile", 0.0), self._score_profile(candidate)),
        ]
        for label, w, raw in synthetic:
            if raw is None or w <= 0:
                continue
            s = max(0.0, min(1.0, float(raw)))
            dim_score = w * s
            weighted_scores.append(dim_score)
            weighted_max += w
            score_details[label] = {
                "weight": w,
                "score": round(dim_score, 2),
                "value": round(s, 3),
            }
            if s >= 0.5:
                matched_required_skills.append(f"{label}: {round(s * 100)}%")

        # ---- Location hard gate (evidence-based). Only vetoes when the
        # candidate's location is known AND confirmed outside the radius. ----
        location_veto = self._location_hard_gate(candidate, criteria)

        score = 0
        if weighted_max > 0:
            score = round(max(0.0, min(100.0, (sum(weighted_scores) / weighted_max) * 100)))

        score_details["hard_veto"] = {
            "triggered": bool(hard_veto_hits) or bool(location_veto),
            "reasons": (hard_veto_hits + ([location_veto] if location_veto else []))[:3],
        }

        if hard_veto_hits or location_veto:
            score = 0
            reason = hard_veto_hits[0] if hard_veto_hits else location_veto
            explainability.insert(
                0,
                f"Hard exclusion: matches recruiter exclusion rule ({reason})"
                if hard_veto_hits
                else f"Hard exclusion: {reason}",
            )
        elif score >= 85:
            explainability.insert(0, "Excellent rubric and sourcing alignment")
        elif score >= 70:
            explainability.insert(0, "Strong overall fit across active filters")
        elif score >= 50:
            explainability.insert(0, "Partial fit; review missing rubric requirements")
        else:
            explainability.insert(0, "Limited fit against active rubric and sourcing filters")

        # Out-of-radius JobDiva-JobAgent rows are kept and scored (see the
        # _location_hard_gate exemption) — say so in the score popup, so the
        # distance badge and a non-zero score don't read as a contradiction.
        if (
            not hard_veto_hits
            and not location_veto
            and candidate.get("location_out_of_radius")
            and str(candidate.get("source") or "") == "JobDiva-JobAgent"
        ):
            _dist = candidate.get("distance_miles")
            _note = (
                f"~{round(float(_dist))} mi from the job location"
                if isinstance(_dist, (int, float))
                else "Outside the job's location radius"
            )
            explainability.insert(
                1,
                f"{_note} — kept: JobDiva agent results follow the "
                "recruiter's own criteria, so location isn't hard-enforced",
            )

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
                # Exclusions ("must not have X") are documented as ALWAYS hard
                # filters, so they must not depend on extraction quality. The
                # `skills` collection is only as good as whatever populated it
                # — for JobDiva rows that's `_extract_candidate_skills`, which
                # falls back to guessing from the job title and ultimately to a
                # literal ["Communication", "Problem Solving"] placeholder. On
                # the JobAgent high-level path the LLM never runs to replace
                # those, so an exclusion scoped to `skills` alone could never
                # fire. Matching exclusions against the résumé text too makes
                # them real without touching how the 45% is *scored*.
                "excluded_collections": ["skills", "text"],
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
                # Exclusion ("must not company X") is current-employer-only;
                # past tenure at X must not penalize.
                "excluded_collections": ["current_companies"],
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
            # Rubric-driven scored dimensions. "domain" and "education_certs"
            # are merges of the legacy keywords / education+certifications
            # dimensions. Location and standalone company-match dimensions are
            # gone: location is now a hard gate (see _score_candidate) and
            # company signal is split into the same_client synthetic dimension
            # plus the company_exclusion veto below.
            "title_recent": {
                "label": "Recent Title Relevance",
                "weight": weights["title_recent"],
                "collections": ["titles"],
            },
            "skills": {
                "label": "Skills Match",
                "weight": weights["skills"],
                "collections": ["skills"],
            },
            "domain": {
                "label": "Domain Experience",
                "weight": weights["domain"],
                "collections": ["skills", "titles", "companies", "education", "certifications", "locations"],
            },
            "education_certs": {
                "label": "Education & Certifications",
                "weight": weights["education_certs"],
                "collections": ["education", "certifications"],
            },
            # Non-scored (weight 0). Carries the "currently employed by client" /
            # "must not be employed by" exclusion into the soft-penalty + hard-veto
            # path. Scoped to CURRENT employers only (excluded_collections) so a
            # candidate who merely worked at the client in the past is not vetoed.
            "company_exclusion": {
                "label": "Currently Employed by Client",
                "weight": 0.0,
                "collections": ["companies"],
                "excluded_collections": ["current_companies"],
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
            # recent=True → title relevance is recency-weighted (rubric: "Recent (3y)").
            add_terms("title_recent", item.get("match_type", "must"), variants, label=value, years=int(item.get("years") or 0), recent=True)

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
                add_terms("company_exclusion", "exclude", [term], weight=fw)
            elif "title" in category:
                add_terms("title_recent", "can" if "preferred" in category or raw_value.lower().startswith("can ") else "must", [term], weight=fw, recent=True)
            elif "skill" in category:
                add_terms("skills", "can" if "preferred" in category or raw_value.lower().startswith("can ") else "must", [term], weight=fw)
            elif "edu" in category:
                add_terms("education_certs", "can" if "preferred" in category or raw_value.lower().startswith("can ") else "must", [term], weight=fw)
            elif "cert" in category or "license" in category:
                add_terms("education_certs", "can" if "preferred" in category or raw_value.lower().startswith("can ") else "must", [term], weight=fw)
            elif "domain" in category:
                add_terms("domain", "can", [term], weight=fw)
            elif "local" in term.lower() or "location" in category:
                # Location is a hard gate now (see _score_candidate), not a
                # scored dimension — the structured criteria.location /
                # within_miles already carry the requirement.
                pass
            else:
                add_terms("domain", "must", [term], weight=fw)

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
                    # JobDiva's structured city/state is authoritative for
                    # residence; the LLM's resume-parsed current_location only
                    # fills a blank (it can latch onto a past employer or
                    # education city — e.g. a candidate living in Ajax, ON
                    # whose resume mentions Hyderabad). Both sides sanitized:
                    # a work-arrangement string is never a place.
                    candidate["location"] = (
                        sanitize_candidate_location(candidate.get("location"))
                        or sanitize_candidate_location(candidate["enhanced_info"].get("current_location"))
                    )
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
                    if self._candidate_below_min_years_pre_llm(candidate, criteria):
                        counters["pre_llm_skipped_min_years"] += 1
                        self._log_stage(
                            "LLMGate",
                            "skipping LLM for candidate_id=%s reason=below_min_years_pre_llm threshold=%s (kept, scored)" % (
                                cid,
                                int(criteria.min_experience_years or 0),
                            ),
                        )
                        await _keep("kept_min_years")
                        return

                    if criteria.bypass_screening:
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
                    # Source-native location wins; LLM extraction fills blanks
                    # only (resume text can name past-employer/education
                    # cities). Both sides sanitized: a work-arrangement string
                    # is never a place.
                    candidate["location"] = (
                        sanitize_candidate_location(candidate.get("location"))
                        or sanitize_candidate_location(candidate["enhanced_info"].get("current_location"))
                    )
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
                # LinkedIn profile location is authoritative; LLM extraction
                # from the synthesized profile text only fills a blank. Both
                # sides sanitized: LinkedIn areas can literally read "Remote".
                candidate["location"] = (
                    sanitize_candidate_location(candidate.get("location"))
                    or sanitize_candidate_location(candidate["enhanced_info"].get("current_location"))
                )
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

    async def _search_linkedin(self, criteria: SearchCriteria) -> Dict[str, Any]:
        try:
            # Unipile expects skills as a list of dicts. Carry each term's real
            # rubric priority (Must Have vs Preferred) so the Unipile layer can
            # AND only the genuine requirements and OR the rest — stamping every
            # term "Must Have" here was ANDing all of them on LinkedIn Recruiter
            # and collapsing the result set to ~1 profile.
            skills = criteria.sourcing_skills_with_priority()
            candidates = await self.unipile_service.search_candidates(
                skills=skills,
                location=self._search_location_for_source(criteria),
                open_to_work=criteria.open_to_work,
                limit=criteria.page_size,
                # The wizard sends ONE boolean, rendered in JobDiva's dialect
                # whenever JobDiva is a selected source. LinkedIn Recruiter
                # parses none of that, so `TITLES= (...)`, `IN {US}` and
                # `OVER N YRS` are stripped rather than matched as literal
                # keywords. Nothing is lost: titles ride in `skills` above and
                # location in its own argument.
                #
                # Deliberately NOT country-scoped: LinkedIn Recruiter keywords
                # are free-text matched against profile BODIES, so a literal
                # AND "United States" collapses results to near-zero — the
                # exact failure mode documented for the removed "Open to
                # Work" literal (see unipile.py). Geo scoping is handled by
                # the structured location URN + the post-search country/
                # radius gates.
                boolean_string=strip_jobdiva_dialect(
                    criteria.boolean_string
                    or self._build_boolean_string(criteria, dialect="generic")
                ),
            )

            # Open-to-Work enrichment via Apify (same path as Exa). Unipile
            # candidates carry a `profile_url`, so the resolver treats them
            # identically: cache-first fill of `open_to_work`, background
            # fetch for the rest which the frontend resolves by polling
            # /candidates/open-to-work-statuses. This is the real per-candidate
            # signal that replaced the old literal-keyword hack in unipile.py.
            if criteria.open_to_work and candidates:
                try:
                    from services.apify_open_to_work import (
                        annotate as _otw_annotate,
                        enqueue as _otw_enqueue,
                    )
                    pending_urls = await _otw_annotate(candidates)
                    logger.info(
                        "Unipile OTW: %d candidates, %d need Apify lookup",
                        len(candidates),
                        len(pending_urls),
                    )
                    await _otw_enqueue(pending_urls)
                except Exception as otw_exc:
                    logger.warning(f"Unipile OTW enrichment skipped: {otw_exc}", exc_info=True)

            candidates = self._drop_client_employees(
                candidates, criteria, "LinkedIn-Unipile"
            )
            return {"candidates": candidates, "source_type": "LinkedIn-Unipile"}
        except Exception as e:
            logger.error(f"LinkedIn search failed: {e}")
            return {"candidates": [], "source_type": "LinkedIn-Unipile"}
    
    def _extract_linkedin_profile_data(self, profile: Dict[str, Any]) -> Dict[str, Any]:
        """Extract detailed data from full LinkedIn profile for enrichment."""
        extracted = {}

        # Profile-level location: more precise than the search row's coarse
        # area string, and required for the radius/country gates to place
        # Unipile candidates at all. Tolerant to Unipile schema variants.
        profile_location = profile.get("location") or profile.get("location_name")
        if not profile_location and isinstance(profile.get("profile"), dict):
            profile_location = profile["profile"].get("location")
        if isinstance(profile_location, dict):
            profile_location = profile_location.get("name") or profile_location.get("default")
        if profile_location and str(profile_location).strip():
            extracted["location"] = str(profile_location).strip()

        # Extract experience (Unipile has been observed to use both
        # `experience` and `work_experience` for LinkedIn payloads)
        experience = (
            profile.get("experience")
            or profile.get("work_experience")
            or profile.get("work_history")
            or []
        )
        if experience:
            company_exp = []
            for exp in experience[:10]:  # Limit to last 10 positions
                company_exp.append({
                    "company": exp.get("company", exp.get("company_name", "")),
                    "title": exp.get("title", exp.get("job_title", exp.get("position", ""))),
                    "start_date": exp.get("start_date", exp.get("start", "")),
                    "end_date": exp.get("end_date", exp.get("end", "Present"))
                })
            extracted["company_experience"] = company_exp
            # Current job title from the most recent position — the search
            # row's `title` is the LinkedIn HEADLINE ("Dreamer | Builder"),
            # which is what the role-anchor gate used to run against.
            recent_title = str(company_exp[0].get("title") or "").strip()
            if recent_title:
                extracted["title"] = recent_title
        
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

        # Exa's `title` is a page title ("Jane Doe | LinkedIn"), not a job
        # title, so the phrase check leans on the highlight text. 500 chars
        # was dropping legitimate matches whose role line sat deeper in the
        # blob; the widened window only affects the exact-PHRASE check —
        # token-level matching still requires the title/headline fields —
        # and the score/location gates downstream now enforce precision.
        snippet = str(cand.get("resume_text") or "")[:2000].lower()
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

    async def _search_dice(self, criteria: SearchCriteria) -> Dict[str, Any]:
        try:
            # Structured criteria feed natural-language queries (one role per
            # search — Exa doesn't parse boolean syntax). Only the recruiter-
            # edited boolean is passed as a last-resort term source; the
            # auto-built one is derived from these same fields and adds
            # nothing. US scoping rides on `location`, never empty here.
            candidates = await self.exa_service.search_dice_candidates(
                skills=criteria.skill_only_values(),
                location=self._search_location_for_source(criteria),
                limit=min(criteria.page_size, 50),
                # Exa parses plain expressions only — strip JobDiva's
                # TITLES=/IN {US}/OVER N YRS before handing the shared wizard
                # boolean over (titles ride in `titles=` just below).
                boolean_string=strip_jobdiva_dialect(criteria.boolean_string or ""),
                titles=criteria.sourcing_titles(),
                min_experience_years=criteria.min_experience_years,
                companies=criteria.companies,
                keywords=criteria.keywords,
            )
            return {"candidates": candidates, "source_type": "Dice"}
        except Exception as e:
            logger.error(f"Dice search failed: {e}")
            return {"candidates": [], "source_type": "Dice"}

    async def _search_vetted(self, criteria: SearchCriteria) -> Dict[str, Any]:
        try:
            candidates = await self.vetted_service.search_candidates(
                skills=criteria.sourcing_skill_values(),
                location=self._search_location_for_source(criteria),
                limit=criteria.page_size
            )
            return {"candidates": candidates, "source_type": "VettedDB"}
        except Exception as e:
            logger.error(f"VettedDB search failed: {e}")
            return {"candidates": [], "source_type": "VettedDB"}

    async def _search_exa(self, criteria: SearchCriteria) -> Dict[str, Any]:
        try:
            # Floor at 30 so the Exa Research Pass B has a meaningful seed-URL
            # sample even when the recruiter's page_size is small, and cap at
            # 50 (Exa's per-call sweet spot for people search before relevance
            # falls off). Without the floor, page_size=10 → only 10 LinkedIn
            # candidates surface and the deep-search pass discovers little.
            requested = criteria.page_size or 30
            exa_limit = min(50, max(30, requested))
            # Structured criteria feed natural-language queries (one role per
            # search — Exa doesn't parse boolean syntax). Only the recruiter-
            # edited boolean is passed as a last-resort term source; the
            # auto-built one is derived from these same fields and adds
            # nothing. US scoping rides on `location`, never empty here.
            candidates = await self.exa_service.search_candidates(
                skills=criteria.skill_only_values(),
                location=self._search_location_for_source(criteria),
                limit=exa_limit,
                # Exa parses plain expressions only — strip JobDiva's
                # TITLES=/IN {US}/OVER N YRS before handing the shared wizard
                # boolean over (titles ride in `titles=` just below).
                boolean_string=strip_jobdiva_dialect(criteria.boolean_string or ""),
                titles=criteria.sourcing_titles(),
                min_experience_years=criteria.min_experience_years,
                companies=criteria.companies,
                keywords=criteria.keywords,
            )
            # Open-to-Work enrichment via Apify (mirrors Hoonrai/Revelio path).
            # Cache-first: fills `open_to_work` for any LinkedIn URL already
            # resolved in-process; fires background fetches for the rest, which
            # the frontend resolves by polling /candidates/open-to-work-statuses.
            if criteria.open_to_work and candidates:
                try:
                    from services.apify_open_to_work import (
                        annotate as _otw_annotate,
                        enqueue as _otw_enqueue,
                    )
                    pending_urls = await _otw_annotate(candidates)
                    logger.info(
                        "Exa OTW: %d candidates, %d need Apify lookup",
                        len(candidates),
                        len(pending_urls),
                    )
                    await _otw_enqueue(pending_urls)
                except Exception as otw_exc:
                    logger.warning(f"Exa OTW enrichment skipped: {otw_exc}", exc_info=True)
            candidates = self._drop_client_employees(candidates, criteria, "LinkedIn-Exa")
            return {"candidates": candidates, "source_type": "LinkedIn-Exa"}
        except Exception as e:
            logger.error(f"Exa search failed: {e}")
            return {"candidates": [], "source_type": "LinkedIn-Exa"}

    def _cand_is_jobdiva(self, candidate: Dict[str, Any]) -> bool:
        """True when a candidate carries JobDiva provenance — by `source`
        prefix, by an entry in its merged `sources` list, or by a JobDiva id.
        Used so cross-source dedup never makes a JobDiva row the dropped side.
        """
        if str(candidate.get("source") or "").startswith("JobDiva"):
            return True
        srcs = candidate.get("sources")
        if isinstance(srcs, list) and any(str(s or "").startswith("JobDiva") for s in srcs):
            return True
        return bool(
            str(candidate.get("jobdiva_candidate_id") or candidate.get("jobdiva_id") or "").strip()
        )

    def _merge_candidate_best_of(
        self, dst: Dict[str, Any], src: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Fold `src`'s best fields into `dst` (the surviving row) when the two
        are the same person from different sources/ids. `dst` keeps its own
        identity and match score; `src` only fills gaps or upgrades weak values
        (a real email over a synthetic one, a real resume over "unavailable").
        Returns a dict of the fields that changed, suitable for a UI patch.
        """
        changed: Dict[str, Any] = {}

        def _src_list(c: Dict[str, Any]) -> List[str]:
            out: List[str] = []
            s = c.get("sources")
            if isinstance(s, list):
                out.extend([str(x) for x in s if x])
            one = c.get("source")
            if one:
                out.append(str(one))
            return out

        merged_sources = list(dict.fromkeys(_src_list(dst) + _src_list(src)))
        cur_sources = dst.get("sources") if isinstance(dst.get("sources"), list) else None
        if merged_sources and merged_sources != cur_sources:
            dst["sources"] = merged_sources
            changed["sources"] = merged_sources

        # Fill missing scalars (incl. JobDiva identity so the survivor stays
        # Launch-PAIR-actionable when it absorbs a JobDiva duplicate).
        for f in (
            "phone", "location", "city", "state", "title", "headline",
            "resume_id", "profile_url", "linkedin_url", "image_url",
            "experience_years", "jobdiva_candidate_id", "jobdiva_id",
        ):
            if not dst.get(f) and src.get(f):
                dst[f] = src[f]
                changed[f] = src[f]

        # Email: prefer a real address over a missing/synthetic one.
        d_email = str(dst.get("email") or "").strip()
        s_email = str(src.get("email") or "").strip()
        if s_email and s_email != d_email and (
            not d_email or (is_placeholder_email(d_email) and not is_placeholder_email(s_email))
        ):
            dst["email"] = s_email
            changed["email"] = s_email

        # Resume text: prefer a real, longer resume over a missing/placeholder one.
        def _bad_resume(r: str) -> bool:
            r = r or ""
            return (not r.strip()) or ("Resume content unavailable" in r)

        d_resume = dst.get("resume_text") or ""
        s_resume = src.get("resume_text") or ""
        if s_resume and not _bad_resume(s_resume) and (_bad_resume(d_resume) or len(s_resume) > len(d_resume)):
            dst["resume_text"] = s_resume
            changed["resume_text"] = s_resume

        # Skills: fill only when the survivor has none (avoid noisy unions).
        d_skills = dst.get("skills") or []
        s_skills = src.get("skills") or []
        if isinstance(s_skills, list) and s_skills and not (isinstance(d_skills, list) and d_skills):
            dst["skills"] = s_skills
            changed["skills"] = s_skills

        return changed

    # Generic company-inbox local parts: a valid, deliverable address that is
    # nonetheless NOT personal identity — contact enrichment can return the
    # same info@/hr@ address for N different profiles at one employer, and an
    # email dedup key would silently collapse all N rows into one.
    _GENERIC_INBOX_LOCALPARTS = frozenset({
        "info", "hr", "careers", "jobs", "contact", "hello", "admin",
        "office", "sales", "support", "recruiting", "recruitment", "team",
        "hiring", "talent",
    })

    def _dedup_keys(self, candidate: Dict[str, Any]) -> List[str]:
        """Cross-source dedup keys for one candidate.

        Only *strong* identity signals are used so two different people never
        collide (a false merge is worse than a duplicate row):
          - `email:` — a real, well-formed, non-synthetic, non-generic-inbox
            address (info@/hr@/… are deliverable but shared, so they are not
            identity).
          - `phone-name:` — a phone (>=7 digits, >=4 distinct, not a shared/
            placeholder line) paired with a full name.
          - `linkedin:` — a normalised LinkedIn URL.
        Name+location alone is intentionally NOT a key — two distinct people
        sharing a common name and city would otherwise be merged.
        """
        keys: List[str] = []

        email = str(candidate.get("email") or "").strip().lower()
        if (
            email and "@" in email
            and not is_placeholder_email(email)
            and email.split("@", 1)[0] not in self._GENERIC_INBOX_LOCALPARTS
        ):
            keys.append(f"email:{email}")

        first = str(candidate.get("firstName") or "").strip().lower()
        last = str(candidate.get("lastName") or "").strip().lower()
        full_name = f"{first} {last}".strip()
        if not full_name:
            full_name = str(candidate.get("name") or "").strip().lower()

        phone_raw = str(candidate.get("phone") or "")
        phone_digits = "".join(filter(str.isdigit, phone_raw))
        # Phone is an identity signal only when paired with a name and not an
        # obviously shared/placeholder line (a single agency number must not
        # collapse a whole agency's candidates into one).
        if (
            len(phone_digits) >= 7
            and len(set(phone_digits)) >= 4
            and full_name
            and " " in full_name
        ):
            keys.append(f"phone-name:{phone_digits[-10:]}|{full_name}")

        profile_url = str(candidate.get("profile_url") or "").strip().lower()
        if profile_url and "linkedin.com" in profile_url:
            normalized = profile_url.split("?", 1)[0].rstrip("/")
            keys.append(f"linkedin:{normalized}")

        return keys

    def _deduplicate_candidates(self, candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        seen = {}
        unique_results = []
        
        for cand in candidates:
            email = cand.get("email", "").lower().strip()
            phone_raw = cand.get("phone", "")
            phone_digits = "".join(filter(str.isdigit, phone_raw))
            phone_key = phone_digits[-10:] if len(phone_digits) >= 7 else ""
            
            name = f"{cand.get('firstName', '')} {cand.get('lastName', '')}".lower().strip()
            city = cand.get("city", "").lower().strip()
            
            if email:
                key = f"email:{email}"
            elif phone_key:
                key = f"phone:{phone_key}"
            else:
                key = f"name_loc:{name}|{city}"
            
            if not key or key == "name_loc:|":
                unique_results.append(cand)
                continue
                
            if key not in seen:
                seen[key] = cand
                unique_results.append(cand)
            else:
                existing = seen[key]
                has_both_curr = bool(cand.get("email", "").strip()) and bool(cand.get("phone", "").strip())
                has_both_exist = bool(existing.get("email", "").strip()) and bool(existing.get("phone", "").strip())
                
                # Prioritize: 1. Both email+phone, 2. JobDiva-Applicants
                should_replace = False
                if has_both_curr and not has_both_exist:
                    should_replace = True
                elif not has_both_curr and has_both_exist:
                    should_replace = False
                elif cand.get("source") == "JobDiva-Applicants" and existing.get("source") != "JobDiva-Applicants":
                    should_replace = True

                if should_replace:
                    for i, r in enumerate(unique_results):
                        if r == existing:
                            unique_results[i] = cand
                            break
                    seen[key] = cand
                    
        try:
            from core.newrelic import record_custom_event
            record_custom_event("CandidateSearchSummary", {
                "total_unique_results": len(unique_results),
            })
        except Exception:
            pass

        return unique_results

unified_search_service = UnifiedCandidateSearch()
