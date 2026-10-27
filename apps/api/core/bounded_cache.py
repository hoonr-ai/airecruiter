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
        self.move_to_end(key)
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


class AsyncTTLCache:
    """Short-lived, size-bounded cache for async read-only lookups.

    ``get_or_load`` returns a fresh cached value, joins a load already in
    flight for the same key (a burst of identical lookups costs one call), or
    runs ``loader``. Only values ``keep(value)`` approves are stored, so
    errors are never cached. Hits are deep copies, so a caller mutating its
    result can't change what the next caller gets. ``ttl_s <= 0`` disables it.
    """

    def __init__(self, maxsize: int, ttl_s: float, clock=None):
        import time

        self.ttl_s = float(ttl_s)
        self._clock = clock or time.monotonic
        self._values: BoundedDict = BoundedDict(maxsize)
        self._loading: dict = {}

    async def get_or_load(self, key: Hashable, loader, keep=lambda v: v is not None) -> Any:
        import asyncio
        import copy

        if self.ttl_s <= 0:
            return await loader()
        hit = self._values.get(key)
        if hit is not None and self._clock() - hit[0] < self.ttl_s:
            return copy.deepcopy(hit[1])
        loop = asyncio.get_running_loop()
        pending = self._loading.get(key)
        if pending is not None and pending[0] is loop:
            return copy.deepcopy(await asyncio.shield(pending[1]))
        task = loop.create_task(loader())
        self._loading[key] = (loop, task)
        try:
            value = await asyncio.shield(task)
        finally:
            if self._loading.get(key, (None, None))[1] is task:
                del self._loading[key]
        if keep(value):
            self._values[key] = (self._clock(), copy.deepcopy(value))
        return value

    def invalidate(self, key: Hashable) -> None:
        self._values.pop(key, None)
