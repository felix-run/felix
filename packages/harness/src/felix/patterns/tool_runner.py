"""Executing a batch of tool calls for one manifest.

Lifted out of the react agent, which had grown to hold the turn loop, session plumbing
and tool execution in one class. Tool execution needs almost nothing from the agent — the
tool map, the manifest id, and the configured execution mode — so it reads better as its
own thing than as three more methods on an already-large class.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from felix.audit.emit import emit_agent_audit
from felix.hooks import run_after_tool, run_before_tool
from felix.observability.metrics import record_counter
from felix.observability.tracing import with_span
from felix.patterns.types import ChatMessage, ToolCall
from felix.steer import should_cancel_remaining_tools
from felix.tools.errors import infer_error_code, read_tool_error_code, tool_output_content
from felix.tools.types import Tool, ToolInvocationCtx, is_wrapper_deny

logger = logging.getLogger("felix.patterns.tool_runner")

_SEQUENTIAL_TRANSPORTS = frozenset({"client", "approval"})


@dataclass
class ToolRunner:
    """Runs tool calls for one compiled manifest."""

    tool_map: dict[str, Tool]
    manifest_id: str
    tool_execution: str = "sequential"

    def batch_mode(self, calls: list[ToolCall]) -> str:
        mode = self.tool_execution or "sequential"
        if mode != "parallel":
            return "sequential"
        for call in calls:
            tool = self.tool_map.get(call.name)
            if tool is None:
                continue
            transport = getattr(tool.executor, "transport", "local")
            if transport in _SEQUENTIAL_TRANSPORTS:
                return "sequential"
            if getattr(tool, "execution_mode", None) == "sequential":
                return "sequential"
        return "parallel"

    async def dispatch(self, call: ToolCall, thread_id: str | None) -> tuple[str, ChatMessage, bool]:
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
            tool = self.tool_map.get(call.name)
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
            # Only the call itself. Everything after it is bookkeeping over a tool that has
            # already run, and it used to sit inside this `try` with the execution — so a
            # failure there fell into the handler below, which calls `run_after_tool` a second
            # time and tells the model `[error/...]` for a call that succeeded. A model told a
            # side-effecting tool failed may run it again.
            #
            # Reachable, though not by the obvious route: `run_after_tool` isolates each hook
            # and `emit_agent_audit` swallows its own failures, so neither can raise. What can
            # is the handling of a hook's *return* — an after-tool hook replacing `content`
            # with an object whose `__str__` raises produced exactly two hook invocations,
            # `[False, True]`, and an `[error/internal]` message for a successful tool.
            try:
                result = await tool.executor.execute(
                    call.args,
                    ToolInvocationCtx(
                        manifest_id=self.manifest_id,
                        tool_call_id=call.id,
                        thread_id=thread_id,
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

            content, terminate = await self._record_and_postprocess(tool, call, result, thread_id=thread_id)

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

        return await with_span("tool.call", _run, {"tool": call.name})

    async def _record_and_postprocess(
        self,
        tool: Tool,
        call: ToolCall,
        result: Any,
        *,
        thread_id: str | None,
    ) -> tuple[str, bool]:
        """Metering, audit and the after-tool hook, for a tool that has already run.

        Separate from the call itself because none of it can undo the call. It used to share
        the call's `try`, so a failure here fell into that handler — which invokes
        `run_after_tool` a second time and reports `[error/...]` to the model for a tool that
        succeeded. A model told a side-effecting tool failed may run it again.

        `content` starts empty and keeps whatever it reached, because the thing that failed
        may be `tool_output_content` itself.
        """
        content = ""
        terminate = False
        try:
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
            emit_agent_audit(
                "tool_call" if status != "denied" else "policy_deny",
                status=status,
                manifest_id=self.manifest_id,
                payload={"tool": call.name, "tool_call_id": call.id, "thread_id": thread_id},
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
        except Exception:
            logger.warning("post-call handling failed for %s; the tool already ran", call.name, exc_info=True)
            record_counter(
                "felix_control_degraded",
                {"control": "after_tool", "manifest_id": self.manifest_id},
            )
        return content, terminate

    async def run_batch(
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

        mode = self.batch_mode(calls)
        tool_msgs: list[ChatMessage] = []
        terminates: list[bool] = []
        had_fatal = False

        if mode == "parallel" and len(calls) > 1:
            results = await asyncio.gather(
                *[self.dispatch(c, thread_id) for c in calls],
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
                kind, tool_msg, terminate = await self.dispatch(call, thread_id)
                tool_msgs.append(tool_msg)
                terminates.append(terminate)
                if kind == "fatal":
                    had_fatal = True
                    break

        all_terminate = bool(terminates) and all(terminates)
        return tool_msgs, had_fatal, all_terminate
