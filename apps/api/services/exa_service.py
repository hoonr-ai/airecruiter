import asyncio
import logging
import os
import re
from typing import List, Dict, Any, Optional, Tuple
from core.config import EXA_API_KEY
from exa_py import Exa
from services.location import extract_us_location_from_text

logger = logging.getLogger(__name__)


def _normalize_linkedin_url(url: str) -> str:
    """Stable key for a LinkedIn profile: scheme/host stripped, lowercased,
    query/fragment dropped, trailing slash removed. Used to dedupe Exa
    results across runs where the same profile can come back at different
    result positions."""
    if not url:
        return ""
    u = str(url).strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    u = u.split("?", 1)[0].split("#", 1)[0]
    u = u.rstrip("/")
    return u


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

# City: 1-3 capitalised words, allowing hyphens and single spaces, NO
# embedded periods. Disallowing periods stops the regex from bridging
# sentence boundaries ("Jane Doe - Product Manager. Plano" used to match
# "Doe - Product Manager. Plano" as a city name).
_CITY_PAT = r"[A-Z][a-zA-Z]+(?:[\- ][A-Z][a-zA-Z]+){0,2}"
_LOCATED_IN_RE = re.compile(
    r"\b(?:Located|Based|Lives|Living|Currently|Resides|Resident|From|Headquartered)"
    r"(?:\s+(?:in|out\s+of|at|near))?\s+"
    rf"({_CITY_PAT}),\s*([A-Z]{{2}})\b"
)
_CITY_STATE_RE = re.compile(rf"\b({_CITY_PAT}),\s*([A-Z]{{2}})\b")
# LinkedIn-style "City, State Area" (e.g., "Greater Denver Area", "Dallas, Texas Area").
# Matched without anchoring to a verb so it catches the common header pattern
# where the city sits alone with no "Located in" preamble.
_AREA_RE = re.compile(
    rf"\b(?:Greater\s+)?({_CITY_PAT})(?:,\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?))?\s+(?:Metropolitan\s+)?Area\b"
)
# Full state names → 2-letter codes, used when the highlight uses e.g.
# "Dallas, Texas" instead of "Dallas, TX".
_US_STATE_NAMES_TO_CODE = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC",
}
_CITY_STATE_NAME_RE = re.compile(
    rf"\b({_CITY_PAT}),\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){{0,2}})\b"
)


def _detect_relocation(text: str) -> bool:
    """True when highlights/resume text signals open-to-relocation/remote."""
    if not text:
        return False
    return bool(_RELOCATION_PATTERNS.search(text))


def _extract_city_from_highlights(text: str) -> Tuple[str, str]:
    """Best-effort city/state extraction from Exa highlight text.

    Tries multiple LinkedIn-style patterns in order of confidence:
      1. "Located/Based/Lives/Currently/Resides in CITY, ST"
      2. "City, ST" with a real US state code in the first ~400 chars
      3. "City, FullStateName" (e.g. "Dallas, Texas") — normalises to ST
      4. "Greater <City> Area" / "<City>, <State> Area" — LinkedIn header
      5. Fallback to `extract_us_location_from_text` and split its result

    Returns ("", "") on miss — callers MUST treat empty as "unknown" rather
    than substituting the query string.
    """
    if not text:
        return "", ""

    # 1. Strict "Located/Based/Lives/etc. in CITY, ST"
    m = _LOCATED_IN_RE.search(text)
    if m:
        st = m.group(2).strip().upper()
        if st in _US_STATE_CODES:
            return m.group(1).strip(), st

    # 2. "City, ST" with a valid US state code — widened from 200 → 400 chars
    head = text[:400]
    for cand in _CITY_STATE_RE.finditer(head):
        st = cand.group(2).strip().upper()
        if st in _US_STATE_CODES:
            return cand.group(1).strip(), st

    # 3. "City, FullStateName" — normalise to (City, ST)
    for cand in _CITY_STATE_NAME_RE.finditer(head):
        state_name = cand.group(2).strip().lower()
        code = _US_STATE_NAMES_TO_CODE.get(state_name)
        if code:
            return cand.group(1).strip(), code

    # 4. "Greater <City> Area" / "<City>, <State> Area" (LinkedIn header)
    for cand in _AREA_RE.finditer(head):
        city = cand.group(1).strip()
        state_token = (cand.group(2) or "").strip().lower()
        code = _US_STATE_NAMES_TO_CODE.get(state_token) if state_token else ""
        if city:
            return city, code or ""

    # 5. Delegate to the broader helper (used by Step-5 elsewhere) and split.
    full = extract_us_location_from_text(text)
    if full and "," in full:
        city_part, state_part = full.split(",", 1)
        state_token = state_part.strip()
        # extract_us_location_from_text returns either "City, ST" (US) or
        # "City, Country" (non-US). Only accept the US form here.
        if state_token.upper() in _US_STATE_CODES:
            return city_part.strip(), state_token.upper()
        code = _US_STATE_NAMES_TO_CODE.get(state_token.lower())
        if code:
            return city_part.strip(), code

    return "", ""


