"""Agent-loop plugin hooks — before_turn, filter_history, before_compact, model and tool hooks."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any

from felix_ai.types import ChatMessage, ModelChatOptions, ModelChatResult, ModelClient

from felix.observability.metrics import record_counter

logger = logging.getLogger("felix.hooks")

# Every hook is called positionally — `hook(value, ctx)`, and `after_tool` as
# `hook(tool_call, result, is_error, ctx)` — so the parameters are spelled out rather than `...`:
# a plugin hook written against `**kwargs` raised on every call, and nothing said so.
BeforeTurnHook = Callable[
    [list[ChatMessage], dict[str, Any]],
    Awaitable[list[ChatMessage] | None] | list[ChatMessage] | None,
]
FilterHistoryHook = Callable[
    [list[ChatMessage], dict[str, Any]],
    Awaitable[list[ChatMessage] | None] | list[ChatMessage] | None,
]
BeforeCompactHook = Callable[
    [dict[str, Any], dict[str, Any]], Awaitable[dict[str, Any] | None] | dict[str, Any] | None
]
BeforeToolHook = Callable[
    [dict[str, Any], dict[str, Any]], Awaitable[dict[str, Any] | None] | dict[str, Any] | None
]
AfterToolHook = Callable[
    [dict[str, Any], Any, bool, dict[str, Any]],
    Awaitable[dict[str, Any] | None] | dict[str, Any] | None,
]
CompactFailedHook = Callable[[dict[str, Any], dict[str, Any]], Awaitable[None] | None]
BeforeModelHook = Callable[
    [dict[str, Any], dict[str, Any]], Awaitable[dict[str, Any] | None] | dict[str, Any] | None
]
AfterModelHook = Callable[
    [dict[str, Any], dict[str, Any]], Awaitable[dict[str, Any] | None] | dict[str, Any] | None
]


@dataclass
class AgentHookRegistry:
    before_turn: list[BeforeTurnHook] = field(default_factory=list)
    filter_history: list[FilterHistoryHook] = field(default_factory=list)
    before_compact: list[BeforeCompactHook] = field(default_factory=list)
    before_tool: list[BeforeToolHook] = field(default_factory=list)
    after_tool: list[AfterToolHook] = field(default_factory=list)
    compact_failed: list[CompactFailedHook] = field(default_factory=list)
    before_model: list[BeforeModelHook] = field(default_factory=list)
    after_model: list[AfterModelHook] = field(default_factory=list)
    # Hooks that have already logged a failure at warning, by `id`. Safe as a key because the
    # registry holds every hook it names, so no id is reused while it is here.
    warned: set[int] = field(default_factory=set)

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

    def register_before_model(self, hook: BeforeModelHook) -> None:
        self.before_model.append(hook)

    def register_after_model(self, hook: AfterModelHook) -> None:
        self.after_model.append(hook)


_hooks = AgentHookRegistry()


def get_agent_hooks() -> AgentHookRegistry:
    return _hooks


def reset_agent_hooks() -> None:
    """Test helper — clear all registered hooks."""
    global _hooks
    _hooks = AgentHookRegistry()


# What `_call` returns for a hook that raised, so a runner can tell it from a hook that
# returned `None` on purpose.
_FAILED: Any = object()


def _name(hook: Callable[..., Any]) -> str:
    return f"{getattr(hook, '__module__', '?')}.{getattr(hook, '__qualname__', type(hook).__name__)}"


def _note_misbehaviour(kind: str, hook: Callable[..., Any], what: str, *, exc_info: bool) -> None:
    """Log a hook that failed or answered in the wrong shape: loudly once, quietly after.

    Hooks fail open, so a broken one is skipped on every call and the run carries on. At debug
    level that was indistinguishable from a hook that worked — the reference plugin's
    `before_tool` raised on every tool call for as long as it existed. A warning on each call
    would bury the log under one bad hook instead, so the first failure of each hook is a
    warning with the traceback and the rest are debug; `felix_hook_failures` counts them all.
    """
    record_counter("felix_hook_failures", {"hook": kind})
    key = id(hook)
    if key in _hooks.warned:
        logger.debug("%s hook %s %s", kind, _name(hook), what, exc_info=exc_info)
        return
    _hooks.warned.add(key)
    logger.warning(
        "%s hook %s %s; skipped, here and on every later call it fails (logged at debug from now on)",
        kind,
        _name(hook),
        what,
        exc_info=exc_info,
    )


async def _call(kind: str, hook: Callable[..., Any], *args: Any) -> Any:
    """Call one hook, sync or async; `_FAILED` if it raised, which is logged and not re-raised."""
    try:
        result = hook(*args)
        if inspect.isawaitable(result):
            result = await result
        return result
    except Exception:
        _note_misbehaviour(kind, hook, "raised", exc_info=True)
        return _FAILED


async def run_before_turn(
    messages: list[ChatMessage],
    *,
    context: dict[str, Any] | None = None,
) -> list[ChatMessage]:
    """Allow hooks to inject messages before a model turn. Returns messages to prepend."""
    injected: list[ChatMessage] = []
    ctx = context or {}
    for hook in list(_hooks.before_turn):
        result = await _call("before_turn", hook, messages, ctx)
        if result is not _FAILED and result:
            injected.extend(list(result))
    return injected


async def run_filter_history(
    history: list[ChatMessage],
    *,
    context: dict[str, Any] | None = None,
) -> list[ChatMessage]:
    current = list(history)
    ctx = context or {}
    for hook in list(_hooks.filter_history):
        result = await _call("filter_history", hook, current, ctx)
        if result is not _FAILED and result is not None:
            current = list(result)
    return current


async def run_before_compact(
    preparation: dict[str, Any],
    *,
    context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return a custom compaction dict ``{summary, covers_to_seq, ...}`` or None."""
    ctx = context or {}
    for hook in list(_hooks.before_compact):
        result = await _call("before_compact", hook, preparation, ctx)
        if isinstance(result, dict):
            if result.get("cancel"):
                return {"cancel": True}
            if "summary" in result or "compaction" in result:
                return result
    return None


