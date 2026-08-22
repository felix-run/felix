"""Client-executed tools — server pauses until the client posts a result."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from felix.manifests.schema import ClientToolRef
from felix.side_events import emit as emit_side_event
from felix.tools.types import (
    Tool,
    ToolInput,
    ToolInvocationCtx,
    ToolOutput,
    define_tool_with_executor,
)
from felix.waiters import signal as waiter_signal
from felix.waiters import wait as waiter_wait

DEFAULT_TIMEOUT_SECONDS = 120.0


@dataclass(slots=True)
class ClientToolResult:
    content: str
    error: bool = False


def _name(thread_id: str, tool_call_id: str) -> str:
    return f"client:{thread_id}:{tool_call_id}"


async def wait_for_result(
    thread_id: str,
    tool_call_id: str,
    *,
    timeout: float | None = None,
) -> ClientToolResult:
    limit = DEFAULT_TIMEOUT_SECONDS if timeout is None else float(timeout)
    payload = await waiter_wait(_name(thread_id, tool_call_id), timeout=limit)
    if payload is None:
        return ClientToolResult(content="[error/timeout] client tool timed out", error=True)
    return ClientToolResult(
        content=str(payload.get("content") or ""),
        error=bool(payload.get("error")),
    )


async def complete_result(
    thread_id: str,
    tool_call_id: str,
    content: str,
    *,
    error: bool = False,
) -> bool:
    return await waiter_signal(
        _name(thread_id, tool_call_id),
        {"content": content, "error": error},
    )


async def prepare_waiter(thread_id: str, tool_call_id: str) -> None:
    _ = (thread_id, tool_call_id)


class _ClientToolExecutor:
    transport = "client"

    def __init__(self, *, name: str, timeout_seconds: float | None) -> None:
        self._name = name
        self._timeout = timeout_seconds

    async def execute(self, args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
        thread_id = (ctx.thread_id if ctx else None) or ""
        tool_call_id = (ctx.tool_call_id if ctx else None) or ""
        if not thread_id or not tool_call_id:
            return "[error/invalid_arguments] client tools require thread_id and tool_call_id"

        await emit_side_event(
            thread_id,
            "tool_request",
            {
                "id": tool_call_id,
                "name": self._name,
                "args": dict(args),
                "thread_id": thread_id,
                "transport": "client",
            },
        )
        result = await wait_for_result(thread_id, tool_call_id, timeout=self._timeout)
        return result.content


def tools_from_client_refs(refs: list[ClientToolRef]) -> list[Tool]:
    tools: list[Tool] = []
    for ref in refs:
        schema = ref.args_schema or {
            "type": "object",
            "properties": {
                "input": {"type": "string", "description": "Opaque client input."},
            },
            "additionalProperties": True,
        }
        description = ref.description or f"Client-executed tool `{ref.name}`."
        tools.append(
            define_tool_with_executor(
                name=ref.name,
                description=description,
                executor=_ClientToolExecutor(name=ref.name, timeout_seconds=ref.timeout_seconds),
                args_schema=schema,
                raw_input_schema=schema if isinstance(schema, dict) else None,
                fatal=ref.fatal,
                source="client",
            )
        )
    return tools


def client_tool_result_json(content: Any) -> str:
    if isinstance(content, str):
        return content
    return json.dumps(content, default=str)


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "ClientToolResult",
    "client_tool_result_json",
    "complete_result",
    "prepare_waiter",
    "tools_from_client_refs",
    "wait_for_result",
]