def _strip_zip_for_query(location: str) -> str:
    """Drop zip codes from a location destined for Exa query/prompt text.

    LinkedIn profile location lines never show zips ("Tempe, Arizona,
    United States"), so a zip in the neural query is pure noise. The zip
    still drives the offline radius post-filter — it just doesn't belong
    in the NL text.
    """
    cleaned = re.sub(r"\b\d{5}(?:-\d{4})?\b", "", str(location or ""))
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    cleaned = re.sub(r"\s+,", ",", cleaned)
    cleaned = re.sub(r",\s*,", ",", cleaned)
    return cleaned.strip(" ,")


def _exa_query_from_boolean(boolean_string: str, skills: List[str], location: str, role_hint: str = "") -> str:
    """Build an Exa-friendly query.

    Exa's `type="auto"` handles a raw boolean string as free text reasonably
    well — AND/OR/NOT survive as word tokens and quoted phrases still bias
    matches. When no boolean is provided, fall back to the skills+location
    heuristic that Dice/LinkedIn-Exa used previously.

    When a boolean is provided we still re-append top must-have skills + the
    primary location if they're missing from the boolean — defends against
    upstream boolean builders that drop skills or location, which manifested
    in production as Exa results ignoring the user's stated requirements.
    """
    bs = (boolean_string or "").strip()
    if bs:
        # Drop ` within N mi` radius hints — Exa can't act on them and they
        # introduce noise. Location (if present) still appears as a quoted
        # phrase elsewhere in the boolean.
        cleaned = re.sub(r'\s+within\s+\d+\s*mi\b', '', bs, flags=re.IGNORECASE).strip()
        lower_bs = cleaned.lower()
        # Top-5 skills only — caps query length around Exa's quality knee
        # (~512 chars). Skills already in the boolean are skipped to avoid
        # duplication.
        if skills:
            for skill in skills[:5]:
                if not skill:
                    continue
                token = skill.strip()
                if not token:
                    continue
                if re.search(rf'\b{re.escape(token.lower())}\b', lower_bs):
                    continue
                cleaned = f'{cleaned} AND "{token}"'
                lower_bs = cleaned.lower()
        # Match the location only on its city portion (`split(",", 1)[0]`)
        # so a boolean that already says "located in New York" won't get
        # "New York, NY" appended on top of it.
        loc = _strip_zip_for_query(location)
        if loc:
            city_token = loc.split(",", 1)[0].strip().lower()
            if city_token and city_token not in lower_bs:
                cleaned = f'{cleaned} located in {loc}'
        return cleaned

    skills_str = ", ".join(skills) if skills else ""
    prefix = role_hint or "candidate"
    query = f"{prefix} {skills_str}".strip()
    location = _strip_zip_for_query(location)
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

    async def search_candidates(self, skills: List[str], location: str, limit: int = 10, boolean_string: str = "", role_hint: str = "") -> List[Dict[str, Any]]:
        if not self.exa:
            logger.warning("Exa API key is not set. Skipping Exa search.")
            return []

        try:
            query = _exa_query_from_boolean(
                boolean_string, skills, location,
                role_hint=role_hint,
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
                    extracted_location = extract_us_location_from_text(
                        f"{title}\n{highlights_text}"
                    )
                    if not extracted_location and (extracted_city or extracted_state):
                        extracted_location = ", ".join(p for p in [extracted_city, extracted_state] if p)

                    # Stable id: derived from the profile URL so the same
                    # LinkedIn profile returned at different result positions
                    # across runs upserts onto the same sourced_candidates row
                    # via UNIQUE(jobdiva_id, candidate_id, source).
                    stable_key = (
                        _normalize_linkedin_url(url)
                        or getattr(result, 'id', None)
                        or f"exa_{idx}"
                    )
                    cand = {
                        "id": f"exa_{stable_key}",
                        "provider_id": getattr(result, 'id', f"exa_{idx}"),
                        "firstName": first_name,
                        "lastName": last_name,
                        "email": "",
                        "city": extracted_city,
                        "state": extracted_state,
                        "location": extracted_location,
                        "title": title,
                        "source": "LinkedIn-Exa",
                        "match_score": 0,
                        "profile_url": url,
                        "image_url": "",
                        # open_to_work intentionally omitted here — populated
                        # asynchronously by services.apify_open_to_work via
                        # unified_candidate_search._search_exa.
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

    async def deep_analyze_candidate(
        self,
        url: str,
        query_skills: List[str],
        query_location: str,
    ) -> Dict[str, str]:
        """Fetch Exa contents + a targeted match-rationale summary for one URL.

        Returns {'text': str (≤8000 chars), 'summary': str} on success or `{}`
        on any failure (missing SDK, timeout, HTTP error, parse error).

        Billing is per-URL on Exa's side, so callers should gate this to
        post-filter survivors rather than calling for every search hit.
        """
        if not self.exa or not url:
            return {}

        skills_csv = ", ".join((query_skills or [])[:8]) or "(no skills)"
        location_str = (query_location or "").strip() or "(no location filter)"
        summary_query = (
            f"Does this candidate match: {skills_csv} located in {location_str}? "
            "Pull years of experience, current title, current location, and a "
            "brief match rationale."
        )

        loop = asyncio.get_event_loop()

        def do_contents() -> Any:
            return self.exa.get_contents(
                urls=[url],
                text=True,
                summary={"query": summary_query},
                livecrawl="fallback",
            )

        try:
            response = await asyncio.wait_for(
                loop.run_in_executor(None, do_contents),
                timeout=25.0,
            )
        except asyncio.TimeoutError:
            logger.warning("Exa deep_analyze timeout for %s", url)
            return {}
        except Exception as e:
            logger.warning("Exa deep_analyze failed for %s: %s", url, e)
            return {}

        try:
            results = getattr(response, "results", None) or []
            if not results:
                return {}
            r0 = results[0]
            text = getattr(r0, "text", "") or ""
            summary = getattr(r0, "summary", "") or ""
        except Exception as e:
            logger.warning("Exa deep_analyze parse failed for %s: %s", url, e)
            return {}

        return {
            "text": (text or "")[:8000],
            "summary": summary or "",
        }

    async def deep_research_candidates(
        self,
        jd_title: str,
        jd_role: str,
        skills: List[str],
        location: str,
        seed_urls: List[str],
        within_miles: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Exa Agent API (Websets 2.0) pass — agentic enrichment + discovery.

        Calls `exa.beta.agent.runs.create(...)` with:
          - `query`: JD-focused natural-language task description.
          - `input.data`: seed URLs from Pass A as first-class input records
            (no 4096-char instructions limit, no string mangling).
          - `output_schema`: JSON Schema for the 4 structured fields.
          - `effort`: cost cap (`low`/`medium`/`high`/`xhigh`/`auto`).
          - `betas=["agent-2026-05-07"]`: required to access the beta surface.

        Polls the run to completion via `poll_until_finished`. Returns
        `output.structured["candidates"]` on success, `[]` on any failure
        (timeout, beta-not-available, schema mismatch, etc.).
        """
        if not self.exa:
            logger.warning("Exa API key is not set. Skipping Agent deep search.")
            return []

        # Read knobs from env at call time so per-deploy tuning doesn't
        # require a process restart.
        effort = (os.getenv("EXA_AGENT_EFFORT", "medium").strip().lower() or "medium")
        if effort not in {"low", "medium", "high", "xhigh", "auto"}:
            logger.warning("EXA_AGENT_EFFORT=%r is invalid; falling back to 'medium'", effort)
            effort = "medium"
        try:
            timeout_s = int(os.getenv("EXA_AGENT_TIMEOUT", "180").strip() or "180")
        except ValueError:
            timeout_s = 180
        try:
            max_input = int(os.getenv("EXA_AGENT_MAX_INPUT", "25").strip() or "25")
        except ValueError:
            max_input = 25

        # Soft-warn if a stale EXA_AGENT_MODEL is set from the prior Research
        # API implementation. It's ignored now — kept here so operators don't
        # think the agent is still on the old path.
        if os.getenv("EXA_AGENT_MODEL"):
            logger.info(
                "EXA_AGENT_MODEL is set but ignored — Agent API uses EXA_AGENT_EFFORT instead."
            )

        top_skills = ", ".join((skills or [])[:5]) or "(any)"
        seeds = [u for u in (seed_urls or []) if u][:max_input]

        # Natural-language task. URLs go into `input.data`, not the query.
        #
        # Tone shift vs first version: every "or null" hint was making the
        # agent give up on follower_count / last_activity the moment they
        # weren't on the first page of search results. Now we explicitly
        # mark them REQUIRED-TO-ATTEMPT and tell the agent how to find them
        # (visit the linkedin profile page, search "<name> linkedin
        # followers", etc.). Schema still allows null so the agent doesn't
        # fail validation when a profile genuinely doesn't expose the data,
        # but the prose strongly biases toward "go look".
        query_location = _strip_zip_for_query(location)
        radius_hint = ""
        if query_location and query_location.lower() not in ("united states", "usa", "us"):
            radius = max(1, min(100, int(within_miles or 25)))
            radius_hint = (
                f"LOCATION REQUIREMENT: candidates must CURRENTLY live in or within "
                f"~{radius} miles of {query_location}. Verify against the LinkedIn "
                "profile's own location line — a past job, employer HQ, or university "
                "in that city does NOT count. Prefer verified-local candidates; if you "
                "cannot verify, still include the candidate but report the location "
                "string their profile actually shows. "
            )
        query = (
            f"Find LinkedIn profiles matching: title=\"{jd_title}\" | role=\"{jd_role}\" "
            f"| skills=\"{top_skills}\" | location=\"{query_location}\". "
            + radius_hint +
            "Also enrich every profile passed in `input.data`. "
            "For each candidate, you MUST attempt to extract ALL of the following — "
            "treat null as a last resort, not a default:\n"
            "  1. follower_count: visit the candidate's LinkedIn profile page and "
            "read the visible follower count (e.g. '226 followers', '11,978,553 "
            "followers'). If the page redirects to a sign-in wall, search the web "
            "for '\"<candidate name>\" linkedin followers' and parse the number "
            "from any cached snippet or third-party profile aggregator.\n"
            "  2. last_activity: scan the activity tab or recent posts section "
            "of the LinkedIn profile. Report the most recent post/comment/repost "
            "with a relative date (e.g. '3 days ago', '2 weeks ago', or an ISO "
            "date). Only return null if the profile shows zero activity in the "
            "last 12 months.\n"
            "  3. recent_companies: last 2 positions, each with company, title, "
            "start (YYYY-MM), end (YYYY-MM or 'Present').\n"
            f"  4. fit_rationale: one sentence (≤300 chars) on why this candidate's "
            f"titles fit the role \"{jd_role}\".\n"
            "  5. location: the candidate's CURRENT residence exactly as the "
            "LinkedIn profile's location line shows it (e.g. 'Tempe, Arizona, "
            "United States' or 'Greater Phoenix Area'). Never substitute a "
            "company HQ or a past position's city; leave empty only if the "
            "profile shows no location at all.\n"
            "Return at least 30 candidates with linkedin_url populated. Do not "
            "drop candidates just because one field is hard to find — partial "
            "enrichment is better than skipping them."
        )

        # Pass A URLs as first-class input records — the Agent API processes
        # them in addition to its own search-driven discovery.
        agent_input: Optional[Dict[str, Any]] = None
        if seeds:
            agent_input = {"data": [{"linkedin_url": u} for u in seeds]}

        output_schema = {
            "type": "object",
            "required": ["candidates"],
            "properties": {
                "candidates": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["linkedin_url", "fit_rationale"],
                        "properties": {
                            "linkedin_url": {"type": "string"},
                            "name": {"type": "string"},
                            "current_title": {"type": "string"},
                            "location": {
                                "type": "string",
                                "description": (
                                    "Candidate's CURRENT residence from the LinkedIn "
                                    "profile's own location line, e.g. 'Tempe, Arizona, "
                                    "United States' or 'Greater Phoenix Area'. Not a "
                                    "company HQ, not a past position's city. Empty "
                                    "string if the profile shows no location."
                                ),
                            },
                            "last_activity": {"type": ["string", "null"]},
                            "follower_count": {"type": ["integer", "null"]},
                            "recent_companies": {
                                "type": "array",
                                "maxItems": 2,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "company": {"type": "string"},
                                        "title": {"type": "string"},
                                        "start": {"type": "string"},
                                        "end": {"type": "string"},
                                    },
                                },
                            },
                            "fit_rationale": {"type": "string"},
                        },
                    },
                },
            },
        }

        loop = asyncio.get_event_loop()
        betas = ["agent-2026-05-07"]

        def do_create():
            return self.exa.beta.agent.runs.create(
                betas=betas,
                query=query,
                input=agent_input,
                output_schema=output_schema,
                effort=effort,
            )

        def do_poll(rid: str):
            return self.exa.beta.agent.runs.poll_until_finished(
                rid,
                betas=betas,
                poll_interval=2000,
                timeout_ms=timeout_s * 1000,
            )

        try:
            logger.info(
                "Exa Agent create: effort=%s seeds=%d title=%r role=%r",
                effort, len(seeds), jd_title, jd_role,
            )
            created = await asyncio.wait_for(
                loop.run_in_executor(None, do_create),
                timeout=30.0,
            )
            run_id = getattr(created, "id", None)
            if not run_id:
                logger.warning("Exa Agent create returned no run id; response=%r", created)
                return []
        except asyncio.TimeoutError:
            logger.warning("Exa Agent create timed out")
            return []
        except Exception as e:
            logger.warning("Exa Agent create failed: %s", e)
            return []

        try:
            completed = await asyncio.wait_for(
                loop.run_in_executor(None, do_poll, run_id),
                timeout=timeout_s + 10,
            )
        except asyncio.TimeoutError:
            logger.warning("Exa Agent poll timeout for run_id=%s", run_id)
            return []
        except Exception as e:
            logger.warning("Exa Agent poll failed for run_id=%s: %s", run_id, e)
            return []

        status = getattr(completed, "status", "")
        stop_reason = getattr(completed, "stop_reason", None) or getattr(completed, "stopReason", None)
        if status != "completed":
            err = getattr(completed, "error", None)
            logger.warning(
                "Exa Agent finished with status=%s stop_reason=%s error=%r",
                status, stop_reason, err,
            )
            return []

        cost = getattr(completed, "cost_dollars", None) or getattr(completed, "costDollars", None)
        if cost is not None:
            logger.info("Exa Agent run_id=%s cost=%s stop_reason=%s", run_id, cost, stop_reason)

        output = getattr(completed, "output", None)
        if output is None:
            return []
        structured = getattr(output, "structured", None)
        if not isinstance(structured, dict):
            return []
        candidates = structured.get("candidates") or []
        if not isinstance(candidates, list):
            return []
        logger.info("Exa Agent run_id=%s returned %d candidates", run_id, len(candidates))
        return candidates

    async def search_dice_candidates(self, skills: List[str], location: str, limit: int = 10, boolean_string: str = "", role_hint: str = "") -> List[Dict[str, Any]]:
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
                role_hint=role_hint or "resume profile",
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
                    extracted_location = extract_us_location_from_text(
                        f"{title}\n{highlights_text}"
                    )
                    if not extracted_location and (extracted_city or extracted_state):
                        extracted_location = ", ".join(p for p in [extracted_city, extracted_state] if p)
                    results.append({
                        "id": f"dice_{idx}_{getattr(result, 'id', idx)}",
                        "provider_id": getattr(result, "id", f"dice_{idx}"),
                        "firstName": first_name,
                        "lastName": last_name,
                        "email": "",
                        "city": extracted_city,
                        "state": extracted_state,
                        "location": extracted_location,
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
