"""Tool execution transport — local_executor + wrap_executor."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from felix.tools.types import Tool, ToolExecutor, ToolInput, ToolInvocationCtx, ToolOutput

ExecuteFn = Callable[[ToolInput, ToolInvocationCtx | None], Awaitable[ToolOutput]]


class _FnExecutor:
    __slots__ = ("_execute", "_transport")

    def __init__(self, transport: str, execute: ExecuteFn) -> None:
        self._transport = transport
        self._execute = execute

    @property
    def transport(self) -> str:
        return self._transport

    async def execute(
        self, args: ToolInput, ctx: ToolInvocationCtx | None = None
    ) -> ToolOutput:
        return await self._execute(args, ctx)


def local_executor(execute: ExecuteFn) -> ToolExecutor:
    return _FnExecutor("local", execute)


def wrap_executor(inner: Any, execute: Callable[..., Awaitable[ToolOutput]]) -> ToolExecutor:
    """Wrap an executor while preserving its transport label."""

    async def _run(args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
        try:
            return await execute(args, ctx, inner)
        except TypeError:
            return await execute(args, ctx)

    return _FnExecutor(getattr(inner, "transport", "local"), _run)


def wrap_tool(tool: Tool, fn: Callable[..., Awaitable[ToolOutput]]) -> Tool:
    return Tool(
        name=tool.name,
        description=tool.description,
        args_schema=tool.args_schema,
        executor=wrap_executor(tool.executor, fn),
        raw_input_schema=tool.raw_input_schema,
        fatal=tool.fatal,
        peer=tool.peer,
        is_peer=tool.is_peer,
        source=tool.source,
    )


__all__ = ["ToolExecutor", "local_executor", "wrap_executor", "wrap_tool"]
