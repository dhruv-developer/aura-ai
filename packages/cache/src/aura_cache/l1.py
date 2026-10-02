"""Bounded in-process TTL + LRU cache (per pod)."""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

_MISSING = object()


class L1Cache:
    def __init__(
        self, maxsize: int = 10_000, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        if maxsize <= 0:
            raise ValueError("maxsize must be positive")
        self._data: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._maxsize = maxsize
        self._clock = clock

    def get(self, key: str, default: Any = _MISSING) -> Any:
        item = self._data.get(key)
        if item is None:
            return default
        expires_at, value = item
        if expires_at <= self._clock():
            del self._data[key]
            return default
        self._data.move_to_end(key)
        return value

    def set(self, key: str, value: Any, ttl_s: float) -> None:
        self._data[key] = (self._clock() + ttl_s, value)
        self._data.move_to_end(key)
        while len(self._data) > self._maxsize:
            self._data.popitem(last=False)

    def delete(self, key: str) -> None:
        self._data.pop(key, None)

    def delete_prefix(self, prefix: str) -> int:
        doomed = [k for k in self._data if k.startswith(prefix)]
        for k in doomed:
            del self._data[k]
        return len(doomed)

    def clear(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)

    MISSING = _MISSING
