"""Tool runtime types — Tool, deny marker, define_tool helpers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    pass

type ToolInput = dict[str, Any]
WrapperSource = Literal["policy", "limits", "guardrails", "approvals", "command", "screening"]

# Module-private marker — never a string key. Only deny_output can stamp it.
_WRAPPER_DENY_MARKER: object = object()


@dataclass(slots=True)
class ToolOutputDict:
    content: str
    metadata: dict[Any, Any] = field(default_factory=dict)


type ToolOutput = str | ToolOutputDict | dict[str, Any]


@dataclass(slots=True)
class ToolInvocationCtx:
    manifest_id: str | None = None
    tool_call_id: str | None = None
    thread_id: str | None = None
    signal: Any | None = None


@runtime_checkable
class ToolExecutor(Protocol):
    @property
    def transport(self) -> str: ...

    async def execute(self, args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput: ...


@dataclass(slots=True)
class Tool:
    name: str
    description: str
    args_schema: dict[str, Any] | type[Any] | None
    executor: Any  # ToolExecutor
    raw_input_schema: dict[str, Any] | None = None
    is_peer: bool = False
    peer: bool = False
    source: str | None = None
    fatal: bool = False
    # Whether this tool may be re-executed when a run resumes after a crash.
    #
    # A run that dies mid-tool leaves a call with no result, and the harness cannot tell
    # from the outside whether the effect happened. Re-running a search costs a little
    # latency; re-running a payment charges twice. Defaults to False so a tool that has
    # not considered the question is never replayed.
    replay_safe: bool = False

    def __post_init__(self) -> None:
        if self.peer and not self.is_peer:
            self.is_peer = True
        if self.is_peer and not self.peer:
            self.peer = True


def deny_output(content: str, source: WrapperSource) -> ToolOutputDict:
    return ToolOutputDict(
        content=content,
        metadata={"source": source, _WRAPPER_DENY_MARKER: True},
    )


def is_wrapper_deny(output: ToolOutput) -> bool:
    if isinstance(output, str):
        return False
    if isinstance(output, ToolOutputDict):
        return output.metadata.get(_WRAPPER_DENY_MARKER) is True
    if isinstance(output, dict):
        md = output.get("metadata")
        return isinstance(md, dict) and md.get(_WRAPPER_DENY_MARKER) is True
    return False


def tool_output_content(output: ToolOutput) -> str:
    if isinstance(output, str):
        return output
    if isinstance(output, ToolOutputDict):
        return output.content
    if isinstance(output, dict):
        return str(output.get("content", ""))
    return str(getattr(output, "content", output))


output_text = tool_output_content

ToolHandler = Callable[..., Awaitable[ToolOutput]]


def define_tool(
    *,
    name: str,
    description: str,
    handler: ToolHandler,
    args_schema: dict[str, Any] | type[Any] | None = None,
    args: type[Any] | None = None,
    raw_input_schema: dict[str, Any] | None = None,
    is_peer: bool = False,
    peer: bool = False,
    source: str | None = None,
    fatal: bool = False,
    transport: str = "local",
    replay_safe: bool = False,
    validate: Callable[[ToolInput], ToolInput | Mapping[str, Any]] | None = None,
) -> Tool:
    from felix.tools.errors import tool_error_output
    from felix.tools.executor import local_executor

    schema = args_schema if args_schema is not None else args

    async def _execute(a: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
        if validate is not None:
            try:
                parsed: Any = dict(validate(a))
            except Exception as exc:
                return tool_error_output(
                    "invalid_arguments",
                    f"[invalid args for {name}] {exc}",
                )
        elif isinstance(schema, type) and hasattr(schema, "model_validate"):
            try:
                parsed = schema.model_validate(a)
            except Exception as exc:
                return tool_error_output(
                    "invalid_arguments",
                    f"[invalid args for {name}] {exc}",
                )
        else:
            parsed = a
        try:
            return await handler(parsed, ctx)
        except TypeError:
            return await handler(parsed)

    return Tool(
        name=name,
        description=description,
        args_schema=schema if not isinstance(schema, dict) else schema,
        raw_input_schema=raw_input_schema
        if raw_input_schema is not None
        else (schema if isinstance(schema, dict) else None),
        is_peer=is_peer or peer,
        peer=peer or is_peer,
        source=source,
        fatal=fatal,
        replay_safe=replay_safe,
        executor=local_executor(_execute, transport=transport),
    )


def define_tool_with_executor(
    *,
    name: str,
    description: str,
    executor: ToolExecutor,
    args_schema: dict[str, Any] | type[Any] | None = None,
    args: type[Any] | None = None,
    raw_input_schema: dict[str, Any] | None = None,
    is_peer: bool = False,
    peer: bool = False,
    source: str | None = None,
    fatal: bool = False,
    replay_safe: bool = False,
) -> Tool:
    schema = args_schema if args_schema is not None else args
    return Tool(
        name=name,
        description=description,
        args_schema=schema,
        raw_input_schema=raw_input_schema,
        is_peer=is_peer or peer,
        peer=peer or is_peer,
        source=source,
        fatal=fatal,
        replay_safe=replay_safe,
        executor=executor,
    )


__all__ = [
    "Tool",
    "ToolExecutor",
    "ToolInput",
    "ToolInvocationCtx",
    "ToolOutput",
    "ToolOutputDict",
    "WrapperSource",
    "define_tool",
    "define_tool_with_executor",
    "deny_output",
    "is_wrapper_deny",
    "output_text",
    "tool_output_content",
]
