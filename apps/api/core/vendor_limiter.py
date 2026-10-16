"""Cross-worker pacing and 429 cooldown for third-party APIs (OpenAI, Exa, ...).

The same idea as ``services/jobdiva_rate_limit.py``, without its priority
lanes: one quota per vendor account is shared by every uvicorn worker, so the
limit has to be shared too.

Per vendor, in Redis:

- ``vendor:<name>:next`` — earliest time (ms) the next request may start.
  Only used when ``min_interval_s`` > 0.
- ``vendor:<name>:cooldown`` — set on a 429 for ``Retry-After`` (or a jittered
  default). Every caller on every worker waits it out instead of retrying
  into the same exhausted quota.

- ``vendor:<name>:no_credits:<env>`` — the out-of-credits breaker (Exa only,
  ``no_credits_ttl_s`` > 0): set by the first worker that sees the vendor's
  "no credits" answer, so every worker skips the vendor until it expires.
  Separate from ``acquire()``, which it never changes: callers check
  ``no_credits_left()`` and turn it into their own fatal answer.

Fails closed: when Redis is configured but unreachable each worker paces at
``min_interval_s × local_workers``, its share of the budget, rather than the
whole budget per worker. Every interval and cooldown can be overridden with
``VENDOR_<NAME>_MIN_INTERVAL_S`` / ``_COOLDOWN_MIN_S`` / ``_COOLDOWN_MAX_S`` /
``_COOLDOWN_CAP_S`` / ``_NO_CREDITS_TTL_S`` and ``VENDOR_LOCAL_WORKERS``.
"""
from __future__ import annotations

import asyncio
import email.utils
import logging
import os
import random
import time
from typing import Any, Dict, Optional, Tuple

from core import config as _cfg

log = logging.getLogger(__name__)

_REDIS_RETRY_S = 30.0

# KEYS: next, cooldown. ARGV: interval_ms. Returns 0 when the slot was taken,
# else ms to wait. Redis TIME so worker clocks don't matter.
_TAKE_LUA = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local interval = tonumber(ARGV[1])
local nxt = tonumber(redis.call('GET', KEYS[1]) or '0')
local wait = math.max(nxt - now, redis.call('PTTL', KEYS[2]), 0)
if wait > 0 then
  return wait
end
if interval > 0 then
  redis.call('SET', KEYS[1], now + interval, 'PX', interval + 60000)
end
return 0
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


def _env_float(name: str, default: float) -> float:
    try:
        return float((os.getenv(name) or "").strip() or default)
    except ValueError:
        return default


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
    global _redis_down_until
    if time.monotonic() >= _redis_down_until:
        log.warning("vendor_limiter: redis error, pacing per-worker for %.0fs: %s", _REDIS_RETRY_S, exc)
    _redis_down_until = time.monotonic() + _REDIS_RETRY_S


