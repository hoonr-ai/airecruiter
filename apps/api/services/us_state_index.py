"""Local US state index for fuzzy/acronym matching without geocoder calls.

Loads data/us_states.json once at import time and exposes:
- ``lookup_state(text)``  → canonical {code, name, lat, lng} or None
- ``resolve_state_code(text)`` → bare 2-letter code or None
- ``state_centroid(code)`` → (lat, lng) or None
- ``state_centroid_distance_miles(code_a, code_b)``

The lookup is forgiving: it accepts case-insensitive 2-letter codes
("ca", "CA"), full names ("California", "california"), common short forms
("Calif.", "Cal"), and surrounding whitespace / punctuation. Designed to
replace the soft-keep `target_ungeocodable` path in `_location_match_verdict`
when the criteria location is a bare state.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "us_states.json"


def _load() -> List[Dict]:
    try:
        with _DATA_PATH.open() as f:
            payload = json.load(f)
        return payload.get("states") or []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


_STATES: List[Dict] = _load()

# Index by code (uppercase) and by lowercase canonical name.
_BY_CODE: Dict[str, Dict] = {s["code"].upper(): s for s in _STATES if s.get("code")}
_BY_NAME: Dict[str, Dict] = {s["name"].strip().lower(): s for s in _STATES if s.get("name")}

# Small alias table for common short forms not in the JSON. Lowercased keys.
_ALIASES: Dict[str, str] = {
    "calif": "CA", "calif.": "CA", "cali": "CA",
    "mass": "MA", "mass.": "MA",
    "tex": "TX", "tex.": "TX",
    "fla": "FL", "fla.": "FL",
    "penn": "PA", "penn.": "PA", "penna": "PA",
    "minn": "MN", "minn.": "MN",
    "wash": "WA", "wash.": "WA",
    "ill": "IL", "ill.": "IL",
    "n.c.": "NC", "n. c.": "NC",
    "s.c.": "SC", "s. c.": "SC",
    "n.y.": "NY", "n. y.": "NY",
    "n.j.": "NJ", "n. j.": "NJ",
    "d.c.": "DC", "d. c.": "DC",
    "wash dc": "DC", "washington dc": "DC", "washington d.c.": "DC",
}

# Stripper for incidental punctuation around the token (keeps internal "." for "D.C.").
_STRIP_EDGES = re.compile(r"^[\s,;:.|/\-]+|[\s,;:.|/\-]+$")


def _normalize(text: str) -> str:
    return _STRIP_EDGES.sub("", str(text or "")).strip().lower()


def resolve_state_code(text: str) -> Optional[str]:
    """Return canonical 2-letter state code for ``text``, or None.

    Accepts: code ("CA", "ca"), full name ("California"), known alias
    ("Calif."), dotted forms ("N.C.", "D.C."), and case/whitespace
    variants. Returns None for empty input or unknown strings.
    """
    raw = _normalize(text)
    if not raw:
        return None
    # Variants we'll try in order: as-is, dots removed, dots+internal
    # whitespace removed (handles "n. c." → "nc").
    candidates = [raw]
    no_dots = raw.replace(".", "")
    if no_dots != raw:
        candidates.append(no_dots)
    compact = re.sub(r"\s+", "", no_dots)
    if compact != no_dots:
        candidates.append(compact)
    for cand in candidates:
        if len(cand) == 2:
            up = cand.upper()
            if up in _BY_CODE:
                return up
        if cand in _BY_NAME:
            return _BY_NAME[cand]["code"].upper()
        if cand in _ALIASES:
            return _ALIASES[cand]
    return None


def lookup_state(text: str) -> Optional[Dict]:
    """Return the full state dict for ``text``, or None."""
    code = resolve_state_code(text)
    if not code:
        return None
    return _BY_CODE.get(code)


def state_centroid(code: str) -> Optional[Tuple[float, float]]:
    state = _BY_CODE.get((code or "").upper())
    if not state:
        return None
    try:
        return (float(state["lat"]), float(state["lng"]))
    except (KeyError, ValueError, TypeError):
        return None


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
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


def state_centroid_distance_miles(code_a: str, code_b: str) -> Optional[float]:
    a = state_centroid(code_a)
    b = state_centroid(code_b)
    if not a or not b:
        return None
    return _haversine_miles(a[0], a[1], b[0], b[1])


def is_state_only(text: str) -> bool:
    """True if ``text`` looks like a bare state (code, name, or alias)
    with no city component."""
    raw = _normalize(text)
    if not raw or "," in raw or "/" in raw or "\\" in raw:
        return False
    return resolve_state_code(raw) is not None


def known_codes() -> Iterable[str]:
    return _BY_CODE.keys()
