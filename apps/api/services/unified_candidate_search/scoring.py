"""UnifiedCandidateSearch methods: scoring."""
import re
from typing import List, Dict, Any, Optional, Sequence, Tuple
from services.location import sanitize_candidate_location
from services.no_contact import apply_no_contact_flag
from services.company_match import apply_client_conflict_flag
from core.config import SCORING_REQUIRED_WEIGHT, SCORING_PREFERRED_WEIGHT, SCORING_EXCLUSION_CAP, SCORING_EXCLUSION_PER_HIT, SCORING_EXCLUSION_HARD_VETO_THRESHOLD, SCORING_UNMATCHED_REQUIRED_FLOOR, SCORING_UNMATCHED_PREFERRED_FLOOR, SCORING_PARSING_GAP_FLOOR, SCORING_COVERAGE_BLEND_THRESHOLD, SCORING_RECENCY_WINDOW_YEARS, SCORING_MATRIX_V2, SCORING_MATRIX_WEIGHTS, SCORING_MATRIX_REQUIRED_FLOOR, SCORING_MATRIX_RECENCY_DECAY, SCORING_MATRIX_YEARS_UNKNOWN_MULT, SCORING_MATRIX_YEARS_FLOOR, SCORING_MATRIX_TITLE_TIER_CREDIT, SOURCE_TIER_BONUS, OPEN_TO_WORK_SCORE_BONUS, JOBAGENT_RANK_SCORE_FLOOR, scoring_weights_for_family, embedding_skill_match_for_family, score_band
from services import skill_embeddings
from .common import (
    SearchCriteria,
    _MATRIX_BAND_LINES,
    _candidate_titles,
    _compute_title_boost,
    _off_search_family,
    logger,
)


