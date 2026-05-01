import logging
import re
from typing import List, Dict, Any, Tuple
from core.config import EXA_API_KEY
from exa_py import Exa

logger = logging.getLogger(__name__)


_RELOCATION_PATTERNS = re.compile(
    r"\b("
    r"open\s+to\s+relocat\w+"
    r"|willing\s+to\s+relocat\w+"
    r"|will\s+relocate"
    r"|relocat\w+\s+(?:available|negotiable|possible|preferred)"
    r"|open\s+to\s+(?:travel|remote|hybrid|relocation)"
    r"|any\s+location"
    r"|remote\s+only"
    r")\b",
    re.IGNORECASE,
)

# US state codes; used to validate "City, ST" extractions.
_US_STATE_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI",
    "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN",
    "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH",
    "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA",
    "WV", "WI", "WY", "PR",
}

_LOCATED_IN_RE = re.compile(
    r"\b(?:Located|Based|Lives|Living)\s+in\s+([A-Z][a-zA-Z\.\- ]{1,30}),\s*([A-Z]{2})\b"
)
_CITY_STATE_RE = re.compile(r"\b([A-Z][a-zA-Z\.\- ]{1,30}),\s*([A-Z]{2})\b")


def _detect_relocation(text: str) -> bool:
    """True when highlights/resume text signals open-to-relocation/remote."""
    if not text:
        return False
    return bool(_RELOCATION_PATTERNS.search(text))


def _extract_city_from_highlights(text: str) -> Tuple[str, str]:
    """Best-effort city/state extraction from Exa highlight text.

    Priority: explicit "Located/Based/Lives in CITY, ST" phrasing. Fallback:
    first "City, ST" hit in the first ~200 chars where ST is a US code.
    Returns ("", "") on miss — callers MUST treat empty as "unknown" rather
    than substituting the query string.
    """
    if not text:
        return "", ""
    m = _LOCATED_IN_RE.search(text)
    if m:
        return m.group(1).strip(), m.group(2).strip().upper()
    head = text[:200]
    for cand in _CITY_STATE_RE.finditer(head):
        st = cand.group(2).strip().upper()
        if st in _US_STATE_CODES:
            return cand.group(1).strip(), st
    return "", ""


def _exa_query_from_boolean(boolean_string: str, skills: List[str], location: str, role_hint: str = "") -> str:
    """Build an Exa-friendly query.

    Exa's `type="auto"` handles a raw boolean string as free text reasonably
    well — AND/OR/NOT survive as word tokens and quoted phrases still bias
    matches. When no boolean is provided, fall back to the skills+location
    heuristic that Dice/LinkedIn-Exa used previously.
    """
    bs = (boolean_string or "").strip()
    if bs:
        # Drop ` within N mi` radius hints — Exa can't act on them and they
        # introduce noise. Location (if present) still appears as a quoted
        # phrase elsewhere in the boolean.
        cleaned = re.sub(r'\s+within\s+\d+\s*mi\b', '', bs, flags=re.IGNORECASE)
        return cleaned.strip()

    skills_str = ", ".join(skills) if skills else ""
    prefix = role_hint or "candidate"
    query = f"{prefix} {skills_str}".strip()
    if location:
        query += f" located in {location}"
    return query

