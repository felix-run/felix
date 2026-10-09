"""Tool runtime types — Tool, deny marker, define_tool helpers."""

from __future__ import annotations

import copy
import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, get_args, runtime_checkable

from felix_ai.types import ImageAttachment

if TYPE_CHECKING:
    pass

_logger = logging.getLogger("felix.tools.types")


def accepts_positional(fn: Callable[..., Any], count: int) -> bool:
    """Can `fn` be called with exactly `count` positional arguments?

    Asked once, at wrap time. The alternative — calling with the wider signature and catching
    `TypeError` — cannot tell "wrong arity" from "`TypeError` raised inside a body that ran",
    so any tool whose body raises `TypeError` on attacker-shaped input executed **twice** for
    one model tool call. Measured at two sites: `wrap_executor`, where `apply_artifact_spill`
    passes a three-parameter `execute` that dispatches on the first call, and `define_tool`,
    whose `handler(parsed, ctx)` fell back to `handler(parsed)`. An MCP server returning a list
    where a dict was expected is enough to reach it.

    Unintrospectable callables (some C functions) answer `False`, which selects the narrower
    call. Guessing narrow is safe: a genuine arity mismatch raises once, loudly, instead of
    running a side effect a second time.
    """

    try:
        sig = inspect.signature(fn)
    except TypeError, ValueError:  # pragma: no cover - builtins without signatures
        return False
    required = 0
    allowed = 0
    var_positional = False
    for param in sig.parameters.values():
        if param.kind is param.VAR_POSITIONAL:
            # `*args` removes the upper bound. It does not remove the lower one: an earlier
            # version returned True here, so `f(a, b, c, *rest)` answered yes at count=2 —
            # which a real call rejects with "missing a required argument". The parametrized
            # case covering this branch used `lambda *a: None`, the one shape that cannot
            # expose it, which is why the table below is now checked against
            # `signature().bind` rather than against hand-written expectations.
            var_positional = True
            continue
        if param.kind not in (param.POSITIONAL_ONLY, param.POSITIONAL_OR_KEYWORD):
            continue
        allowed += 1
        if param.default is param.empty:
            required += 1
    if var_positional:
        return required <= count
    return required <= count <= allowed


type ToolInput = dict[str, Any]
WrapperSource = Literal["policy", "limits", "guardrails", "approvals", "command", "screening"]

# Module-private marker — never a string key. Only deny_output can stamp it.
_WRAPPER_DENY_MARKER: object = object()
# The same, for output relayed from an untrusted author. Only `untrusted_output` stamps it.
_UNTRUSTED_OUTPUT_MARKER: object = object()


@dataclass(slots=True)
class ToolOutputDict:
    content: str
    metadata: dict[Any, Any] = field(default_factory=dict)
    # Images for the model to *see*, not read: the runner puts them on the tool message, the
    # wire renders them as image blocks, and they never enter `content`. Before this a tool
    # could only return an image as base64 text, which a model reads as noise.
    attachments: list[ImageAttachment] = field(default_factory=list)


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
    # One line for the system prompt about how to use this tool well, assembled by the compile
    # from the tools the agent actually has — so the prompt cannot recommend a tool that was
    # removed. `spec.tool_guidance` sets it per manifest; this is for tools defined in code.
    prompt_guidance: str = ""
    # What a person approving a call to this tool reads, computed by the harness from the call's
    # arguments rather than written by the model. `apply_approvals` stores it on the pending row
    # and the `approval_required` frame under `args["preview"]`, and keeps it out of the call
    # signature and out of the arguments the tool runs with. For a tool whose arguments are a
    # reference (a commit sha) rather than the content itself, this is what makes the approval
    # row show what is about to happen. A preview that raises never blocks the approval.
    approval_preview: Callable[[ToolInput], Awaitable[str]] | None = None
    # What else an approval of a call binds, beyond its arguments: state the tool resolves when it
    # runs and its arguments do not name. `apply_approvals` hashes it into the call signature, so
    # a grant given for one answer authorizes no call that gets another. `create_skill` and
    # `update_skill` answer with the library they would save into, which under
    # `personal_skills: write` depends on the caller rather than on the arguments.
    approval_binding: Callable[[ToolInput], Awaitable[str]] | None = None
    # A trusted tool that may return text an untrusted author wrote, marking each such result
    # (`untrusted_output`): content screening wraps it so those results are screened, and only
    # those (`builder.apply_content_screening`). `activate_skill` relaying an imported skill.
    relays_untrusted: bool = False

    def __post_init__(self) -> None:
        if self.peer and not self.is_peer:
            self.is_peer = True
        if self.is_peer and not self.peer:
            self.peer = True


