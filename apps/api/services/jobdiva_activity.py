"""Markers for jobs being actively searched or launched, shared across workers.

While a recruiter search (``search_candidates``) or a PAIR launch is running
for a job, background JobDiva work — detail hydration and applicant AutoSync —
backs off so the interactive path gets the BI quota.

Keys (Redis, shared with the BI limiter's client):

- ``jd:active:{job_id}`` — TTL ``JOBDIVA_ACTIVE_JOB_TTL_S``; refreshed by
  callers during long work. Cleared when the work ends.
- ``jd:active:index`` — sorted set of active job ids scored by expiry (ms), so
  "is any OTHER job active?" needs no SCAN. Expired members are pruned lazily.

Who yields to what:

- AutoSync yields to any active job, its own included (a launch for the same
  job is exactly the work it must not compete with).
- Hydration started by a search yields only to OTHER jobs: the search that
  marked its own job active spawned that hydration, so pausing on its own
  marker would deadlock it until the TTL ran out.

No Redis (or a Redis error) → marks are no-ops and every check says inactive.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import os
import time
from typing import Iterator, Optional

from core import config as _cfg
from services import jobdiva_rate_limit as _rl

log = logging.getLogger(__name__)

_PREFIX = "jd:active:"
_INDEX_KEY = "jd:active:index"

# How often a paused background worker re-checks, and the most it waits per page.
POLL_S = 5.0
MAX_PAUSE_S = 120.0


def _max_yield_s() -> float:
    try:
        return max(0.0, float(os.getenv("JOBDIVA_AUTOSYNC_MAX_YIELD_S", "300") or 300))
    except ValueError:
        return 300.0


class YieldBudget:
    """Total seconds a background run (one AutoSync) may spend paused across
    all its pages. Once spent, waits return immediately so a busy system
    cannot starve the run indefinitely."""

    def __init__(self, total_s: float, label: str = "autosync"):
        self.total_s = float(total_s)
        self.used_s = 0.0
        self.label = label
        self.exhausted_logged = False

    @property
    def remaining_s(self) -> float:
        return max(0.0, self.total_s - self.used_s)


_BUDGET: "contextvars.ContextVar[Optional[YieldBudget]]" = contextvars.ContextVar(
    "jobdiva_yield_budget", default=None
)


@contextlib.contextmanager
def yield_budget(total_s: Optional[float] = None, label: str = "autosync") -> Iterator[YieldBudget]:
    """Scope a yield budget over a run. Tasks spawned inside inherit it via
    the context. Nested use reuses the outer budget (one cap per run)."""
    outer = _BUDGET.get()
    if outer is not None:
        yield outer
        return
    budget = YieldBudget(_max_yield_s() if total_s is None else total_s, label)
    token = _BUDGET.set(budget)
    try:
        yield budget
    finally:
        _BUDGET.reset(token)


def _ttl_s() -> int:
    return max(1, int(getattr(_cfg, "JOBDIVA_ACTIVE_JOB_TTL_S", 900) or 900))


def _key(job_id: str) -> str:
    return f"{_PREFIX}{job_id}"


async def mark_job_active(job_id: str) -> None:
    """Mark (or refresh) ``job_id`` as actively searched/launched."""
    if not job_id:
        return
    client = _rl._get_redis()
    if client is None:
        return
    ttl = _ttl_s()
    try:
        pipe = client.pipeline(transaction=False)
        pipe.set(_key(str(job_id)), "1", ex=ttl)
        pipe.zadd(_INDEX_KEY, {str(job_id): int((time.time() + ttl) * 1000)})
        pipe.expire(_INDEX_KEY, ttl + 60)
        await pipe.execute()
    except Exception as exc:
        _rl._mark_redis_down(exc)


async def clear_job_active(job_id: str) -> None:
    if not job_id:
        return
    client = _rl._get_redis()
    if client is None:
        return
    try:
        pipe = client.pipeline(transaction=False)
        pipe.delete(_key(str(job_id)))
        pipe.zrem(_INDEX_KEY, str(job_id))
        await pipe.execute()
    except Exception as exc:
        _rl._mark_redis_down(exc)


async def is_job_active(job_id: str) -> bool:
    if not job_id:
        return False
    client = _rl._get_redis()
    if client is None:
        return False
    try:
        return bool(await client.exists(_key(str(job_id))))
    except Exception as exc:
        _rl._mark_redis_down(exc)
        return False


async def other_job_active(job_id: Optional[str]) -> bool:
    """True when some job other than ``job_id`` is actively searched/launched."""
    client = _rl._get_redis()
    if client is None:
        return False
    try:
        now_ms = int(time.time() * 1000)
        await client.zremrangebyscore(_INDEX_KEY, "-inf", now_ms)
        members = await client.zrange(_INDEX_KEY, 0, 20)
    except Exception as exc:
        _rl._mark_redis_down(exc)
        return False
    me = str(job_id) if job_id else None
    for m in members or []:
        m = m.decode() if isinstance(m, bytes) else str(m)
        if m != me:
            return True
    return False


async def wait_while_busy(
    job_id: Optional[str],
    *,
    include_self: bool,
    label: str,
    max_pause_s: float = MAX_PAUSE_S,
    poll_s: float = POLL_S,
) -> float:
    """Pause a background JobDiva page while interactive work is running.

    ``include_self`` — also pause while ``job_id`` itself is active (AutoSync);
    hydration passes False. Waits at most ``max_pause_s`` so a stuck marker
    never stalls background work for longer than that per page. Returns the
    seconds paused.

    Inside ``yield_budget()`` the pause also counts against the run's total
    (``JOBDIVA_AUTOSYNC_MAX_YIELD_S``); once that is spent the run proceeds.
    """
    budget = _BUDGET.get()
    if budget is not None:
        if budget.remaining_s <= 0:
            if not budget.exhausted_logged:
                budget.exhausted_logged = True
                log.warning(
                    "JOBDIVA_BG_YIELD_BUDGET_EXHAUSTED label=%s run=%s job=%s used=%.1fs; proceeding",
                    label, budget.label, job_id, budget.used_s,
                )
            return 0.0
        max_pause_s = min(max_pause_s, budget.remaining_s)
    t0 = time.monotonic()
    while time.monotonic() - t0 < max_pause_s:
        busy = await other_job_active(job_id)
        if not busy and include_self and job_id:
            busy = await is_job_active(job_id)
        if not busy:
            break
        await asyncio.sleep(poll_s)
    paused = time.monotonic() - t0
    if budget is not None:
        budget.used_s += paused
    if paused >= 1.0:
        log.info("JOBDIVA_BG_PAUSED label=%s job=%s paused=%.1fs", label, job_id, paused)
    return paused


class active_job:
    """``async with active_job(job_id):`` marks the job active, refreshes the
    marker every third of the TTL, and clears it on exit."""

    def __init__(self, job_id: Optional[str]):
        self.job_id = str(job_id) if job_id else ""
        self._task: Optional[asyncio.Task] = None

    async def _refresh(self) -> None:
        interval = max(5.0, _ttl_s() / 3)
        while True:
            await asyncio.sleep(interval)
            await mark_job_active(self.job_id)

    async def __aenter__(self) -> "active_job":
        if self.job_id:
            await mark_job_active(self.job_id)
            from core.tasks import spawn
            self._task = spawn(self._refresh(), name=f"active_job_refresh:{self.job_id}")
        return self

    async def __aexit__(self, *exc) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        if self.job_id:
            await clear_job_active(self.job_id)
