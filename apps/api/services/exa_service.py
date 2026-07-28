import asyncio
import logging
import os
import re
from typing import List, Dict, Any, Optional, Tuple
from core.config import EXA_API_KEY, EXA_CONTACT_ENRICH_ENABLED
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


# Exa Search does NOT parse boolean operators (confirmed by Exa engineering,
# 2026-07): AND/OR/NOT, parens, and quoted-phrase syntax are treated as plain
# tokens at best and noise at worst. Queries must be natural-language
# sentences, ONE ROLE PER SEARCH, e.g.
#   "Senior Oracle PL/SQL developer with 5+ years of Autosys and performance
#    tuning experience, based in Jersey City, NJ"
# The helpers below compose those sentences from structured criteria and only
# fall back to flattening a legacy boolean string when nothing structured is
# available.


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


_BOOLEAN_OPERATOR_RE = re.compile(r"^(?:AND|OR|NOT|W/\d+)$", re.IGNORECASE)
# Filler words that survive flattening a boolean string but carry no signal
# as standalone query terms.
_BOOLEAN_STOPWORDS = {
    "a", "an", "and", "or", "not", "the", "of", "in", "with",
    "located", "based", "near", "within",
}


def _strip_boolean_not_groups(text: str) -> str:
    """Remove `NOT (...)` / `NOT "term"` / `NOT term` exclusions — Exa has no
    negation, so exclusion terms in the query would *attract* the profiles
    they were meant to filter out. Downstream post-filtering still applies
    the excludes."""
    out = re.sub(r'\bNOT\s*\((?:[^()]|\([^()]*\))*\)', ' ', text, flags=re.IGNORECASE)
    out = re.sub(r'\bNOT\s+"[^"]*"', ' ', out, flags=re.IGNORECASE)
    out = re.sub(r'\bNOT\s+\S+', ' ', out, flags=re.IGNORECASE)
    return out


def _boolean_to_terms(boolean_string: str, cap: int = 8) -> List[str]:
    """Flatten a boolean/ATS keyword string into plain phrases.

    Pulls quoted phrases first (they carry the intent), then bare words,
    dropping operators, parens, radius hints, exclusion groups, and filler
    words. Used only as a fallback when no structured titles/skills exist.
    """
    text = (boolean_string or "").strip()
    if not text or text == "*":
        return []
    text = re.sub(r'\s+within\s+\d+\s*mi\b', ' ', text, flags=re.IGNORECASE)
    text = _strip_boolean_not_groups(text)

    terms: List[str] = []
    seen = set()

    def _add(term: str) -> None:
        t = re.sub(r"\s+", " ", term).strip(' .,;:*')
        key = t.lower()
        if (
            t
            and key not in seen
            and key not in _BOOLEAN_STOPWORDS
            and not _BOOLEAN_OPERATOR_RE.match(t)
        ):
            seen.add(key)
            terms.append(t)

    for quoted in re.findall(r'"([^"]+)"', text):
        _add(quoted)
    remainder = re.sub(r'"[^"]*"', ' ', text)
    remainder = remainder.replace('(', ' ').replace(')', ' ')
    for word in remainder.split():
        _add(word)
    return terms[:cap]


def _split_role_hint(role_hint: str) -> List[str]:
    """Split a legacy role-hint string ('"A" OR "B"' or plain text) into
    individual role titles."""
    hint = (role_hint or "").strip()
    if not hint:
        return []
    quoted = [q.strip() for q in re.findall(r'"([^"]+)"', hint) if q.strip()]
    if quoted:
        return quoted
    return [p.strip() for p in re.split(r"\bOR\b", hint, flags=re.IGNORECASE) if p.strip()]


