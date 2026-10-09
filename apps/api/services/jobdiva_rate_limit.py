"""Cross-worker pacing and 429 cooldown for JobDiva BI candidate endpoints.

Why this exists (incident 2026-09-29, docs/incidents/2026-09-29-jobdiva-429-nginx-503.md):
the old per-call ``asyncio.Semaphore(CANDIDATES_DETAIL_CONCURRENCY)`` only
serialized the chunks of ONE call. Live search enrichment, background
hydration, single-id lookups and the applicant path all ran in parallel,
times 8 uvicorn workers, against one JobDiva account quota — ~66% of
CandidatesDetail attempts 429'd and each chunk retried on its own fixed
schedule into the same exhausted limit.

Shared state, in Redis so every worker sees it:

- ``jobdiva:bi:next`` — earliest time (ms) the next BI request may start.
  A caller takes the slot only when it is free *now*; otherwise it is told
  how long to wait and asks again. Nothing is reserved ahead, so a caller
  that gives up (or sees a cooldown land) never leaves a dead slot behind.
- ``jobdiva:cooldown`` — set on any 429 for ``Retry-After`` (or 10–20s with
  jitter). Every caller waits it out instead of retrying on its own.
- ``jobdiva:bi:fg_waiting`` — refreshed while an interactive caller is
  waiting. Background callers (hydration, AutoSync, backfills) don't take a
  free slot while it is set, so user-facing requests go first.

When ``REDIS_URL`` is empty the same logic runs in-process (per worker). When
Redis errors, it is skipped for ``_REDIS_RETRY_S`` and then tried again; in
that window each worker paces at ``MIN_INTERVAL_S × LOCAL_WORKERS`` so all
workers together still stay within the shared budget (fail closed). Pacing
every worker at the full rate is what multiplied JobDiva 429s by the worker
count whenever Redis was down.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import email.utils
import logging
import os
import random
import time
from typing import Any, Dict, Optional, Tuple

import httpx

from core import config as _cfg

log = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    try:
        return float((os.getenv(name) or "").strip() or default)
    except ValueError:
        return default


# Token refill interval for JobDiva BI candidate requests, across all workers
# (sustained rate = 1 / MIN_INTERVAL_S). JobDiva's real quota is unknown, so the
# default keeps today's 1 call / 1.5s.
MIN_INTERVAL_S = _env_float("JOBDIVA_BI_MIN_INTERVAL_S", 1.5)
# Bucket size. 1 = strict spacing (identical to the old fixed-interval limiter).
# Raise only once JobDiva's burst allowance is known.
BURST = max(1.0, _env_float("JOBDIVA_BI_BURST", 1.0))
# Burst background callers may use; the rest is reserved for interactive ones.
BACKGROUND_BURST = max(1.0, min(BURST, _env_float("JOBDIVA_BI_BACKGROUND_BURST", max(1.0, BURST / 2))))
# Cooldown after a 429 that carries no usable Retry-After: uniform in this range.
COOLDOWN_MIN_S = _env_float("JOBDIVA_BI_COOLDOWN_MIN_S", 10.0)
COOLDOWN_MAX_S = _env_float("JOBDIVA_BI_COOLDOWN_MAX_S", 20.0)
# Never honor a Retry-After longer than this (a bad header must not stall Step 5).
COOLDOWN_CAP_S = _env_float("JOBDIVA_BI_COOLDOWN_CAP_S", 60.0)

# Workers sharing the quota on this host (uvicorn --workers). Only used when
# Redis is configured but unreachable, to split the budget between them.
LOCAL_WORKERS = max(1, int(_env_float("JOBDIVA_BI_LOCAL_WORKERS", 8)))

INTERACTIVE = "interactive"
BACKGROUND = "background"

# Set for the duration of a background job (AutoSync, backfills) so every
# JobDiva call made underneath it is paced at background priority, including
# ones whose callers pass INTERACTIVE or call JobDiva directly.
_priority_ctx: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "jobdiva_priority", default=None
)


@contextlib.contextmanager
def background_context():
    token = _priority_ctx.set(BACKGROUND)
    try:
        yield
    finally:
        _priority_ctx.reset(token)


def in_background() -> bool:
    return _priority_ctx.get() == BACKGROUND


class SlotTimeout(Exception):
    """No JobDiva slot came up within the caller's wait budget."""


