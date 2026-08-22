"""ToolProvider — resolve manifest tool ids into Tool instances."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from felix.tools.types import Tool

ToolFactory = Callable[[], Tool]


@runtime_checkable
class ToolProvider(Protocol):
    def get(self, name: str) -> Tool: ...
    def resolve(self, names: list[str] | tuple[str, ...]) -> list[Tool]: ...
    def list(self) -> list[str]: ...
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

    def resolve(self, names: list[str] | tuple[str, ...]) -> list[Tool]:
        seen: set[str] = set()
        out: list[Tool] = []
        for name in names:
            if name in seen:
                continue
            seen.add(name)
            out.append(self.get(name))
        return out

    def list(self) -> list[str]:
        return list(self._factories.keys())

    # Alias used by some composition call sites.
    def list_names(self) -> list[str]:
        return self.list()


__all__ = ["InMemoryToolProvider", "ToolFactory", "ToolProvider"]
