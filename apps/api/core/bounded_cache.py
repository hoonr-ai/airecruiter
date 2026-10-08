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
            self.popitem(last=False)
