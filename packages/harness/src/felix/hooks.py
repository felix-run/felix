"""Agent-loop plugin hooks — before_turn, filter_history, before_compact."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("felix.hooks")

BeforeTurnHook = Callable[..., Awaitable[list[Any] | None] | list[Any] | None]
FilterHistoryHook = Callable[..., Awaitable[list[Any]] | list[Any]]
BeforeCompactHook = Callable[..., Awaitable[dict[str, Any] | None] | dict[str, Any] | None]


@dataclass
class AgentHookRegistry:
    before_turn: list[BeforeTurnHook] = field(default_factory=list)
    filter_history: list[FilterHistoryHook] = field(default_factory=list)
    before_compact: list[BeforeCompactHook] = field(default_factory=list)

    def register_before_turn(self, hook: BeforeTurnHook) -> None:
        self.before_turn.append(hook)

    def register_filter_history(self, hook: FilterHistoryHook) -> None:
        self.filter_history.append(hook)

    def register_before_compact(self, hook: BeforeCompactHook) -> None:
        self.before_compact.append(hook)


_hooks = AgentHookRegistry()


def get_agent_hooks() -> AgentHookRegistry:
    return _hooks


def reset_agent_hooks() -> None:
    """Test helper — clear all registered hooks."""
    global _hooks
    _hooks = AgentHookRegistry()


async def run_before_turn(
    messages: list[Any],
    *,
    context: dict[str, Any] | None = None,
) -> list[Any]:
    """Allow hooks to inject messages before a model turn. Returns messages to prepend."""
    injected: list[Any] = []
    ctx = context or {}
    for hook in list(_hooks.before_turn):
        try:
            result = hook(messages, ctx)
            if hasattr(result, "__await__"):
                result = await result  # type: ignore[misc]
            if result:
                injected.extend(list(result))
        except Exception:
            logger.debug("before_turn hook failed", exc_info=True)
    return injected


async def run_filter_history(
    history: list[Any],
    *,
    context: dict[str, Any] | None = None,
) -> list[Any]:
    current = list(history)
    ctx = context or {}
    for hook in list(_hooks.filter_history):
        try:
            result = hook(current, ctx)
            if hasattr(result, "__await__"):
                result = await result  # type: ignore[misc]
            if result is not None:
                current = list(result)
        except Exception:
            logger.debug("filter_history hook failed", exc_info=True)
    return current


async def run_before_compact(
    preparation: dict[str, Any],
    *,
    context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return a custom compaction dict ``{summary, covers_to_seq, ...}`` or None."""
    ctx = context or {}
    for hook in list(_hooks.before_compact):
        try:
            result = hook(preparation, ctx)
            if hasattr(result, "__await__"):
                result = await result  # type: ignore[misc]
            if isinstance(result, dict):
                if result.get("cancel"):
                    return {"cancel": True}
                if "summary" in result or "compaction" in result:
                    return result
        except Exception:
            logger.debug("before_compact hook failed", exc_info=True)
    return None


__all__ = [
    "AgentHookRegistry",
    "get_agent_hooks",
    "reset_agent_hooks",
    "run_before_compact",
    "run_before_turn",
    "run_filter_history",
]