class ScoringMixin:

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
        if candidate.get("open_to_work") or (
            criteria.include_relocation_candidates
            and candidate.get("open_to_relocation")
        ):
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

    # ------------------------------------------------------------------
    # Scoring Matrix v2 (recruiter rubric, 2026-09-11) — see core.config
    # SCORING_MATRIX_* for the weights / knobs and the rationale.
    # ------------------------------------------------------------------
    @staticmethod
    def _sample_row_hard_failed(cand: Dict[str, Any]) -> bool:
        """Rows that must never take a sample-preview slot: hard-filter fails
        (0% with hard_veto), no-contact companies and client-conflict rows.
        The full run still shows them greyed out / at 0%; the preview is
        about the *best* a source has to offer."""
        if cand.get("no_contact") is True or cand.get("client_conflict") is True:
            return True
        details = cand.get("match_score_details") or {}
        veto = details.get("hard_veto") if isinstance(details, dict) else None
        return bool(isinstance(veto, dict) and veto.get("triggered"))

    def _matrix_skill_meta(self, profile: Dict[str, Any], group: Any) -> Dict[str, Any]:
        """Per-skill {years, last_used} the résumé parse attached to whichever
        of the group's terms (rubric skill + its JobDiva-mapped similar terms)
        the candidate carries. Empty when the parse has no per-skill data
        (older cache entries, external profiles) — callers fall back to total
        YOE and the recent-text proxy."""
        meta_map = profile.get("skill_meta") or {}
        if not meta_map:
            return {}
        best: Dict[str, Any] = {}
        for term in self._group_terms(group):
            if not term:
                continue
            for key, meta in meta_map.items():
                if not (term == key or term in key or key in term):
                    continue
                for field in ("years", "last_used"):
                    val = meta.get(field)
                    if val is None:
                        continue
                    if best.get(field) is None or val > best[field]:
                        best[field] = val
        return best

    def _matrix_years_factor(
        self, profile: Dict[str, Any], group: Any, meta: Dict[str, Any]
    ) -> Tuple[float, str]:
        """Credit multiplier for a must-have skill's required years.
        Per-skill years from the parse win; otherwise total YOE stands in.
        Returns (multiplier, note) — note is non-empty when years fall short
        or are unproven, for the Gaps list."""
        required = self._group_min_years(group)
        if required <= 0:
            return 1.0, ""
        label = self._group_label(group)
        years = meta.get("years")
        basis = "skill"
        if years is None:
            total = float(profile.get("years_of_experience") or 0)
            years = total if total > 0 else None
            basis = "total"
        if years is None:
            return SCORING_MATRIX_YEARS_UNKNOWN_MULT, f"{label}: {required}+ yrs required, years not proven"
        if years >= required:
            return 1.0, ""
        shown = f"{years:g}{' total' if basis == 'total' else ''}"
        return (
            max(SCORING_MATRIX_YEARS_FLOOR, float(years) / float(required)),
            f"{label}: needs {required}+ yrs (shows {shown})",
        )

    def _matrix_recency_factor(
        self,
        profile: Dict[str, Any],
        group: Any,
        meta: Dict[str, Any],
        judge_text: bool = True,
    ) -> Tuple[float, bool]:
        """Credit multiplier for whether a must-have skill is RECENT.
        Per-skill last-used year (within SCORING_RECENCY_WINDOW_YEARS) wins;
        otherwise the head of the match text (most recent roles) is the
        proxy — but only when `judge_text` says there is real résumé /
        profile narrative to judge from (a bare skills list carries no
        recency signal, so it earns no penalty). Returns (multiplier,
        judged) — judged is False when nothing could be judged."""
        last_used = meta.get("last_used")
        if last_used:
            try:
                from datetime import datetime, timezone
                age = datetime.now(timezone.utc).year - int(last_used)
            except Exception:
                age = None
            if age is not None:
                return (
                    1.0 if age <= SCORING_RECENCY_WINDOW_YEARS else SCORING_MATRIX_RECENCY_DECAY
                ), True
        recent_text = profile.get("recent_text", "") or ""
        if not judge_text or not recent_text.strip():
            return 1.0, False
        for term in self._group_terms(group):
            if term and term in recent_text:
                return 1.0, True
            words = [w for w in term.split() if len(w) > 2]
            if words and all(w in recent_text for w in words):
                return 1.0, True
        return SCORING_MATRIX_RECENCY_DECAY, True

    async def warm_scoring_embeddings(
        self, criteria: SearchCriteria, candidates: Sequence[Dict[str, Any]]
    ) -> None:
        """Pre-warm embeddings for an off-search scoring pass.

        `skill_embeddings.best_cosine` is cache-only — it returns 0.0 for any
        term that was never warmed. A live search warms the query side once in
        `search_candidates` and each candidate's side in `emit_candidate`, so
        an off-search scorer that skipped warming would silently score every
        embedding-matched skill as a miss and come out BELOW the Step-5
        number. Warms both sides in one batched call; no-op when the family
        has embedding matching off.
        """
        family = self._resolve_search_family(criteria)
        if not embedding_skill_match_for_family(family):
            return
        terms = list(self._criteria_query_terms(criteria))
        for cand in candidates or []:
            terms.extend(self._candidate_skill_terms(cand))
        if not terms:
            return
        try:
            await skill_embeddings.warm_terms(terms)
        except Exception as exc:  # never let embedding warm break a scoring pass
            logger.warning(f"off-search embedding warm failed: {exc}")

    def score_candidate_off_search(
        self, cand: Dict[str, Any], criteria: SearchCriteria
    ) -> Dict[str, Any]:
        """`apply_scoring_policy` for callers outside a live search.

        Resolves the role family from `criteria` into a task-local ContextVar
        rather than the shared `_current_family` instance attribute, so the
        score matches what Step 5 would produce for the same job and a search
        streaming concurrently on this worker keeps its own family.
        """
        token = _off_search_family.set(self._resolve_search_family(criteria))
        try:
            return self.apply_scoring_policy(cand, criteria)
        finally:
            _off_search_family.reset(token)

    def apply_scoring_policy(
        self, cand: Dict[str, Any], criteria: SearchCriteria
    ) -> Dict[str, Any]:
        """The Step-5 scoring pipeline, in one place.

        This is what produces the match % the recruiter sees on Step 5:
        location hygiene → no-contact / client-conflict flags → the
        scoring matrix (`_score_candidate`) → the N/A policies. Every
        path that shows or stores a match % MUST go through here, so a
        candidate cannot score one way on Step 5 and another way
        somewhere else (see `routers.candidates._compute_resume_matching`,
        which the cross-submissions scan and the re-score endpoints use).

        Mutates and returns `cand`.
        """
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

        # Hiring-client conflict: current OR last employer is the client.
        # Stamped at the same choke-point so every source is covered, and
        # kept as a FLAG rather than a drop — the row stays visible in the
        # candidate list, greyed out with the reason on it, so a recruiter
        # can see why the person is off-limits instead of silently never
        # meeting them. (External rows whose CURRENT employer is the client
        # are still dropped upstream by _drop_client_employees, per the
        # standing "never source a client's own employees" rule.) Scoring
        # is left intact: unlike the no-contact list this is a per-job
        # conflict, and the match quality is still worth showing.
        apply_client_conflict_flag(cand, getattr(criteria, "client_name", ""))

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
                *_MATRIX_BAND_LINES.values(),
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
        # Scoring matrix v2: the percentage IS the matrix — none of the
        # additive floors/bonuses below apply (title relevance lives inside
        # the 15% bucket; source trust is not a rubric signal).
        source = str(cand.get("source") or "")
        api_rank = cand.get("api_rank")
        if (
            not SCORING_MATRIX_V2
            and source == "JobDiva-JobAgent"
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
        if bonus and base_score > 0 and not SCORING_MATRIX_V2:
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
        if base_score > 0 and criteria.title_criteria and not SCORING_MATRIX_V2:
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
        if (
            base_score > 0
            and OPEN_TO_WORK_SCORE_BONUS
            and cand.get("open_to_work") is True
            and not SCORING_MATRIX_V2
        ):
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
        # Exception: assess_all_sources (sample→approve→auto-launch flow)
        # runs the full LLM assessment on agent rows precisely so they
        # carry a real, comparable percentage — keep it.
        if (
            str(cand.get("source") or "") == "JobDiva-JobAgent"
            and not criteria.assess_all_sources
        ):
            cand["match_score"] = None

        return cand

    def _score_candidate_matrix(self, candidate: Dict[str, Any], criteria: SearchCriteria) -> Dict[str, Any]:
        """Score against the recruiter matrix:

            Recent Must-Have Skills 75 · Recent Title/Role 15 · Preferred 10

        plus pass/fail hard filters (score → 0): exclusion rules (currently
        employed by client / "must not have"), no must-have skill evidenced,
        required certification missing, confirmed outside the mandatory
        location, below the minimum-years floor. Work authorization is a
        Pairbot screening hard filter (asked on the call) and is only
        reported here as such.

        A bucket with no rubric input drops out of the denominator so the
        remaining buckets carry its weight (a rubric with no preferred
        skills is scored on must-haves + title alone). Same return shape as
        the legacy scorer: score, missing_skills, explainability,
        matched_skills, score_details.
        """
        profile = self._candidate_profile(candidate)
        dims: Dict[str, Dict[str, Any]] = {}
        for dim in self._collect_scoring_dimensions(criteria):
            dims[str(dim.get("key") or dim.get("label"))] = dim
        skills_dim = dims.get("skills") or {}
        title_dim = dims.get("title_recent") or {}
        domain_dim = dims.get("domain") or {}
        edu_dim = dims.get("education_certs") or {}

        def _groups(dim: Dict[str, Any], bucket: str) -> List[Any]:
            return [g for g in (dim.get(bucket) or []) if self._group_terms(g)]

        def _group_weight(group: Any) -> float:
            try:
                w = float(group.get("weight") or 1.0) if isinstance(group, dict) else 1.0
            except (TypeError, ValueError):
                w = 1.0
            return w if w > 0 else 1.0

        # "Evidence" = something to judge skills from. Without it (no résumé
        # text, no parsed skills) the evidence-based hard filters stay
        # unknown rather than failing the candidate on a data gap. Recency
        # is judged from narrative text only — a bare skills list says
        # nothing about WHEN a skill was used.
        has_text = bool(
            str(candidate.get("resume_text") or candidate.get("deep_text") or "").strip()
        )
        has_evidence = has_text or bool(profile.get("skills"))

        buckets: List[Tuple[str, float, float]] = []
        explainability: List[str] = []
        missing_required: List[str] = []
        matched_terms: List[str] = []
        score_details: Dict[str, Any] = {}
        hard_fail_reasons: List[str] = []
        hard_filters: Dict[str, str] = {}
        # Buckets left out of the denominator, with why — the score is always
        # normalized to 100 from whatever COULD be evaluated: a rubric with no
        # preferred items, or a profile with no title to judge, must not drag
        # a strong must-have match down. Surfaced in score_details and the
        # popup so the recruiter sees why a 75-point match reads 100%.
        not_evaluated: List[str] = []

        # ── Hard filter: exclusion rules ─────────────────────────────────
        # "Currently employed by client" / "must not be employed by X" ride
        # the company_exclusion dimension (current employers only); "must not
        # have X" skill exclusions ride their own dimension. Any excluded
        # group matching at/above the veto threshold fails the candidate.
        exclusion_hits: List[str] = []
        client_rule_present = False
        client_hit = False
        for key, dim in dims.items():
            excluded_groups = _groups(dim, "excluded_groups")
            if not excluded_groups:
                continue
            if key == "company_exclusion":
                client_rule_present = True
            excl_collections = dim.get("excluded_collections", dim.get("collections") or [])
            for group in excluded_groups:
                strength = self._term_group_score(profile, group, excl_collections)
                if strength >= SCORING_EXCLUSION_HARD_VETO_THRESHOLD:
                    exclusion_hits.append(f"{dim.get('label')}: {self._group_label(group)}")
                    if key == "company_exclusion":
                        client_hit = True
        if exclusion_hits:
            hard_fail_reasons.extend(
                f"matches exclusion rule ({hit})" for hit in exclusion_hits[:2]
            )
        hard_filters["client_employee"] = (
            "fail" if client_hit else ("pass" if client_rule_present else "n/a")
        )

        # ── Bucket 1: Recent must-have skills (75) ───────────────────────
        must_groups = _groups(skills_dim, "required_groups")
        skills_collections = skills_dim.get("collections") or ["skills"]
        w_must = float(SCORING_MATRIX_WEIGHTS.get("must_have_skills", 75.0))
        if must_groups:
            weighted = 0.0
            total_w = 0.0
            matched_labels: List[str] = []
            not_recent: List[str] = []
            items: List[Dict[str, Any]] = []
            for group in must_groups:
                label = self._group_label(group)
                w = _group_weight(group)
                raw = self._term_group_fuzzy_score(profile, group, skills_collections)
                meta = self._matrix_skill_meta(profile, group)
                item: Dict[str, Any] = {"skill": label, "match": round(raw, 3), "weight": w}
                if raw > 0.5:
                    years_mult, years_note = self._matrix_years_factor(profile, group, meta)
                    recency_mult, judged = self._matrix_recency_factor(
                        profile, group, meta, judge_text=has_text
                    )
                    credit = raw * years_mult * recency_mult
                    matched_labels.append(label)
                    matched_terms.append(label)
                    if years_note:
                        missing_required.append(years_note)
                    if judged and recency_mult < 1.0:
                        not_recent.append(label)
                    item.update({
                        "matched": True,
                        "years_factor": round(years_mult, 2),
                        "recency_factor": round(recency_mult, 2),
                    })
                    if meta.get("years") is not None:
                        item["years_used"] = meta["years"]
                    if meta.get("last_used") is not None:
                        item["last_used_year"] = meta["last_used"]
                else:
                    credit = max(raw, SCORING_MATRIX_REQUIRED_FLOOR)
                    missing_required.append(label)
                    item["matched"] = False
                item["credit"] = round(credit, 3)
                items.append(item)
                weighted += credit * w
                total_w += w
            must_ratio = weighted / total_w if total_w > 0 else 0.0
            buckets.append(("Recent Must-Have Skills", w_must, must_ratio))
            score_details["Recent Must-Have Skills"] = {
                "weight": w_must,
                "score": round(w_must * must_ratio, 2),
                "required_matched": len(matched_labels),
                "required_total": len(must_groups),
                "items": items,
            }
            line = f"Must-have skills: matched {len(matched_labels)}/{len(must_groups)}"
            if matched_labels:
                line += f" ({', '.join(matched_labels[:4])}{'…' if len(matched_labels) > 4 else ''})"
            explainability.append(line)
            if not_recent:
                explainability.append(
                    f"Not in recent experience (last {SCORING_RECENCY_WINDOW_YEARS}y): "
                    + ", ".join(not_recent[:3])
                )
            if matched_labels:
                hard_filters["must_have_skills"] = "pass"
            elif has_evidence:
                hard_fail_reasons.append("no must-have skill evidenced in the profile")
                hard_filters["must_have_skills"] = "fail"
            else:
                hard_filters["must_have_skills"] = "unknown"
        else:
            hard_filters["must_have_skills"] = "n/a"
            not_evaluated.append("Must-have skills (no must-have skills in the rubric)")

        # ── Bucket 2: Recent title / role relevancy (15) ─────────────────
        title_groups = _groups(title_dim, "required_groups") + _groups(title_dim, "preferred_groups")
        title_criteria = list(getattr(criteria, "title_criteria", []) or [])
        w_title = float(SCORING_MATRIX_WEIGHTS.get("title_recent", 15.0))
        has_title_data = bool(profile.get("titles")) or bool(_candidate_titles(candidate))
        if (title_groups or title_criteria) and not has_title_data:
            # Rubric asks for a title but the profile carries none to judge —
            # a data gap, not a mismatch. Leave the bucket out (weight 0 hides
            # it from the popup bars) and normalize on the rest.
            score_details["Recent Title Relevance"] = {
                "weight": 0.0, "score": 0.0, "skipped": "no title on profile",
            }
            not_evaluated.append("Title relevance (no title on the profile)")
        elif title_groups or title_criteria:
            title_collections = title_dim.get("collections") or ["titles"]
            term_credit = 0.0
            best_title_label = ""
            for group in title_groups:
                s = self._term_group_score(profile, group, title_collections)
                if s > term_credit:
                    term_credit = s
                    best_title_label = self._group_label(group)
            tier = ""
            cand_title = ""
            if title_criteria:
                candidate.pop("title_match_source", None)
                _compute_title_boost(candidate, title_criteria)
                src = candidate.get("title_match_source") or {}
                tier = str(src.get("relevance") or "")
                cand_title = str(src.get("candidate") or "")
            tier_credit = float(SCORING_MATRIX_TITLE_TIER_CREDIT.get(tier, 0.0))
            title_ratio = max(term_credit, tier_credit)
            buckets.append(("Recent Title Relevance", w_title, title_ratio))
            score_details["Recent Title Relevance"] = {
                "weight": w_title,
                "score": round(w_title * title_ratio, 2),
                "required_matched": 1 if title_ratio > 0.5 else 0,
                "required_total": 1,
                "taxonomy_tier": tier or None,
                "term_match": round(term_credit, 3),
            }
            if tier and tier_credit >= term_credit:
                explainability.append(
                    f"Title relevance: {tier} role match"
                    + (f" — {cand_title}" if cand_title else "")
                )
                matched_terms.append(f"Title: {cand_title or best_title_label or tier}")
            elif term_credit > 0.5:
                explainability.append(f"Title relevance: matched {best_title_label}")
                matched_terms.append(f"Title: {best_title_label}")
            else:
                explainability.append("Title relevance: no recent title match")
                if best_title_label:
                    missing_required.append(f"Title: {best_title_label}")
        else:
            not_evaluated.append("Title relevance (no title criteria in the rubric)")

        # ── Bucket 3: Preferred (10) ─────────────────────────────────────
        # Preferred skills + domain/industry + education + preferred certs.
        pref_items: List[Tuple[Any, List[str], str]] = []
        for g in _groups(skills_dim, "preferred_groups"):
            pref_items.append((g, skills_collections, "Preferred skill"))
        domain_collections = domain_dim.get("collections") or ["skills", "titles", "companies"]
        for g in _groups(domain_dim, "required_groups") + _groups(domain_dim, "preferred_groups"):
            pref_items.append((g, domain_collections, "Domain"))
        edu_collections = edu_dim.get("collections") or ["education", "certifications"]
        edu_required = _groups(edu_dim, "required_groups")
        cert_required = [g for g in edu_required if isinstance(g, dict) and g.get("kind") == "certification"]
        for g in edu_required:
            if isinstance(g, dict) and g.get("kind") == "certification":
                continue
            pref_items.append((g, edu_collections, "Education"))
        for g in _groups(edu_dim, "preferred_groups"):
            kind = g.get("kind") if isinstance(g, dict) else ""
            pref_items.append((g, edu_collections, "Certification" if kind == "certification" else "Education"))
        w_pref = float(SCORING_MATRIX_WEIGHTS.get("preferred", 10.0))
        # Evaluable only when the profile has SOMETHING the preferred items
        # can be judged against — résumé text or data in any collection the
        # items score on. An empty parse must not read as "missed every
        # preferred item".
        has_pref_data = has_text or any(
            profile.get(collection)
            for _group, collections, _prefix in pref_items
            for collection in collections
        )
        if pref_items and not has_pref_data:
            score_details["Preferred"] = {
                "weight": 0.0, "score": 0.0, "skipped": "no profile data to judge preferred items",
            }
            not_evaluated.append("Preferred (no profile data to judge against)")
        elif not pref_items:
            not_evaluated.append("Preferred (no preferred skills / domain / education in the rubric)")
        if pref_items and has_pref_data:
            weighted = 0.0
            total_w = 0.0
            pref_matched: List[str] = []
            for group, collections, prefix in pref_items:
                w = _group_weight(group)
                raw = self._term_group_fuzzy_score(profile, group, collections)
                weighted += raw * w
                total_w += w
                label = self._group_label(group)
                if raw > 0.5:
                    pref_matched.append(label)
                    matched_terms.append(f"{prefix}: {label}")
            pref_ratio = weighted / total_w if total_w > 0 else 0.0
            buckets.append(("Preferred", w_pref, pref_ratio))
            score_details["Preferred"] = {
                "weight": w_pref,
                "score": round(w_pref * pref_ratio, 2),
                "preferred_matched": len(pref_matched),
                "preferred_total": len(pref_items),
            }
            explainability.append(
                f"Preferred (skills / domain / education): matched {len(pref_matched)}/{len(pref_items)}"
            )

        # ── Hard filter: required certifications ────────────────────────
        if cert_required:
            missing_certs = [
                self._group_label(g) for g in cert_required
                if not self._term_group_matches(profile, g, edu_collections)
            ]
            for g in cert_required:
                label = self._group_label(g)
                if label not in missing_certs:
                    matched_terms.append(f"Certification: {label}")
            if missing_certs and has_evidence:
                hard_fail_reasons.append(f"required certification missing: {', '.join(missing_certs[:2])}")
                hard_filters["certifications"] = "fail"
                missing_required.extend(f"Certification: {c}" for c in missing_certs)
            elif missing_certs:
                hard_filters["certifications"] = "unknown"
                missing_required.extend(f"Certification: {c}" for c in missing_certs)
            else:
                hard_filters["certifications"] = "pass"
        else:
            hard_filters["certifications"] = "n/a"

        # ── Hard filter: mandatory location (evidence-based) ─────────────
        # The emission gate drops missing local locations, but a geocoder
        # failure is retained as unverified. Scoring only hard-fails a
        # confirmed mismatch; uncertainty must not read as "definitely
        # somewhere else".
        location_veto = self._location_hard_gate(candidate, criteria)
        confirmed_location_veto = location_veto and candidate.get("location_veto_reason") in (
            "state_mismatch", "outside_radius_confirmed",
        )
        if confirmed_location_veto:
            hard_fail_reasons.append(location_veto)
            hard_filters["location"] = "fail"
        else:
            hard_filters["location"] = "pass" if self._should_enforce_location(criteria) else "n/a"
        # Résumé-is-final residence: say which location was judged when the
        # résumé overrode the profile, so a veto (or a pass) on "India"
        # doesn't read as a contradiction of the "Dallas, TX" the CRM shows.
        _conflict = candidate.get("location_conflict")
        if isinstance(_conflict, dict) and _conflict.get("resume"):
            explainability.append(
                f"Location taken from résumé: {_conflict['resume']}"
                + (f" (profile said {_conflict['profile']})" if _conflict.get("profile") else "")
            )

        # ── Hard filter: minimum years floor (Step-5 "min experience") ──
        min_years = int(getattr(criteria, "min_experience_years", 0) or 0)
        total_years = float(profile.get("years_of_experience") or 0)
        if min_years > 0:
            if 0 < total_years < min_years:
                hard_fail_reasons.append(
                    f"below minimum experience ({total_years:g} yrs < {min_years} required)"
                )
                hard_filters["min_years"] = "fail"
            else:
                hard_filters["min_years"] = "pass" if total_years >= min_years else "unknown"
        else:
            hard_filters["min_years"] = "n/a"
        # Asked by Pairbot during screening; not parsed from résumés.
        hard_filters["work_authorization"] = "screening"

        # ── Compose ──────────────────────────────────────────────────────
        weighted_sum = sum(w * r for _, w, r in buckets)
        weighted_max = sum(w for _, w, _ in buckets)
        score = 0
        if weighted_max > 0:
            score = round(max(0.0, min(100.0, (weighted_sum / weighted_max) * 100)))

        score_details["hard_veto"] = {
            "triggered": bool(hard_fail_reasons),
            "reasons": hard_fail_reasons[:3],
        }
        score_details["hard_filters"] = hard_filters
        score_details["matrix"] = "v2"
        # Always scored out of 100: what was actually evaluated and what was
        # left out (rubric gap or profile data gap), so the popup can say
        # "normalized from 75 evaluated points".
        full_weight = float(sum(SCORING_MATRIX_WEIGHTS.values())) or 100.0
        score_details["normalization"] = {
            "evaluated_weight": round(weighted_max, 2),
            "full_weight": round(full_weight, 2),
            "not_evaluated": not_evaluated,
        }
        if buckets and not_evaluated and weighted_max < full_weight:
            explainability.append(
                f"Normalized to 100% from {round(weighted_max):g} evaluated points — not evaluated: "
                + "; ".join(not_evaluated[:3])
            )

        if hard_fail_reasons:
            score = 0
            score_details["band"] = {
                "tier": "excluded", "label": "Excluded", "action": "Hard filter", "min": None,
            }
            explainability.insert(0, f"Hard exclusion: {hard_fail_reasons[0]}")
        else:
            band = score_band(score)
            score_details["band"] = band
            explainability.insert(0, _MATRIX_BAND_LINES.get(band["tier"], ""))

        # Step 5 enforces the configured radius for every source, including
        # JobDiva-JobAgent — a confirmed-outside location is already a
        # hard_fail_reasons entry by this point, so no per-source carve-out.
        explainability = [line for line in explainability if line]
        if not explainability or (len(explainability) == 1 and not buckets):
            explainability = ["No active rubric criteria were available for scoring"]

        return {
            "score": score,
            "missing_skills": self._dedupe_terms(missing_required),
            "explainability": explainability[:6],
            "matched_skills": self._dedupe_terms(matched_terms),
            "score_details": score_details,
        }

    def _score_candidate(self, candidate: Dict[str, Any], criteria: SearchCriteria) -> Dict[str, Any]:
        if SCORING_MATRIX_V2:
            return self._score_candidate_matrix(candidate, criteria)
        profile = self._candidate_profile(candidate)
        dimensions = self._collect_scoring_dimensions(criteria)  # Use scoring dimensions for evaluation
        weights = scoring_weights_for_family(self._active_family)

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
        # candidate's location is known AND confirmed outside the radius.
        # Missing local locations are dropped at emission; geocoder failures
        # remain unverified and must not read as confirmed mismatches here.
        location_veto = self._location_hard_gate(candidate, criteria)
        if location_veto and candidate.get("location_veto_reason") not in (
            "state_mismatch", "outside_radius_confirmed",
        ):
            location_veto = None
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
    _EXPERIENCE_MONTHS = {
        "jan": 1, "january": 1,
        "feb": 2, "february": 2,
        "mar": 3, "march": 3,
        "apr": 4, "april": 4,
        "may": 5,
        "jun": 6, "june": 6,
        "jul": 7, "july": 7,
        "aug": 8, "august": 8,
        "sep": 9, "sept": 9, "september": 9,
        "oct": 10, "october": 10,
        "nov": 11, "november": 11,
        "dec": 12, "december": 12,
    }
    _EXPERIENCE_RANGE_RE = re.compile(
        r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|"
        r"Dec(?:ember)?)\.?\s+((?:19|20)\d{2})\s*(?:-|–|—|\bto\b)\s*"
        r"(?:(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|"
        r"Dec(?:ember)?)\.?\s+((?:19|20)\d{2})|(Present|Current|Now|Ongoing))\b",
        re.IGNORECASE,
    )
    _EXPLICIT_TOTAL_YEARS_RE = re.compile(
        r"\b(\d{1,2})\s*(\+)?\s*(?:years?|yrs?)\s+of\s+"
        r"(?:professional\s+|relevant\s+|total\s+|industry\s+)?experience\b",
        re.IGNORECASE,
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

    def _month_index(self, month_name: str, year: int) -> Optional[int]:
        month = self._EXPERIENCE_MONTHS.get(str(month_name or "").strip().lower().rstrip("."))
        if not month or year < 1900 or year > 2100:
            return None
        return year * 12 + month

    _EXPERIENCE_HEADING_RE = re.compile(
        r"(?im)^[ \t]*(?:professional\s+|work\s+)?(?:experience|employment|work\s+history)\b.*$"
    )
    _EXPERIENCE_END_RE = re.compile(
        r"(?im)^[ \t]*(?:education|academic|certifications?|licenses|skills|technical\s+skills|"
        r"projects|publications|awards|references)\b.*$"
    )
    _NUMERIC_RANGE_RE = re.compile(
        r"\b(0?[1-9]|1[0-2])[/-]((?:19|20)\d{2})\s*(?:-|–|—|\bto\b)\s*"
        r"(?:(0?[1-9]|1[0-2])[/-]((?:19|20)\d{2})|(present|current|now|ongoing))\b",
        re.IGNORECASE,
    )
    _YEAR_RANGE_RE = re.compile(
        r"\b((?:19|20)\d{2})\s*(?:-|–|—|\bto\b)\s*((?:19|20)\d{2}|present|current|now|ongoing)\b",
        re.IGNORECASE,
    )

    def _experience_section(self, text: str) -> str:
        """The work-history block only. Education and projects stay out."""
        match = self._EXPERIENCE_HEADING_RE.search(str(text or ""))
        if not match:
            return ""
        rest = text[match.end():]
        end = self._EXPERIENCE_END_RE.search(rest)
        return rest[:end.start()] if end else rest

    def _add_span(self, spans: List[Tuple[int, int]], start: Optional[int], end: Optional[int]) -> None:
        if start is None or end is None or end < start or end - start > 50 * 12:
            return
        spans.append((start, end + 1))

    def _timeline_years(self, text: str, now: Any = None) -> Optional[float]:
        """Union of employment date ranges, in years. Skill lines are ignored."""
        from datetime import datetime, timezone

        if now is None:
            now = datetime.now(timezone.utc)
        now_index = now.year * 12 + now.month
        spans: List[Tuple[int, int]] = []
        body = str(text or "")
        for match in self._EXPERIENCE_RANGE_RE.finditer(body):
            start = self._month_index(match.group(1), int(match.group(2)))
            if match.group(5):
                end = now_index
            else:
                end = self._month_index(match.group(3), int(match.group(4)))
            self._add_span(spans, start, end)
        for match in self._NUMERIC_RANGE_RE.finditer(body):
            start = int(match.group(1)) + int(match.group(2)) * 12
            if match.group(5):
                end = now_index
            else:
                end = int(match.group(3)) + int(match.group(4)) * 12
            self._add_span(spans, start, end)
        for match in self._YEAR_RANGE_RE.finditer(body):
            prefix = body[max(0, match.start() - 12):match.start()]
            if re.search(r"(?i)jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec", prefix):
                continue
            start_year = int(match.group(1))
            start = start_year * 12 + 1
            if re.fullmatch(r"(?i)present|current|now|ongoing", match.group(2) or ""):
                end = now_index
            else:
                end = int(match.group(2)) * 12 + 12
            self._add_span(spans, start, end)
        if not spans:
            return None
        spans.sort()
        merged: List[List[int]] = []
        for start, end in spans:
            if not merged or start > merged[-1][1]:
                merged.append([start, end])
            else:
                merged[-1][1] = max(merged[-1][1], end)
        months = sum(end - start for start, end in merged)
        if months <= 0:
            return None
        return round(months / 12.0, 2)

    def _explicit_total_years(self, text: str) -> Optional[float]:
        """A stated career total, such as '7+ years of experience'.

        'N+' is just over N, so a ceiling of N excludes it. A skill line
        like 'java: 9 years' is not a career total.
        """
        best: Optional[float] = None
        for match in self._EXPLICIT_TOTAL_YEARS_RE.finditer(str(text or "")[:4000]):
            try:
                years = int(match.group(1))
            except ValueError:
                continue
            if years <= 0 or years > 50:
                continue
            value = years + (0.1 if match.group(2) else 0.0)
            best = value if best is None else max(best, value)
        return best

    def _measured_total_years(self, candidate: Dict[str, Any], now: Any = None) -> Optional[float]:
        """Total career years from work history, for every source.

        Job dates win. A summary line such as "15+ years of experience" cannot
        raise a shorter work history. "6+" is used only when the résumé has
        no job dates, and it counts as just over 6. Education, projects, and
        per-skill lines ("java: 9 years") are not the career.
        """
        enhanced = candidate.get("enhanced_info") or {}
        job_lines: List[str] = []
        if isinstance(enhanced, dict):
            for item in enhanced.get("company_experience") or []:
                if not isinstance(item, dict):
                    continue
                start = str(item.get("start_date") or item.get("start") or "").strip()
                end = str(item.get("end_date") or item.get("end") or "").strip()
                if start or end:
                    job_lines.append(f"{start} - {end}")
        experience_text = ""
        if job_lines:
            experience_text = "\n".join(job_lines)
        else:
            sections = [
                self._experience_section(str(candidate.get(key) or ""))
                for key in ("resume_text", "deep_text")
            ]
            experience_text = "\n".join(section for section in sections if section)
        timeline = self._timeline_years(experience_text, now=now) if experience_text else None
        if timeline:
            return timeline
        stated_parts = [
            str(candidate.get(key) or "")
            for key in ("headline", "summary", "abstract", "resume_text", "deep_text")
        ]
        return self._explicit_total_years("\n".join(stated_parts))

    def _candidate_outside_years_range_pre_llm(
        self,
        candidate: Dict[str, Any],
        criteria: SearchCriteria,
    ) -> bool:
        """True when the résumé's total years sit outside Min YOE or Max YOE.

        Unknown totals are left for the post-parse gate. This does not use
        JobDiva's stored experience_years field or a per-skill 'N years' line.
        """
        min_years = int(getattr(criteria, "min_experience_years", 0) or 0)
        max_years = getattr(criteria, "max_experience_years", None)
        try:
            max_years = int(max_years) if max_years is not None else 0
        except (TypeError, ValueError):
            max_years = 0
        if min_years <= 0 and max_years <= 0:
            return False
        years = self._measured_total_years(candidate)
        if not years:
            return False
        if min_years > 0 and years < min_years:
            return True
        if max_years > 0 and years > max_years:
            return True
        return False