# How a refused or failed tool call reads once it is a `ChatMessage` and the metadata markers
# are gone. Every `deny_output` text a wrapper writes starts with one of these, and every
# error the runner writes into a tool message does too; `tests/unit/test_eval_trajectory_rules.py`
# scans the producers so a new spelling fails there rather than going uncounted. Read by
# `felix.eval.runner.trajectory_of` — the one consumer that only has the text.
FAILURE_CONTENT_PREFIXES: tuple[str, ...] = (
    "[error/",
    "[fatal/",
    "[tool error/",
    # `define_tool`'s refusal of arguments that fail the tool's schema. It starts with `[`, so
    # `tool_error_output` adds no `[tool error/` in front of it, and eval read it as a success.
    "[invalid args ",
    "[policy ",
    "[command ",
    "[screening ",
    "[limits]",
    "[guardrails]",
    "[judge ",
    "[approval ",
)


def is_failure_content(text: str) -> bool:
    """Does this tool-message text record a denial or a failure, by its spelling alone?"""
    return (text or "").startswith(FAILURE_CONTENT_PREFIXES)


def deny_output(content: str, source: WrapperSource) -> ToolOutputDict:
    return ToolOutputDict(
        content=content,
        metadata={"source": source, _WRAPPER_DENY_MARKER: True},
    )


def untrusted_output(content: str) -> ToolOutputDict:
    """An output whose text an in-process tool relays from an untrusted author -- an imported
    skill's body, say. Content screening covers it as it covers an untrusted tool's, though the
    tool that returned it is trusted (`builder.apply_content_screening`). The marker is a private
    object, so a remote tool cannot stamp it; and stamping it could only add screening."""
    return ToolOutputDict(content=content, metadata={_UNTRUSTED_OUTPUT_MARKER: True})


def is_untrusted_output(output: ToolOutput) -> bool:
    md = output_metadata(output)
    return md is not None and md.get(_UNTRUSTED_OUTPUT_MARKER) is True


def output_metadata(output: ToolOutput) -> dict[Any, Any] | None:
    """The metadata dict of a tool output, whichever of its three shapes it takes.

    The one place the `str | ToolOutputDict | dict` ladder is written; every marker check
    (`is_wrapper_deny`, `deny_source`, `read_tool_error_code`) reads through it, so a change to
    the output shape is a change here and not a hunt.
    """
    if isinstance(output, ToolOutputDict):
        return output.metadata
    if isinstance(output, dict):
        md = output.get("metadata")
        return md if isinstance(md, dict) else None
    return None


def is_wrapper_deny(output: ToolOutput) -> bool:
    md = output_metadata(output)
    return md is not None and md.get(_WRAPPER_DENY_MARKER) is True


def deny_source(output: ToolOutput) -> WrapperSource | None:
    """Which governance wrapper produced this deny, or None when it is not a wrapper deny.

    `deny_output` stamps the source on every denial; this is the read side. It is what lets an
    audit row say *which* control refused a call rather than only that one did. The marker is
    unforgeable, so the value can only be one of `WrapperSource`; the membership check is what
    lets the return type say so.
    """
    md = output_metadata(output)
    if md is None or md.get(_WRAPPER_DENY_MARKER) is not True:
        return None
    source = md.get("source")
    return source if source in get_args(WrapperSource) else None


