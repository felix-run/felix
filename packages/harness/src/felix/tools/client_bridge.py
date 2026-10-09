"""Client-executed tools — server pauses until the client posts a result."""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass
from typing import Any

from felix.manifests.schema import ClientToolRef
from felix.side_events import emit as emit_side_event
from felix.tools import client_requests
from felix.tools.errors import ToolErrorCode, tool_error_output
from felix.tools.types import (
    Tool,
    ToolInput,
    ToolInvocationCtx,
    ToolOutput,
    define_tool_with_executor,
)
from felix.waiters import signal as waiter_signal
from felix.waiters import wait as waiter_wait
from felix.waiters import waiter_name

DEFAULT_TIMEOUT_SECONDS = 120.0

# The longest `tool_call_id` that may reach a waiter name.
#
# It arrives off the model wire uncapped (`wire/openai_completions.py` takes
# `str(tc.get("id") or "")`) and is interpolated into a Redis key that `signal` holds for an
# hour. Escaping expands it up to threefold, so an uncapped id is an uncapped key -- the
# same reason `threads.py` caps `thread_id` at `MAX_THREAD_ID`, which is the rule this repo
# states as re-validating a value against the grammar it is crossing into rather than only
# the one it arrived in. Real ids are tens of characters (OpenAI ~29, Anthropic ~24).
MAX_TOOL_CALL_ID = 256


@dataclass(slots=True)
class ClientToolResult:
    content: str
    error: bool = False


def _name(thread_id: str, tool_call_id: str) -> str:
    """The waiter a pending client tool is answered through.

    Two parts, both of which can contain the separator -- `thread_id` legitimately
    (`{tenant}:{suffix}`, or `{tenant}:fiber:{id}`) and `tool_call_id` because it arrives
    off the model wire unvalidated. `waiter_name` is what keeps the pair injective; an
    f-string here let one thread's call forge another's key. See its docstring.
    """
    return waiter_name("client", thread_id, tool_call_id)


#: How often a client tool's wait asks whether its run was stopped.
ABORT_CHECK_SECONDS = 2.0


async def wait_for_result(
    thread_id: str,
    tool_call_id: str,
    *,
    timeout: float | None = None,
    tenant_id: str = "",
) -> ClientToolResult:
    """Wait for the client's answer, the deadline, or -- given `tenant_id` -- a Stop.

    A Stop used to leave a client tool waiting out its whole timeout: the abort flag is read
    only between tool rounds, and this is inside one (felix-run/felix#532).
    """
    limit = DEFAULT_TIMEOUT_SECONDS if timeout is None else float(timeout)
    waiter = asyncio.ensure_future(waiter_wait(_name(thread_id, tool_call_id), timeout=limit))
    try:
        while tenant_id:
            done, _ = await asyncio.wait({waiter}, timeout=ABORT_CHECK_SECONDS)
            if done:
                break
            from felix.steer import is_aborted

            try:
                stopped = await is_aborted(tenant_id, thread_id)
            except Exception:
                stopped = False
            if stopped:
                return ClientToolResult(content="[tool error/user_aborted] the run was stopped", error=True)
        payload = await waiter
    finally:
        if not waiter.done():
            waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await waiter
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
        if len(tool_call_id) > MAX_TOOL_CALL_ID:
            # Deny now rather than wait the full timeout: the route that would answer this
            # refuses the same id, so the wait could only ever end in `[error/timeout]`.
            return "[error/invalid_arguments] tool_call_id is too long"

        request = {
            "id": tool_call_id,
            "name": self._name,
            "args": dict(args),
            "thread_id": thread_id,
            "transport": "client",
        }
        timeout = DEFAULT_TIMEOUT_SECONDS if self._timeout is None else float(self._timeout)
        # Recorded as well as emitted: the side event reaches only a stream in this process,
        # and a durable run's stream is served by another one. See `client_requests`.
        await client_requests.record(thread_id, request, timeout=timeout, tenant_id=_tenant())
        try:
            await emit_side_event(thread_id, "tool_request", request)
            result = await wait_for_result(thread_id, tool_call_id, timeout=timeout, tenant_id=_tenant())
        finally:
            await client_requests.clear(thread_id, tool_call_id, tenant_id=_tenant())
        if result.error:
            # Kept as a failure, not flattened into text: `error` was dropped here, so a client
            # that reported a failed write handed the model a plain string, and the trajectory,
            # the run's metrics and every client's badge read it as a success unless the text
            # happened to start with a failure prefix (felix-run/felix#532).
            return tool_error_output(ToolErrorCode.PROVIDER_ERROR, result.content or "the client tool failed")
        return result.content


def _tenant() -> str:
    from felix.context import try_get_context

    ctx = try_get_context()
    return ctx.auth.tenant_id if ctx is not None else ""


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
