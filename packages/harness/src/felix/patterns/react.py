"""react pattern — canonical tool-calling loop."""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from felix.config import get_settings
from felix.manifests.schema import ABSOLUTE_LIMITS
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
from felix.tools.errors import infer_error_code, read_tool_error_code, tool_output_content
from felix.tools.types import Tool, ToolInvocationCtx, is_wrapper_deny

logger = logging.getLogger("felix.patterns.react")

DEFAULT_RECURSION = 10


def _clamp(value: int, ceiling: int) -> int:
    return min(max(value, 1), ceiling)


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

    async def invoke(self, input: InvokeInput) -> InvokeOutput:
        model = build_model(self.settings or get_settings(), self.model_spec)
        messages: list[ChatMessage] = [
            ChatMessage(role="system", content=self.system_prompt),
            *input.messages,
        ]
        # Session hydration (best-effort).
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

        produced: list[ChatMessage] = list(input.messages)
        final = ChatMessage(role="assistant", content="")

        for _step in range(self.recursion_limit):
            result = await model.chat(messages, self.tools)
            record_usage(result, manifest_id=self.manifest_id, model_id=model.model_id)
            assistant = result.message
            messages.append(assistant)
            produced.append(assistant)
            final = assistant

            if not assistant.tool_calls:
                break

            for call in assistant.tool_calls:
                if not call.id:
                    call.id = f"call_{uuid.uuid4().hex[:12]}"
                kind, tool_msg = await self._dispatch(call, input.thread_id)
                messages.append(tool_msg)
                produced.append(tool_msg)
                if kind == "fatal":
                    return InvokeOutput(messages=produced, final=assistant)

            # Persist fire-and-forget when session available.
            if input.thread_id and self.session_store is not None:
                try:
                    session = self.session_store.open(input.thread_id)
                    from felix.session.types import chat_message_to_event

                    await session.append_batch(
                        [chat_message_to_event(m) for m in produced[-1 - len(assistant.tool_calls) :]]
                    )
                except Exception:
                    logger.debug("session append failed", exc_info=True)

        return InvokeOutput(messages=produced, final=final)

    async def stream_events(self, input: InvokeInput) -> AsyncIterator[Event]:
        model = build_model(self.settings or get_settings(), self.model_spec)
        messages: list[ChatMessage] = [
            ChatMessage(role="system", content=self.system_prompt),
            *input.messages,
        ]
        produced: list[ChatMessage] = list(input.messages)
        final = ChatMessage(role="assistant", content="")

        for _step in range(self.recursion_limit):
            chunks: list[str] = []
            async for delta in model.stream(messages, self.tools):
                chunks.append(delta)
                yield Event(event="on_chat_model_stream", data={"chunk": {"content": delta}})
            # After stream, do a structured chat for tool calls (providers that
            # don't multiplex tool_calls in stream).
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
                break
            for call in assistant.tool_calls:
                if not call.id:
                    call.id = f"call_{uuid.uuid4().hex[:12]}"
                yield Event(
                    event="on_tool_start",
                    data={"name": call.name, "input": call.args},
                )
                _kind, tool_msg = await self._dispatch(call, input.thread_id)
                yield Event(
                    event="on_tool_end",
                    data={"name": call.name, "output": tool_msg.content},
                )
                messages.append(tool_msg)
                produced.append(tool_msg)

        yield Event(
            event="on_chain_end",
            data={"output": InvokeOutput(messages=produced, final=final)},
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
    )


async def _build_react(ctx: PatternBuildContext) -> Agent:
    return build_react_agent(ctx)


register_pattern("react", _build_react, kind="single-agent")

__all__ = ["build_react_agent"]