def _join_natural(items: List[str]) -> str:
    """['a','b','c'] -> 'a, b and c' — reads as prose, not as a keyword list."""
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def compose_people_query(
    role: str,
    skills: Optional[List[str]] = None,
    location: str = "",
    min_experience_years: Optional[int] = None,
    companies: Optional[List[str]] = None,
    keywords: Optional[List[str]] = None,
) -> str:
    """One natural-language people-search sentence for one role.

    'Senior Oracle PL/SQL Developer with 5+ years of Autosys and performance
    tuning experience, who has worked at Acme Corp, TS/SCI clearance, based
    in Jersey City, NJ, United States'
    """
    role_clean = re.sub(r"\s+", " ", str(role or "")).strip().strip('"')
    if not role_clean:
        role_clean = "Experienced professional"
    role_lower = role_clean.lower()

    seen = set()

    def _clean_terms(values: Optional[List[str]], cap: int, skip_in_role: bool) -> List[str]:
        out: List[str] = []
        for v in values or []:
            t = re.sub(r"\s+", " ", str(v or "")).strip().strip('"')
            key = t.lower()
            if not t or key in seen:
                continue
            # Skip terms already named inside the role title ("Senior PL/SQL
            # Developer" + skill "PL/SQL" reads badly twice). Word-boundary
            # match, NOT substring — a plain `in` deleted "Java" for
            # "JavaScript Developer" and "Go" for "Django Developer".
            if skip_in_role and re.search(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", role_lower):
                continue
            seen.add(key)
            out.append(t)
            if len(out) >= cap:
                break
        return out

    skill_list = _clean_terms(skills, cap=5, skip_in_role=True)
    # Keywords/companies are cleaned AFTER skills but get their own sentence
    # slots, so a job with 5+ skills can't silently truncate them away.
    keyword_list = _clean_terms(keywords, cap=3, skip_in_role=True)
    company_list = _clean_terms(companies, cap=2, skip_in_role=False)

    years = min_experience_years if (min_experience_years or 0) > 0 else None
    parts = [role_clean]
    if years and skill_list:
        parts.append(f"with {years}+ years of {_join_natural(skill_list)} experience")
    elif skill_list:
        parts.append(f"with {_join_natural(skill_list)} experience")
    elif years:
        parts.append(f"with {years}+ years of experience")

    query = " ".join(parts)
    if company_list:
        query += f", who has worked at {_join_natural(company_list)}"
    if keyword_list:
        query += f", {_join_natural(keyword_list)}"
    # Strip zips — LinkedIn location lines never show them, so a zip in the
    # NL query is noise. The zip still drives the offline radius post-filter.
    loc = _strip_zip_for_query(location)
    if loc:
        if loc.lower() == "united states":
            loc = "the United States"
        query += f", based in {loc}"
    return query


def build_people_queries(
    titles: Optional[List[str]] = None,
    skills: Optional[List[str]] = None,
    location: str = "",
    min_experience_years: Optional[int] = None,
    boolean_string: str = "",
    role_hint: str = "",
    max_queries: int = 3,
    companies: Optional[List[str]] = None,
    keywords: Optional[List[str]] = None,
) -> List[str]:
    """Natural-language Exa queries — one role per query (Exa guidance).

    Priority for the role list: structured `titles` → legacy `role_hint`
    ('"A" OR "B"') → first term of a flattened boolean string (recruiter-
    edited booleans conventionally lead with the title group, and this is
    the only place a recruiter-typed role that never made it into
    title_criteria can surface). When no roles are derivable at all, emits
    a single generic query anchored on skills + location so the search
    still runs.
    """
    roles: List[str] = []
    seen_roles = set()
    for t in titles or []:
        clean = re.sub(r"\s+", " ", str(t or "")).strip().strip('"')
        key = clean.lower()
        if clean and key not in seen_roles:
            seen_roles.add(key)
            roles.append(clean)
    if not roles:
        roles = _split_role_hint(role_hint)

    skill_terms = [str(s or "").strip() for s in (skills or []) if str(s or "").strip()]

    if not roles:
        terms = _boolean_to_terms(boolean_string)
        if terms:
            # Boolean builders put the primary title group first; treat the
            # leading term as the role. The remaining terms only fill the
            # skill slot when no structured skills exist — structured chips
            # are always the better signal.
            roles = terms[:1]
            if not skill_terms:
                skill_terms = terms[1:6]

    if not roles:
        return [
            compose_people_query(
                "", skill_terms, location, min_experience_years,
                companies=companies, keywords=keywords,
            )
        ]

    return [
        compose_people_query(
            role, skill_terms, location, min_experience_years,
            companies=companies, keywords=keywords,
        )
        for role in roles[:max_queries]
    ]

def build_deep_research_output_schema(include_contact_fields: Optional[bool] = None) -> Dict[str, Any]:
    """Output schema for the Exa Agent deep-research run.

    Contact fields carry `description`s because that's what activates the
    Agent API's contact-enrichment tool (per Exa engineering) — a bare
    {"type": "string"} is NOT enough. Billable per hit (email $0.02 / phone
    $0.07), so gated behind the same flag as the per-candidate enrichment
    path (defaults to EXA_CONTACT_ENRICH_ENABLED).
    """
    if include_contact_fields is None:
        include_contact_fields = EXA_CONTACT_ENRICH_ENABLED

    candidate_props: Dict[str, Any] = {
        "linkedin_url": {"type": "string"},
        "name": {"type": "string"},
        "current_title": {"type": "string"},
        "location": {
            "type": "string",
            "description": (
                "Candidate's CURRENT residence from the LinkedIn profile's own "
                "location line, e.g. 'Tempe, Arizona, United States' or 'Greater "
                "Phoenix Area'. Not a company HQ, not a past position's city. "
                "Empty string if the profile shows no location."
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
    }
    if include_contact_fields:
        candidate_props["email"] = {
            "type": ["string", "null"],
            "description": (
                "The candidate's best current email address "
                "(work email preferred, personal email acceptable)."
            ),
        }
        candidate_props["phone"] = {
            "type": ["string", "null"],
            "description": (
                "The candidate's best direct phone number "
                "(mobile preferred), including country code."
            ),
        }

    return {
        "type": "object",
        "required": ["candidates"],
        "properties": {
            "candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["linkedin_url", "fit_rationale"],
                    "properties": candidate_props,
                },
            },
        },
    }


def _common_people_fields(result: Any) -> Dict[str, Any]:
    """Parse the source-agnostic fields off one Exa people-search result."""
    title = getattr(result, "title", "Unknown Candidate")
    url = getattr(result, "url", "")

    # Often for people search, the title contains their name or headline.
    name_parts = title.split(" - ")[0].split("|")[0].strip().split(" ")
    first_name = name_parts[0] if name_parts else "Unknown"
    last_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""

    highlights_text = ""
    if getattr(result, "highlights", None):
        highlights_text = "\n".join(result.highlights)

    extracted_city, extracted_state = _extract_city_from_highlights(highlights_text)
    extracted_location = extract_us_location_from_text(f"{title}\n{highlights_text}")
    if not extracted_location and (extracted_city or extracted_state):
        extracted_location = ", ".join(p for p in [extracted_city, extracted_state] if p)

    return {
        "firstName": first_name,
        "lastName": last_name,
        "email": "",
        "city": extracted_city,
        "state": extracted_state,
        "location": extracted_location,
        "title": title,
        "match_score": 0,
        "profile_url": url,
        "image_url": "",
        "open_to_relocation": _detect_relocation(highlights_text),
        "resume_text": highlights_text,
        "recruiter_candidate_id": None,
    }


class ExaService:
    def __init__(self):
        self.api_key = EXA_API_KEY
        self.exa = None
        if self.api_key:
            try:
                self.exa = Exa(api_key=self.api_key)
            except Exception as e:
                logger.error(f"Failed to initialize Exa SDK: {e}")

    async def _people_search_fanout(
        self,
        queries: List[str],
        limit: int,
        include_domains: Optional[List[str]] = None,
    ) -> List[Any]:
        """Run one Exa people search per query concurrently and merge.

        Merging interleaves round-robin by rank so every role gets fair
        representation in the capped result, then dedupes by normalised
        profile URL (the same person often matches sibling role queries).
        Per-query failures are logged and skipped — one bad query must not
        sink the whole search.
        """
        loop = asyncio.get_event_loop()

        def do_search(q: str):
            kwargs: Dict[str, Any] = dict(
                category="people",
                type="auto",
                num_results=limit,
                highlights={"max_characters": 4000},
            )
            if include_domains:
                kwargs["include_domains"] = include_domains
            return self.exa.search_and_contents(q, **kwargs)

        responses = await asyncio.gather(
            *[loop.run_in_executor(None, do_search, q) for q in queries],
            return_exceptions=True,
        )

        per_query: List[List[Any]] = []
        for q, resp in zip(queries, responses):
            if isinstance(resp, BaseException):
                logger.warning("Exa people search failed for query %r: %s", q, resp)
                continue
            per_query.append(list(getattr(resp, "results", None) or []))

        merged: List[Any] = []
        seen = set()
        for rank in range(max((len(r) for r in per_query), default=0)):
            for results in per_query:
                if rank >= len(results):
                    continue
                r = results[rank]
                key = (
                    _normalize_linkedin_url(getattr(r, "url", "") or "")
                    or str(getattr(r, "id", "") or "")
                    or f"anon_{len(merged)}"
                )
                if key in seen:
                    continue
                seen.add(key)
                merged.append(r)
        return merged[:limit]

    async def search_candidates(
        self,
        skills: List[str],
        location: str,
        limit: int = 10,
        boolean_string: str = "",
        role_hint: str = "",
        titles: Optional[List[str]] = None,
        min_experience_years: Optional[int] = None,
        companies: Optional[List[str]] = None,
        keywords: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        if not self.exa:
            logger.warning("Exa API key is not set. Skipping Exa search.")
            return []

        try:
            queries = build_people_queries(
                titles=titles,
                skills=skills,
                location=location,
                min_experience_years=min_experience_years,
                boolean_string=boolean_string,
                role_hint=role_hint,
                companies=companies,
                keywords=keywords,
            )
            logger.info("Executing Exa people search with %d natural-language queries: %s", len(queries), queries)

            search_results = await self._people_search_fanout(queries, limit)

            results = []
            for idx, result in enumerate(search_results):
                fields = _common_people_fields(result)
                url = fields["profile_url"]
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
                    "source": "LinkedIn-Exa",
                    # open_to_work intentionally omitted here — populated
                    # asynchronously by services.apify_open_to_work via
                    # unified_candidate_search._search_exa.
                    **fields,
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
        exclude_company: str = "",
    ) -> List[Dict[str, Any]]:
        """Exa Agent API (Websets 2.0) pass — agentic enrichment + discovery.

        `exclude_company` is the HIRING CLIENT: the agent is told to skip
        anyone whose CURRENT employer is that company (we can never submit a
        client's own employees). The orchestrator re-checks the returned
        `recent_companies` against the same name, so this instruction is a
        recall optimization, not the enforcement point.

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
        # require a process restart. Env default must match
        # sourcing_config.EXA_AGENT_EFFORT ("high" — the tier that reliably
        # fills all four schema fields); it silently drifted to "medium" here.
        from core import sourcing_config as _sc
        _effort_default = str(getattr(_sc, "EXA_AGENT_EFFORT", "high") or "high").lower()
        if _effort_default not in {"low", "medium", "high", "xhigh", "auto"}:
            _effort_default = "high"
        effort = (os.getenv("EXA_AGENT_EFFORT", _effort_default).strip().lower() or _effort_default)
        if effort not in {"low", "medium", "high", "xhigh", "auto"}:
            logger.warning("EXA_AGENT_EFFORT=%r is invalid; falling back to %r", effort, _effort_default)
            effort = _effort_default
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

        seeds = [u for u in (seed_urls or []) if u][:max_input]

        # Natural-language task (Exa doesn't parse boolean/ATS syntax — the
        # role line is composed as a prose sentence). URLs go into
        # `input.data`, not the query.
        #
        # Tone shift vs first version: every "or null" hint was making the
        # agent give up on follower_count / last_activity the moment they
        # weren't on the first page of search results. Now we explicitly
        # mark them REQUIRED-TO-ATTEMPT and tell the agent how to find them
        # (visit the linkedin profile page, search "<name> linkedin
        # followers", etc.). Schema still allows null so the agent doesn't
        # fail validation when a profile genuinely doesn't expose the data,
        # but the prose strongly biases toward "go look".
        # jd_role carries the fuller title list (callers join the top titles
        # with " or "), so prefer it over the single-title jd_title — the
        # other way around silently dropped every title after the first.
        role_text = (jd_role or "").strip() or (jd_title or "").strip()
        role_line = compose_people_query(role_text, skills, location)
        include_contacts = EXA_CONTACT_ENRICH_ENABLED
        contact_clause = (
            "  5. email and phone: the candidate's best current email address "
            "and direct phone number, using your contact enrichment tooling.\n"
            if include_contacts
            else ""
        )
        # Residency requirement: bias the agent toward candidates who actually
        # live in/near the job location and to report the profile's true
        # location (not a company HQ or a past job's city). Skipped for
        # US-wide / remote searches.
        query_location = _strip_zip_for_query(location)
        radius_hint = ""
        if query_location and query_location.lower() not in (
            "united states", "the united states", "usa", "us",
        ):
            radius = max(1, min(100, int(within_miles or 25)))
            radius_hint = (
                f"LOCATION REQUIREMENT: candidates must CURRENTLY live in or within "
                f"~{radius} miles of {query_location}. Verify against the LinkedIn "
                "profile's own location line — a past job, employer HQ, or university "
                "in that city does NOT count. Prefer verified-local candidates; if you "
                "cannot verify, still include the candidate but report the location "
                "string their profile actually shows. "
            )
        location_clause = (
            "  6. location: the candidate's CURRENT residence exactly as the "
            "LinkedIn profile's location line shows it (e.g. 'Tempe, Arizona, "
            "United States' or 'Greater Phoenix Area'). Never substitute a "
            "company HQ or a past position's city; leave empty only if the "
            "profile shows no location at all.\n"
        )
        exclude_clause = ""
        exclude_clean = str(exclude_company or "").strip()
        if exclude_clean and exclude_clean.lower() not in ("external", "unknown", "n/a"):
            exclude_clause = (
                f"HARD EXCLUSION: skip anyone whose CURRENT employer is "
                f"\"{exclude_clean}\" (or an obvious subsidiary/brand of it) — "
                "this is the hiring company and its own employees must not be "
                "sourced. Past employment there is fine. "
            )
        query = (
            f"Find LinkedIn profiles of candidates matching this role: {role_line}. "
            "Prefer candidates who are currently active in this role — skip retired "
            "or long-inactive profiles. "
            + exclude_clause
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
            f"titles fit the role \"{role_text or jd_role}\".\n"
            f"{contact_clause}"
            f"{location_clause}"
            "Return at least 30 candidates with linkedin_url populated. Do not "
            "drop candidates just because one field is hard to find — partial "
            "enrichment is better than skipping them."
        )

        # Pass A URLs as first-class input records — the Agent API processes
        # them in addition to its own search-driven discovery.
        agent_input: Optional[Dict[str, Any]] = None
        if seeds:
            agent_input = {"data": [{"linkedin_url": u} for u in seeds]}

        output_schema = build_deep_research_output_schema(include_contacts)

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

    async def search_dice_candidates(
        self,
        skills: List[str],
        location: str,
        limit: int = 10,
        boolean_string: str = "",
        role_hint: str = "",
        titles: Optional[List[str]] = None,
        min_experience_years: Optional[int] = None,
        companies: Optional[List[str]] = None,
        keywords: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Search Dice (dice.com) profiles via Exa with domain filtering.
        Dice hosts tech candidate profiles publicly indexable by Exa; we scope
        the people-search to dice.com to pull those records.
        """
        if not self.exa:
            logger.warning("Exa API key is not set. Skipping Dice search.")
            return []

        try:
            queries = build_people_queries(
                titles=titles,
                skills=skills,
                location=location,
                min_experience_years=min_experience_years,
                boolean_string=boolean_string,
                role_hint=role_hint,
                companies=companies,
                keywords=keywords,
            )
            logger.info("Executing Dice (via Exa) search with %d natural-language queries: %s", len(queries), queries)

            search_results = await self._people_search_fanout(
                queries, limit, include_domains=["dice.com"],
            )

            results = []
            for idx, result in enumerate(search_results):
                fields = _common_people_fields(result)
                results.append({
                    "id": f"dice_{idx}_{getattr(result, 'id', idx)}",
                    "provider_id": getattr(result, "id", f"dice_{idx}"),
                    "source": "Dice",
                    "open_to_work": False,
                    **fields,
                })

            logger.info(f"Dice-via-Exa returned {len(results)} candidates.")
            return results

        except Exception as e:
            logger.error(f"Dice (via Exa) search failed: {e}")
            return []

exa_service = ExaService()
