"""UnifiedCandidateSearch methods: exa."""
from typing import Dict, Any
from services.exa_errors import ExaOutOfCreditsError
from services.jobdiva_boolean_translator import strip_jobdiva_dialect
from ..common import SearchCriteria, logger


class ExaMixin:

    async def _search_exa(self, criteria: SearchCriteria) -> Dict[str, Any]:
        if criteria.additional_locations:
            return await self._fan_out_multi_location_search(
                criteria, self._search_exa, "LinkedIn-Exa",
                dedupe_key=self._search_location_for_source,
            )
        try:
            # Floor at 30 so the Exa Research Pass B has a meaningful seed-URL
            # sample even when the recruiter's page_size is small, and cap at
            # 50 (Exa's per-call sweet spot for people search before relevance
            # falls off). Without the floor, page_size=10 → only 10 LinkedIn
            # candidates surface and the deep-search pass discovers little.
            # Not shrunk for sample mode on purpose: the sample run asks Exa
            # exactly what the full run will, so the full run is served from
            # the Pass A search cache (services/exa_search_cache.py).
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
        except ExaOutOfCreditsError:
            raise  # produce_external reports it as the source's status
        except Exception as e:
            logger.error(f"Exa search failed: {e}")
            return {"candidates": [], "source_type": "LinkedIn-Exa"}
