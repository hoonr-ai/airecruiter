"""UnifiedCandidateSearch methods: matching."""
import json
import math
import re
from typing import List, Dict, Any
from services.location import sanitize_candidate_location
from core.config import SCORING_YEARS_UNKNOWN_MULT, SCORING_YEARS_FLOOR, SCORING_RECENCY_DECAY, EMBEDDING_MATCH_THRESHOLD, scoring_weights_for_family, embedding_skill_match_for_family
from services import skill_embeddings
from .common import SearchCriteria


class MatchingMixin:

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
        # Per-skill years / last-used year from the résumé parse (see
        # sourced_candidates_storage._normalize_llm_skills), keyed by the
        # normalized skill name. Feeds the "recent must-have skills" bucket of
        # the scoring matrix; absent for older cache entries and for sources
        # whose skills arrive as bare strings (the scorer then falls back to
        # total YOE + the recent-text proxy).
        skill_meta: Dict[str, Dict[str, Any]] = {}
        for source in [enhanced.get("key_skills", []), enhanced.get("structured_skills", []), enhanced.get("skills", []), candidate.get("skills", [])]:
            if not isinstance(source, list):
                continue
            for item in source:
                if isinstance(item, dict):
                    names: List[str] = []
                    if item.get("skill"):
                        skill_terms.append(str(item.get("skill")))
                        names.append(str(item.get("skill")))
                    if item.get("name"):
                        skill_terms.append(str(item.get("name")))
                        names.append(str(item.get("name")))
                    years_used = item.get("years_used")
                    last_used = item.get("last_used_year")
                    if names and (years_used is not None or last_used is not None):
                        for raw_name in names:
                            key = self._normalize_term(raw_name)
                            if not key:
                                continue
                            meta = skill_meta.setdefault(key, {"years": None, "last_used": None})
                            try:
                                if years_used is not None and float(years_used) > 0:
                                    meta["years"] = max(float(meta["years"] or 0), float(years_used))
                            except (TypeError, ValueError):
                                pass
                            try:
                                if last_used is not None and int(last_used) > 1900:
                                    meta["last_used"] = max(int(meta["last_used"] or 0), int(last_used))
                            except (TypeError, ValueError):
                                pass
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

        # Résumé is final for residence: the explicitly stated résumé
        # location is the only location term when present (see
        # _candidate_structured_locations). Otherwise source-native fields.
        # Arrangement strings are stripped — "Remote" must not be scored as a
        # place.
        resume_location_term = sanitize_candidate_location(enhanced.get("current_location", ""))
        if resume_location_term and self._resume_location_authoritative():
            location_terms = unique_terms([resume_location_term])
        else:
            location_terms = unique_terms([
                sanitize_candidate_location(
                    f"{candidate.get('city', '')}, {candidate.get('state', '')}".strip(", ")
                ),
                sanitize_candidate_location(candidate.get("location", "")),
                resume_location_term,
            ])

        resume_years = 0.0
        # The résumé's own timeline is the total. A parsed "6" must not
        # hide "6+" or a career that runs past the recruiter's ceiling.
        measured_years = self._measured_total_years(candidate)
        if measured_years:
            resume_years = measured_years
        else:
            raw_years = enhanced.get("years_of_experience") or candidate.get("experience_years")
            if raw_years is not None:
                try:
                    match = re.search(r"\d+(?:\.\d+)?", str(raw_years))
                    if match:
                        resume_years = float(match.group(0))
                except Exception:
                    resume_years = 0.0

        match_text = self._candidate_match_text(candidate)
        return {
            "titles": title_terms,
            "skills": unique_terms(skill_terms),
            "companies": unique_terms(company_terms),
            "current_companies": unique_terms(current_company_terms),
            "education": unique_terms(education_terms),
            "certifications": unique_terms(certification_terms),
            "locations": location_terms,
            "years_of_experience": resume_years,
            "skill_meta": skill_meta,
            "text": match_text,
            "recent_text": match_text[:3000],
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
        embedding_active = embedding_skill_match_for_family(self._active_family)
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
        max_years = getattr(criteria, "max_experience_years", None)

        if enforce_years:
            years = float(profile.get("years_of_experience") or 0)
            if years > 0:
                if min_years > 0 and years < min_years:
                    return {
                        "passes": False,
                        "missing": [f"YOE: needs {min_years}+ years (resume shows {int(years)})"],
                        "matched": self._dedupe_terms(matched),
                        "excluded": self._dedupe_terms(excluded),
                        "min_years_failure": True,
                    }
                if max_years is not None and int(max_years) > 0 and years > int(max_years):
                    return {
                        "passes": False,
                        "missing": [f"YOE: maximum {int(max_years)} years (resume shows {int(years)})"],
                        "matched": self._dedupe_terms(matched),
                        "excluded": self._dedupe_terms(excluded),
                        "max_years_failure": True,
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
            all_loc_values = []
            if criteria.location:
                all_loc_values.append(criteria.location)
            for loc in criteria.additional_locations:
                v = str(loc.value or "").strip()
                if v and v not in all_loc_values:
                    all_loc_values.append(v)
            if all_loc_values:
                add_terms("location", "must", all_loc_values)

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
        weights = scoring_weights_for_family(self._active_family)
        dimensions = {
            # Rubric-driven scored dimensions. "domain" and "education_certs"
            # are merges of the legacy keywords / education+certifications
            # dimensions. Location and standalone company-match dimensions are
            # gone: location is now a hard gate (see _score_candidate) and
            # company signal is split into the same_client synthetic dimension
            # plus the company_exclusion veto below.
            "title_recent": {
                "key": "title_recent",
                "label": "Recent Title Relevance",
                "weight": weights["title_recent"],
                "collections": ["titles"],
            },
            "skills": {
                "key": "skills",
                "label": "Skills Match",
                "weight": weights["skills"],
                "collections": ["skills"],
            },
            "domain": {
                "key": "domain",
                "label": "Domain Experience",
                "weight": weights["domain"],
                "collections": ["skills", "titles", "companies", "education", "certifications", "locations"],
            },
            "education_certs": {
                "key": "education_certs",
                "label": "Education & Certifications",
                "weight": weights["education_certs"],
                "collections": ["education", "certifications"],
            },
            # Non-scored (weight 0). Carries the "currently employed by client" /
            # "must not be employed by" exclusion into the soft-penalty + hard-veto
            # path. Scoped to CURRENT employers only (excluded_collections) so a
            # candidate who merely worked at the client in the past is not vetoed.
            "company_exclusion": {
                "key": "company_exclusion",
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
            kind: str = "",
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
                # "certification" / "education" inside the merged
                # education_certs dimension — the scoring matrix treats a
                # required certification as a hard filter, education as a
                # preferred signal. Empty for every other dimension.
                "kind": kind,
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
                add_terms("education_certs", "can" if "preferred" in category or raw_value.lower().startswith("can ") else "must", [term], weight=fw, kind="education")
            elif "cert" in category or "license" in category:
                add_terms("education_certs", "can" if "preferred" in category or raw_value.lower().startswith("can ") else "must", [term], weight=fw, kind="certification")
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
