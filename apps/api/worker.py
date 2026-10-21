"""arq worker for durable background jobs (see core/queue.py).

Run with:  arq worker.WorkerSettings
Deployed as the airecruiter-worker systemd unit, next to the API.
"""

from __future__ import annotations

import functools
import logging
from typing import Any, Callable

from arq import cron, func as arq_func

import core.queue_tasks  # noqa: F401  (registers the tasks)
from core.queue import redis_settings, registered_tasks

logger = logging.getLogger("worker")


def _strip_ctx(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    async def wrapper(ctx: dict, *args: Any, **kwargs: Any) -> Any:
        logger.info("queue_job_start job_id=%s try=%s", ctx.get("job_id"), ctx.get("job_try"))
        return await fn(*args, **kwargs)

    return wrapper


async def _relay_outbox(ctx: dict) -> None:
    from services import task_outbox

    await task_outbox.relay_once()


async def _purge_outbox(ctx: dict) -> None:
    import asyncio

    from services import task_outbox

    await asyncio.to_thread(task_outbox.purge_old)


async def _startup(ctx: dict) -> None:
    logger.info("arq worker started with tasks: %s", ", ".join(sorted(registered_tasks())))


class WorkerSettings:
    functions = [
        arq_func(_strip_ctx(t.func), name=t.name, max_tries=t.max_tries, timeout=t.timeout_s)
        for t in registered_tasks().values()
    ]
    redis_settings = redis_settings()
    on_startup = _startup
    # Transactional outbox relay (services/task_outbox.py), every 10 s.
    # The relay runs tasks itself, so its timeout must cover the longest one
    # (arq's default job timeout is 300 s).
    cron_jobs = [
        cron(
            _relay_outbox, second=set(range(0, 60, 10)), run_at_startup=True, unique=True,
            timeout=max(t.timeout_s for t in registered_tasks().values()) + 300,
        ),
        cron(_purge_outbox, hour=3, minute=17, unique=True),
    ]
    max_jobs = 4
    # Let a job finish during a deploy restart instead of being cancelled and retried.
    job_completion_wait = 600