class ExaService:
    def __init__(self):
        self.api_key = EXA_API_KEY
        self.exa = None
        if self.api_key:
            try:
                self.exa = Exa(api_key=self.api_key)
            except Exception as e:
                logger.error(f"Failed to initialize Exa SDK: {e}")

    async def search_candidates(self, skills: List[str], location: str, limit: int = 10, boolean_string: str = "") -> List[Dict[str, Any]]:
        if not self.exa:
            logger.warning("Exa API key is not set. Skipping Exa search.")
            return []

        try:
            query = _exa_query_from_boolean(
                boolean_string, skills, location,
                role_hint="software engineer OR developer",
            )

            logger.info(f"Executing Exa people search for query: {query}")
            
            # Note: the python SDK's search method supports synchronous wrapper? 
            # If exa_py is sync, we should probably run it in an executor, but we can try it directly.
            # Using type="auto" as recommended in the config for most queries
            import asyncio
            loop = asyncio.get_event_loop()
            
            def do_search():
                return self.exa.search_and_contents(
                    query,
                    category="people",
                    type="auto",
                    num_results=limit,
                    highlights={"max_characters": 4000}
                )

            # Wait for sync search call
            response = await loop.run_in_executor(None, do_search)
            
            results = []
            if response and hasattr(response, 'results'):
                for idx, result in enumerate(response.results):
                    # Exa returns title, url, author, id. 
                    # Often for people search, the title contains their name or headline.
                    title = getattr(result, 'title', 'Unknown Candidate')
                    url = getattr(result, 'url', '')
                    
                    # Try to separate first and last name from the title
                    name_parts = title.split(" - ")[0].split("|")[0].strip().split(" ")
                    first_name = name_parts[0] if len(name_parts) > 0 else "Unknown"
                    last_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""
                    
                    # Store highlights if any
                    highlights_text = ""
                    if getattr(result, 'highlights', None):
                        highlights_text = "\n".join(result.highlights)
                    
                    extracted_city, extracted_state = _extract_city_from_highlights(highlights_text)
                    cand = {
                        "id": f"exa_{idx}_{getattr(result, 'id', idx)}",
                        "provider_id": getattr(result, 'id', f"exa_{idx}"),
                        "firstName": first_name,
                        "lastName": last_name,
                        "email": "",
                        # Empty when unknown — never substitute the query string
                        # (causes false-positive radius matches downstream).
                        "city": extracted_city,
                        "state": extracted_state,
                        "title": title,
                        "source": "LinkedIn-Exa",
                        "match_score": 0,
                        "profile_url": url,
                        "image_url": "",
                        "open_to_work": False,
                        "open_to_relocation": _detect_relocation(highlights_text),
                        "resume_text": highlights_text,
                        "recruiter_candidate_id": None
                    }
                    results.append(cand)
                    
            logger.info(f"Exa search returned {len(results)} candidates.")
            return results

        except Exception as e:
            logger.error(f"Exa search failed: {e}")
            return []

    async def search_dice_candidates(self, skills: List[str], location: str, limit: int = 10, boolean_string: str = "") -> List[Dict[str, Any]]:
        """
        Search Dice (dice.com) profiles via Exa with domain filtering.
        Dice hosts tech candidate profiles publicly indexable by Exa; we scope
        the people-search to dice.com to pull those records.
        """
        if not self.exa:
            logger.warning("Exa API key is not set. Skipping Dice search.")
            return []

        try:
            import asyncio
            query = _exa_query_from_boolean(
                boolean_string, skills, location,
                role_hint="resume profile",
            )

            logger.info(f"Executing Dice (via Exa) search for query: {query}")
            loop = asyncio.get_event_loop()

            def do_search():
                return self.exa.search_and_contents(
                    query,
                    category="people",
                    type="auto",
                    num_results=limit,
                    include_domains=["dice.com"],
                    highlights={"max_characters": 4000},
                )

            response = await loop.run_in_executor(None, do_search)

            results = []
            if response and hasattr(response, "results"):
                for idx, result in enumerate(response.results):
                    title = getattr(result, "title", "Unknown Candidate")
                    url = getattr(result, "url", "")
                    name_parts = title.split(" - ")[0].split("|")[0].strip().split(" ")
                    first_name = name_parts[0] if name_parts else "Unknown"
                    last_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""
                    highlights_text = ""
                    if getattr(result, "highlights", None):
                        highlights_text = "\n".join(result.highlights)
                    extracted_city, extracted_state = _extract_city_from_highlights(highlights_text)
                    results.append({
                        "id": f"dice_{idx}_{getattr(result, 'id', idx)}",
                        "provider_id": getattr(result, "id", f"dice_{idx}"),
                        "firstName": first_name,
                        "lastName": last_name,
                        "email": "",
                        "city": extracted_city,
                        "state": extracted_state,
                        "title": title,
                        "source": "Dice",
                        "match_score": 0,
                        "profile_url": url,
                        "image_url": "",
                        "open_to_work": False,
                        "open_to_relocation": _detect_relocation(highlights_text),
                        "resume_text": highlights_text,
                        "recruiter_candidate_id": None,
                    })

            logger.info(f"Dice-via-Exa returned {len(results)} candidates.")
            return results

        except Exception as e:
            logger.error(f"Dice (via Exa) search failed: {e}")
            return []

exa_service = ExaService()
