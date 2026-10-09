"""Tracked fire-and-forget background tasks.

`asyncio.create_task` only keeps a weak reference to the task, so an untracked
task can be garbage-collected mid-flight, and an exception in it is reported
only as "Task exception was never retrieved" at GC time (if ever). `spawn`
keeps a strong reference until the task finishes, logs failures with the task
name, and `drain` lets shutdown wait for in-flight work before cancelling it.

Interim measure until a durable queue lands (pipeline optimization Fix 9).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Coroutine, Optional, Set

logger = logging.getLogger(__name__)

_TASKS: Set["asyncio.Task[Any]"] = set()


def _on_done(task: "asyncio.Task[Any]") -> None:
    _TASKS.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "background_task_failed name=%s error=%s",
            task.get_name(), exc,
            exc_info=(type(exc), exc, exc.__traceback__),
        )


def spawn(coro: Coroutine[Any, Any, Any], name: Optional[str] = None) -> "asyncio.Task[Any]":
    """Schedule `coro` on the running loop and track it until it completes."""
    if name is None:
        name = getattr(coro, "__qualname__", None)
    task = asyncio.get_running_loop().create_task(coro, name=name)
    _TASKS.add(task)
    task.add_done_callback(_on_done)
    return task


def pending_count() -> int:
    return sum(1 for t in _TASKS if not t.done())


async def drain(timeout: float = 25.0) -> None:
    """Wait up to `timeout` seconds for tracked tasks, then cancel the rest."""
    pending = [t for t in _TASKS if not t.done()]
    if not pending:
        return
    logger.info("background_tasks_drain pending=%d timeout=%.1fs", len(pending), timeout)
    _done, still = await asyncio.wait(pending, timeout=timeout)
    for t in still:
        logger.warning("background_task_cancelled_on_shutdown name=%s", t.get_name())
        t.cancel()
    if still:
        await asyncio.gather(*still, return_exceptions=True)