_BUCKET_KEY = "jobdiva:bi:bucket"
_COOLDOWN_KEY = "jobdiva:cooldown"
_FG_WAITING_KEY = "jobdiva:bi:fg_waiting"
_REDIS_RETRY_S = 30.0

# Token bucket: refill one token every ``interval`` ms up to ``burst``; take one
# if available, else return the ms until one will be. Background callers keep
# ``reserve`` tokens untouched (their effective burst is burst - reserve) and
# never take a token while an interactive caller is waiting.
# KEYS: bucket, cooldown, fg_waiting. ARGV: interval_ms, burst, reserve, is_background.
# Uses Redis TIME so worker clocks don't matter. Returns {wait_ms, cooldown_ms}.
_TAKE_LUA = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local interval = math.max(tonumber(ARGV[1]), 1)
local burst = tonumber(ARGV[2])
local reserve = tonumber(ARGV[3])
local background = ARGV[4] == '1'
local b = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(b[1])
local ts = tonumber(b[2])
if tokens == nil or ts == nil then
  tokens = burst
  ts = now
end
if now > ts then
  tokens = math.min(burst, tokens + (now - ts) / interval)
  ts = now
end
local ttl = math.ceil(interval * burst) + 60000
local cooldown = redis.call('PTTL', KEYS[2])
if cooldown < 0 then cooldown = 0 end
local need = 1
if background then need = 1 + reserve end
local wait = 0
if cooldown > 0 then
  wait = cooldown
elseif tokens < need then
  wait = math.ceil((need - tokens) * interval)
elseif background and redis.call('EXISTS', KEYS[3]) == 1 then
  wait = interval
end
if wait > 0 then
  redis.call('HSET', KEYS[1], 'tokens', tostring(tokens), 'ts', tostring(ts))
  redis.call('PEXPIRE', KEYS[1], ttl)
  if not background then
    redis.call('SET', KEYS[3], '1', 'PX', wait + interval + 250)
  end
  return {wait, cooldown}
end
redis.call('HSET', KEYS[1], 'tokens', tostring(tokens - 1), 'ts', tostring(ts))
redis.call('PEXPIRE', KEYS[1], ttl)
return {0, 0}
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
_redis_down_until = 0.0

# In-process fallback state (monotonic seconds). The lock is created per
# event loop on first use, not at import.
_local_lock: Optional[Tuple[Any, asyncio.Lock]] = None
_local_tokens: Optional[float] = None  # None = full bucket
_local_ts = 0.0
_local_cooldown_until = 0.0
_local_fg_waiting_until = 0.0

_stats: Dict[str, int] = {"granted": 0, "timeouts": 0, "http_429": 0}
# Per-priority counters: bi_wait_ms (time spent waiting for a token, incl.
# cooldown), bi_cooldown_ms (the part of it spent in a 429 cooldown),
# bi_429_total.
_prio_stats: Dict[str, Dict[str, int]] = {}


def _bump(priority: str, name: str, value: int) -> None:
    d = _prio_stats.setdefault(priority, {"bi_wait_ms": 0, "bi_cooldown_ms": 0, "bi_429_total": 0})
    d[name] = d.get(name, 0) + int(value)


def stats() -> Dict[str, Any]:
    """Counters for this worker since start: slots granted, slot timeouts, 429s,
    plus ``by_priority`` -> {bi_wait_ms, bi_cooldown_ms, bi_429_total}."""
    out: Dict[str, Any] = dict(_stats)
    out["by_priority"] = {k: dict(v) for k, v in _prio_stats.items()}
    return out


def _record(outcome: str, label: str, priority: str, **extra: Any) -> None:
    try:
        from core.newrelic import record_custom_event
        record_custom_event("JobDivaBiLimiter", {
            "outcome": outcome, "label": label, "priority": priority, **extra,
        })
    except Exception:
        pass


def _emit_counters(priority: str, label: str) -> None:
    """Cumulative per-priority limiter counters -> NR ``JobDivaBILimiter`` + log."""
    d = dict(_prio_stats.get(priority) or {})
    log.info(
        "JOBDIVA_BI_COUNTERS priority=%s label=%s bi_wait_ms=%s bi_cooldown_ms=%s bi_429_total=%s",
        priority, label, d.get("bi_wait_ms", 0), d.get("bi_cooldown_ms", 0), d.get("bi_429_total", 0),
    )
    try:
        from core.newrelic import record_custom_event
        record_custom_event("JobDivaBILimiter", {"priority": priority, "label": label, **d})
    except Exception:
        pass


