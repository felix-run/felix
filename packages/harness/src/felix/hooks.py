"""Agent-loop plugin hooks — before_turn, filter_history, before_compact, tool hooks."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("felix.hooks")

BeforeTurnHook = Callable[..., Awaitable[list[Any] | None] | list[Any] | None]
FilterHistoryHook = Callable[..., Awaitable[list[Any]] | list[Any]]
BeforeCompactHook = Callable[..., Awaitable[dict[str, Any] | None] | dict[str, Any] | None]
BeforeToolHook = Callable[..., Awaitable[dict[str, Any] | None] | dict[str, Any] | None]
AfterToolHook = Callable[..., Awaitable[dict[str, Any] | None] | dict[str, Any] | None]
CompactFailedHook = Callable[..., Awaitable[None] | None]


@dataclass
class AgentHookRegistry:
    before_turn: list[BeforeTurnHook] = field(default_factory=list)
    filter_history: list[FilterHistoryHook] = field(default_factory=list)
    before_compact: list[BeforeCompactHook] = field(default_factory=list)
    before_tool: list[BeforeToolHook] = field(default_factory=list)
    after_tool: list[AfterToolHook] = field(default_factory=list)
    compact_failed: list[CompactFailedHook] = field(default_factory=list)

    def register_before_turn(self, hook: BeforeTurnHook) -> None:
        self.before_turn.append(hook)

    def register_filter_history(self, hook: FilterHistoryHook) -> None:
        self.filter_history.append(hook)

    def register_before_compact(self, hook: BeforeCompactHook) -> None:
        self.before_compact.append(hook)

    def register_before_tool(self, hook: BeforeToolHook) -> None:
        self.before_tool.append(hook)

    def register_after_tool(self, hook: AfterToolHook) -> None:
        self.after_tool.append(hook)

    def register_compact_failed(self, hook: CompactFailedHook) -> None:
        self.compact_failed.append(hook)


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


async def run_before_tool(
    tool_call: dict[str, Any],
    *,
    context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Preflight a tool call. Return ``{block: true, reason?, terminate?}`` to deny."""
    ctx = context or {}
    for hook in list(_hooks.before_tool):
        try:
            result = hook(tool_call, ctx)
            if hasattr(result, "__await__"):
                result = await result  # type: ignore[misc]
            if isinstance(result, dict) and result.get("block"):
                return result
            if isinstance(result, dict) and result:
                return result
        except Exception:
            logger.debug("before_tool hook failed", exc_info=True)
    return None


async def run_after_tool(
    tool_call: dict[str, Any],
    result: Any,
    *,
    is_error: bool = False,
    context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Postprocess a tool result. May return ``{terminate: true, details?, content?}``."""
    ctx = context or {}
    merged: dict[str, Any] = {}
    for hook in list(_hooks.after_tool):
        try:
            out = hook(tool_call, result, is_error, ctx)
            if hasattr(out, "__await__"):
                out = await out  # type: ignore[misc]
            if isinstance(out, dict):
                merged.update(out)
        except Exception:
            logger.debug("after_tool hook failed", exc_info=True)
    return merged or None


async def run_compact_failed(
    info: dict[str, Any],
    *,
    context: dict[str, Any] | None = None,
) -> None:
    ctx = context or {}
    for hook in list(_hooks.compact_failed):
        try:
            result = hook(info, ctx)
            if hasattr(result, "__await__"):
                await result  # type: ignore[misc]
        except Exception:
            logger.debug("compact_failed hook failed", exc_info=True)


__all__ = [
    "AgentHookRegistry",
    "get_agent_hooks",
    "reset_agent_hooks",
    "run_after_tool",
    "run_before_compact",
    "run_before_tool",
    "run_before_turn",
    "run_compact_failed",
    "run_filter_history",
]
