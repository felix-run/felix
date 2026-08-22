"""react pattern — canonical tool-calling loop."""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from felix.config import get_settings
from felix.hooks import run_before_turn, run_filter_history
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
from felix.steer import (
    clear_cancel_flag,
    drain_follow_up,
    drain_steer,
    ensure_run_queue,
    release_run_queue,
    should_cancel_remaining_tools,
)
from felix.tools.errors import infer_error_code, read_tool_error_code, tool_output_content
from felix.tools.types import Tool, ToolInvocationCtx, is_wrapper_deny

logger = logging.getLogger("felix.patterns.react")

DEFAULT_RECURSION = 10


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
    session_store: Any | None = None
    session_strategy: Any | None = None
    tenant_id: str = "default"
    memory_capture: Any | None = None
    _tool_map: dict[str, Tool] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._tool_map = {t.name: t for t in self.tools}

    async def _dispatch(
        self, call: ToolCall, thread_id: str | None
    ) -> tuple[str, ChatMessage]:
        async def _run(span: Any) -> tuple[str, ChatMessage]:
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
                return (
                    "ok",
                    ChatMessage(
                        role="tool",
                        tool_call_id=call.id,
                        name=call.name,
                        content=content,
                    ),
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
                if tool.fatal:
                    return (
                        "fatal",
                        ChatMessage(
                            role="tool",
                            tool_call_id=call.id,
                            name=call.name,
                            content=f"[fatal/{code.value}] {exc}",
                        ),
                    )
                return (
                    "ok",
                    ChatMessage(
                        role="tool",
                        tool_call_id=call.id,
                        name=call.name,
                        content=f"[error/{code.value}] {exc}",
                    ),
                )

        return await with_span("tool.call", _run, {"tool": call.name})

    def _resolve_model(self, input: InvokeInput) -> Any:
        settings = self.settings or get_settings()
        spec = _model_spec_with_override(self.model_spec, input.model_id)
        return build_model(settings, spec)

    async def _persist_model_change(self, input: InvokeInput) -> None:
        if not input.model_id or not input.thread_id or self.session_store is None:
            return
        try:
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
        except Exception:
            logger.debug("model_change persist failed", exc_info=True)

    async def _append_produced(
        self, thread_id: str | None, messages: list[ChatMessage]
    ) -> None:
        if not thread_id or self.session_store is None or not messages:
            return
        try:
            from felix.session.tree import annotate_and_append
            from felix.session.types import chat_message_to_event

            session = self.session_store.open(thread_id)
            await annotate_and_append(
                session, [chat_message_to_event(m) for m in messages]
            )
        except Exception:
            logger.debug("session append failed", exc_info=True)

    async def _maybe_capture_memory(
        self, input: InvokeInput, final: ChatMessage, model: Any
    ) -> None:
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

    async def invoke(self, input: InvokeInput) -> InvokeOutput:
        model = self._resolve_model(input)
        await self._persist_model_change(input)
        tenant_id = input.tenant_id or "default"
        if input.thread_id:
            await ensure_run_queue(tenant_id, input.thread_id)

        messages: list[ChatMessage] = [
            ChatMessage(role="system", content=self.system_prompt),
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

        messages = await run_filter_history(
            messages,
            context={"manifest_id": self.manifest_id, "thread_id": input.thread_id},
        )

        produced: list[ChatMessage] = list(input.messages)
        # Persist inbound user messages on first turn.
        await self._append_produced(
            input.thread_id, [m for m in input.messages if m.role == "user"]
        )
        final = ChatMessage(role="assistant", content="")

        try:
            for _step in range(self.recursion_limit):
                injected = await run_before_turn(
                    messages,
                    context={"manifest_id": self.manifest_id, "thread_id": input.thread_id},
                )
                if injected:
                    messages.extend(injected)

                result = await model.chat(messages, self.tools)
                record_usage(result, manifest_id=self.manifest_id, model_id=model.model_id)
                assistant = result.message
                messages.append(assistant)
                produced.append(assistant)
                final = assistant

                if not assistant.tool_calls:
                    await self._append_produced(input.thread_id, [assistant])
                    break

                tool_msgs: list[ChatMessage] = []
                for i, call in enumerate(assistant.tool_calls):
                    if not call.id:
                        call.id = f"call_{uuid.uuid4().hex[:12]}"
                    if (
                        input.thread_id
                        and i > 0
                        and await should_cancel_remaining_tools(tenant_id, input.thread_id)
                    ):
                        skipped = ChatMessage(
                            role="tool",
                            tool_call_id=call.id,
                            name=call.name,
                            content="[cancelled] remaining tools interrupted by steer",
                        )
                        messages.append(skipped)
                        produced.append(skipped)
                        tool_msgs.append(skipped)
                        continue
                    kind, tool_msg = await self._dispatch(call, input.thread_id)
                    messages.append(tool_msg)
                    produced.append(tool_msg)
                    tool_msgs.append(tool_msg)
                    if kind == "fatal":
                        await self._append_produced(
                            input.thread_id, [assistant, *tool_msgs]
                        )
                        return InvokeOutput(messages=produced, final=assistant)

                await self._append_produced(input.thread_id, [assistant, *tool_msgs])
                if input.thread_id:
                    await clear_cancel_flag(tenant_id, input.thread_id)
                    for steermsg in await drain_steer(tenant_id, input.thread_id):
                        steer_chat = ChatMessage(role="user", content=steermsg.text)
                        messages.append(steer_chat)
                        produced.append(steer_chat)
                        await self._append_produced(input.thread_id, [steer_chat])

            if input.thread_id:
                for follow in await drain_follow_up(tenant_id, input.thread_id):
                    # Follow-ups are returned as trailing user messages for the client
                    # to re-invoke, or we can immediately continue one more turn.
                    follow_chat = ChatMessage(role="user", content=follow.text)
                    produced.append(follow_chat)
                    await self._append_produced(input.thread_id, [follow_chat])
                    # One more model turn for each follow-up.
                    messages.append(follow_chat)
                    result = await model.chat(messages, self.tools)
                    record_usage(result, manifest_id=self.manifest_id, model_id=model.model_id)
                    assistant = result.message
                    messages.append(assistant)
                    produced.append(assistant)
                    final = assistant
                    await self._append_produced(input.thread_id, [assistant])
        finally:
            if input.thread_id:
                await release_run_queue(tenant_id, input.thread_id)

        await self._maybe_capture_memory(input, final, model)
        return InvokeOutput(messages=produced, final=final)

    async def stream_events(self, input: InvokeInput) -> AsyncIterator[Event]:
        model = self._resolve_model(input)
        await self._persist_model_change(input)
        tenant_id = input.tenant_id or "default"
        if input.thread_id:
            await ensure_run_queue(tenant_id, input.thread_id)

        messages: list[ChatMessage] = [
            ChatMessage(role="system", content=self.system_prompt),
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

        messages = await run_filter_history(
            messages,
            context={"manifest_id": self.manifest_id, "thread_id": input.thread_id},
        )
        produced: list[ChatMessage] = list(input.messages)
        await self._append_produced(
            input.thread_id, [m for m in input.messages if m.role == "user"]
        )
        final = ChatMessage(role="assistant", content="")

        try:
            for _step in range(self.recursion_limit):
                injected = await run_before_turn(
                    messages,
                    context={"manifest_id": self.manifest_id, "thread_id": input.thread_id},
                )
                if injected:
                    messages.extend(injected)

                chunks: list[str] = []
                async for delta in model.stream(messages, self.tools):
                    chunks.append(delta)
                    yield Event(
                        event="text_delta",
                        data={"chunk": {"content": delta}, "delta": delta},
                    )
                    yield Event(
                        event="on_chat_model_stream",
                        data={"chunk": {"content": delta}},
                    )
                result = await model.chat(messages, self.tools)
                record_usage(result, manifest_id=self.manifest_id, model_id=model.model_id)
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
                    await self._append_produced(input.thread_id, [assistant])
                    break

                tool_msgs: list[ChatMessage] = []
                for i, call in enumerate(assistant.tool_calls):
                    if not call.id:
                        call.id = f"call_{uuid.uuid4().hex[:12]}"
                    if (
                        input.thread_id
                        and i > 0
                        and await should_cancel_remaining_tools(tenant_id, input.thread_id)
                    ):
                        skipped = ChatMessage(
                            role="tool",
                            tool_call_id=call.id,
                            name=call.name,
                            content="[cancelled] remaining tools interrupted by steer",
                        )
                        yield Event(
                            event="tool_cancelled",
                            data={"name": call.name, "reason": "steer"},
                        )
                        messages.append(skipped)
                        produced.append(skipped)
                        tool_msgs.append(skipped)
                        continue
                    yield Event(
                        event="tool_start",
                        data={"name": call.name, "input": call.args, "id": call.id},
                    )
                    yield Event(
                        event="on_tool_start",
                        data={"name": call.name, "input": call.args},
                    )
                    _kind, tool_msg = await self._dispatch(call, input.thread_id)
                    yield Event(
                        event="tool_end",
                        data={"name": call.name, "output": tool_msg.content, "id": call.id},
                    )
                    yield Event(
                        event="on_tool_end",
                        data={"name": call.name, "output": tool_msg.content},
                    )
                    messages.append(tool_msg)
                    produced.append(tool_msg)
                    tool_msgs.append(tool_msg)

                await self._append_produced(input.thread_id, [assistant, *tool_msgs])
                if input.thread_id:
                    await clear_cancel_flag(tenant_id, input.thread_id)
                    for steermsg in await drain_steer(tenant_id, input.thread_id):
                        steer_chat = ChatMessage(role="user", content=steermsg.text)
                        messages.append(steer_chat)
                        produced.append(steer_chat)
                        yield Event(event="steer", data={"content": steermsg.text})
                        await self._append_produced(input.thread_id, [steer_chat])

            if input.thread_id:
                for follow in await drain_follow_up(tenant_id, input.thread_id):
                    follow_chat = ChatMessage(role="user", content=follow.text)
                    produced.append(follow_chat)
                    yield Event(event="follow_up", data={"content": follow.text})
                    await self._append_produced(input.thread_id, [follow_chat])
                    messages.append(follow_chat)
                    result = await model.chat(messages, self.tools)
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
    return _ReactAgent(
        tools=list(ctx.get("tools") or []),
        pattern="react",
        manifest_id=str(ctx.get("manifest_id") or ""),
        manifest_version=str(ctx.get("manifest_version") or "1.0.0"),
        system_prompt=str(ctx.get("system_prompt") or ""),
        model_spec=ctx.get("model_spec"),
        settings=ctx.get("settings"),
        recursion_limit=limit,
        session_store=ctx.get("session_store"),
        session_strategy=ctx.get("session_strategy"),
        tenant_id=str(ctx.get("tenant_id") or "default"),
        memory_capture=ctx.get("memory_capture"),
    )


async def _build_react(ctx: PatternBuildContext) -> Agent:
    return build_react_agent(ctx)


register_pattern("react", _build_react, kind="single-agent")

__all__ = ["build_react_agent"]
