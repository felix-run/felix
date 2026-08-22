"""ToolProvider — resolve manifest tool ids into Tool instances."""

from __future__ import annotations

import builtins
from collections.abc import Callable, Sequence
from typing import Protocol, runtime_checkable

from felix.tools.types import Tool

ToolFactory = Callable[[], Tool]


@runtime_checkable
class ToolProvider(Protocol):
    def get(self, name: str) -> Tool: ...
    def resolve(self, names: Sequence[str]) -> builtins.list[Tool]: ...
    # Method name shadows builtin list; annotate via builtins.list.
    def list(self) -> builtins.list[str]: ...
    def has(self, name: str) -> bool: ...


class InMemoryToolProvider:
    def __init__(self, factories: dict[str, ToolFactory] | None = None) -> None:
        self._factories: dict[str, ToolFactory] = dict(factories or {})
        self._cache: dict[str, Tool] = {}

    def register(self, name: str, factory: ToolFactory) -> None:
        self._factories[name] = factory
        self._cache.pop(name, None)

    def has(self, name: str) -> bool:
        return name in self._factories

    def get(self, name: str) -> Tool:
        cached = self._cache.get(name)
        if cached is not None:
            return cached
        factory = self._factories.get(name)
        if factory is None:
            raise KeyError(f"Unknown tool: {name}")
        tool = factory()
        self._cache[name] = tool
        return tool

    def resolve(self, names: Sequence[str]) -> builtins.list[Tool]:
        seen: set[str] = set()
        out: builtins.list[Tool] = []
        for name in names:
            if name in seen:
                continue
            seen.add(name)
            out.append(self.get(name))
        return out

    def list(self) -> builtins.list[str]:
        return builtins.list(self._factories.keys())

    # Alias used by some composition call sites.
    def list_names(self) -> builtins.list[str]:
        return self.list()


__all__ = ["InMemoryToolProvider", "ToolFactory", "ToolProvider"]
