"""UnifiedCandidateSearch methods: dice."""
from typing import Dict, Any
from services.exa_errors import ExaOutOfCreditsError
from services.jobdiva_boolean_translator import strip_jobdiva_dialect
from ..common import SearchCriteria, logger


class DiceMixin:

    async def _search_dice(self, criteria: SearchCriteria) -> Dict[str, Any]:
        if criteria.additional_locations:
            return await self._fan_out_multi_location_search(
                criteria, self._search_dice, "Dice",
                dedupe_key=self._search_location_for_source,
            )
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
        except ExaOutOfCreditsError:
            raise  # produce_external reports it as the source's status
        except Exception as e:
            logger.error(f"Dice search failed: {e}")
            return {"candidates": [], "source_type": "Dice"}

    async def _search_vetted(self, criteria: SearchCriteria) -> Dict[str, Any]:
        if criteria.additional_locations:
            return await self._fan_out_multi_location_search(
                criteria, self._search_vetted, "VettedDB"
            )
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
