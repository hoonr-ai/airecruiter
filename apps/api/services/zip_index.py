"""Offline US zip-code centroid index — no geocoder calls.

Loads data/us_zip_centroids.json (GeoNames, CC BY 4.0) lazily on first use
and exposes:
- ``lookup_zip(text)``    → {"zip", "city", "state", "lat", "lng"} or None
- ``zip_centroid(zip5)``  → (lat, lng) or None
- ``zip_city_state(zip5)``→ ("Tempe", "AZ") or None
- ``zip_distance_miles(zip_a, zip_b)`` → float or None
- ``extract_zip(text)``   → first *known* 5-digit zip found in free text
- ``is_known_zip(text)``  → bool
- ``city_state_centroid(city, state)`` → (lat, lng) averaged over the
  city's zips, or None
- ``city_state_default_zip(city, state)`` → the zip nearest the city
  centroid (for APIs that take a zip but the job only has city/state)

Zip↔zip distances are centroid-to-centroid haversine, so treat them as
estimates (±a few miles for large rural zips) — good enough for radius
checks that previously fell through to best-effort Nominatim geocoding.
"""

from __future__ import annotations

import json
import math
import re
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "us_zip_centroids.json"

_ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")

# RLock: _city_index() triggers _load() while already holding the lock.
_lock = threading.RLock()
_ZIPS: Optional[Dict[str, List]] = None  # zip5 -> [city, state, lat, lng]


def _load() -> Dict[str, List]:
    global _ZIPS
    if _ZIPS is None:
        with _lock:
            if _ZIPS is None:
                try:
                    with _DATA_PATH.open() as f:
                        _ZIPS = json.load(f).get("zips") or {}
                except (FileNotFoundError, json.JSONDecodeError):
                    _ZIPS = {}
    return _ZIPS


def _clean(text: str) -> str:
    return str(text or "").strip()


def is_known_zip(text: str) -> bool:
    raw = _clean(text)
    return len(raw) == 5 and raw.isdigit() and raw in _load()


def lookup_zip(text: str) -> Optional[Dict]:
    """Return {"zip", "city", "state", "lat", "lng"} for a 5-digit zip
    (or zip+4) string, or None if unknown."""
    raw = _clean(text)
    m = _ZIP_RE.fullmatch(raw)
    if not m:
        return None
    row = _load().get(m.group(1))
    if not row:
        return None
    return {"zip": m.group(1), "city": row[0], "state": row[1], "lat": row[2], "lng": row[3]}


def zip_centroid(zip5: str) -> Optional[Tuple[float, float]]:
    entry = lookup_zip(zip5)
    if not entry:
        return None
    return (entry["lat"], entry["lng"])


def zip_city_state(zip5: str) -> Optional[Tuple[str, str]]:
    entry = lookup_zip(zip5)
    if not entry:
        return None
    return (entry["city"], entry["state"])


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


def zip_distance_miles(zip_a: str, zip_b: str) -> Optional[float]:
    a = zip_centroid(zip_a)
    b = zip_centroid(zip_b)
    if not a or not b:
        return None
    return _haversine_miles(a[0], a[1], b[0], b[1])


def extract_zip(text: str) -> Optional[str]:
    """Return the first zip in ``text`` that exists in the index.

    Validating against the index avoids false positives on other 5-digit
    numbers (salaries, employee counts, street numbers)."""
    zips = _load()
    for m in _ZIP_RE.finditer(str(text or "")):
        if m.group(1) in zips:
            return m.group(1)
    return None


# (city_lower, "ST") -> (lat, lng, zip nearest the averaged centroid)
_CITY_INDEX: Optional[Dict[Tuple[str, str], Tuple[float, float, str]]] = None


def _city_index() -> Dict[Tuple[str, str], Tuple[float, float, str]]:
    global _CITY_INDEX
    if _CITY_INDEX is None:
        with _lock:
            if _CITY_INDEX is None:
                groups: Dict[Tuple[str, str], List[Tuple[str, float, float]]] = {}
                for zip5, row in _load().items():
                    key = (str(row[0]).strip().lower(), str(row[1]).upper())
                    groups.setdefault(key, []).append((zip5, row[2], row[3]))
                index: Dict[Tuple[str, str], Tuple[float, float, str]] = {}
                for key, rows in groups.items():
                    lat = sum(r[1] for r in rows) / len(rows)
                    lng = sum(r[2] for r in rows) / len(rows)
                    rep = min(rows, key=lambda r: (r[1] - lat) ** 2 + (r[2] - lng) ** 2)[0]
                    index[key] = (round(lat, 4), round(lng, 4), rep)
                _CITY_INDEX = index
    return _CITY_INDEX


def _city_key(city: str, state: str) -> Optional[Tuple[str, str]]:
    c = _clean(city).lower()
    s = _clean(state).upper()
    if not c or not s:
        return None
    if len(s) != 2:
        try:
            from services.us_state_index import resolve_state_code
            s = resolve_state_code(s) or ""
        except Exception:
            s = ""
        if not s:
            return None
    return (c, s)


def city_state_centroid(city: str, state: str) -> Optional[Tuple[float, float]]:
    """Centroid for a US city (averaged over its zips), or None if the
    city+state pair isn't in the index (misspellings, neighborhoods)."""
    key = _city_key(city, state)
    if not key:
        return None
    entry = _city_index().get(key)
    if not entry:
        return None
    return (entry[0], entry[1])


def city_state_default_zip(city: str, state: str) -> Optional[str]:
    """A representative zip for a US city — the one nearest the city's
    averaged centroid. For APIs that take zip+radius when the job record
    only carries city/state."""
    key = _city_key(city, state)
    if not key:
        return None
    entry = _city_index().get(key)
    return entry[2] if entry else None
