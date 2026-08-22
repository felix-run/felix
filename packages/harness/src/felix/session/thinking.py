"""Thinking levels — map discrete levels onto token budgets."""

from __future__ import annotations

from typing import Literal

ThinkingLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"]

THINKING_LEVELS: tuple[ThinkingLevel, ...] = (
    "off",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)

THINKING_BUDGETS: dict[ThinkingLevel, int | None] = {
    "off": None,
    "minimal": 128,
    "low": 512,
    "medium": 1024,
    "high": 2048,
    "xhigh": 8192,
    "max": 32000,
}


def parse_thinking_level(raw: str | None) -> ThinkingLevel:
    if not raw:
        return "off"
    level = str(raw).strip().lower()
    if level in THINKING_BUDGETS:
        return level  # type: ignore[return-value]
    raise ValueError(f"unknown_thinking_level:{raw}")


def budget_for_level(
    level: ThinkingLevel | str,
    *,
    overrides: dict[str, int] | None = None,
) -> int | None:
    parsed = parse_thinking_level(level) if isinstance(level, str) else level
    if overrides and parsed in overrides:
        return int(overrides[parsed])
    return THINKING_BUDGETS.get(parsed)


def apply_thinking_to_spec(spec: object, level: ThinkingLevel | str) -> object:
    """Return a ModelSpec-like object with thinking_budget set from level."""
    from copy import deepcopy

    from felix.manifests.schema import ModelSpec

    budget = budget_for_level(level)
    if isinstance(spec, ModelSpec):
        data = spec.model_dump()
        data["thinking_budget"] = budget
        data["thinking_level"] = parse_thinking_level(level if isinstance(level, str) else level)
        return ModelSpec.model_validate(data)
    try:
        clone = deepcopy(spec)
        setattr(clone, "thinking_budget", budget)
        setattr(clone, "thinking_level", parse_thinking_level(str(level)))
        return clone
    except Exception:
        return spec


__all__ = [
    "THINKING_BUDGETS",
    "THINKING_LEVELS",
    "ThinkingLevel",
    "apply_thinking_to_spec",
    "budget_for_level",
    "parse_thinking_level",
]
