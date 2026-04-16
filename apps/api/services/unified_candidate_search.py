import logging
import asyncio
import json
import re
import time
from typing import List, Dict, Any, Optional
from pydantic import BaseModel

from services.jobdiva import JobDivaService
from services.unipile import unipile_service
from services.vetted import vetted_service

logger = logging.getLogger(__name__)

class SearchCriteria(BaseModel):
    job_id: str
    titles: List[str] = []
    skills: List[str] = []
    title_criteria: List[Dict[str, Any]] = []
    skill_criteria: List[Dict[str, Any]] = []
    keywords: List[str] = []
    resume_match_filters: List[Dict[str, Any]] = []
    location: str = ""
    within_miles: int = 25
    companies: List[str] = []
    page_size: int = 100
    sources: List[str] = ["JobDiva"]
    open_to_work: bool = True
    boolean_string: str = ""

class UnifiedCandidateSearch:
    def __init__(self):
        self.jobdiva_service = JobDivaService()
        self.unipile_service = unipile_service
        self.vetted_service = vetted_service

    def _log_stage(self, stage: str, message: str) -> None:
        logger.info("[CandidateSearch] %s | %s", stage, message)

    async def search_candidates(self, criteria: SearchCriteria) -> Dict[str, Any]:
        """
        Orchestrate candidate search across multiple providers with tiered JobDiva logic.
        """
        start_time = time.time()
        self._log_stage("Start", f"job={criteria.job_id} sources={', '.join(criteria.sources or [])}")
        self._log_stage("Criteria", f"title_criteria={len(criteria.title_criteria)}, skill_criteria={len(criteria.skill_criteria)}")
        self._log_stage("Criteria", f"resume_match_filters={len(criteria.resume_match_filters)} (used for scoring only)")
        self._log_stage("Criteria", f"keywords={len(criteria.keywords)}, companies={len(criteria.companies)}")
        
        jobdiva_selected = "JobDiva" in criteria.sources
        all_screened_candidates = []
        
        summary = {
            "total_candidates": 0,
            "job_applicants_count": 0,
            "linkedin_count": 0,
            "dice_count": 0,
            "vetted_count": 0,
            "talent_search_count": 0,
            "cached_results": 0,
            "new_extractions": 0,
            "qualified_applicants": 0,
            "qualified_talent": 0
        }

        # --- STEP 1: JOBDIVA APPLICANTS ---
        if jobdiva_selected:
            self._log_stage("Applicants", "fetching job applicants")
            applicants_res = await self._search_jobdiva_applicants(criteria)
            applicants = applicants_res.get("candidates", [])
            summary["job_applicants_count"] = len(applicants)
            
            if applicants:
                self._log_stage("Applicants", f"Found {len(applicants)} applicants; starting resume screen...")
                # Immediate enrichment and screening for applicants
                self._attach_cached_enhanced_info(applicants)
                summary["new_extractions"] += await self._enrich_filtered_jobdiva_candidates(applicants, criteria)
                
                # Filter to find truly qualified ones
                qualified_apps = []
                for cand in applicants:
                    assessment = self._filter_assessment(cand, criteria, enforce_years=True)
                    if assessment["passes"]:
                        cand["screening_summary"] = {
                            "matched": assessment["matched"][:10],
                            "missing": [],
                            "excluded": [],
                        }
                        qualified_apps.append(cand)
                
                summary["qualified_applicants"] = len(qualified_apps)
                all_screened_candidates.extend(qualified_apps)
                self._log_stage("Applicants", f"Qualified: {len(qualified_apps)} of {len(applicants)} applicants passed screen.")
            else:
                self._log_stage("Applicants", "No applicants found.")

        # --- STEP 2: TIERED TALENT SEARCH ---
        # Trigger talent pool search if we have fewer than 3 qualified applicants
        should_search_talent = jobdiva_selected and summary["qualified_applicants"] < 3
        if should_search_talent:
            reason = f"only {summary['qualified_applicants']} qualified applicants" if summary["qualified_applicants"] > 0 else "no qualified applicants"
            self._log_stage("TalentSearch", f"Triggering talent search: {reason}")
            
            talent_res = await self._search_jobdiva_talent(criteria)
            talent_pool = talent_res.get("candidates", [])
            summary["talent_search_count"] = len(talent_pool)
            
            if talent_pool:
                # Screen talent pool
                self._attach_cached_enhanced_info(talent_pool)
                summary["new_extractions"] += await self._enrich_filtered_jobdiva_candidates(talent_pool, criteria)
                
                qualified_talent = []
                for cand in talent_pool:
                    assessment = self._filter_assessment(cand, criteria, enforce_years=True)
                    if assessment["passes"]:
                        cand["screening_summary"] = {
                            "matched": assessment["matched"][:10],
                            "missing": [],
                            "excluded": [],
                        }
                        qualified_talent.append(cand)
                
                summary["qualified_talent"] = len(qualified_talent)
                all_screened_candidates.extend(qualified_talent)
                self._log_stage("TalentSearch", f"Qualified: {len(qualified_talent)} of {len(talent_pool)} talent candidates passed screen.")
        else:
            if jobdiva_selected:
                self._log_stage("TalentSearch", f"Skipped: already have {summary['qualified_applicants']} qualified applicants.")

        # --- STEP 3: EXTERNAL SOURCES (LinkedIn, etc.) ---
        external_tasks = []
        if "LinkedIn" in criteria.sources: external_tasks.append(self._search_linkedin(criteria))
        if "Dice" in criteria.sources: external_tasks.append(self._search_dice(criteria))
        
        if external_tasks:
            self._log_stage("External", f"Searching {len(external_tasks)} external sources...")
            ext_results = await asyncio.gather(*external_tasks, return_exceptions=True)
            for res in ext_results:
                if isinstance(res, Exception) or not res: continue
                
                ext_candidates = res.get("candidates", [])
                source_type = res.get("source_type", "External")
                summary[f"{source_type.lower()}_count"] = len(ext_candidates)
                
                passed_ext = []
                for cand in ext_candidates:
                    cand["source"] = source_type
                    assessment = self._filter_assessment(cand, criteria, enforce_years=False)
                    if assessment["passes"]:
                        passed_ext.append(cand)
                
                all_screened_candidates.extend(passed_ext)
                self._log_stage(source_type, f"Found {len(ext_candidates)} profiles; {len(passed_ext)} passed initial filter.")

        # --- STEP 4: DEDUPLICATION & SCORING ---
        final_list = self._deduplicate_candidates(all_screened_candidates)
        
        for candidate in final_list:
            score_result = self._score_candidate(candidate, criteria)
            candidate["match_score"] = score_result["score"]
            candidate["missing_skills"] = score_result["missing_skills"]
            candidate["explainability"] = score_result["explainability"]
            candidate["match_score_details"] = score_result.get("score_details", {})

        # Final Sort: Match Score DESC, Applicants first
        final_list.sort(
            key=lambda c: (
                int(c.get("match_score") or 0),
                1 if str(c.get("source", "")).startswith("JobDiva-Applicants") else 0
            ),
            reverse=True
        )

        summary["total_candidates"] = len(final_list)
        duration = time.time() - start_time
        
        # User Friendly Log
        self._log_stage("Done", f"Search complete for job {criteria.job_id} in {int(duration)}s.")
        
        # Build source-specific summary for the log
        log_parts = []
        if summary["job_applicants_count"] > 0:
            log_parts.append(f"JobDiva-Applicants: {summary['qualified_applicants']}/{summary['job_applicants_count']} qualified")
        if summary["talent_search_count"] > 0:
            log_parts.append(f"JobDiva-TalentSearch: {summary['qualified_talent']}/{summary['talent_search_count']} qualified")
        if summary.get("linkedin_count", 0) > 0:
            qualified_li = len([c for c in final_list if c.get("source") == "LinkedIn"])
            log_parts.append(f"LinkedIn: {qualified_li}/{summary['linkedin_count']} qualified")
        
        source_summary = ", ".join(log_parts) if log_parts else "No candidates found"
        
        self._log_stage("Summary", f"Returning {len(final_list)} qualified candidates. Breakdown: {source_summary}")
        
        return {
            "candidates": final_list,
            "summary": summary,
            "search_criteria": criteria.dict(),
            "extraction_time_seconds": round(duration, 1)
        }

        
    async def _search_jobdiva_talent(self, criteria: SearchCriteria) -> Dict[str, Any]:
        try:
            candidates = await self.jobdiva_service.search_candidates(
                skills=self._jobdiva_search_terms(criteria),
                location=criteria.location,
                limit=criteria.page_size,
                job_id=None,
                boolean_string=criteria.boolean_string or self._build_boolean_string(criteria)
            )
            self._log_stage("TalentSearch", f"JobDiva returned {len(candidates)} candidate(s)")
            for c in candidates:
                c["source"] = "JobDiva-TalentSearch"
            # No pre-screening needed - boolean string already filters candidates
            self._log_stage("TalentSearch", f"Proceeding to LLM extraction for {len(candidates)} candidate(s)")
            return {"candidates": candidates, "source_type": "JobDiva-TalentSearch"}
        except Exception as e:
            logger.error(f"JobDiva Talent Search failed: {e}")
            return {"candidates": [], "source_type": "JobDiva-TalentSearch"}

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
            if item.get("recent"):
                group = f"{group} recent"
            if int(item.get("years") or 0) > 0:
                years = int(item.get("years") or 0)
                group = f"{group} over {years} year{'s' if years > 1 else ''}"
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
        for location in [criteria.location]:
            if location and location.strip():
                add_unique(parts, seen_must, quote(location), location)
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
        for candidate in candidates:
            haystack = self._candidate_summary_text(candidate)

            if any(group_matches(haystack, group) for group in exclude_groups):
                continue

            # Location is a hard source filter. JobDiva Talent Search can return
            # candidates outside the location in the Boolean string, so we verify
            # the returned candidate location before any LLM enrichment.
            if enforce_location and not self._location_matches(candidate, criteria):
                continue

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

        self._log_stage("SummaryScreen", f"{source_type}: kept {len(filtered)} of {len(candidates)} candidate(s)")
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
        if not criteria.location:
            return True

        required = self._parse_location(criteria.location)
        if not required["city"] and not required["state"]:
            return True

        candidate_location = self._parse_location(
            candidate.get("location")
            or f"{candidate.get('city', '')}, {candidate.get('state', '')}".strip(", ")
        )
        candidate_text = self._normalize_search_text(
            " ".join([
                str(candidate.get("location") or ""),
                str(candidate.get("city") or ""),
                str(candidate.get("state") or ""),
            ])
        )

        if required["city"] and required["city"] not in candidate_text:
            return False
        if required["state"] and required["state"] not in candidate_text:
            return False
        if required["state"] and candidate_location["state"] and required["state"] != candidate_location["state"]:
            return False

        return True

    def _should_enforce_location(self, criteria: SearchCriteria) -> bool:
        normalized_location = self._normalize_term(criteria.location)
        if not normalized_location:
            return False

        normalized_boolean = self._normalize_term(criteria.boolean_string)
        if not normalized_boolean:
            return True

        return normalized_location in normalized_boolean

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
        text = self._normalize_search_text(value)
        text = re.sub(r"\bwithin\s+\d+\s+mi\b", "", text)
        text = re.sub(r"\bmetro\b", "", text)
        text = re.sub(r"^must be local to\s+", "", text).strip(" ,")
        parts = [part.strip() for part in re.split(r",|\\|/", text) if part.strip()]

        city = parts[0] if parts else ""
        state = ""
        if len(parts) > 1:
            state = parts[1].split()[0]
        elif len(parts) == 1:
            tokens = parts[0].split()
            if len(tokens) > 1 and len(tokens[-1]) == 2:
                city = " ".join(tokens[:-1])
                state = tokens[-1]

        return {
            "city": self._normalize_term(city),
            "state": state_aliases.get(self._normalize_term(state), self._normalize_term(state)),
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
        for source in [enhanced.get("key_skills", []), enhanced.get("structured_skills", []), candidate.get("skills", [])]:
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

        company_terms: List[str] = []
        for item in enhanced.get("company_experience", []) or []:
            if isinstance(item, dict):
                for key in ["company", "company_name", "employer", "name"]:
                    if item.get(key):
                        company_terms.append(str(item.get(key)))

        education_terms: List[str] = []
        for item in enhanced.get("candidate_education", []) or []:
            if isinstance(item, dict):
                for key in ["degree", "field", "institution", "school", "specialization"]:
                    if item.get(key):
                        education_terms.append(str(item.get(key)))
            elif isinstance(item, str):
                education_terms.append(item)

        certification_terms: List[str] = []
        for item in enhanced.get("candidate_certification", []) or []:
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
                if normalized in item or item in normalized:
                    return True

        return normalized in profile.get("text", "")

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
        return any(self._contains_term(profile, term, *collections) for term in terms)

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
        if not self._term_group_matches(profile, group, collections):
            return 0.0

        score = 1.0
        min_years = self._group_min_years(group)
        if min_years > 0:
            years = float(profile.get("years_of_experience") or 0)
            if years <= 0:
                score *= 0.75
            elif years < min_years:
                score *= max(0.35, years / min_years)

        if self._group_recent(group):
            terms = self._group_terms(group)
            recent_text = profile.get("recent_text", "")
            if terms and not any(term in recent_text for term in terms):
                score *= 0.85

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
        profile = self._candidate_profile(candidate)
        missing: List[str] = []
        matched: List[str] = []
        excluded: List[str] = []

        if self._should_enforce_location(criteria) and not self._location_matches(candidate, criteria):
            missing.append(f"Location: {criteria.location}")

        for dimension in self._collect_match_dimensions(criteria):
            collections = dimension["collections"]
            required_groups = [
                group for group in dimension.get("required_groups", [])
                if self._group_terms(group)
            ]
            excluded_groups = [
                group for group in dimension.get("excluded_groups", [])
                if self._group_terms(group)
            ]

            for group in excluded_groups:
                if self._term_group_matches(profile, group, collections):
                    excluded.append(f"{dimension['label']}: {self._group_label(group)}")

            if not required_groups:
                continue

            matched_groups = [
                group for group in required_groups
                if self._term_group_fully_matches(profile, group, collections, enforce_years=enforce_years)
            ]

            if dimension["label"] == "Titles":
                if matched_groups:
                    matched.append(f"{dimension['label']}: {self._group_label(matched_groups[0])}")
                else:
                    missing.append(f"{dimension['label']}: one of {[self._group_label(g) for g in required_groups]}")
                continue

            if dimension["label"] == "Location":
                continue

            for group in required_groups:
                if self._term_group_fully_matches(profile, group, collections, enforce_years=enforce_years):
                    matched.append(f"{dimension['label']}: {self._group_label(group)}")
                else:
                    label = self._group_label(group)
                    min_years = self._group_min_years(group)
                    if enforce_years and min_years > 0:
                        years = float(profile.get("years_of_experience") or 0)
                        if years and years < min_years:
                            label = f"{label} ({min_years}+ years required, candidate has {years:g})"
                    missing.append(f"{dimension['label']}: {label}")

        return {
            "passes": not missing and not excluded,
            "missing": self._dedupe_terms(missing),
            "matched": self._dedupe_terms(matched),
            "excluded": self._dedupe_terms(excluded),
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

            required_ratio = (
                sum(self._term_group_score(profile, group, dimension["collections"]) for group in required_groups)
                / len(required_groups)
            ) if required_groups else 1.0
            preferred_ratio = (
                sum(self._term_group_score(profile, group, dimension["collections"]) for group in preferred_groups)
                / len(preferred_groups)
            ) if preferred_groups else 1.0
            base_ratio = 0.0
            if required_groups and preferred_groups:
                base_ratio = (required_ratio * 0.75) + (preferred_ratio * 0.25)
            elif required_groups:
                base_ratio = required_ratio
            elif preferred_groups:
                base_ratio = preferred_ratio

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
                penalty = min(total_weight * 0.6, len(excluded_matches) * max(4.0, total_weight * 0.25))
                weighted_scores.append(-penalty)
                score_details[dimension["label"]]["exclusion_penalty"] = round(penalty, 2)
                explainability.append(
                    f"{dimension['label']}: conflicting match on {', '.join(excluded_matches[:2])}"
                )

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

            if dimension["label"] == "Skills":
                matched_required_skills.extend(required_matches)

        score = 0
        if weighted_max > 0:
            score = round(max(0.0, min(100.0, (sum(weighted_scores) / weighted_max) * 100)))

        if score >= 85:
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

    def _collect_sourcing_dimensions(self, criteria: SearchCriteria) -> List[Dict[str, Any]]:
        """Collect match dimensions for PRE-SCREENING using ONLY Page 5 sourcing filters."""
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

        for keyword in criteria.keywords:
            add_terms("keywords", "must", [keyword])
        for company in criteria.companies:
            add_terms("companies", "must", [company])
        if self._should_enforce_location(criteria):
            add_terms("location", "must", [criteria.location])

        # DO NOT include resume_match_filters here - only for scoring!
        
        return list(dimensions.values())

    def _collect_scoring_dimensions(self, criteria: SearchCriteria) -> List[Dict[str, Any]]:
        """Collect match dimensions for SCORING using Page 3 rubrics + Page 4 resume match filters."""
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
            "education": {
                "label": "Education",
                "weight": 8.0,
                "collections": ["education"],
                "required": [],
                "preferred": [],
                "excluded": [],
            },
            "certifications": {
                "label": "Certifications",
                "weight": 7.0,
                "collections": ["certifications"],
                "required": [],
                "preferred": [],
                "excluded": [],
            },
            "keywords": {
                "label": "Keywords",
                "weight": 5.0,
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

        # Add Page 3 rubrics (title_criteria, skill_criteria)
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

        for keyword in criteria.keywords:
            add_terms("keywords", "must", [keyword])
        for company in criteria.companies:
            add_terms("companies", "must", [company])
        if self._should_enforce_location(criteria):
            add_terms("location", "must", [criteria.location])

        # Add Page 4 resume match filters (THIS IS CORRECT FOR SCORING)
        for filter_item in criteria.resume_match_filters:
            if not filter_item.get("active", True):
                logger.debug(f"Skipping inactive filter: {filter_item.get('category')} - {filter_item.get('value')}")
                continue
            logger.debug(f"Using active filter for scoring: {filter_item.get('category')} - {filter_item.get('value')}")
            category = str(filter_item.get("category", "")).lower()
            raw_value = str(filter_item.get("value", "")).strip()
            if not raw_value:
                continue

            term = self._resume_filter_term(filter_item)
            if not term:
                continue

            if "customer" in category or raw_value.lower().startswith("must not"):
                add_terms("companies", "exclude", [term])
            elif "title" in category:
                add_terms("titles", "can" if "preferred" in category or raw_value.lower().startswith("can ") else "must", [term])
            elif "skill" in category:
                add_terms("skills", "can" if "preferred" in category or raw_value.lower().startswith("can ") else "must", [term])
            elif "education" in category:
                add_terms("education", "can" if "preferred" in category or raw_value.lower().startswith("can ") else "must", [term])
            elif "certification" in category or "license" in category:
                add_terms("certifications", "can" if "preferred" in category or raw_value.lower().startswith("can ") else "must", [term])
            elif "domain" in category:
                add_terms("companies", "can", [term])
                add_terms("keywords", "can", [term])
            elif "requirement" in category and "local" in term.lower():
                add_terms("location", "must", [term])
            else:
                add_terms("keywords", "must", [term])

        return list(dimensions.values())

    def _collect_match_dimensions(self, criteria: SearchCriteria) -> List[Dict[str, Any]]:
        """Legacy method - redirects to _collect_scoring_dimensions for backward compatibility."""
        return self._collect_scoring_dimensions(criteria)

    async def _enrich_filtered_jobdiva_candidates(self, candidates: List[Dict[str, Any]], criteria: SearchCriteria) -> int:
        from services.sourced_candidates_storage import process_jobdiva_candidate

        enriched_count = 0
        jobdiva_candidates = [
            candidate for candidate in candidates
            if str(candidate.get("source", "")).startswith("JobDiva")
        ]
        self._log_stage("ResumeScreen", f"checking {len(jobdiva_candidates)} JobDiva candidate resume(s) before LLM")

        screened_count = 0
        skipped_count = 0
        no_resume_count = 0
        failed_filter_count = 0
        
        for index, candidate in enumerate(jobdiva_candidates, 1):
            if candidate.get("enhanced_info"):
                screened_count += 1
                self._log_stage("ResumeScreen", f"candidate_id={candidate.get('candidate_id')} already has enhanced_info, skipping")
                continue

            candidate_id = str(candidate.get("candidate_id") or candidate.get("id") or "")
            if not candidate_id:
                self._log_stage("ResumeScreen", f"skipped candidate at index {index}; no candidate_id")
                continue

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
                    no_resume_count += 1
                    skipped_count += 1
                    self._log_stage("ResumeScreen", f"skipped candidate_id={candidate_id}; no resume text available")
                    continue

                self._log_stage("ResumeScreen", f"running quick filter for candidate_id={candidate_id}")
                assessment = self._filter_assessment(candidate, criteria, enforce_years=False)
                if not assessment["passes"]:
                    failed_filter_count += 1
                    skipped_count += 1
                    self._log_stage(
                        "ResumeScreen",
                        "FAILED FILTER candidate_id=%s matched=%s missing=%s excluded=%s" % (
                            candidate_id,
                            assessment["matched"][:5],
                            assessment["missing"][:5],
                            assessment["excluded"][:5],
                        ),
                    )
                    continue

                screened_count += 1
                self._log_stage(
                    "ResumeScreen",
                    "PASSED FILTER candidate_id=%s matched=%s - proceeding to LLM" % (
                        candidate_id,
                        assessment["matched"][:5],
                    ),
                )
                self._log_stage(
                    "LLM",
                    "STARTING LLM extraction candidate %s of %s screened (candidate_id=%s, resume_id=%s)" % (
                        enriched_count + 1,
                        screened_count,
                        candidate_id,
                        candidate.get("resume_id") or "unknown",
                    ),
                )
                enhanced = await process_jobdiva_candidate(candidate)
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
                if candidate["enhanced_info"].get("structured_skills"):
                    candidate["skills"] = candidate["enhanced_info"].get("structured_skills")
                enriched_count += 1
                self._log_stage("LLM", f"COMPLETED LLM extraction for candidate_id={candidate_id}, enriched_count={enriched_count}")
            except Exception as e:
                logger.error(f"❌ Filtered JobDiva enhancement FAILED for {candidate_id}: {e}", exc_info=True)

        self._log_stage(
            "ResumeScreen",
            "RESULTS: kept %s of %s JobDiva candidate(s); skipped %s total (no_resume=%s, failed_filter=%s)" % (
                screened_count,
                len(jobdiva_candidates),
                skipped_count,
                no_resume_count,
                failed_filter_count,
            ),
        )
        self._log_stage("LLM", f"completed {enriched_count} JobDiva candidate(s)")
        return enriched_count

    def _attach_cached_enhanced_info(self, candidates: List[Dict[str, Any]]) -> None:
        candidate_ids = [str(c.get("candidate_id") or c.get("id")) for c in candidates if c.get("candidate_id") or c.get("id")]
        if not candidate_ids:
            return

        try:
            import psycopg2
            import psycopg2.extras
            from core.config import DATABASE_URL

            with psycopg2.connect(DATABASE_URL) as conn:
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
            # Unipile expects skills as a list of dicts or strings
            skills = [{"value": s, "priority": "Must Have"} for s in criteria.skills]
            candidates = await self.unipile_service.search_candidates(
                skills=skills,
                location=criteria.location,
                open_to_work=criteria.open_to_work,
                limit=criteria.page_size
            )
            return {"candidates": candidates, "source_type": "LinkedIn"}
        except Exception as e:
            logger.error(f"LinkedIn search failed: {e}")
            return {"candidates": [], "source_type": "LinkedIn"}

    async def _search_dice(self, criteria: SearchCriteria) -> Dict[str, Any]:
        return {"candidates": [], "source_type": "Dice"}

    async def _search_vetted(self, criteria: SearchCriteria) -> Dict[str, Any]:
        try:
            # Simple vetted search
            candidates = await self.vetted_service.search_candidates(
                skills=criteria.skills,
                location=criteria.location,
                limit=criteria.page_size
            )
            return {"candidates": candidates, "source_type": "VettedDB"}
        except Exception as e:
            logger.error(f"VettedDB search failed: {e}")
            return {"candidates": [], "source_type": "VettedDB"}

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