async def run_before_tool(
    tool_call: dict[str, Any],
    *,
    context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Preflight a tool call. Return ``{block: true, reason?, terminate?}`` to deny."""
    ctx = context or {}
    for hook in list(_hooks.before_tool):
        result = await _call("before_tool", hook, tool_call, ctx)
        if isinstance(result, dict) and result:
            return result
    return None


async def run_after_tool(
    tool_call: dict[str, Any],
    result: Any,
    *,
    is_error: bool = False,
    context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Postprocess a tool result. May return ``{terminate: true, content?}``.

    On a failed call ``result`` is the error text the model would see, and ``content`` replaces it.
    """
    ctx = context or {}
    merged: dict[str, Any] = {}
    for hook in list(_hooks.after_tool):
        out = await _call("after_tool", hook, tool_call, result, is_error, ctx)
        if isinstance(out, dict):
            merged.update(out)
    return merged or None


async def run_compact_failed(
    info: dict[str, Any],
    *,
    context: dict[str, Any] | None = None,
) -> None:
    ctx = context or {}
    for hook in list(_hooks.compact_failed):
        await _call("compact_failed", hook, info, ctx)


async def run_before_model(
    messages: list[ChatMessage],
    *,
    tools: list[str],
    context: dict[str, Any] | None = None,
) -> list[ChatMessage]:
    """Return the messages one model call sends; a hook returns ``{messages: [...]}`` to replace them.

    The replacement goes to this call only. The run's history is untouched, so whatever a hook
    leaves out is still there for the next call and for the session log; `filter_history` is the
    hook that changes the history itself. Hooks run in order, each seeing the list the one before
    it returned. Build a new list and new messages rather than editing these in place: they are
    the run's own objects.
    """
    current = list(messages)
    ctx = context or {}
    for hook in list(_hooks.before_model):
        result = await _call("before_model", hook, {"messages": list(current), "tools": list(tools)}, ctx)
        if not isinstance(result, dict) or "messages" not in result:
            continue
        replaced = result["messages"]
        # Checked here rather than left to the wire encoder, where a stray dict fails the
        # whole run instead of this one hook.
        if not isinstance(replaced, list) or not all(isinstance(m, ChatMessage) for m in replaced):
            _note_misbehaviour(
                "before_model", hook, "returned something other than a list of messages", exc_info=False
            )
            continue
        current = list(replaced)
    return current


async def run_after_model(
    message: ChatMessage,
    *,
    stop_reason: str | None,
    context: dict[str, Any] | None = None,
) -> ChatMessage:
    """Return the assistant message a model call produced; a hook returns ``{message: ...}`` to replace it.

    The replacement is what the run records, persists and acts on — tool calls included — but
    not what has already streamed: text deltas reach the client as the model writes them, before
    any hook sees the turn. Hooks run in order, each seeing the message the one before it returned.
    """
    current = message
    ctx = context or {}
    for hook in list(_hooks.after_model):
        result = await _call("after_model", hook, {"message": current, "stop_reason": stop_reason}, ctx)
        if not isinstance(result, dict) or "message" not in result:
            continue
        replaced = result["message"]
        if not isinstance(replaced, ChatMessage) or replaced.role != "assistant":
            _note_misbehaviour("after_model", hook, "returned a non-assistant message", exc_info=False)
            continue
        current = replaced
    return current


def model_hook_context(
    model: Any, *, manifest_id: str | None, thread_id: str | None, purpose: str
) -> dict[str, Any]:
    """The `ctx` both model hooks receive.

    `purpose` says what the call is for, so a hook can act on one kind and leave the rest:
    `turn` (a react step), `router` (choosing a sub-agent), `reflect` (scoring a draft),
    `plan` (planning or replanning), `synthesis` (composing a composite pattern's answer).
    """
    return {
        "manifest_id": manifest_id,
        "thread_id": thread_id,
        "model_id": getattr(model, "model_id", None),
        "purpose": purpose,
    }


async def chat_with_model_hooks(
    model: ModelClient,
    messages: list[ChatMessage],
    tools: list[Any],
    opts: ModelChatOptions | None = None,
    *,
    context: dict[str, Any],
) -> ModelChatResult:
    """One `model.chat` with `before_model` ahead of it and `after_model` on its reply.

    For a call site that makes a single, unstreamed request. The react loop's main turn does
    not come through here: it streams, retries on overflow, and fixes up the stop reason, so it
    runs the two halves itself. Usage stays the model's — metering the result is the caller's.
    """
    outgoing = await run_before_model(
        messages, tools=[str(getattr(t, "name", t)) for t in tools], context=context
    )
    result = await model.chat(outgoing, tools, opts)
    message = await run_after_model(
        result.message, stop_reason=getattr(result, "stop_reason", None), context=context
    )
    return result if message is result.message else replace(result, message=message)


__all__ = [
    "AgentHookRegistry",
    "chat_with_model_hooks",
    "get_agent_hooks",
    "model_hook_context",
    "reset_agent_hooks",
    "run_after_model",
    "run_after_tool",
    "run_before_compact",
    "run_before_model",
    "run_before_tool",
    "run_before_turn",
    "run_compact_failed",
    "run_filter_history",
]
