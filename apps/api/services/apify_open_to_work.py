"""Apify-backed LinkedIn Open-to-Work enrichment for Exa-sourced candidates.

Call the `freshdata/linkedin-open-to-work-status` Apify actor per LinkedIn URL,
cache the boolean result, dedup concurrent in-flight requests, and let the
frontend poll for resolution.

Cache topology
--------------
Resolved statuses are persisted in **Redis** (shared across all API replicas)
when ``REDIS_URL`` is configured, with a per-process dict as an L1 cache and
local fallback. This matters because production runs MULTIPLE API replicas
behind a load balancer: the Exa search that fires the Apify fetch resolves the
status on whichever replica handled it, but the frontend's status poll
(`/candidates/open-to-work-statuses`) is round-robined across replicas. With a
purely in-process cache the poll almost always lands on a replica that never
saw the result and returns ``"PENDING"`` forever. Sharing via Redis fixes that
and also survives restarts. When ``REDIS_URL`` is empty (local single-worker
dev) we degrade gracefully to the in-process dict — no behaviour change there.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx

from core import config as _cfg
from core.config import APIFY_API_TOKEN, APIFY_LINKEDIN_OTW_ACTOR

log = logging.getLogger(__name__)

# Per-process L1 cache + local fallback when Redis is unavailable.
# value semantics:
#   True / False  → resolved (by Apify, or a deterministic-failure sentinel)
#   missing key   → never resolved on this replica (may still be in Redis)
_results: Dict[str, bool] = {}
_inflight: set[str] = set()
_lock = asyncio.Lock()

# ---- Shared Redis cache (cross-replica) -----------------------------------
# Resolved OTW statuses live under `otw:status:{normalized_url}` = "1"/"0".
# 24h TTL matches the cached-result expiry intent of the sourcing pipeline.
_REDIS_KEY_PREFIX = "otw:status:"
_REDIS_TTL_S = 24 * 60 * 60

_redis_client: Optional[Any] = None
_redis_unavailable: bool = False  # latched after first failure to stop log spam


def _get_redis() -> Optional[Any]:
    """Lazy-init a redis.asyncio client gated solely on ``REDIS_URL``.

    Deliberately independent of ``LLM_CACHE_ENABLED`` — OTW correctness must
    not hinge on the LLM-cache kill switch. Returns None (caching disabled)
    when REDIS_URL is empty or the client can't be created; callers then fall
    back to the in-process dict.
    """
    global _redis_client, _redis_unavailable
    if _redis_unavailable:
        return None
    if _redis_client is not None:
        return _redis_client
    if not getattr(_cfg, "REDIS_URL", ""):
        _redis_unavailable = True
        return None
    try:
        import redis.asyncio as redis_asyncio  # lazy so the API boots without the dep
        _redis_client = redis_asyncio.from_url(
            _cfg.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        log.info("apify_open_to_work: redis client initialized (shared OTW cache)")
        return _redis_client
    except Exception as exc:
        log.warning(
            "apify_open_to_work: redis init failed, OTW cache is per-process only: %s",
            exc,
        )
        _redis_unavailable = True
        return None


def _redis_key(norm_url: str) -> str:
    return f"{_REDIS_KEY_PREFIX}{norm_url}"


async def _redis_set(norm_url: str, value: bool) -> None:
    """Persist a resolved status to the shared cache. Never raises."""
    client = _get_redis()
    if client is None:
        return
    try:
        await client.set(_redis_key(norm_url), "1" if value else "0", ex=_REDIS_TTL_S)
    except Exception as exc:
        log.debug("apify_open_to_work: redis set failed for %s: %s", norm_url, exc)


async def _redis_get_many(norm_urls: List[str]) -> Dict[str, bool]:
    """Batch-read resolved statuses from the shared cache. Never raises.

    Returns only the keys that are present (hits); missing keys are omitted.
    """
    out: Dict[str, bool] = {}
    client = _get_redis()
    if client is None or not norm_urls:
        return out
    try:
        vals = await client.mget([_redis_key(k) for k in norm_urls])
        for k, v in zip(norm_urls, vals):
            if v is None:
                continue
            out[k] = v == "1" or v is True or v == "true"
    except Exception as exc:
        log.debug("apify_open_to_work: redis mget failed: %s", exc)
    return out


async def _store_resolved(norm_url: str, value: bool) -> None:
    """Record a resolved status in both L1 (local dict) and Redis."""
    async with _lock:
        _results[norm_url] = value
    await _redis_set(norm_url, value)


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

    Cache-aware: returns the cached value immediately (L1, then shared Redis);
    skips if already in flight on this replica. Resolution semantics:
      - clean bool from Apify  → store True/False (L1 + Redis), return it
      - malformed payload       → deterministic miss, store False (L1 + Redis)
                                  so the chip resolves and we don't re-hammer
                                  the actor for an unparseable profile
      - transport error/timeout → transient; do NOT cache, return None so the
                                  next enqueue retries (the frontend re-polls)
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

    try:
        # Another replica may have resolved this already — honour the shared
        # cache before spending an Apify call.
        shared = await _redis_get_many([key])
        if key in shared:
            async with _lock:
                _results[key] = shared[key]
            return shared[key]

        if not APIFY_API_TOKEN:
            log.warning("APIFY_API_TOKEN not configured; skipping OTW fetch for %s", key)
            return None

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
            # Deterministic failure (the same input will parse the same way),
            # so cache False to resolve the chip and avoid re-hammering Apify.
            log.warning("Apify OTW: unexpected payload for %s: %r — caching False", key, data)
            await _store_resolved(key, False)
            return False
        await _store_resolved(key, otw)
        log.info("Apify OTW resolved %s -> %s", key, otw)
        return otw
    except Exception as exc:
        # Transient (network/timeout/5xx). Leave uncached so a later enqueue
        # retries instead of permanently mislabelling the candidate.
        log.warning("Apify OTW fetch failed for %s: %s (will retry on re-poll)", key, exc)
        return None
    finally:
        async with _lock:
            _inflight.discard(key)


async def annotate(candidates: List[Dict[str, Any]]) -> List[str]:
    """Set candidate['open_to_work'] from cache where present.

    Consults the per-process L1 cache first, then the shared Redis cache for
    the remainder (so a status resolved on a sibling replica is honoured).
    Returns the list of LinkedIn URLs (original form, not normalized) that are
    still unresolved — the caller kicks off background fetches via `enqueue`.
    """
    pending: List[str] = []
    to_check: List[tuple] = []  # (candidate, original_url, normalized_key)
    for c in candidates:
        url = _candidate_linkedin_url(c)
        if not url:
            continue
        key = _normalize(url)
        if key in _results:
            c["open_to_work"] = _results[key]
        else:
            to_check.append((c, url, key))

    if to_check:
        shared = await _redis_get_many([k for (_, _, k) in to_check])
        for c, url, key in to_check:
            if key in shared:
                _results[key] = shared[key]  # warm L1
                c["open_to_work"] = shared[key]
            else:
                pending.append(url)
    return pending


async def enqueue(urls: List[str]) -> None:
    """Fire-and-forget Apify fetches for each URL not already resolved / in flight.

    Re-checks the shared Redis cache so we don't spend an Apify call on a URL a
    sibling replica already resolved.
    """
    if not urls:
        log.info("Apify OTW enqueue: no urls (skipping)")
        return
    if not APIFY_API_TOKEN:
        log.warning(
            "Apify OTW enqueue: APIFY_API_TOKEN is EMPTY — set it in .env "
            "and restart the API. Skipping %d urls; the frontend chip will "
            "stay on 'Checking…' forever until the token is configured.",
            len(urls),
        )
        return

    # Warm L1 from the shared cache for anything resolved elsewhere.
    norm_by_url = {u: _normalize(u) for u in urls}
    unknown_keys = [k for k in norm_by_url.values() if k not in _results]
    if unknown_keys:
        shared = await _redis_get_many(unknown_keys)
        if shared:
            async with _lock:
                _results.update(shared)

    async with _lock:
        to_fire = []
        for u in urls:
            key = norm_by_url[u]
            if key in _results or key in _inflight:
                continue
            to_fire.append(u)
    log.info(
        "Apify OTW enqueue: %d urls input → %d new tasks fired "
        "(%d already cached / in-flight, skipped)",
        len(urls),
        len(to_fire),
        len(urls) - len(to_fire),
    )
    for u in to_fire:
        asyncio.create_task(fetch_open_to_work(u))


def diagnostics() -> Dict[str, Any]:
    """One-shot health snapshot for debugging the OTW pipeline.

    Surfaced via /candidates/open-to-work-statuses so a single curl against the
    running API reveals the common failure modes (missing token, Redis off).
    Note: `cache_size`/`inflight_count` are PER-REPLICA (the L1 dict); with
    Redis enabled the authoritative shared store is keyed `otw:status:*`.
    """
    return {
        "apify_token_configured": bool(APIFY_API_TOKEN),
        "actor": APIFY_LINKEDIN_OTW_ACTOR,
        "redis_shared_cache": _get_redis() is not None,
        "cache_size": len(_results),  # per-replica L1 only
        "inflight_count": len(_inflight),
    }


async def lookup_statuses(urls: List[str]) -> Dict[str, Any]:
    """Read-only cache lookup for the polling endpoint.

    Checks the per-process L1 cache, then the shared Redis cache for misses, so
    a poll served by any replica sees statuses resolved on any other replica.
    Returns a mapping of original-form URL -> True | False | "PENDING".
    """
    out: Dict[str, Any] = {}
    missing: List[tuple] = []  # (original_url, normalized_key)
    for u in urls or []:
        key = _normalize(u)
        if key in _results:
            out[u] = _results[key]
        else:
            missing.append((u, key))

    if missing:
        shared = await _redis_get_many([k for (_, k) in missing])
        for u, key in missing:
            if key in shared:
                _results[key] = shared[key]  # warm L1
                out[u] = shared[key]
            else:
                out[u] = "PENDING"
    return out
