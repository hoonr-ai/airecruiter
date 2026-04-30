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
