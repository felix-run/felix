"""Tool execution transport — local_executor + wrap_executor."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace
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

    async def execute(self, args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
        return await self._execute(args, ctx)


def local_executor(execute: ExecuteFn, *, transport: str = "local") -> ToolExecutor:
    return _FnExecutor(transport, execute)


def wrap_executor(inner: Any, execute: Callable[..., Awaitable[ToolOutput]]) -> ToolExecutor:
    """Wrap an executor while preserving its transport label."""

    async def _run(args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
        try:
            return await execute(args, ctx, inner)
        except TypeError:
            return await execute(args, ctx)

    return _FnExecutor(getattr(inner, "transport", "local"), _run)


def wrap_tool(tool: Tool, fn: Callable[..., Awaitable[ToolOutput]]) -> Tool:
    """Copy a tool with a wrapped executor, carrying every other field forward.

    `dataclasses.replace`, never a field-by-field rebuild: a field the rebuild forgets is
    silently reset to its default on every tool that passes through. This one forgot
    `replay_safe`, and so did all seven wrappers in `manifests/builder.py` — which between
    them wrap every tool in every manifest, so the field read `False` everywhere it was
    consulted and the feature had never worked in any release.
    """
    return replace(tool, executor=wrap_executor(tool.executor, fn))


__all__ = ["ToolExecutor", "local_executor", "wrap_executor", "wrap_tool"]