def parse_retry_after(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    value = str(value).strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        dt = email.utils.parsedate_to_datetime(value)
        return max(0.0, dt.timestamp() - time.time())
    except Exception:
        return None


class VendorLimiter:
    def __init__(
        self,
        name: str,
        *,
        min_interval_s: float = 0.0,
        cooldown_min_s: float = 5.0,
        cooldown_max_s: float = 15.0,
        cooldown_cap_s: float = 60.0,
        no_credits_ttl_s: float = 0.0,
        label: str = "",
    ):
        env = f"VENDOR_{name.upper()}_"
        self.name = name
        self.label = label or name
        self.min_interval_s = _env_float(env + "MIN_INTERVAL_S", min_interval_s)
        self.cooldown_min_s = _env_float(env + "COOLDOWN_MIN_S", cooldown_min_s)
        self.cooldown_max_s = _env_float(env + "COOLDOWN_MAX_S", cooldown_max_s)
        self.cooldown_cap_s = _env_float(env + "COOLDOWN_CAP_S", cooldown_cap_s)
        # 0 = no out-of-credits breaker.
        self.no_credits_ttl_s = max(0.0, _env_float(env + "NO_CREDITS_TTL_S", no_credits_ttl_s))
        self.local_workers = max(1, int(_env_float("VENDOR_LOCAL_WORKERS", 8)))
        self._next_key = f"vendor:{name}:next"
        self._cooldown_key = f"vendor:{name}:cooldown"
        self._local_lock: Optional[Tuple[Any, asyncio.Lock]] = None
        self._local_next = 0.0
        self._local_cooldown_until = 0.0
        self._local_no_credits_until = 0.0
        self.stats: Dict[str, int] = {"granted": 0, "timeouts": 0, "http_429": 0, "no_credits": 0}

    # ---- slot ------------------------------------------------------------

    def _local_interval_s(self) -> float:
        if getattr(_cfg, "REDIS_URL", ""):
            return self.min_interval_s * self.local_workers
        return self.min_interval_s

    def _lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._local_lock is None or self._local_lock[0] is not loop:
            self._local_lock = (loop, asyncio.Lock())
        return self._local_lock[1]

    async def _take_local(self) -> float:
        async with self._lock():
            now = time.monotonic()
            wait = max(self._local_next - now, self._local_cooldown_until - now, 0.0)
            if wait > 0:
                return wait
            self._local_next = now + self._local_interval_s()
            return 0.0

    async def _take(self) -> float:
        client = _get_redis()
        if client is not None:
            try:
                wait_ms = await client.eval(
                    _TAKE_LUA, 2, self._next_key, self._cooldown_key,
                    int(self.min_interval_s * 1000),
                )
                return max(0, int(wait_ms)) / 1000.0
            except Exception as exc:
                _mark_redis_down(exc)
        return await self._take_local()

    async def acquire(self, max_wait_s: float = 60.0) -> bool:
        """Wait for a turn. False (nothing sent) if none comes up in ``max_wait_s``."""
        t0 = time.monotonic()
        deadline = t0 + max(0.0, max_wait_s)
        while True:
            wait = await self._take()
            if wait == 0:
                self.stats["granted"] += 1
                return True
            if wait > deadline - time.monotonic():
                self.stats["timeouts"] += 1
                log.warning(
                    "VENDOR_SLOT_TIMEOUT vendor=%s waited=%.1fs budget=%.1fs next_wait=%.1fs",
                    self.name, time.monotonic() - t0, max_wait_s, wait,
                )
                return False
            await asyncio.sleep(wait + random.uniform(0, 0.25))

    async def _cooldown_left(self) -> float:
        wait = max(self._local_cooldown_until - time.monotonic(), 0.0)
        client = _get_redis()
        if client is not None:
            try:
                ms = int(await client.pttl(self._cooldown_key))
                wait = max(wait, ms / 1000.0 if ms > 0 else 0.0)
            except Exception as exc:
                _mark_redis_down(exc)
        return wait

    async def wait_cooldown(self, max_wait_s: float = 60.0) -> bool:
        """Wait out only the shared 429 cooldown (no pacing slot): one PTTL
        per check instead of the pacing EVAL. For vendors with
        ``min_interval_s == 0``. False if it outlasts ``max_wait_s``."""
        deadline = time.monotonic() + max(0.0, max_wait_s)
        while True:
            wait = await self._cooldown_left()
            if wait == 0:
                return True
            if wait > deadline - time.monotonic():
                self.stats["timeouts"] += 1
                return False
            await asyncio.sleep(wait + random.uniform(0, 0.25))

    # ---- 429 -------------------------------------------------------------

    async def note_429(self, retry_after: Optional[str] = None) -> float:
        """Start (or extend) the shared cooldown. Returns its length in seconds."""
        secs = parse_retry_after(retry_after)
        if secs is None:
            secs = random.uniform(self.cooldown_min_s, self.cooldown_max_s)
        secs = min(max(secs, 1.0), self.cooldown_cap_s)
        self.stats["http_429"] += 1
        self._local_cooldown_until = max(self._local_cooldown_until, time.monotonic() + secs)
        client = _get_redis()
        if client is not None:
            try:
                await client.eval(_COOLDOWN_LUA, 1, self._cooldown_key, int(secs * 1000))
            except Exception as exc:
                _mark_redis_down(exc)
        log.warning("%s 429: shared cooldown %.1fs (Retry-After=%r)", self.name, secs, retry_after)
        return secs

    # ---- out of credits --------------------------------------------------

    def _no_credits_key(self) -> str:
        # Per environment: QA and PROD may share a Redis, and a QA key's
        # budget running out must not switch PROD's Exa off.
        from core import newrelic as _nr

        return f"vendor:{self.name}:no_credits:{_nr.environment()}"

    async def no_credits_left(self) -> float:
        """Seconds the out-of-credits breaker stays open (0 = closed). Adopts a
        breaker another worker opened (the Redis PTTL), mirrored locally."""
        if self.no_credits_ttl_s <= 0:
            return 0.0
        left = self._local_no_credits_until - time.monotonic()
        if left > 0:
            return left
        client = _get_redis()
        if client is not None:
            try:
                ms = int(await client.pttl(self._no_credits_key()))
            except Exception as exc:
                _mark_redis_down(exc)
                return 0.0
            if ms > 0:
                self._local_no_credits_until = time.monotonic() + ms / 1000.0
                return ms / 1000.0
        return 0.0

    async def trip_no_credits(self, tag: str = "") -> bool:
        """Open the breaker for ``no_credits_ttl_s`` on every worker. True when
        this call opened it: that caller logs the ERROR, so it appears once
        per window (once per worker per window while Redis is down). The first
        real call after the TTL is the probe; a 402 is not billed."""
        if self.no_credits_ttl_s <= 0:
            return False
        now = time.monotonic()
        opened = now >= self._local_no_credits_until
        self._local_no_credits_until = max(self._local_no_credits_until, now + self.no_credits_ttl_s)
        client = _get_redis()
        if client is not None:
            try:
                opened = bool(await client.set(
                    self._no_credits_key(), tag or "1", nx=True, px=int(self.no_credits_ttl_s * 1000),
                ))
            except Exception as exc:
                _mark_redis_down(exc)
        if opened:
            self.stats["no_credits"] += 1
            log.error(
                "%s is OUT OF CREDITS (%s): skipping %s for %ds on all workers; "
                "top up the account or raise the key's budget",
                self.label, tag or "402", self.label, int(self.no_credits_ttl_s),
            )
        return opened


# Initial budgets. All overridable per env (see module docstring).
# OpenAI: no fixed spacing (TPM-bound, many models); a shared cooldown on 429
# stops every worker's SDK retries from piling into the same exhausted limit.
OPENAI = VendorLimiter("openai", min_interval_s=0.0, cooldown_min_s=2.0, cooldown_max_s=8.0, cooldown_cap_s=30.0)
# Exa: ~5 requests/s across the account. Out-of-credits breaker 300 s
# (VENDOR_EXA_NO_CREDITS_TTL_S, 0 = off): short, because a top-up is only
# noticed by the first call after it expires (services/exa_errors.py).
EXA = VendorLimiter(
    "exa", min_interval_s=0.2, cooldown_min_s=10.0, cooldown_max_s=15.0, cooldown_cap_s=60.0,
    no_credits_ttl_s=300.0, label="Exa",
)
# Unipile: global cap of ~4 requests/s across every LinkedIn account and worker.
# LinkedIn 429s are per account and handled by the 15-min account bench in
# services/unipile.py, so this limiter only paces; it never starts a cooldown.
UNIPILE = VendorLimiter("unipile", min_interval_s=0.25)
