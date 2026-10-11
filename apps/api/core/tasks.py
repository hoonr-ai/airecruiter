"""Tracked fire-and-forget background tasks.

`asyncio.create_task` only keeps a weak reference to the task, so an untracked
task can be garbage-collected mid-flight, and an exception in it is reported
only as "Task exception was never retrieved" at GC time (if ever). `spawn`
keeps a strong reference until the task finishes, logs failures with the task
name, and `drain` lets shutdown wait for in-flight work before cancelling it.

Interim measure until a durable queue lands (pipeline optimization Fix 9).

New Relic: a spawned coroutine first drops the trace it inherited from the
task that spawned it (``core.newrelic.detach_inherited_trace``), then runs in
its own background transaction (``core.newrelic.background_transaction``),
named after ``name`` up to the first ':' (the rest becomes the ``task_key``
attribute). Pass ``nr_txn=False`` when the coroutine opens its own
transactions (the launch-session runner opens one per stage); it is detached
either way, so those open as new transactions, not spans of the request.

``stopping`` is set first thing in lifespan shutdown. Long-running work
checks ``is_stopping()`` between units and must not start new side effects
once it is set.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from typing import Any, Coroutine, Optional, Set

logger = logging.getLogger(__name__)

_TASKS: Set["asyncio.Task[Any]"] = set()

# Spawn names (prefix before ':') that never get a transaction: long-lived
# keepalive loops would be one endless, empty transaction each.
_NO_NR_TXN_PREFIXES = ("active_job_refresh",)


class _StopFlag:
    """asyncio.Event-like shutdown flag: is_set()/set()/clear()/await wait().

    A plain asyncio.Event binds to the first loop that waits on it; the test
    suite runs many loops, so waiters get a per-loop Event instead.
    """

    def __init__(self) -> None:
        self._flag = False
        self._events: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Event]" = weakref.WeakKeyDictionary()

    def is_set(self) -> bool:
        return self._flag

    def set(self) -> None:
        self._flag = True
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        for loop, event in list(self._events.items()):
            try:
                if loop is running:
                    event.set()
                elif not loop.is_closed():
                    loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                pass

    def clear(self) -> None:
        """Tests only: a process that started shutting down never resumes."""
        self._flag = False
        for event in list(self._events.values()):
            event.clear()

    async def wait(self) -> bool:
        if self._flag:
            return True
        loop = asyncio.get_running_loop()
        event = self._events.get(loop)
        if event is None:
            event = asyncio.Event()
            self._events[loop] = event
        if self._flag:
            return True
        await event.wait()
        return True


stopping = _StopFlag()


def is_stopping() -> bool:
    return stopping.is_set()


def mark_stopping() -> None:
    """Lifespan shutdown calls this before stopping the scheduler."""
    stopping.set()


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


async def _detached(coro: Coroutine[Any, Any, Any], name: str, txn: bool) -> Any:
    from core.newrelic import background_transaction, detach_inherited_trace

    detach_inherited_trace()
    if not txn:
        return await coro
    async with background_transaction(name):
        return await coro


def _nr_enabled() -> bool:
    try:
        from core.newrelic import is_enabled

        return is_enabled()
    except Exception:
        return False


def spawn(
    coro: Coroutine[Any, Any, Any], name: Optional[str] = None, nr_txn: bool = True
) -> "asyncio.Task[Any]":
    """Schedule `coro` on the running loop and track it until it completes.

    nr_txn: run it in its own New Relic background transaction (no-op when the
    agent is off). False when `coro` opens its own transactions. Detached from
    the spawning request's trace either way.
    """
    if name is None:
        name = getattr(coro, "__qualname__", None)
    runner = coro
    if _nr_enabled():
        txn = nr_txn and (name or "").split(":", 1)[0] not in _NO_NR_TXN_PREFIXES
        runner = _detached(coro, name or "task", txn)
    task = asyncio.get_running_loop().create_task(runner, name=name)
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
