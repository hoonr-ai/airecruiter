"""Durable background jobs on arq (Redis).

`enqueue` puts a registered task on the arq queue, where the separate worker
process (`worker.py`, systemd unit `airecruiter-worker`) runs it. Unlike
`core.tasks.spawn`, a queued job survives an API restart and arq re-runs a job
whose worker died mid-flight, up to the task's ``max_tries``.

Tasks must be registered with `@task` so the worker can find them by name and
so `enqueue` can fall back to running them in-process. The fallback (via
`core.tasks.spawn`) is used when ``ARQ_ENABLED`` is off, ``REDIS_URL`` is
empty, or the enqueue itself fails, so the API keeps working exactly as it did
before the queue existed. Turn ``ARQ_ENABLED`` on only once the worker is
deployed, or queued jobs will sit in Redis unprocessed.

Task bodies may run more than once (retry after a crash, fallback after a
failed enqueue that actually landed), so tasks with ``max_tries > 1`` must be
idempotent. For a ``max_tries=1`` task the fallback runs only when the job
never reached Redis (the pool could not connect); if ``enqueue_job`` itself
fails the outcome is unknown, so `enqueue` returns ``"failed"`` rather than
risk running it twice. Pass a ``job_id`` to have arq drop a duplicate while
one is queued.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

from core import config as _cfg

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Task:
    name: str
    func: Callable[..., Awaitable[Any]]
    max_tries: int
    timeout_s: int


_REGISTRY: Dict[str, _Task] = {}
_pool: Any = None
_pool_lock: Optional[asyncio.Lock] = None


def task(name: str, *, max_tries: int = 1, timeout_s: int = 1800) -> Callable:
    """Register a coroutine function as a queue task under ``name``."""

    def deco(func: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        if name in _REGISTRY and _REGISTRY[name].func is not func:
            raise ValueError(f"queue task {name!r} registered twice")
        _REGISTRY[name] = _Task(name, func, max_tries, timeout_s)
        return func

    return deco


def registered_tasks() -> Dict[str, _Task]:
    return dict(_REGISTRY)


def queue_enabled() -> bool:
    return bool(getattr(_cfg, "ARQ_ENABLED", False) and getattr(_cfg, "REDIS_URL", ""))


def redis_settings() -> Any:
    from arq.connections import RedisSettings

    # Empty REDIS_URL (local dev, tests): arq's localhost default, so the
    # worker module still imports; enqueue never uses it (queue_enabled()).
    if not getattr(_cfg, "REDIS_URL", ""):
        return RedisSettings()
    return RedisSettings.from_dsn(_cfg.REDIS_URL)


async def _get_pool() -> Any:
    global _pool, _pool_lock
    if _pool is None:
        if _pool_lock is None:
            _pool_lock = asyncio.Lock()
        async with _pool_lock:
            if _pool is None:
                from arq import create_pool

                _pool = await create_pool(redis_settings())
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        try:
            await _pool.aclose()
        except Exception as exc:  # noqa: BLE001
            logger.warning("arq pool close failed: %s", exc)
        _pool = None


async def enqueue(name: str, *args: Any, job_id: Optional[str] = None, **kwargs: Any) -> str:
    """Queue task ``name``; fall back to an in-process spawn if the queue is unavailable.

    Returns ``"queued"``, ``"duplicate"`` (arq already holds ``job_id``),
    ``"spawned"``, or ``"failed"`` (a ``max_tries=1`` task whose enqueue may or
    may not have landed; it is not run, to avoid a duplicate side effect).
    """
    t = _REGISTRY.get(name)
    if t is None:
        raise KeyError(f"unknown queue task {name!r}")

    if queue_enabled():
        try:
            pool = await _get_pool()
        except Exception as exc:  # noqa: BLE001
            logger.error("queue_pool_failed task=%s error=%s; running in-process", name, exc)
        else:
            try:
                job = await pool.enqueue_job(name, *args, _job_id=job_id, **kwargs)
            except Exception as exc:  # noqa: BLE001
                if t.max_tries <= 1:
                    logger.error("queue_enqueue_failed task=%s error=%s; not retry-safe, dropped", name, exc)
                    return "failed"
                logger.error("queue_enqueue_failed task=%s error=%s; running in-process", name, exc)
            else:
                if job is None:
                    logger.info("queue_enqueue_duplicate task=%s job_id=%s", name, job_id)
                    return "duplicate"
                logger.info("queue_enqueued task=%s job_id=%s", name, job.job_id)
                return "queued"

    from core.tasks import spawn

    spawn(t.func(*args, **kwargs), name=f"{name}:{job_id or 'inline'}")
    return "spawned"
