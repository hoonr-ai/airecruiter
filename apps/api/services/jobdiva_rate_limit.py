"""Cross-worker pacing and 429 cooldown for JobDiva BI candidate endpoints.

Why this exists (incident 2026-09-29, see fix.md): the old per-call
``asyncio.Semaphore(CANDIDATES_DETAIL_CONCURRENCY)`` only serialized the
chunks of ONE call. Live search enrichment, background hydration, single-id
lookups and the applicant path all ran in parallel, times 8 uvicorn workers,
against one JobDiva account quota — ~66% of CandidatesDetail attempts 429'd
and each chunk retried on its own fixed schedule into the same exhausted
limit.

Two shared pieces of state, both in Redis so every worker sees them:

- ``jobdiva:bi:next`` — the earliest time (ms) the next BI request may start.
  Each caller reserves a slot atomically and sleeps until it, so requests
  across all workers go out at most one per ``JOBDIVA_BI_MIN_INTERVAL_S``.
- ``jobdiva:cooldown`` — set on any 429 for ``Retry-After`` (or 10–20s with
  jitter). Every caller waits it out instead of retrying on its own.

When ``REDIS_URL`` is empty or Redis is down, the same logic runs in-process
(per worker) — still far better than per-call, and never blocks the caller
on Redis errors.
"""
from __future__ import annotations

import asyncio
import email.utils
import logging
import os
import random
import time
from typing import Any, Optional

import httpx

from core import config as _cfg

log = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    try:
        return float((os.getenv(name) or "").strip() or default)
    except ValueError:
        return default


# Minimum spacing between JobDiva BI candidate requests, across all workers.
MIN_INTERVAL_S = _env_float("JOBDIVA_BI_MIN_INTERVAL_S", 1.5)
# Cooldown after a 429 that carries no usable Retry-After: uniform in this range.
COOLDOWN_MIN_S = _env_float("JOBDIVA_BI_COOLDOWN_MIN_S", 10.0)
COOLDOWN_MAX_S = _env_float("JOBDIVA_BI_COOLDOWN_MAX_S", 20.0)
# Never honor a Retry-After longer than this (a bad header must not stall Step 5).
COOLDOWN_CAP_S = _env_float("JOBDIVA_BI_COOLDOWN_CAP_S", 60.0)

_NEXT_KEY = "jobdiva:bi:next"
_COOLDOWN_KEY = "jobdiva:cooldown"

# Reserve the next slot. Returns ms to wait, or -1 if that exceeds max_wait_ms
# (nothing is reserved then). Uses Redis TIME so worker clocks don't matter.
_RESERVE_LUA = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local nxt = tonumber(redis.call('GET', KEYS[1]) or '0')
local cd = redis.call('PTTL', KEYS[2])
if cd > 0 then nxt = math.max(nxt, now + cd) end
local slot = math.max(nxt, now)
local wait = slot - now
if wait > tonumber(ARGV[2]) then return -1 end
local interval = tonumber(ARGV[1])
redis.call('SET', KEYS[1], slot + interval, 'PX', wait + interval + 60000)
return wait
"""

# Extend (never shorten) the shared cooldown.
_COOLDOWN_LUA = """
local cur = redis.call('PTTL', KEYS[1])
if cur < tonumber(ARGV[1]) then
  redis.call('SET', KEYS[1], '1', 'PX', ARGV[1])