def _get_redis() -> Optional[Any]:
    global _redis_client
    if not getattr(_cfg, "REDIS_URL", ""):
        return None
    if time.monotonic() < _redis_down_until:
        return None
    if _redis_client is not None:
        return _redis_client
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
        _mark_redis_down(exc)
        return None


def _mark_redis_down(exc: Exception) -> None:
    """Skip Redis for a while after an error, then try it again."""
    global _redis_down_until
    if time.monotonic() >= _redis_down_until:
        log.warning(
            "jobdiva_rate_limit: redis error, pacing per-worker for %.0fs: %s",
            _REDIS_RETRY_S, exc,
        )
    _redis_down_until = time.monotonic() + _REDIS_RETRY_S


def _get_local_lock() -> asyncio.Lock:
    global _local_lock
    loop = asyncio.get_running_loop()
    if _local_lock is None or _local_lock[0] is not loop:
        _local_lock = (loop, asyncio.Lock())
    return _local_lock[1]


def _local_interval_s() -> float:
    """Per-worker spacing for the in-process fallback.

    No Redis configured (dev, single worker): the full budget. Redis
    configured but down: this worker's share of it.
    """
    if getattr(_cfg, "REDIS_URL", ""):
        return MIN_INTERVAL_S * LOCAL_WORKERS
    return MIN_INTERVAL_S


def _bucket_step(
    tokens: float, ts: float, now: float, interval: float, background: bool,
    fg_waiting: bool, cooldown: float,
) -> Tuple[float, float, float]:
    """Pure token-bucket step (seconds). Returns (wait, new_tokens, new_ts);
    wait == 0 means a token was taken. Mirrors ``_TAKE_LUA``."""
    if interval <= 0:  # pacing disabled
        return (cooldown if cooldown > 0 else 0.0), BURST, now
    if now > ts:
        tokens = min(BURST, tokens + (now - ts) / interval)
        ts = now
    need = 1.0 + ((BURST - BACKGROUND_BURST) if background else 0.0)
    if cooldown > 0:
        return cooldown, tokens, ts
    if tokens < need:
        return (need - tokens) * interval, tokens, ts
    if background and fg_waiting:
        return interval, tokens, ts
    return 0.0, tokens - 1.0, ts


async def _take_local(background: bool) -> Tuple[float, float]:
    global _local_tokens, _local_ts, _local_fg_waiting_until
    interval = _local_interval_s()
    async with _get_local_lock():
        now = time.monotonic()
        tokens = BURST if _local_tokens is None else _local_tokens
        ts = now if _local_tokens is None else _local_ts
        cooldown = max(_local_cooldown_until - now, 0.0)
        wait, _local_tokens, _local_ts = _bucket_step(
            tokens, ts, now, interval, background, now < _local_fg_waiting_until, cooldown,
        )
        if wait > 0 and not background:
            _local_fg_waiting_until = max(
                _local_fg_waiting_until, now + wait + interval + 0.25
            )
        return wait, cooldown


async def _take(background: bool) -> Tuple[float, float]:
    """(0, 0) if a token was taken, else (seconds until it's worth asking
    again, the part of that which is a 429 cooldown)."""
    client = _get_redis()
    if client is not None:
        try:
            res = await client.eval(
                _TAKE_LUA, 3, _BUCKET_KEY, _COOLDOWN_KEY, _FG_WAITING_KEY,
                int(MIN_INTERVAL_S * 1000), str(BURST), str(BURST - BACKGROUND_BURST),
                "1" if background else "0",
            )
            wait_ms, cooldown_ms = (res[0], res[1]) if isinstance(res, (list, tuple)) else (res, 0)
            return max(0, int(wait_ms)) / 1000.0, max(0, int(cooldown_ms)) / 1000.0
        except Exception as exc:
            _mark_redis_down(exc)
    return await _take_local(background)