def tool_output_images(output: ToolOutput) -> list[ImageAttachment]:
    """The images a tool returned, from either structured shape. A plain string carries none.

    A dict-shaped output may carry its images as dicts (`{"url": ..., "media_type": ...}`),
    which is what anything built from JSON produces; each becomes an `ImageAttachment`. An
    entry that is neither is dropped *with a warning* -- an image a tool meant to show and
    the model never saw should not be invisible to the operator as well.
    """
    # The two shapes the contract names, and only those: an `attachments` attribute on any other
    # object is not read, because `replace_tool_output` cannot clear it -- a quarantine would
    # replace the text and the images would go through beside it.
    if isinstance(output, ToolOutputDict):
        raw: Any = output.attachments
    elif isinstance(output, dict):
        raw = output.get("attachments")
    else:
        return []
    images: list[ImageAttachment] = []
    for item in raw or ():
        if isinstance(item, ImageAttachment):
            images.append(item)
        elif isinstance(item, Mapping) and isinstance(item.get("url"), str) and item["url"]:
            images.append(
                # No `filename`: a tool's own label never reaches the log, where secret masking
                # (which reads `content`) would not see it.
                ImageAttachment(url=item["url"], media_type=str(item.get("media_type") or "image/png"))
            )
        else:
            _logger.warning("tool output attachment dropped: a %s is not an image", type(item).__name__)
    return images


class _Keep:
    """Sentinel: leave this part of the output as it is."""


_KEEP: Any = _Keep()


def replace_tool_output(
    output: ToolOutput, *, content: str | Any = _KEEP, images: list[ImageAttachment] | Any = _KEEP
) -> ToolOutput:
    """`output` with its text and/or images replaced, as a copy -- never edited in place.

    The one way a wrapper rewrites what a tool returned. There were five, and some edited the
    inner executor's object while others copied it, so after a quarantine the returned copy
    was clean and the original still carried its images: any wrapper holding the inner result
    would have seen them. Every shape keeps what it does not replace, `metadata` included (a
    deny marker lives there). A plain string stays a string unless it gains images.
    """
    new_images = tool_output_images(output) if images is _KEEP else list(images)
    text = tool_output_content(output) if content is _KEEP else content
    if isinstance(output, str):
        return ToolOutputDict(content=text, attachments=new_images) if new_images else text
    if isinstance(output, ToolOutputDict):
        return ToolOutputDict(content=text, metadata=dict(output.metadata), attachments=new_images)
    if isinstance(output, dict):
        out = {**output, "content": text}
        if new_images or "attachments" in output:
            out["attachments"] = new_images
        return out
    clone = copy.copy(output)
    clone.content = text  # type: ignore[attr-defined]
    return clone


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
    prompt_guidance: str = "",
    validate: Callable[[ToolInput], ToolInput | Mapping[str, Any]] | None = None,
    relays_untrusted: bool = False,
) -> Tool:
    from felix.tools.errors import tool_error_output
    from felix.tools.executor import local_executor

    schema = args_schema if args_schema is not None else args
    # Once, at definition. See `accepts_positional`: probing by calling and catching
    # `TypeError` ran the handler twice whenever the handler itself raised one.
    handler_takes_ctx = accepts_positional(handler, 2)

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
        if handler_takes_ctx:
            return await handler(parsed, ctx)
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
        prompt_guidance=prompt_guidance,
        relays_untrusted=relays_untrusted,
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
    prompt_guidance: str = "",
    approval_preview: Callable[[ToolInput], Awaitable[str]] | None = None,
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
        prompt_guidance=prompt_guidance,
        approval_preview=approval_preview,
        executor=executor,
    )


__all__ = [
    "FAILURE_CONTENT_PREFIXES",
    "Tool",
    "ToolExecutor",
    "ToolInput",
    "ToolInvocationCtx",
    "ToolOutput",
    "ToolOutputDict",
    "WrapperSource",
    "accepts_positional",
    "define_tool",
    "define_tool_with_executor",
    "deny_output",
    "deny_source",
    "is_failure_content",
    "is_untrusted_output",
    "is_wrapper_deny",
    "output_metadata",
    "output_text",
    "replace_tool_output",
    "tool_output_content",
    "tool_output_images",
    "untrusted_output",
]
