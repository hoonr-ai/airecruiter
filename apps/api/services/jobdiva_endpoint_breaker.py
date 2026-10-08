"""Circuit breaker for JobDiva endpoints that keep returning the same error.

`JobSubmittalsDetail` started returning 404 for every job, and AutoSync
called it for every monitored job every cycle: ~59k errors a week, all
spent from the shared JobDiva quota. After `threshold` consecutive
failures the breaker opens for `open_s`; while open, callers skip the
request entirely and one WARNING is logged per open window.

State is shared across workers via Redis (`jobdiva:breaker:<name>`, with a
TTL) and mirrored in-process so it still works when Redis is unavailable.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, Optional

from services import jobdiva_rate_limit as _rl

log = logging.getLogger(__name__)


class EndpointBreaker:
    def __init__(self, name: str, threshold: int = 5, open_s: float = 6 * 60 * 60):
        self.name = name
        self.threshold = threshold
        self.open_s = open_s
        self._failures = 0
        self._open_until = 0.0

    @property
    def redis_key(self) -> str:
        return f"jobdiva:breaker:{self.name}"

    async def is_open(self) -> bool:
        if time.monotonic() < self._open_until:
            return True
        client = _rl._get_redis()
        if client is None:
            return False
        try:
            ttl_ms = await client.pttl(self.redis_key)
        except Exception as exc:
            _rl._mark_redis_down(exc)
            return False
        if ttl_ms and ttl_ms > 0:
            # Another worker opened it; mirror locally to skip the Redis
            # round trip on every call until it expires.
            self._open_until = time.monotonic() + ttl_ms / 1000.0
            return True
        return False

    def record_success(self) -> None:
        self._failures = 0

    async def record_failure(self, status_code: int) -> None:
        self._failures += 1
        if self._failures < self.threshold:
            return
        self._failures = 0
        self._open_until = time.monotonic() + self.open_s
        log.warning(
            "jobdiva breaker %s OPEN for %.0fh after %d consecutive %s responses; "
            "skipping calls until it expires",
            self.name, self.open_s / 3600, self.threshold, status_code,
        )
        client = _rl._get_redis()
        if client is None:
            return
        try:
            await client.set(self.redis_key, str(status_code), px=int(self.open_s * 1000))
        except Exception as exc:
            _rl._mark_redis_down(exc)


_breakers: Dict[str, EndpointBreaker] = {}


def get(name: str, threshold: int = 5, open_s: float = 6 * 60 * 60) -> EndpointBreaker:
    breaker: Optional[EndpointBreaker] = _breakers.get(name)
    if breaker is None:
        breaker = _breakers[name] = EndpointBreaker(name, threshold, open_s)
    return breaker
