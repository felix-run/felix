"""Open pattern registry — builders register at import time."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from felix.patterns.types import Agent

PatternKind = Literal["single-agent", "multi-agent"]
PatternBuildContext = dict[str, Any]
PatternBuilder = Callable[[PatternBuildContext], Agent | Awaitable[Agent]]


@dataclass(slots=True)
class PatternDescriptor:
    build: PatternBuilder
    kind: PatternKind = "single-agent"


_patterns: dict[str, PatternDescriptor] = {}


def register_pattern(
    name: str,
    build: PatternBuilder,
    *,
    kind: PatternKind = "single-agent",
) -> None:
    _patterns[name] = PatternDescriptor(build=build, kind=kind)


def get_pattern(name: str) -> PatternBuilder | None:
    desc = _patterns.get(name)
    return desc.build if desc else None


def get_pattern_descriptor(name: str) -> PatternDescriptor | None:
    return _patterns.get(name)


def list_patterns() -> list[str]:
    return list(_patterns.keys())


def is_multi_agent_pattern(name: str) -> bool:
    desc = _patterns.get(name)
    return desc is not None and desc.kind == "multi-agent"


def reset_pattern_registry() -> None:
    _patterns.clear()


__all__ = [
    "PatternBuildContext",
    "PatternBuilder",
    "PatternDescriptor",
    "PatternKind",
    "get_pattern",
    "get_pattern_descriptor",
    "is_multi_agent_pattern",
    "list_patterns",
    "register_pattern",
    "reset_pattern_registry",
]
