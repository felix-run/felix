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
    # Whether this builder reads `ctx["output_schema"]` and gets it onto the model call that
    # produces the user-visible answer. Declared rather than inferred, and default `False`,
    # because the failure it prevents is silent: `spec.output_schema` validates, compiles and
    # reaches every pattern context, so a builder that ignores it leaves a manifest field
    # reading as a contract while the answer comes back as free text. Worse on a composite,
    # where the schema can reach an *intermediate* turn and shape the one nobody sees.
    #
    # A flag rather than a list of pattern names in the manifest schema: the registry is open,
    # so a plugin's pattern must be able to say yes for itself.
    honours_output_schema: bool = False


_patterns: dict[str, PatternDescriptor] = {}


def register_pattern(
    name: str,
    build: PatternBuilder,
    *,
    kind: PatternKind = "single-agent",
    honours_output_schema: bool = False,
) -> None:
    _patterns[name] = PatternDescriptor(build=build, kind=kind, honours_output_schema=honours_output_schema)


def get_pattern(name: str) -> PatternBuilder | None:
    desc = _patterns.get(name)
    return desc.build if desc else None


def get_pattern_descriptor(name: str) -> PatternDescriptor | None:
    return _patterns.get(name)


def list_patterns() -> list[str]:
    return list(_patterns.keys())


def honours_output_schema(name: str) -> bool:
    """Whether this pattern will actually enforce a declared `spec.output_schema`."""
    desc = _patterns.get(name)
    return desc is not None and desc.honours_output_schema


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
    "honours_output_schema",
    "is_multi_agent_pattern",
    "list_patterns",
    "register_pattern",
    "reset_pattern_registry",
]