async def acquire(
    max_wait_s: float = 120.0,
    *,
    priority: str = INTERACTIVE,
    label: str = "bi",
) -> bool:
    """Wait for a turn at the JobDiva BI quota.

    Returns False (without sending anything) when no slot comes up within
    ``max_wait_s`` — callers treat that like a failed fetch and let background
    hydration pick the ids up later. Background callers yield to waiting
    interactive ones.
    """
    if in_background():
        priority = BACKGROUND
    background = priority == BACKGROUND
    t0 = time.monotonic()
    deadline = t0 + max(0.0, max_wait_s)
    cooldown_s = 0.0
    while True:
        wait, cooldown = await _take(background)
        if wait == 0:
            _stats["granted"] += 1
            waited_ms = int((time.monotonic() - t0) * 1000)
            _bump(priority, "bi_wait_ms", waited_ms)
            _bump(priority, "bi_cooldown_ms", int(cooldown_s * 1000))
            if waited_ms >= 1000:
                _emit_counters(priority, label)
                _record("granted", label, priority, bi_wait_ms=waited_ms,
                        bi_cooldown_ms=int(cooldown_s * 1000))
            return True
        remaining = deadline - time.monotonic()
        if wait > remaining:
            _stats["timeouts"] += 1
            waited = time.monotonic() - t0
            _bump(priority, "bi_wait_ms", int(waited * 1000))
            _bump(priority, "bi_cooldown_ms", int(cooldown_s * 1000))
            log.warning(
                "JOBDIVA_BI_SLOT_TIMEOUT label=%s priority=%s waited=%.1fs budget=%.1fs next_wait=%.1fs",
                label, priority, waited, max_wait_s, wait,
            )
            _record("slot_timeout", label, priority, waited_s=round(waited, 2))
            return False
        cooldown_s += min(cooldown, wait)
        # Jitter so waiters woken for the same token don't all ask at once;
        # background waiters wake a little later so interactive ones win ties.
        extra = 0.2 if background else 0.0
        await asyncio.sleep(wait + extra + random.uniform(0, min(0.25, MIN_INTERVAL_S / 4)))


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


async def note_429(retry_after: Optional[str] = None, *, label: str = "bi", priority: str = INTERACTIVE) -> float:
    """Start (or extend) the shared cooldown after a JobDiva 429. Returns seconds."""
    global _local_cooldown_until
    secs = _parse_retry_after(retry_after)
    if secs is None:
        secs = random.uniform(COOLDOWN_MIN_S, COOLDOWN_MAX_S)
    secs = min(max(secs, 1.0), COOLDOWN_CAP_S)
    _stats["http_429"] += 1
    _bump(priority, "bi_429_total", 1)
    _local_cooldown_until = max(_local_cooldown_until, time.monotonic() + secs)
    client = _get_redis()
    if client is not None:
        try:
            await client.eval(_COOLDOWN_LUA, 1, _COOLDOWN_KEY, int(secs * 1000))
        except Exception as exc:
            _mark_redis_down(exc)
    log.warning(
        "JobDiva BI 429 (%s): shared cooldown %.1fs (Retry-After=%r)", label, secs, retry_after
    )
    _emit_counters(priority, label)
    _record("http_429", label, priority, cooldown_s=round(secs, 2), bi_429_total=_stats["http_429"])
    return secs


async def bi_get(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_wait_s: float = 120.0,
    priority: str = INTERACTIVE,
    label: str = "bi",
    **kwargs: Any,
) -> Optional[httpx.Response]:
    """``client.get`` gated by the shared limiter.

    Returns None when no slot came up within ``max_wait_s``. On a 429 it
    starts the shared cooldown and still returns the response, so the caller
    decides whether to retry (its next ``bi_get`` waits the cooldown out).
    """
    if not await acquire(max_wait_s, priority=priority, label=label):
        return None
    response = await client.get(url, **kwargs)
    if response.status_code == 429:
        await note_429(response.headers.get("Retry-After"), label=label, priority=priority)
    return response


async def bg_get(
    client: httpx.AsyncClient,
    url: str,
    *,
    label: str,
    max_wait_s: float = 120.0,
    **kwargs: Any,
) -> httpx.Response:
    """``client.get`` for JobDiva calls that are not always paced.

    Inside ``background_context()`` the call waits for a background slot
    (raising ``SlotTimeout`` if none comes up). Interactive callers go
    straight through, unchanged. Either way a 429 starts the shared
    cooldown so every other caller backs off.
    """
    if in_background():
        response = await bi_get(
            client, url, max_wait_s=max_wait_s, priority=BACKGROUND, label=label, **kwargs
        )
        if response is None:
            raise SlotTimeout(f"no JobDiva slot for {label} within {max_wait_s:.0f}s")
        return response
    response = await client.get(url, **kwargs)
    if response.status_code == 429:
        await note_429(response.headers.get("Retry-After"), label=label, priority=INTERACTIVE)
    return response
