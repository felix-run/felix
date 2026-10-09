"""A least-recently-used mapping with a size ceiling, for process-global caches."""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any


class BoundedCache:
    """A mapping that forgets its least-recently-used entry instead of growing.

    Deliberately not a `dict` subclass. The first version was, and inheriting from
    `dict` meant overriding `get` -- whose stdlib signature is positional-only with a
    key typed `object` -- which produced a stream of type errors for no benefit. The
    resolver needs five operations; a class that offers exactly those five has no
    signature to conflict with, and it cannot be handed somewhere that quietly expects
    the other thirty.

    With ``ttl_s``, an entry also lapses that many seconds after it was set (monotonic
    clock): `get` drops it and answers the default. The compile-path caches each hand-rolled
    this as an expiry stored beside the value, at a different tuple index each time.
    """

    __slots__ = ("_data", "_expires", "_maxsize", "_ttl_s")

    def __init__(self, maxsize: int, *, ttl_s: float | None = None) -> None:
        self._data: OrderedDict[str, Any] = OrderedDict()
        self._expires: dict[str, float] = {}
        self._maxsize = maxsize
        self._ttl_s = ttl_s

    def get(self, key: str, default: Any = None) -> Any:
        if key not in self._data:
            return default
        if self._ttl_s is not None and self._expires.get(key, 0.0) <= time.monotonic():
            self.pop(key)
            return default
        self._data.move_to_end(key)
        return self._data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._data[key] = value
        self._data.move_to_end(key)
        if self._ttl_s is not None:
            self._expires[key] = time.monotonic() + self._ttl_s
        while len(self._data) > self._maxsize:
            oldest, _ = self._data.popitem(last=False)
            self._expires.pop(oldest, None)

    def pop(self, key: str, default: Any = None) -> Any:
        self._expires.pop(key, None)
        return self._data.pop(key, default)

    def clear(self) -> None:
        self._data.clear()
        self._expires.clear()

    def __len__(self) -> int:
        return len(self._data)

    def __contains__(self, key: str) -> bool:
        return key in self._data


__all__ = ["BoundedCache"]
