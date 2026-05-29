"""Apify-backed LinkedIn Open-to-Work enrichment for Exa-sourced candidates.

Mirrors the pattern used in the sibling Hoonrai/Revelio path: call the
`freshdata/linkedin-open-to-work-status` Apify actor per LinkedIn URL, cache
the boolean result in-process, dedup concurrent in-flight requests, and let
the frontend poll for resolution.

No DB persistence — cache is per-process and lost on restart (by design).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx

from ..core.config import APIFY_API_TOKEN, APIFY_LINKEDIN_OTW_ACTOR

log = logging.getLogger(__name__)

# Module-level in-process cache. Single FastAPI worker => single dict.
# value semantics:
#   True / False  → resolved by Apify (or seeded)
#   missing key   → never seen
_results: Dict[str, bool] = {}
_inflight: set[str] = set()
_lock = asyncio.Lock()

_APIFY_ENDPOINT = (
    "https://api.apify.com/v2/acts/"
    f"{APIFY_LINKEDIN_OTW_ACTOR.replace('/', '~')}"
    "/run-sync-get-dataset-items"
)
_REQUEST_TIMEOUT_S = 180.0


def _normalize(url: str) -> str:
    """Lowercase host, strip query/fragment/trailing slash on path.

    Keeps the path case (LinkedIn vanity slugs are technically case-sensitive
    on display, but the canonical form is lowercase). We lowercase the whole
    URL to match how the dedup logic in unified_candidate_search compares
    profile URLs.
    """
    if not url:
        return ""
    try:
        parts = urlsplit(url.strip())
        host = (parts.netloc or "").lower()
        path = (parts.path or "").rstrip("/").lower()
        return urlunsplit((parts.scheme.lower() or "https", host, path, "", ""))
    except Exception:
        return url.strip().lower().rstrip("/")


def _candidate_linkedin_url(candidate: Dict[str, Any]) -> Optional[str]:
    """Extract a LinkedIn URL from an Exa candidate dict. Returns None if missing."""
    raw = candidate.get("profile_url") or candidate.get("linkedin_url") or ""
    if not raw or "linkedin.com" not in raw.lower():
        return None
    return raw


async def fetch_open_to_work(linkedin_url: str) -> Optional[bool]:
    """Fetch open-to-work status for a single LinkedIn URL via Apify.

    Cache-aware: returns cached value immediately; skips if already in flight
    (returns whatever is currently cached, possibly None). On success stores
    the boolean in the cache; on error logs and returns None.
    """
    if not linkedin_url:
        return None
    key = _normalize(linkedin_url)

    async with _lock:
        if key in _results:
            return _results[key]
        if key in _inflight:
            return None
        _inflight.add(key)

    if not APIFY_API_TOKEN:
        log.warning("APIFY_API_TOKEN not configured; skipping OTW fetch for %s", key)
        async with _lock:
            _inflight.discard(key)
        return None

    try:
        params = {"token": APIFY_API_TOKEN}
        body = {"linkedin_url": linkedin_url}
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_S) as client:
            resp = await client.post(_APIFY_ENDPOINT, params=params, json=body)
            resp.raise_for_status()
            data = resp.json()
        # Expected shape: [{"data": {"open_to_work": bool}, "message": "ok"}]
        otw_raw = None
        if isinstance(data, list) and data:
            first = data[0] or {}
            otw_raw = (first.get("data") or {}).get("open_to_work")
        otw = bool(otw_raw) if isinstance(otw_raw, bool) else None
        if otw is None:
            log.warning("Apify OTW: unexpected payload for %s: %r", key, data)
            return None
        async with _lock:
            _results[key] = otw
        log.info("Apify OTW resolved %s -> %s", key, otw)
        return otw
    except Exception as exc:
        log.warning("Apify OTW fetch failed for %s: %s", key, exc)
        return None
    finally:
        async with _lock:
            _inflight.discard(key)


async def annotate(candidates: List[Dict[str, Any]]) -> List[str]:
    """Set candidate['open_to_work'] from cache where present.

    Returns the list of LinkedIn URLs (original form, not normalized) that
    are NOT yet in cache — the caller is responsible for kicking off
    background fetches via `enqueue`.
    """
    pending: List[str] = []
    for c in candidates:
        url = _candidate_linkedin_url(c)
        if not url:
            continue
        key = _normalize(url)
        if key in _results:
            c["open_to_work"] = _results[key]
        else:
            pending.append(url)
    return pending


async def enqueue(urls: List[str]) -> None:
    """Fire-and-forget Apify fetches for each URL not already cached / in flight."""
    if not urls:
        return
    if not APIFY_API_TOKEN:
        log.warning("APIFY_API_TOKEN not configured; skipping OTW enqueue (%d urls)", len(urls))
        return
    async with _lock:
        to_fire = []
        for u in urls:
            key = _normalize(u)
            if key in _results or key in _inflight:
                continue
            to_fire.append(u)
    for u in to_fire:
        asyncio.create_task(fetch_open_to_work(u))


def lookup_statuses(urls: List[str]) -> Dict[str, Any]:
    """Read-only cache lookup for the polling endpoint.

    Returns a mapping of original-form URL -> True | False | "PENDING".
    """
    out: Dict[str, Any] = {}
    for u in urls or []:
        key = _normalize(u)
        if key in _results:
            out[u] = _results[key]
        else:
            out[u] = "PENDING"
    return out
