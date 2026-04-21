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
from services.exa_service import exa_service

logger = logging.getLogger(__name__)

class SearchCriteria(BaseModel):
    job_id: str
    title_criteria: List[Dict[str, Any]] = []
    skill_criteria: List[Dict[str, Any]] = []
    keywords: List[str] = []
    resume_match_filters: List[Dict[str, Any]] = []
    location: str = ""
    within_miles: int = 25
    companies: List[str] = []
    page_size: int = 100
    sources: List[str] = ["JobDiva", "LinkedIn", "Exa"]
    open_to_work: bool = True
    boolean_string: str = ""
    bypass_screening: bool = False

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


    def _log_stage(self, stage: str, message: str) -> None:
        logger.info("[CandidateSearch] %s | %s", stage, message)

    async def search_candidates(self, criteria: SearchCriteria):
        """
        Orchestrate candidate search across multiple providers with tiered JobDiva logic.
        Yields candidates as they are finalized.
        """
        start_time = time.time()
        self._log_stage("Start", f"job={criteria.job_id} sources={', '.join(criteria.sources or [])}")
        
        seen_ids = set()
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
            "qualified_talent": 0
        }

        def finalize_candidate(cand):
            """Apply match scoring to a candidate."""
            # Ensure name is title-cased if it exists
            if cand.get("name"):
                cand["name"] = str(cand["name"]).title()
            
            score_result = self._score_candidate(cand, criteria)
            cand["match_score"] = score_result["score"]
            cand["missing_skills"] = score_result["missing_skills"]
            cand["matched_skills"] = score_result.get("matched_skills", [])
            cand["explainability"] = score_result["explainability"]
            cand["match_score_details"] = score_result.get("score_details", {})
            return cand

        jobdiva_selected = "JobDiva" in criteria.sources
        talent_search_selected = "JobDiva-TalentSearch" in criteria.sources
        hotlist_selected = "JobDiva Hotlist" in criteria.sources

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
            cand["screening_summary"] = build_screening(assessment)
            cand = finalize_candidate(cand)
            cid = str(cand.get("candidate_id") or cand.get("id"))
            if cid and cid in seen_ids:
                return
            if cid:
                seen_ids.add(cid)
            if qualified_counter_key and assessment["passes"]:
                summary[qualified_counter_key] += 1
            summary["total_candidates"] += 1
            await queue.put({"type": "candidate", "data": cand})

        async def produce_jobdiva():
            try:
                if not jobdiva_selected:
                    return
                # External jobs (negative job_id or EXT- ref) have no JobDiva applicants.
                # Skip Applicants and go straight to Talent Search.
                job_id_str = str(criteria.job_id or "")
                is_external_job = job_id_str.startswith("-") or job_id_str.startswith("EXT-")

                if not is_external_job:
                    await queue.put({"type": "stage", "data": "Searching JobDiva applicants..."})
                    applicants_res = await self._search_jobdiva_applicants(criteria)
                    applicants = applicants_res.get("candidates", [])
                    summary["job_applicants_count"] = len(applicants)

                    if applicants:
                        self._log_stage("Applicants", f"Found {len(applicants)} applicants; starting resume screen...")
                        self._attach_cached_enhanced_info(applicants)
                        async for cand in self._enrich_filtered_jobdiva_candidates(applicants, criteria):
                            assessment = self._filter_assessment(cand, criteria, enforce_years=True)
                            if not assessment["passes"]:
                                self._log_stage(
                                    "Applicants",
                                    f"yielding unqualified candidate_id={cand.get('candidate_id')} missing={assessment['missing'][:3]} excluded={assessment['excluded'][:3]}",
                                )
                            await emit_candidate(cand, assessment, "qualified_applicants")
                    else:
                        self._log_stage("Applicants", "No applicants found.")
                else:
                    self._log_stage("Applicants", f"External job {job_id_str} — skipping Applicants; running Talent Search only.")

                if is_external_job or summary["qualified_applicants"] < 3:
                    await queue.put({"type": "stage", "data": "Searching JobDiva Talent Search..."})
                    reason = (
                        f"only {summary['qualified_applicants']} qualified applicants"
                        if summary["qualified_applicants"] > 0
                        else "no qualified applicants"
                    )
                    self._log_stage("TalentSearch", f"Triggering talent search: {reason}")
                    talent_res = await self._search_jobdiva_talent(criteria)
                    talent_pool = talent_res.get("candidates", [])
                    summary["talent_search_count"] = len(talent_pool)
                    if talent_pool:
                        self._attach_cached_enhanced_info(talent_pool)
                        async for cand in self._enrich_filtered_jobdiva_candidates(talent_pool, criteria):
                            assessment = self._filter_assessment(cand, criteria, enforce_years=True)
                            if not assessment["passes"]:
                                self._log_stage(
                                    "TalentSearch",
                                    f"yielding unqualified candidate_id={cand.get('candidate_id')} missing={assessment['missing'][:3]} excluded={assessment['excluded'][:3]}",
                                )
                            await emit_candidate(cand, assessment, "qualified_talent")

                if hotlist_selected:
                    await queue.put({"type": "stage", "data": "Searching JobDiva Hotlist..."})
                    self._log_stage("Hotlist", "Hotlist search requested but not yet implemented.")
            except Exception as e:
                logger.error(f"JobDiva search stage failed: {e}", exc_info=True)
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
                self._log_stage(source_type, f"Found {len(ext_candidates)} profiles; starting streaming enrichment...")

                semaphore = asyncio.Semaphore(5)

                async def _process_external_single(cand):
                    async with semaphore:
                        cand["source"] = source_type
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

        # Build producer tasks for all selected sources — run in parallel
        producers = []
        if jobdiva_selected or hotlist_selected:
            producers.append(asyncio.create_task(produce_jobdiva()))

        external_order = [
            ("LinkedIn", self._search_linkedin),
            ("Dice", self._search_dice),
            ("Exa", self._search_exa),
        ]
        for ext_name, ext_method in external_order:
            if ext_name in criteria.sources:
                producers.append(asyncio.create_task(produce_external(ext_name, ext_method)))

        # Drain the queue until every producer emits its SENTINEL
        active = len(producers)
        while active > 0:
            event = await queue.get()
            if event is SENTINEL:
                active -= 1
                continue
            yield event

        await asyncio.gather(*producers, return_exceptions=True)

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
                norm_item = self._normalize_term(item)
                if normalized == norm_item or normalized in norm_item or norm_item in normalized:
                    return True

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
        text_matches = sum(1 for word in term_words if word in profile_text)
        text_score = (text_matches / len(term_words)) * 0.35
        
        return max(best_overlap_score, text_score)

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
        """
        Heuristic assessment of whether a candidate profile matches sourcing criteria.
        Used for early pre-screening of JobDiva/LinkedIn profiles before full LLM enrichment.
        """
        profile = self._candidate_profile(candidate)
        missing: List[str] = []
        matched: List[str] = []
        excluded: List[str] = []

        if self._should_enforce_location(criteria) and not self._location_matches(candidate, criteria):
            missing.append(f"Location: {criteria.location}")

        for dimension in self._collect_sourcing_dimensions(criteria):
            collections = dimension["collections"]
            # Check exclusions - these are ALWAYS hard filters
            for group in dimension.get("excluded_groups", []):
                if self._term_group_matches(profile, group, collections):
                    excluded.append(f"{dimension['label']}: {self._group_label(group)}")

            # Check matches
            all_groups = dimension.get("required_groups", []) + dimension.get("preferred_groups", [])
            for group in all_groups:
                if self._term_group_matches(profile, group, collections):
                    matched.append(f"{dimension['label']}: {self._group_label(group)}")
                elif any(rg["label"] == group["label"] for rg in dimension.get("required_groups", [])):
                    # Only add to missing if it was a required group that didn't match
                    missing.append(f"{dimension['label']}: {self._group_label(group)}")

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
                "excluded": []
            }

        # Otherwise, enforce required groups (Standard strict scoring/filtering)
        return {
            "passes": not missing,
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

            dim_label = dimension["label"]
            for item in required_matches + preferred_matches:
                matched_required_skills.append(
                    item if dim_label == "Skills" else f"{dim_label}: {item}"
                )

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
            
            # Avoid duplicate identical groups in the same dimension
            existing_groups = dimensions[bucket][f"{target}_groups"]
            if any(set(g["terms"]) == set(clean_values) and g["label"] == (label or clean_values[0]) for g in existing_groups):
                return

            dimensions[bucket][f"{target}_groups"].append({
                "terms": clean_values,
                "label": label or clean_values[0],
                "years": years or 0,
                "recent": recent,
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

            if "customer" in category or raw_value.lower().startswith("must not"):
                add_terms("companies", "exclude", [term])
            elif "title" in category:
                add_terms("titles", "can" if "preferred" in category or raw_value.lower().startswith("can ") else "must", [term])
            elif "skill" in category:
                add_terms("skills", "can" if "preferred" in category or raw_value.lower().startswith("can ") else "must", [term])
            elif "edu" in category:
                add_terms("education", "can" if "preferred" in category or raw_value.lower().startswith("can ") else "must", [term])
            elif "cert" in category or "license" in category:
                add_terms("certifications", "can" if "preferred" in category or raw_value.lower().startswith("can ") else "must", [term])
            elif "domain" in category:
                add_terms("companies", "can", [term])
                add_terms("keywords", "can", [term])
            elif "local" in term.lower() or "location" in category:
                add_terms("location", "must", [term])
            else:
                add_terms("keywords", "must", [term])

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
        counters = {"screened": 0, "skipped": 0, "no_resume": 0, "failed_filter": 0}

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

                    self._log_stage("ResumeScreen", f"running quick filter for candidate_id={candidate_id}")
                    assessment = self._filter_assessment(candidate, criteria, enforce_years=False)
                    if not assessment["passes"]:
                        self._log_stage(
                            "ResumeScreen",
                            "FAILED FILTER candidate_id=%s matched=%s missing=%s excluded=%s" % (
                                candidate_id,
                                assessment["matched"][:5],
                                assessment["missing"][:5],
                                assessment["excluded"][:5],
                            ),
                        )
                        return {"status": "failed_filter", "candidate": None}

                    self._log_stage(
                        "ResumeScreen",
                        "PASSED FILTER candidate_id=%s matched=%s - proceeding to LLM" % (
                            candidate_id,
                            assessment["matched"][:5],
                        ),
                    )
                    self._log_stage("LLM", f"STARTING LLM extraction for candidate_id={candidate_id}, resume_id={candidate.get('resume_id') or 'unknown'}")
                    
                    enhanced = await process_jobdiva_candidate(candidate)
                    if isinstance(enhanced, dict) and enhanced is not candidate:
                        candidate["enhanced_info"] = enhanced.get("raw", enhanced)
                    else:
                        candidate["enhanced_info"] = {}
                        
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
                yield result["candidate"]
            elif status == "no_resume":
                counters["no_resume"] += 1
                counters["skipped"] += 1
            elif status == "failed_filter":
                counters["failed_filter"] += 1
                counters["skipped"] += 1
            elif status == "skipped":
                counters["skipped"] += 1

        self._log_stage(
            "ResumeScreen",
            "RESULTS: kept %s of %s JobDiva candidate(s); skipped %s total (no_resume=%s, failed_filter=%s)" % (
                counters["screened"],
                len(jobdiva_candidates),
                counters["skipped"],
                counters["no_resume"],
                counters["failed_filter"],
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
            # Unipile expects skills as a list of dicts or strings. Derive from
            # skill_criteria + title_criteria so callers don't have to send a
            # redundant flat list.
            skill_values = criteria.sourcing_skill_values()
            skills = [{"value": s, "priority": "Must Have"} for s in skill_values]
            candidates = await self.unipile_service.search_candidates(
                skills=skills,
                location=criteria.location,
                open_to_work=criteria.open_to_work,
                limit=criteria.page_size,
                boolean_string=criteria.boolean_string or self._build_boolean_string(criteria)
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

    async def _search_dice(self, criteria: SearchCriteria) -> Dict[str, Any]:
        try:
            skills_values = criteria.sourcing_skill_values()
            boolean_string = criteria.boolean_string or self._build_boolean_string(criteria)
            candidates = await self.exa_service.search_dice_candidates(
                skills=skills_values,
                location=criteria.location,
                limit=min(criteria.page_size, 20),
                boolean_string=boolean_string,
            )
            return {"candidates": candidates, "source_type": "Dice"}
        except Exception as e:
            logger.error(f"Dice search failed: {e}")
            return {"candidates": [], "source_type": "Dice"}

    async def _search_vetted(self, criteria: SearchCriteria) -> Dict[str, Any]:
        try:
            candidates = await self.vetted_service.search_candidates(
                skills=criteria.sourcing_skill_values(),
                location=criteria.location,
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
                location=criteria.location,
                limit=min(criteria.page_size, 20),
                boolean_string=boolean_string,
            )
            return {"candidates": candidates, "source_type": "LinkedIn-Exa"}
        except Exception as e:
            logger.error(f"Exa search failed: {e}")
            return {"candidates": [], "source_type": "LinkedIn-Exa"}

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
