"""react pattern — canonical tool-calling loop."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from felix.config import get_settings
from felix.hooks import run_after_tool, run_before_tool, run_before_turn, run_filter_history
from felix.manifests.schema import ABSOLUTE_LIMITS, ModelSpec
from felix.observability.metrics import record_counter
from felix.observability.tracing import with_span
from felix.patterns.model import build_model, record_usage
from felix.patterns.registry import PatternBuildContext, register_pattern
from felix.patterns.types import (
    Agent,
    ChatMessage,
    Event,
    InvokeInput,
    InvokeOutput,
    ToolCall,
)
from felix.side_events import drain as drain_side_events
from felix.side_events import release as release_side_events
from felix.steer import (
    clear_abort,
    clear_cancel_flag,
    drain_follow_up,
    drain_steer,
    ensure_run_queue,
    is_aborted,
    release_run_queue,
    should_cancel_remaining_tools,
)
from felix.tools.errors import infer_error_code, read_tool_error_code, tool_output_content
from felix.tools.retrieval import select_tools_from_ctx
from felix.tools.types import Tool, ToolInvocationCtx, is_wrapper_deny

logger = logging.getLogger("felix.patterns.react")

DEFAULT_RECURSION = 10
_SEQUENTIAL_TRANSPORTS = frozenset({"client", "approval"})


def _status_for_stop(stop: str) -> str:
    """Session status for a terminal stop reason.

    `max_tokens` means the answer is cut off mid-thought; `refusal` means a safety
    classifier declined. Neither is a completed turn, and recording either as one hides
    a partial or absent answer behind a successful-looking run.
    """
    if stop == "max_tokens":
        return "truncated"
    if stop == "refusal":
        return "refused"
    return "complete"


def _clamp(value: int, ceiling: int) -> int:
    return min(max(value, 1), ceiling)


def _model_spec_with_override(spec: Any, model_id: str | None) -> Any:
    if not model_id or spec is None:
        return spec
    if isinstance(spec, ModelSpec):
        data = spec.model_dump()
        data["id"] = model_id
        return ModelSpec.model_validate(data)
    # duck-typed
    try:
        clone = deepcopy(spec)
        clone.id = model_id
        return clone
    except Exception:
        return spec


@dataclass
class _ReactAgent:
    tools: list[Tool]
    pattern: str
    manifest_id: str
    manifest_version: str
    system_prompt: str
    model_spec: Any
    settings: Any
    recursion_limit: int
    limits: Any = None
    context_prelude: str = ""
    session_store: Any | None = None
    session_strategy: Any | None = None
    tenant_id: str = "default"
    memory_capture: Any | None = None
    tools_retrieval: Any | None = None
    procedural_memory: Any | None = None
    tool_execution: str = "sequential"
    steering_mode: str = "all"
    follow_up_mode: str = "all"
    compact_after_turn: bool = False
    _tool_map: dict[str, Tool] = field(init=False, repr=False)
    _last_model_id: str | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self._tool_map = {t.name: t for t in self.tools}

    def _active_tools(self, messages: list[ChatMessage]) -> list[Tool]:
        return select_tools_from_ctx(self.tools, messages, self.tools_retrieval)

    def _batch_mode(self, calls: list[ToolCall]) -> str:
        mode = self.tool_execution or "sequential"
        if mode != "parallel":
            return "sequential"
        for call in calls:
            tool = self._tool_map.get(call.name)
            if tool is None:
                continue
            transport = getattr(tool.executor, "transport", "local")
            if transport in _SEQUENTIAL_TRANSPORTS:
                return "sequential"
            if getattr(tool, "execution_mode", None) == "sequential":
                return "sequential"
        return "parallel"

    async def _dispatch(self, call: ToolCall, thread_id: str | None) -> tuple[str, ChatMessage, bool]:
        """Return (kind, tool_message, terminate)."""
        preflight = await run_before_tool(
            {"id": call.id, "name": call.name, "args": call.args},
            context={"manifest_id": self.manifest_id, "thread_id": thread_id},
        )
        if preflight and preflight.get("block"):
            reason = str(preflight.get("reason") or "blocked by before_tool hook")
            terminate = bool(preflight.get("terminate"))
            return (
                "ok",
                ChatMessage(
                    role="tool",
                    tool_call_id=call.id,
                    name=call.name,
                    content=f"[error/blocked] {reason}",
                ),
                terminate,
            )

        async def _run(span: Any) -> tuple[str, ChatMessage, bool]:
            tool = self._tool_map.get(call.name)
            if tool is None:
                span.set_attribute("status", "error")
                record_counter(
                    "felix_tool_calls",
                    {
                        "transport": "unknown",
                        "status": "error",
                        "manifest_id": self.manifest_id,
                    },
                )
                return (
                    "ok",
                    ChatMessage(
                        role="tool",
                        tool_call_id=call.id,
                        name=call.name,
                        content=f"[error/invalid_arguments] unknown tool: {call.name}",
                    ),
                    False,
                )
            span.set_attribute("tool.transport", tool.executor.transport)
            try:
                result = await tool.executor.execute(
                    call.args,
                    ToolInvocationCtx(
                        manifest_id=self.manifest_id,
                        tool_call_id=call.id,
                        thread_id=thread_id,
                    ),
                )
                content = tool_output_content(result)
                err = read_tool_error_code(result)
                status = "denied" if is_wrapper_deny(result) else ("error" if err else "ok")
                record_counter(
                    "felix_tool_calls",
                    {
                        "transport": tool.executor.transport,
                        "status": status,
                        "manifest_id": self.manifest_id,
                    },
                )
                from felix.audit.emit import emit_agent_audit

                emit_agent_audit(
                    "tool_call" if status != "denied" else "policy_deny",
                    status=status,
                    manifest_id=self.manifest_id,
                    payload={
                        "tool": call.name,
                        "tool_call_id": call.id,
                        "thread_id": thread_id,
                    },
                )
                after = await run_after_tool(
                    {"id": call.id, "name": call.name, "args": call.args},
                    result,
                    is_error=bool(err),
                    context={"manifest_id": self.manifest_id, "thread_id": thread_id},
                )
                terminate = bool(after and after.get("terminate"))
                if after and after.get("content") is not None:
                    content = str(after["content"])
                return (
                    "ok",
                    ChatMessage(
                        role="tool",
                        tool_call_id=call.id,
                        name=call.name,
                        content=content,
                    ),
                    terminate,
                )
            except Exception as exc:
                code = infer_error_code(exc)
                span.set_attribute("error", True)
                record_counter(
                    "felix_tool_calls",
                    {
                        "transport": tool.executor.transport,
                        "status": "error",
                        "error_code": code.value,
                        "manifest_id": self.manifest_id,
                    },
                )
                after = await run_after_tool(
                    {"id": call.id, "name": call.name, "args": call.args},
                    None,
                    is_error=True,
                    context={"manifest_id": self.manifest_id, "thread_id": thread_id},
                )
                terminate = bool(after and after.get("terminate"))
                if tool.fatal:
                    return (
                        "fatal",
                        ChatMessage(
                            role="tool",
                            tool_call_id=call.id,
                            name=call.name,
                            content=f"[fatal/{code.value}] {exc}",
                        ),
                        terminate,
                    )
                return (
                    "ok",
                    ChatMessage(
                        role="tool",
                        tool_call_id=call.id,
                        name=call.name,
                        content=f"[error/{code.value}] {exc}",
                    ),
                    terminate,
                )

        return await with_span("tool.call", _run, {"tool": call.name})

    def _resolve_model(self, input: InvokeInput) -> Any:
        settings = self.settings or get_settings()
        spec = _model_spec_with_override(self.model_spec, input.model_id)
        # Apply live thinking level from thread meta or input when present.
        level = getattr(input, "thinking_level", None)
        if level:
            from felix.session.thinking import apply_thinking_to_spec

            spec = apply_thinking_to_spec(spec, level)
        elif getattr(spec, "thinking_level", None):
            from felix.session.thinking import apply_thinking_to_spec

            spec = apply_thinking_to_spec(spec, spec.thinking_level)
        return build_model(settings, spec)

    def _apply_handoff(
        self, messages: list[ChatMessage], *, previous: str | None, next_id: str | None
    ) -> list[ChatMessage]:
        if not next_id:
            return messages
        try:
            from felix.session.handoff import handoff_system_message

            note = handoff_system_message(messages, previous_model=previous, next_model=next_id)
            if note is not None:
                # Insert after the first system message when present.
                if messages and messages[0].role == "system":
                    return [messages[0], note, *messages[1:]]
                return [note, *messages]
        except Exception:
            logger.debug("handoff note failed", exc_info=True)
        return messages

    async def _maybe_compact_after_turn(
        self,
        messages: list[ChatMessage],
        *,
        thread_id: str | None,
        model: Any,
    ) -> list[ChatMessage]:
        """Compact mid-run when over budget, then continue without aborting."""
        if not self.compact_after_turn:
            return messages
        if not thread_id or self.session_store is None or self.session_strategy is None:
            return messages
        compact_now = getattr(self.session_strategy, "compact_now", None)
        render = getattr(self.session_strategy, "render", None)
        if not callable(render):
            return messages
        try:
            from felix.session.compaction import estimate_messages_tokens

            strategy = self.session_strategy
            window = int(getattr(strategy, "context_window_tokens", 128000) or 128000)
            reserve = int(getattr(strategy, "reserve_tokens", 16384) or 16384)
            if estimate_messages_tokens(messages) <= max(0, window - reserve):
                return messages
            session = self.session_store.open(thread_id)
            if callable(compact_now):
                await compact_now(session, model=model, reason="after_turn")
            rebuilt = await render(
                session,
                [],
                {
                    "system_prompt": self.system_prompt,
                    "model": model,
                    "force_compact": True,
                    "compact_reason": "after_turn",
                    "will_retry": True,
                },
            )
            if rebuilt:
                return rebuilt
        except Exception:
            logger.debug("compact_after_turn failed", exc_info=True)
        return messages

    async def _load_thinking_level(self, thread_id: str | None) -> str | None:
        if not thread_id or self.settings is None:
            return None
        try:
            from felix.session.thread_state import get_thread_meta

            meta = await get_thread_meta(
                settings=self.settings,
                tenant_id=self.tenant_id,
                thread_id=thread_id,
            )
            level = meta.get("thinking_level")
            return str(level) if level and level != "off" else None
        except Exception:
            return None

    async def _persist_model_change(self, input: InvokeInput) -> None:
        if not input.model_id or not input.thread_id or self.session_store is None:
            return
        try:
            from felix.session.thread_state import update_thread_meta
            from felix.session.tree import annotate_and_append
            from felix.session.types import AppendableEvent

            session = self.session_store.open(input.thread_id)
            await annotate_and_append(
                session,
                [
                    AppendableEvent(
                        kind="model_change",
                        content=input.model_id,
                        metadata={"type": "model_change", "model_id": input.model_id},
                    )
                ],
            )
            await update_thread_meta(
                settings=self.settings,
                tenant_id=input.tenant_id or self.tenant_id,
                thread_id=input.thread_id,
                model_id=input.model_id,
            )
        except Exception:
            logger.debug("model_change persist failed", exc_info=True)

    async def _append_produced(
        self,
        thread_id: str | None,
        messages: list[ChatMessage],
        *,
        usage: dict[str, Any] | None = None,
        status: str | None = None,
    ) -> None:
        if not thread_id or self.session_store is None or not messages:
            return
        try:
            from felix.session.tree import annotate_and_append
            from felix.session.types import chat_message_to_event

            events = []
            for i, m in enumerate(messages):
                ev = chat_message_to_event(m)
                md = dict(ev.metadata or {})
                if usage and i == len(messages) - 1 and m.role == "assistant":
                    md["usage"] = usage
                if status and m.role == "assistant":
                    md["status"] = status
                ev.metadata = md or None
                events.append(ev)
            session = self.session_store.open(thread_id)
            await annotate_and_append(session, events)
        except Exception:
            logger.debug("session append failed", exc_info=True)

    async def _run_tool_batch(
        self,
        calls: list[ToolCall],
        *,
        thread_id: str | None,
        tenant_id: str,
    ) -> tuple[list[ChatMessage], bool, bool]:
        """Execute tool calls. Returns (tool_msgs, had_fatal, all_terminate)."""
        for call in calls:
            if not call.id:
                call.id = f"call_{uuid.uuid4().hex[:12]}"

        mode = self._batch_mode(calls)
        tool_msgs: list[ChatMessage] = []
        terminates: list[bool] = []
        had_fatal = False

        if mode == "parallel" and len(calls) > 1:
            results = await asyncio.gather(
                *[self._dispatch(c, thread_id) for c in calls],
                return_exceptions=True,
            )
            for call, res in zip(calls, results, strict=True):
                if isinstance(res, BaseException):
                    tool_msgs.append(
                        ChatMessage(
                            role="tool",
                            tool_call_id=call.id,
                            name=call.name,
                            content=f"[error/internal] {res}",
                        )
                    )
                    terminates.append(False)
                    continue
                kind, tool_msg, terminate = res
                tool_msgs.append(tool_msg)
                terminates.append(terminate)
                if kind == "fatal":
                    had_fatal = True
        else:
            for i, call in enumerate(calls):
                if thread_id and i > 0 and await should_cancel_remaining_tools(tenant_id, thread_id):
                    skipped = ChatMessage(
                        role="tool",
                        tool_call_id=call.id,
                        name=call.name,
                        content="[cancelled] remaining tools interrupted by steer",
                    )
                    tool_msgs.append(skipped)
                    terminates.append(True)
                    continue
                kind, tool_msg, terminate = await self._dispatch(call, thread_id)
                tool_msgs.append(tool_msg)
                terminates.append(terminate)
                if kind == "fatal":
                    had_fatal = True
                    break

        all_terminate = bool(terminates) and all(terminates)
        return tool_msgs, had_fatal, all_terminate

    async def _maybe_capture_memory(self, input: InvokeInput, final: ChatMessage, model: Any) -> None:
        capture = self.memory_capture
        if capture is None or not getattr(capture, "enabled", False):
            return
        if self.settings is None:
            return
        user_text = " ".join(m.content for m in input.messages if m.role == "user")
        try:
            from felix.memory.capture import capture_from_turn

            await capture_from_turn(
                self.settings,
                input.tenant_id or self.tenant_id,
                manifest_id=self.manifest_id,
                user_text=user_text,
                assistant_text=final.content or "",
                capture=capture,
                model=model,
            )
        except Exception:
            logger.debug("memory capture failed", exc_info=True)

    async def _inject_procedures(self, messages: list[ChatMessage], tenant_id: str) -> list[ChatMessage]:
        spec = self.procedural_memory
        if spec is None or not getattr(spec, "enabled", False) or self.settings is None:
            return messages
        if any(m.role == "system" and (m.content or "").startswith("[known procedures]") for m in messages):
            return messages
        try:
            from felix.memory.procedural import query_from_user_messages, retrieve_procedures

            block = await retrieve_procedures(
                self.settings,
                tenant_id,
                manifest_id=self.manifest_id,
                query=query_from_user_messages(messages),
                spec=spec,
            )
        except Exception:
            logger.debug("procedural retrieve failed", exc_info=True)
            return messages
        if block:
            messages.append(ChatMessage(role="system", content=block))
        return messages

    def _over_budget(self) -> bool:
        """True when a declared run budget is spent; trips the shared abort flag."""
        from felix.context import try_get_context
        from felix.limits import check_budgets, trip

        req = try_get_context()
        if req is None:
            return False
        ls = req.limit_state
        if ls.aborted:
            return True
        verdict = check_budgets(self.limits, ls)
        if verdict.exceeded:
            trip(ls, verdict.reason)
            logger.info("run over budget: %s", verdict.reason)
            return True
        return False

    def _prelude_messages(self) -> list[ChatMessage]:
        """Volatile per-run reference material, kept out of the cached system prefix.

        Recalled memory facts used to be appended to the system prompt. Caching is a
        prefix match over tools -> system -> messages, so a block that changes whenever
        memory writes a fact invalidated the whole cached prefix every turn. Rendering it
        as user-role material also keeps model-extracted text — which can originate in
        tool output — out of the developer-tier instruction channel.
        """
        if not self.context_prelude:
            return []
        return [ChatMessage(role="user", content=self.context_prelude)]

    async def invoke(self, input: InvokeInput) -> InvokeOutput:
        if not getattr(input, "thinking_level", None) and input.thread_id:
            level = await self._load_thinking_level(input.thread_id)
            if level:
                input.thinking_level = level  # type: ignore[attr-defined]
        model = self._resolve_model(input)
        await self._persist_model_change(input)
        tenant_id = input.tenant_id or "default"
        if input.thread_id:
            await ensure_run_queue(tenant_id, input.thread_id)
            await clear_abort(tenant_id, input.thread_id)

        messages: list[ChatMessage] = [
            ChatMessage(role="system", content=self.system_prompt),
            *self._prelude_messages(),
            *input.messages,
        ]
        if input.thread_id and self.session_store is not None and self.session_strategy is not None:
            try:
                session = self.session_store.open(input.thread_id)
                messages = await self.session_strategy.render(
                    session,
                    input.messages,
                    {"system_prompt": self.system_prompt, "model": model},
                )
            except Exception:
                logger.debug("session render failed; using incoming messages", exc_info=True)

        prev_model = self._last_model_id
        current_model = input.model_id or getattr(model, "model_id", None)
        messages = self._apply_handoff(messages, previous=prev_model, next_id=current_model)
        self._last_model_id = current_model

        messages = await run_filter_history(
            messages,
            context={"manifest_id": self.manifest_id, "thread_id": input.thread_id},
        )
        messages = await self._inject_procedures(messages, tenant_id)

        produced: list[ChatMessage] = list(input.messages)
        await self._append_produced(input.thread_id, [m for m in input.messages if m.role == "user"])
        final = ChatMessage(role="assistant", content="")

        from felix.audit.emit import emit_agent_audit

        user_preview = next(
            (m.content for m in input.messages if m.role == "user" and m.content),
            "",
        )
        emit_agent_audit(
            "user_input",
            status="ok",
            manifest_id=self.manifest_id,
            payload={
                "user_input": (user_preview or "")[:2000],
                "thread_id": input.thread_id,
            },
        )

        try:
            for _step in range(self.recursion_limit):
                if input.thread_id and await is_aborted(tenant_id, input.thread_id):
                    await self._append_produced(
                        input.thread_id, [final] if final.content else [], status="aborted"
                    )
                    break

                # Budgets are checked per turn as well as per tool call: a run with no
                # tool calls could otherwise burn wall clock and tokens unbounded.
                if self._over_budget():
                    break

                messages = await self._maybe_compact_after_turn(
                    messages, thread_id=input.thread_id, model=model
                )

                injected = await run_before_turn(
                    messages,
                    context={"manifest_id": self.manifest_id, "thread_id": input.thread_id},
                )
                if injected:
                    messages.extend(injected)

                result = await model.chat(messages, self._active_tools(messages))
                record_usage(result, manifest_id=self.manifest_id, model_id=model.model_id)
                usage_block = None
                if result.usage:
                    from felix.usage.pricing import usage_with_cost

                    usage_block = usage_with_cost(result.usage, model_id=model.model_id or "")
                assistant = result.message
                messages.append(assistant)
                produced.append(assistant)
                final = assistant

                if not assistant.tool_calls:
                    # The loop never inspected stop_reason, so a response truncated at
                    # max_tokens or declined by a safety classifier was recorded as a
                    # normal completion and handed back as if it were the answer.
                    # getattr: a plugin-supplied ModelClient (or a test double) may not
                    # populate stop_reason, and that must not break the run.
                    stop_reason = getattr(result, "stop_reason", "end_turn")
                    status = _status_for_stop(stop_reason)
                    if status != "complete":
                        logger.warning(
                            "run ended on stop_reason=%s (manifest=%s thread=%s)",
                            stop_reason,
                            self.manifest_id,
                            input.thread_id,
                        )
                        record_counter(
                            "felix_run_stop_reason",
                            {"manifest_id": self.manifest_id, "reason": stop_reason},
                        )
                    await self._append_produced(
                        input.thread_id, [assistant], usage=usage_block, status=status
                    )
                    break

                tool_msgs, had_fatal, all_terminate = await self._run_tool_batch(
                    list(assistant.tool_calls),
                    thread_id=input.thread_id,
                    tenant_id=tenant_id,
                )
                for tool_msg in tool_msgs:
                    messages.append(tool_msg)
                    produced.append(tool_msg)
                await self._append_produced(input.thread_id, [assistant, *tool_msgs], usage=usage_block)
                if had_fatal:
                    return InvokeOutput(messages=produced, final=assistant)
                if all_terminate:
                    break
                if input.thread_id and await is_aborted(tenant_id, input.thread_id):
                    break
                if input.thread_id:
                    await clear_cancel_flag(tenant_id, input.thread_id)
                    for steermsg in await drain_steer(
                        tenant_id,
                        input.thread_id,
                        mode=self.steering_mode,  # type: ignore[arg-type]
                    ):
                        steer_chat = ChatMessage(role="user", content=steermsg.text)
                        messages.append(steer_chat)
                        produced.append(steer_chat)
                        await self._append_produced(input.thread_id, [steer_chat])

            if input.thread_id and not await is_aborted(tenant_id, input.thread_id):
                for follow in await drain_follow_up(
                    tenant_id,
                    input.thread_id,
                    mode=self.follow_up_mode,  # type: ignore[arg-type]
                ):
                    follow_chat = ChatMessage(role="user", content=follow.text)
                    produced.append(follow_chat)
                    await self._append_produced(input.thread_id, [follow_chat])
                    messages.append(follow_chat)
                    result = await model.chat(messages, self._active_tools(messages))
                    record_usage(result, manifest_id=self.manifest_id, model_id=model.model_id)
                    assistant = result.message
                    messages.append(assistant)
                    produced.append(assistant)
                    final = assistant
                    await self._append_produced(input.thread_id, [assistant])
        finally:
            if input.thread_id:
                await release_run_queue(tenant_id, input.thread_id)
                await release_side_events(input.thread_id)

        from felix.audit.emit import emit_agent_audit

        emit_agent_audit(
            "final_response",
            status="ok",
            manifest_id=self.manifest_id,
            payload={
                "thread_id": input.thread_id,
                "chars": len(final.content or ""),
            },
        )
        await self._maybe_capture_memory(input, final, model)
        return InvokeOutput(messages=produced, final=final)

    async def stream_events(self, input: InvokeInput) -> AsyncIterator[Event]:
        if not getattr(input, "thinking_level", None) and input.thread_id:
            level = await self._load_thinking_level(input.thread_id)
            if level:
                input.thinking_level = level  # type: ignore[attr-defined]
        model = self._resolve_model(input)
        await self._persist_model_change(input)
        tenant_id = input.tenant_id or "default"
        if input.thread_id:
            await ensure_run_queue(tenant_id, input.thread_id)
            await clear_abort(tenant_id, input.thread_id)
            yield Event(event="session_progress", data={"phase": "turn"})

        messages: list[ChatMessage] = [
            ChatMessage(role="system", content=self.system_prompt),
            *self._prelude_messages(),
            *input.messages,
        ]
        if input.thread_id and self.session_store is not None and self.session_strategy is not None:
            try:
                session = self.session_store.open(input.thread_id)
                messages = await self.session_strategy.render(
                    session,
                    input.messages,
                    {"system_prompt": self.system_prompt, "model": model},
                )
            except Exception:
                logger.debug("session render failed; using incoming messages", exc_info=True)

        prev_model = self._last_model_id
        current_model = input.model_id or getattr(model, "model_id", None)
        messages = self._apply_handoff(messages, previous=prev_model, next_id=current_model)
        self._last_model_id = current_model

        messages = await run_filter_history(
            messages,
            context={"manifest_id": self.manifest_id, "thread_id": input.thread_id},
        )
        messages = await self._inject_procedures(messages, tenant_id)
        produced: list[ChatMessage] = list(input.messages)
        await self._append_produced(input.thread_id, [m for m in input.messages if m.role == "user"])
        final = ChatMessage(role="assistant", content="")

        try:
            for _step in range(self.recursion_limit):
                if input.thread_id and await is_aborted(tenant_id, input.thread_id):
                    yield Event(event="aborted", data={"thread_id": input.thread_id})
                    break

                before_len = len(messages)
                # Budgets are checked per turn as well as per tool call: a run with no
                # tool calls could otherwise burn wall clock and tokens unbounded.
                if self._over_budget():
                    break

                messages = await self._maybe_compact_after_turn(
                    messages, thread_id=input.thread_id, model=model
                )
                if len(messages) != before_len:
                    yield Event(
                        event="session_progress",
                        data={"phase": "compaction", "reason": "after_turn"},
                    )

                injected = await run_before_turn(
                    messages,
                    context={"manifest_id": self.manifest_id, "thread_id": input.thread_id},
                )
                if injected:
                    messages.extend(injected)

                chunks: list[str] = []
                async for delta in model.stream(messages, self._active_tools(messages)):
                    if input.thread_id and await is_aborted(tenant_id, input.thread_id):
                        break
                    chunks.append(delta)
                    yield Event(
                        event="text_delta",
                        data={"chunk": {"content": delta}, "delta": delta},
                    )
                    yield Event(
                        event="session_progress",
                        data={
                            "progress": {
                                "type": "assistant_delta",
                                "kind": "text",
                                "delta": delta,
                            }
                        },
                    )
                result = await model.chat(messages, self._active_tools(messages))
                record_usage(result, manifest_id=self.manifest_id, model_id=model.model_id)
                usage_block = None
                if result.usage:
                    from felix.usage.pricing import usage_with_cost

                    usage_block = usage_with_cost(result.usage, model_id=model.model_id or "")
                assistant = result.message
                if not assistant.content and chunks:
                    assistant = ChatMessage(
                        role="assistant",
                        content="".join(chunks),
                        tool_calls=assistant.tool_calls,
                    )
                messages.append(assistant)
                produced.append(assistant)
                final = assistant
                if not assistant.tool_calls:
                    # The loop never inspected stop_reason, so a response truncated at
                    # max_tokens or declined by a safety classifier was recorded as a
                    # normal completion and handed back as if it were the answer.
                    # getattr: a plugin-supplied ModelClient (or a test double) may not
                    # populate stop_reason, and that must not break the run.
                    stop_reason = getattr(result, "stop_reason", "end_turn")
                    status = _status_for_stop(stop_reason)
                    if status != "complete":
                        logger.warning(
                            "run ended on stop_reason=%s (manifest=%s thread=%s)",
                            stop_reason,
                            self.manifest_id,
                            input.thread_id,
                        )
                        record_counter(
                            "felix_run_stop_reason",
                            {"manifest_id": self.manifest_id, "reason": stop_reason},
                        )
                    await self._append_produced(
                        input.thread_id, [assistant], usage=usage_block, status=status
                    )
                    break

                for call in assistant.tool_calls:
                    if not call.id:
                        call.id = f"call_{uuid.uuid4().hex[:12]}"
                    yield Event(
                        event="tool_start",
                        data={"name": call.name, "input": call.args, "id": call.id},
                    )
                    yield Event(
                        event="tool_execution_update",
                        data={"name": call.name, "id": call.id, "status": "running"},
                    )

                tool_msgs, had_fatal, all_terminate = await self._run_tool_batch(
                    list(assistant.tool_calls),
                    thread_id=input.thread_id,
                    tenant_id=tenant_id,
                )
                for item in await drain_side_events(input.thread_id):
                    yield Event(event=str(item["event"]), data=dict(item["data"]))
                for tool_msg in tool_msgs:
                    yield Event(
                        event="tool_end",
                        data={
                            "name": tool_msg.name,
                            "output": tool_msg.content,
                            "id": tool_msg.tool_call_id,
                        },
                    )
                    yield Event(
                        event="tool_execution_update",
                        data={
                            "name": tool_msg.name,
                            "id": tool_msg.tool_call_id,
                            "status": "complete",
                        },
                    )
                    messages.append(tool_msg)
                    produced.append(tool_msg)

                await self._append_produced(input.thread_id, [assistant, *tool_msgs], usage=usage_block)
                if had_fatal or all_terminate:
                    break
                if input.thread_id and await is_aborted(tenant_id, input.thread_id):
                    yield Event(event="aborted", data={"thread_id": input.thread_id})
                    break
                if input.thread_id:
                    await clear_cancel_flag(tenant_id, input.thread_id)
                    for steermsg in await drain_steer(
                        tenant_id,
                        input.thread_id,
                        mode=self.steering_mode,  # type: ignore[arg-type]
                    ):
                        steer_chat = ChatMessage(role="user", content=steermsg.text)
                        messages.append(steer_chat)
                        produced.append(steer_chat)
                        yield Event(event="steer", data={"content": steermsg.text})
                        await self._append_produced(input.thread_id, [steer_chat])

            if input.thread_id and not await is_aborted(tenant_id, input.thread_id):
                for follow in await drain_follow_up(
                    tenant_id,
                    input.thread_id,
                    mode=self.follow_up_mode,  # type: ignore[arg-type]
                ):
                    follow_chat = ChatMessage(role="user", content=follow.text)
                    produced.append(follow_chat)
                    yield Event(event="follow_up", data={"content": follow.text})
                    await self._append_produced(input.thread_id, [follow_chat])
                    messages.append(follow_chat)
                    result = await model.chat(messages, self._active_tools(messages))
                    record_usage(result, manifest_id=self.manifest_id, model_id=model.model_id)
                    assistant = result.message
                    messages.append(assistant)
                    produced.append(assistant)
                    final = assistant
                    if assistant.content:
                        yield Event(
                            event="text_delta",
                            data={
                                "chunk": {"content": assistant.content},
                                "delta": assistant.content,
                            },
                        )
                    await self._append_produced(input.thread_id, [assistant])
        finally:
            if input.thread_id:
                await release_run_queue(tenant_id, input.thread_id)
                await release_side_events(input.thread_id)

        await self._maybe_capture_memory(input, final, model)
        yield Event(
            event="on_chain_end",
            data={"output": InvokeOutput(messages=produced, final=final)},
        )
        yield Event(
            event="done",
            data={
                "final": final.model_dump(),
                "messages": [m.model_dump() for m in produced],
            },
        )


def build_react_agent(ctx: PatternBuildContext) -> Agent:
    recursion = ctx.get("recursion_limit")
    limit = _clamp(
        int(recursion if recursion is not None else DEFAULT_RECURSION),
        ABSOLUTE_LIMITS["recursion_limit"],
    )
    execution = ctx.get("execution")
    session_spec = ctx.get("session_spec")
    tool_exec = "sequential"
    if execution is not None:
        tool_exec = str(getattr(execution, "tools", None) or "sequential")
    steer_mode = "all"
    follow_mode = "all"
    compact_after = False
    if session_spec is not None:
        steer_mode = str(getattr(session_spec, "steering_mode", None) or "all")
        follow_mode = str(getattr(session_spec, "follow_up_mode", None) or "all")
        compact_after = bool(getattr(session_spec, "compact_after_turn", False))
    return _ReactAgent(
        tools=list(ctx.get("tools") or []),
        pattern="react",
        manifest_id=str(ctx.get("manifest_id") or ""),
        manifest_version=str(ctx.get("manifest_version") or "1.0.0"),
        system_prompt=str(ctx.get("system_prompt") or ""),
        model_spec=ctx.get("model_spec"),
        settings=ctx.get("settings"),
        recursion_limit=limit,
        limits=ctx.get("limits"),
        context_prelude=str(ctx.get("context_prelude") or ""),
        session_store=ctx.get("session_store"),
        session_strategy=ctx.get("session_strategy"),
        tenant_id=str(ctx.get("tenant_id") or "default"),
        memory_capture=ctx.get("memory_capture"),
        tools_retrieval=ctx.get("tools_retrieval"),
        procedural_memory=ctx.get("procedural_memory"),
        tool_execution=tool_exec,
        steering_mode=steer_mode,
        follow_up_mode=follow_mode,
        compact_after_turn=compact_after,
    )


async def _build_react(ctx: PatternBuildContext) -> Agent:
    return build_react_agent(ctx)


register_pattern("react", _build_react, kind="single-agent")

__all__ = ["build_react_agent"]