end
return 1
"""

_redis_client: Optional[Any] = None
_redis_unavailable = False

# In-process fallback state (monotonic seconds).
_local_lock = asyncio.Lock()
_local_next = 0.0
_local_cooldown_until = 0.0


def _get_redis() -> Optional[Any]:
    global _redis_client, _redis_unavailable
    if _redis_unavailable:
        return None
    if _redis_client is not None:
        return _redis_client
    if not getattr(_cfg, "REDIS_URL", ""):
        _redis_unavailable = True
        return None
    try:
        import redis.asyncio as redis_asyncio
        _redis_client = redis_asyncio.from_url(
            _cfg.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        return _redis_client
    except Exception as exc:
        log.warning("jobdiva_rate_limit: redis init failed, pacing is per-worker only: %s", exc)
        _redis_unavailable = True
        return None


async def _reserve_local(max_wait_s: float) -> float:
    global _local_next
    async with _local_lock:
        now = time.monotonic()
        slot = max(_local_next, _local_cooldown_until, now)
        wait = slot - now
        if wait > max_wait_s:
            return -1.0
        _local_next = slot + MIN_INTERVAL_S
        return wait


async def _reserve(max_wait_s: float) -> float:
    client = _get_redis()
    if client is not None:
        try:
            wait_ms = await client.eval(
                _RESERVE_LUA, 2, _NEXT_KEY, _COOLDOWN_KEY,
                int(MIN_INTERVAL_S * 1000), int(max_wait_s * 1000),
            )
            wait_ms = int(wait_ms)
            return -1.0 if wait_ms < 0 else wait_ms / 1000.0
        except Exception as exc:
            log.debug("jobdiva_rate_limit: redis reserve failed, using local: %s", exc)
    return await _reserve_local(max_wait_s)


async def _cooldown_remaining() -> float:
    client = _get_redis()
    if client is not None:
        try:
            ms = int(await client.pttl(_COOLDOWN_KEY))
            return ms / 1000.0 if ms > 0 else 0.0
        except Exception:
            pass
    return max(0.0, _local_cooldown_until - time.monotonic())


async def acquire(max_wait_s: float = 120.0) -> bool:
    """Wait for this worker's turn at the JobDiva BI quota.

    Returns False (without sending anything) when the wait would exceed
    ``max_wait_s`` — callers treat that like a failed fetch and let background
    hydration pick the ids up later.
    """
    deadline = time.monotonic() + max(0.0, max_wait_s)
    while True:
        remaining = deadline - time.monotonic()
        if remaining < 0:
            return False
        wait = await _reserve(remaining)
        if wait < 0:
            return False
        if wait > 0:
            await asyncio.sleep(wait)
        # A 429 may have landed while we slept toward an earlier reservation.
        if await _cooldown_remaining() <= 0:
            return True


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        dt = email.utils.parsedate_to_datetime(value)
        return max(0.0, dt.timestamp() - time.time())
    except Exception:
        return None


async def note_429(retry_after: Optional[str] = None) -> float:
    """Start (or extend) the shared cooldown after a JobDiva 429. Returns seconds."""
    global _local_cooldown_until
    secs = _parse_retry_after(retry_after)
    if secs is None:
        secs = random.uniform(COOLDOWN_MIN_S, COOLDOWN_MAX_S)
    secs = min(max(secs, 1.0), COOLDOWN_CAP_S)
    _local_cooldown_until = max(_local_cooldown_until, time.monotonic() + secs)
    client = _get_redis()
    if client is not None:
        try:
            await client.eval(_COOLDOWN_LUA, 1, _COOLDOWN_KEY, int(secs * 1000))
        except Exception as exc:
            log.debug("jobdiva_rate_limit: redis cooldown set failed: %s", exc)
    log.warning("JobDiva BI 429: shared cooldown %.1fs (Retry-After=%r)", secs, retry_after)
    return secs


async def bi_get(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_wait_s: float = 120.0,
    **kwargs: Any,
) -> Optional[httpx.Response]:
    """``client.get`` gated by the shared limiter.

    Returns None when no slot came up within ``max_wait_s``. On a 429 it
    starts the shared cooldown and still returns the response, so the caller
    decides whether to retry (its next ``bi_get`` waits the cooldown out).
    """
    if not await acquire(max_wait_s):
        return None
    response = await client.get(url, **kwargs)
    if response.status_code == 429:
        await note_429(response.headers.get("Retry-After"))
    return response
