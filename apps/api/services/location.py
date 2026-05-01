import json
import math
import re
from typing import Optional, Tuple, Dict

import httpx
from openai import AsyncOpenAI
from pydantic import BaseModel
from core.config import OPENAI_API_KEY


_GEOCODE_CACHE: Dict[str, Optional[Tuple[float, float]]] = {}
_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_NOMINATIM_HEADERS = {
    "User-Agent": "airecruiter-location-filter/1.0",
    "Accept": "application/json",
}


def normalize_location_string(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    text = re.sub(r"\bwithin\s+\d+\s+mi\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bmetro\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^must\s+be\s+local\s+to\s+", "", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip(" ,")


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    earth_radius_miles = 3958.7613
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(d_lon / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return earth_radius_miles * c


def geocode_location(value: str) -> Tuple[Optional[Tuple[float, float]], str]:
    normalized = normalize_location_string(value)
    if not normalized:
        return None, "empty"

    if normalized in _GEOCODE_CACHE:
        coords = _GEOCODE_CACHE[normalized]
        return coords, "cached" if coords else "cached_miss"

    try:
        with httpx.Client(timeout=4.5) as client:
            res = client.get(
                _NOMINATIM_URL,
                params={
                    "q": normalized,
                    "format": "jsonv2",
                    "limit": 1,
                    "addressdetails": 0,
                },
                headers=_NOMINATIM_HEADERS,
            )
        if res.status_code >= 400:
            _GEOCODE_CACHE[normalized] = None
            return None, f"http_{res.status_code}"

        payload = res.json()
        if not isinstance(payload, list) or not payload:
            _GEOCODE_CACHE[normalized] = None
            return None, "not_found"

        first = payload[0] if isinstance(payload[0], dict) else {}
        lat = float(first.get("lat"))
        lon = float(first.get("lon"))
        coords = (lat, lon)
        _GEOCODE_CACHE[normalized] = coords
        return coords, "ok"
    except Exception:
        _GEOCODE_CACHE[normalized] = None
        return None, "error"


def within_radius(candidate_loc: str, target_loc: str, miles: int) -> Tuple[bool, str, Optional[float]]:
    candidate_coords, candidate_reason = geocode_location(candidate_loc)
    if not candidate_coords:
        return False, "candidate_ungeocodable", None

    target_coords, target_reason = geocode_location(target_loc)
    if not target_coords:
        return False, "target_ungeocodable", None

    distance = haversine_miles(
        candidate_coords[0],
        candidate_coords[1],
        target_coords[0],
        target_coords[1],
    )

    if distance <= max(1, int(miles or 0)):
        return True, "ok", distance
    return False, "outside_radius", distance


# 2-letter US state codes (incl. DC). Kept here so callers don't need to
# import from unified_candidate_search.py — that module already imports
# from this one, importing back would be circular.
_US_STATE_CODES = frozenset({
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC",
})

# Full state name → 2-letter code, lowercase keys for case-insensitive
# matching during text extraction.
_US_STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT",
    "delaware": "DE", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME",
    "maryland": "MD", "massachusetts": "MA", "michigan": "MI",
    "minnesota": "MN", "mississippi": "MS", "missouri": "MO",
    "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND",
    "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC",
}

# Country tokens that, when found after a city, are confident evidence the
# location is non-US. Mirrors `_NON_US_LOCATION_TOKENS` in
# unified_candidate_search.py — kept duplicated for the same circular-
# import reason. Order doesn't matter; longest substrings are checked
# explicitly in `extract_us_location_from_text` below.
_NON_US_COUNTRY_TOKENS = frozenset({
    "india", "united kingdom", "canada", "australia", "germany",
    "france", "philippines", "pakistan", "china", "ireland", "mexico",
    "brazil", "spain", "italy", "netherlands", "singapore", "uae",
    "dubai", "saudi arabia", "japan", "south korea", "vietnam",
    "indonesia", "malaysia", "thailand", "egypt", "nigeria",
    "south africa", "russia", "ukraine", "poland", "turkey", "israel",
    "argentina", "chile", "colombia", "peru", "venezuela", "bangladesh",
    "sri lanka", "nepal", "kenya", "ghana", "morocco", "switzerland",
    "sweden", "norway", "denmark", "finland", "belgium", "austria",
    "portugal", "greece", "hungary", "romania", "bulgaria", "iran",
    "iraq", "afghanistan", "qatar", "kuwait", "bahrain", "oman",
    "jordan", "lebanon", "ethiopia", "tanzania", "uganda", "zimbabwe",
    "new zealand", "taiwan", "hong kong", "england", "scotland",
})

# `City` token: 1-4 capitalised words, allowing internal hyphens / spaces
# / periods. Anchored to a comma-state suffix in the patterns below.
_CITY_RE = r"([A-Z][A-Za-z.\-]+(?:\s+[A-Z][A-Za-z.\-]+){0,3})"

_RE_GREATER_AREA = re.compile(
    rf"\bGreater\s+{_CITY_RE}\s+Area\b"
)
_RE_CITY_STATE_CODE = re.compile(
    rf"\b{_CITY_RE},\s+([A-Z]{{2}})\b"
)
_RE_CITY_STATE_NAME = re.compile(
    rf"\b{_CITY_RE},\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){{0,2}})\b"
)
_RE_CITY_COUNTRY = re.compile(
    rf"\b{_CITY_RE},\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){{0,2}})\b"
)
_RE_BASED_LOCATED = re.compile(
    r"\b(?:based|located|currently)\s+in\s+([^.\n|·•]{3,60})",
    re.IGNORECASE,
)


def extract_us_location_from_text(text: str) -> str:
    """Best-effort: pull a location string out of free text.

    Returns a normalised location like ``"Plano, TX"`` or
    ``"Bangalore, India"`` — or ``""`` when nothing confident is found.
    Caller is responsible for any downstream country/state interpretation
    (see ``_is_likely_non_us`` and ``_location_match_verdict`` in
    ``unified_candidate_search.py``).
    """
    if not text:
        return ""
    body = str(text)[:6000]

    # 1. LinkedIn classic: "Greater <City> Area"
    match = _RE_GREATER_AREA.search(body)
    if match:
        return normalize_location_string(match.group(1))

    # 2. "City, ST" with a real US state code
    for match in _RE_CITY_STATE_CODE.finditer(body):
        state = match.group(2).upper()
        if state in _US_STATE_CODES:
            return normalize_location_string(f"{match.group(1)}, {state}")

    # 3. "City, FullStateName" → normalise to "City, ST"
    for match in _RE_CITY_STATE_NAME.finditer(body):
        candidate_state = match.group(2).strip().lower()
        state_code = _US_STATE_NAMES.get(candidate_state)
        if state_code:
            return normalize_location_string(f"{match.group(1)}, {state_code}")

    # 4. "City, Country" with a known non-US country
    for match in _RE_CITY_COUNTRY.finditer(body):
        candidate_country = match.group(2).strip().lower()
        if candidate_country in _NON_US_COUNTRY_TOKENS:
            # Preserve the country with a Title-cased rendering for display.
            return normalize_location_string(
                f"{match.group(1)}, {match.group(2).strip()}"
            )

    # 5. "based in X" / "located in X" / "currently in X"
    match = _RE_BASED_LOCATED.search(body)
    if match:
        cleaned = normalize_location_string(match.group(1))
        # Strip a trailing " - ..." or "·..." suffix the regex may have
        # caught before the sentence boundary.
        cleaned = re.split(r"\s+[-—|·•]\s+", cleaned, maxsplit=1)[0]
        if cleaned:
            return cleaned

    return ""


class LocationVerdict(BaseModel):
    is_within_range: bool
    distance_estimate: str # e.g. "15 miles", "Different Country"
    reason: str

class LocationService:
    def __init__(self):
        self.api_key = OPENAI_API_KEY
        self.client = AsyncOpenAI(api_key=self.api_key) if self.api_key else None
        
    async def check_proximity(self, candidate_loc: str, job_loc: str, work_mode: str) -> LocationVerdict:
        """
        Semantically checks if candidate is within commuting distance.
        """
        if not candidate_loc or not job_loc or not self.client:
            return LocationVerdict(is_within_range=True, distance_estimate="Unknown", reason="Location data unavailable (check resume or API key).")

        # Logic: If Remote, always true
        if work_mode.lower() == "remote":
            return LocationVerdict(is_within_range=True, distance_estimate="N/A", reason="Role is Remote.")

        prompt = f"""
        Determine if the Candidate Location is within commuting distance (approx 50 miles / 80 km) of the Job Location.
        
        Candidate Location: {candidate_loc}
        Job Location: {job_loc}
        
        Output JSON:
        {{
            "is_within_range": boolean,
            "distance_estimate": "string (e.g. 'Same City', '400 miles', 'Different Country')",
            "reason": "Short explanation (e.g. 'Newark is a suburb of NYC', 'London is in UK, job in US')"
        }}
        """

        try:
            model = "gpt-4o-mini"
            completion = await self.client.beta.chat.completions.parse(
                model=model, # Cheap model is fine for geography
                messages=[
                    {"role": "system", "content": "You are a Geography Distance Calculator. Be realistic about commuting."},
                    {"role": "user", "content": prompt}
                ],
                response_format=LocationVerdict,
                temperature=0.0
            )

            return completion.choices[0].message.parsed
        except Exception as e:
            print(f"⚠️ Location Check Error: {e}")
            return LocationVerdict(is_within_range=True, distance_estimate="Error", reason="Location check failed.")
