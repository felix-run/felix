"""Open pattern registry — builders register at import time.

A builder receives a `PatternBuildContext`, a plain dict the compile fills (`manifests/builder.py`).
Two keys a plugin pattern reads most: `extensions` (`dict`, `spec.extensions` -- written by whoever
holds `manifests:write`, so tenant input, never trusted operator config) and `manifest_secrets`
(`SecretsProvider | None`, the secrets backend scoped to the compiling tenant by
`FELIX_MANIFEST_SECRETS`; `None` with no settings). A `secret:NAME` found in `extensions` resolves
through `manifest_secrets`, never `felix.secrets.build_secrets`.
"""

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
    # Whether this builder hands `ctx["hooks"]` (`spec.hooks`) to a loop that fires them. Declared
    # for the same reason: a manifest's hooks reach every pattern context, and one that ignored
    # them would compile an author's controls into nothing.
    honours_hooks: bool = False
    # Whether the answer the person reads is written under this agent's own system prompt, where
    # `spec.output_styles` appends the chosen style. A router's chosen child writes it under the
    # child's prompt, so a style declared on the router would change nothing.
    honours_output_styles: bool = False


_patterns: dict[str, PatternDescriptor] = {}


def register_pattern(
    name: str,
    build: PatternBuilder,
    *,
    kind: PatternKind = "single-agent",
    honours_output_schema: bool = False,
    honours_hooks: bool = False,
    honours_output_styles: bool = False,
) -> None:
    _patterns[name] = PatternDescriptor(
        build=build,
        kind=kind,
        honours_output_schema=honours_output_schema,
        honours_hooks=honours_hooks,
        honours_output_styles=honours_output_styles,
    )


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


def honours_hooks(name: str) -> bool:
    """Whether this pattern will actually fire a manifest's `spec.hooks`."""
    desc = _patterns.get(name)
    return desc is not None and desc.honours_hooks


def honours_output_styles(name: str) -> bool:
    """Whether this pattern's own system prompt writes the answer, so a chosen style reaches it."""
    desc = _patterns.get(name)
    return desc is not None and desc.honours_output_styles


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
    "honours_hooks",
    "honours_output_schema",
    "honours_output_styles",
    "is_multi_agent_pattern",
    "list_patterns",
    "register_pattern",
    "reset_pattern_registry",
]
