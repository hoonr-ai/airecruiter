"""Small in-process LRU dict for module-level caches.

Module-level ``dict`` caches live as long as the uvicorn worker and were
never evicted, so they grew with every distinct key the worker ever saw
(part of the 1.5–4.6 GB worker RSS and the kernel OOM kills). This keeps
the dict API those call sites use (``in``, ``[]``, ``get``, assignment)
and evicts the least recently used entry past ``maxsize``. Not thread-safe;
these caches are only touched from the event loop.
"""
from __future__ import annotations

from collections import OrderedDict
from typing import Any, Hashable


class BoundedDict(OrderedDict):
    def __init__(self, maxsize: int):
        super().__init__()
        self.maxsize = max(1, int(maxsize))

    def __getitem__(self, key: Hashable) -> Any:
        value = super().__getitem__(key)
        # Not self.move_to_end(key): confirmed by direct testing (not a
        # documented CPython internal, so stated as observed behavior, not a
        # claimed mechanism) that calling it here makes dict.pop() on this
        # same key raise a spurious KeyError for a key that is actually
        # still present — pop()'s own lookup/relink and this call's relink
        # land on the same OrderedDict machinery while pop() is still
        # mid-call. Calling OrderedDict's own __delitem__ + __setitem__
        # directly (neither overridden here) moves the key to the end
        # without going through move_to_end at all, which doesn't hit this.
        # Not atomic against a concurrent reader between the two calls, but
        # per this module's own docstring these caches are event-loop-only,
        # never touched from more than one thread.
        OrderedDict.__delitem__(self, key)
        OrderedDict.__setitem__(self, key, value)
        return value

    def get(self, key: Hashable, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def __setitem__(self, key: Hashable, value: Any) -> None:
        if key in self:
            self.move_to_end(key)
        super().__setitem__(key, value)
        while len(self) > self.maxsize:
            # popitem() reads through __getitem__, which would move the
            # oldest key to the end and then fail to delete it.
            oldest = next(iter(self))
            super().__delitem__(oldest)


# get_or_load's token_fn returns this when the current generation can't be
# read (e.g. Redis down): the lookup goes straight to the loader, uncached.
BYPASS = object()


class AsyncTTLCache:
    """Short-lived, size-bounded cache for async read-only lookups.

    ``get_or_load`` returns a fresh cached value, joins a load already in
    flight for the same key (a burst of identical lookups costs one call), or
    runs ``loader``. Only values ``keep(value)`` approves are stored, so
    errors are never cached. Hits are deep copies, so a caller mutating its
    result can't change what the next caller gets. ``ttl_s <= 0`` disables it.

    ``token_fn`` (async) names the key's current generation, e.g. a counter
    every write to the record bumps, shared across processes. A hit counts
    only if it was stored under the current token, and a load is stored only
    if the token didn't change while it ran — so a write elsewhere, or one
    that lands mid-load, is never answered with the pre-write value.
    """

    def __init__(self, maxsize: int, ttl_s: float, clock=None):
        import time

        self.ttl_s = float(ttl_s)
        self._clock = clock or time.monotonic
        self._values: BoundedDict = BoundedDict(maxsize)
        self._loading: dict = {}

    async def get_or_load(self, key: Hashable, loader, keep=lambda v: v is not None, token_fn=None) -> Any:
        import asyncio
        import copy

        if self.ttl_s <= 0:
            return await loader()
        token = await token_fn() if token_fn is not None else None
        if token is BYPASS:
            return await loader()
        hit = self._values.get(key)
        if hit is not None and hit[1] == token and self._clock() - hit[0] < self.ttl_s:
            return copy.deepcopy(hit[2])
        loop = asyncio.get_running_loop()
        # Keyed with the token: a lookup after a write never joins a load
        # that started before it.
        flight = (key, token)
        pending = self._loading.get(flight)
        if pending is not None and pending[0] is loop:
            return copy.deepcopy(await asyncio.shield(pending[1]))
        task = loop.create_task(loader())
        # If every caller waiting on it is cancelled, nobody awaits the task:
        # retrieve its outcome so a failure isn't logged as "Task exception
        # was never retrieved".
        task.add_done_callback(lambda t: t.cancelled() or t.exception())
        self._loading[flight] = (loop, task)
        try:
            value = await asyncio.shield(task)
        finally:
            if self._loading.get(flight, (None, None))[1] is task:
                del self._loading[flight]
        if keep(value) and (token_fn is None or await token_fn() == token):
            self._values[key] = (self._clock(), token, copy.deepcopy(value))
        return value

    def invalidate(self, key: Hashable) -> None:
        self._values.pop(key, None)
