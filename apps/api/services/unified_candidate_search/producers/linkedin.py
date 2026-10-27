"""UnifiedCandidateSearch methods: linkedin."""
import asyncio
from typing import List, Dict, Any, Callable, Optional
from services.exa_errors import ExaOutOfCreditsError
from services.jobdiva_boolean_translator import strip_jobdiva_dialect
from ..common import SearchCriteria, logger


class LinkedinMixin:

    async def _fan_out_multi_location_search(
        self,
        criteria: SearchCriteria,
        single_loc_fn: Callable,
        source_type: str,
        dedupe_key: Optional[Callable[[SearchCriteria], Any]] = None,
    ) -> Dict[str, Any]:
        """Fan out a single-location search function over all location variants.

        Uses asyncio.gather with bounded concurrency (3 parallel calls) to
        protect latency and provider quota. Deduplicates the merged results.
        Capped at _MAX_LOCATIONS total variants.

        ``dedupe_key``: variants it maps to the same value would send the
        provider the same request, so only the first is searched. The Exa
        sources pass ``_search_location_for_source``: a Remote job searches
        its country whatever the location, so its L locations used to buy the
        same Exa searches L times for identical results.
        """
        variants = self._location_criteria_variants(criteria)
        if dedupe_key is not None:
            distinct: List[SearchCriteria] = []
            seen_keys: set = set()
            for variant in variants:
                try:
                    key = dedupe_key(variant)
                except Exception:
                    key = None  # unknown: search it
                if key is not None and key in seen_keys:
                    continue
                if key is not None:
                    seen_keys.add(key)
                distinct.append(variant)
            if len(distinct) < len(variants):
                logger.info(
                    "%s multi-location: %d locations, %d distinct searches",
                    source_type, len(variants), len(distinct),
                )
            variants = distinct
        _sem = asyncio.Semaphore(3)
        credits_errors: List[ExaOutOfCreditsError] = []

        async def _guarded(variant: SearchCriteria) -> Dict[str, Any]:
            async with _sem:
                try:
                    return await single_loc_fn(variant)
                except ExaOutOfCreditsError as exc:
                    # Exa sources only. Keep the other locations' rows; raise
                    # (below) only when there are none.
                    credits_errors.append(exc)
                    return {}

        results = await asyncio.gather(*[_guarded(v) for v in variants])
        merged: List[Dict[str, Any]] = []
        for r in results:
            merged.extend(r.get("candidates") or [])
        if credits_errors and not merged:
            raise credits_errors[0]
        return {
            "candidates": self._deduplicate_candidates(merged),
            "source_type": source_type,
        }

    async def _search_linkedin(self, criteria: SearchCriteria) -> Dict[str, Any]:
        if criteria.additional_locations:
            return await self._fan_out_multi_location_search(
                criteria, self._search_linkedin, "LinkedIn-Unipile"
            )
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

        # Public vanity URL from the full profile. Recruiter search rows
        # occasionally lack `public_profile_url` (~2%); the profile payload
        # usually has it (or the bare `public_identifier` slug). Only a
        # public `/in/` URL is ever written — the Recruiter deep link is
        # RPS-only and lives in `recruiter_profile_url`.
        _public = str(profile.get("public_profile_url") or "").strip()
        if not _public:
            _ident = str(profile.get("public_identifier") or "").strip().strip("/")
            if _ident and "/" not in _ident and " " not in _ident:
                _public = f"https://www.linkedin.com/in/{_ident}"
        if "linkedin.com/in/" in _public.lower():
            extracted["profile_url"] = _public

        # Positive open-to-work signal from the profile itself; absence is
        # left unset so the Apify resolver still runs for the row.
        if profile.get("is_open_to_work") is True:
            extracted["open_to_work"] = True

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
                    "year": edu.get("end_date", edu.get("end", edu.get("year", "")))
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
                    # Unipile's `linkedin_sections=*` payload names the
                    # issuer `organization`.
                    "issuer": cert.get("authority", cert.get("issuer", cert.get("organization", ""))),
                    "year": cert.get("issue_date", cert.get("start", cert.get("year", "")))
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
