"""A least-recently-used mapping with a size ceiling, for process-global caches."""

from __future__ import annotations

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
    """

    __slots__ = ("_data", "_maxsize")

    def __init__(self, maxsize: int) -> None:
        self._data: OrderedDict[str, Any] = OrderedDict()
        self._maxsize = maxsize

    def get(self, key: str, default: Any = None) -> Any:
        if key not in self._data:
            return default
        self._data.move_to_end(key)
        return self._data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._data[key] = value
        self._data.move_to_end(key)
        while len(self._data) > self._maxsize:
            self._data.popitem(last=False)

    def pop(self, key: str, default: Any = None) -> Any:
        return self._data.pop(key, default)

    def clear(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)

    def __contains__(self, key: str) -> bool:
        return key in self._data


__all__ = ["BoundedCache"]
