"""Agent-facing memory tools.

Recall reaches the model two ways, and they are not equivalent. The automatic prelude
(`capture.py:active_facts_prompt`, injected per turn in ReAct) is convenient but
bypasses the governance stack entirely — nothing screens it, nothing limits it,
nothing audits it. These tools are bound *before* the wrapper stack in
`manifests/builder.py`, so a recalled memory passes through secret masking, policies,
**content screening**, limits, guardrails, judges and approvals like any other tool
output.

That matters because recalled text is not trusted input. It was extracted by a model
from earlier turns, and those turns can contain whatever a tool returned. The prelude
compensates with `_neutralize` and a fence; the tool path gets the real thing.

The descriptions do real work too: they are where the model learns that `topic_key` is
how a newer value supersedes an older one, which is the difference between memory that
stays current and memory that accumulates contradictions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from felix.config import Settings
from felix.memory import store as memory_store
from felix.tools.types import Tool, ToolInvocationCtx, define_tool

logger = logging.getLogger("felix.memory.tools")

MEMORY_TOOL_NAMES = ("remember", "recall", "forget", "list_memories")

_KINDS = ["fact", "event", "instruction", "task", "procedure"]


class RememberArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(description="The memory, as one self-contained sentence.")
    kind: str = Field(default="fact", description=f"One of: {', '.join(_KINDS)}.")
    topic_key: str = Field(
        default="",
        description=(
            "Stable dotted key such as 'user.timezone'. Storing a new memory with the "
            "same topic_key usually supersedes the old value rather than sitting beside "
            "it. Set "
            "it for facts and instructions; leave it empty for events and tasks."
        ),
    )
    importance: float = Field(default=0.5, ge=0.0, le=1.0)


class RecallArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(description="What you are trying to remember.")
    limit: int = Field(default=5, ge=1, le=20)
    kind: str = Field(default="", description=f"Optional filter, one of: {', '.join(_KINDS)}.")


class ForgetArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Id of the memory to forget, as returned by recall.")


class ListMemoriesArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(default="", description=f"Optional filter, one of: {', '.join(_KINDS)}.")
    limit: int = Field(default=20, ge=1, le=100)


async def _provenance(settings: Settings) -> tuple[str, int | None]:
    """The thread and turn ordinal this tool call belongs to.

    Resolved at call time, not when the tool is bound: the agent is compiled before
    the request's thread is known, so a bind-time value is always empty. Without this
    a memory the agent writes through `remember` carries no provenance at all, while
    one captured automatically does — and `as_of` would then misreport, because half
    the writes look like genesis.
    """
    from felix.context import try_get_context

    req = try_get_context()
    thread_id = getattr(req, "thread_id", None) if req is not None else None
    if not thread_id:
        return "", None
    try:
        from felix.session.store import get_session_store

        head = await get_session_store(settings).open(thread_id).head()
        return thread_id, int(head.get("seq") or 0)
    except Exception:
        logger.debug("turn ordinal unavailable for %s", thread_id, exc_info=True)
        return thread_id, None


def _render(hits: list[Any]) -> str:
    if not hits:
        return "(no relevant memories)"
    lines = []
    for hit in hits:
        topic = f" [{hit.topic_key}]" if getattr(hit, "topic_key", None) else ""
        lines.append(f"- ({hit.id}){topic} {hit.content}")
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class _Binding:
    """What every memory tool needs to know about the agent it belongs to."""

    settings: Settings
    tenant_id: str
    manifest_id: str
    default_limit: int
    thread_id: str


def _remember_tool(b: _Binding) -> Tool:
    async def handler(args: RememberArgs, _ctx: ToolInvocationCtx | None = None) -> str:
        thread_id, origin_seq = await _provenance(b.settings)
        row = await memory_store.put_memory(
            b.settings,
            b.tenant_id,
            content=args.content,
            kind=args.kind if args.kind in _KINDS else "fact",
            manifest_id=b.manifest_id,
            topic_key=args.topic_key or None,
            importance=args.importance,
            thread_id=thread_id or b.thread_id,
            origin_seq=origin_seq,
            metadata={"source": "remember_tool"},
        )
        return f"remembered:{row['id']}"

    return define_tool(
        name="remember",
        description=(
            "Store something worth knowing in future sessions. Use 'fact' for stable "
            "knowledge or preferences, 'event' for something that happened, "
            "'instruction' for a rule to follow, 'task' for work in progress. Set "
            "topic_key on facts and instructions so a newer value can supersede the old "
            "one rather than sitting alongside it."
        ),
        args=RememberArgs,
        handler=handler,
        source="memory",
    )


def _recall_tool(b: _Binding) -> Tool:
    async def handler(args: RecallArgs, _ctx: ToolInvocationCtx | None = None) -> str:
        from felix.memory.embedder import build_embedder
        from felix.memory.recall import recall

        hits = await recall(
            b.settings,
            b.tenant_id,
            args.query,
            manifest_id=b.manifest_id,
            limit=min(args.limit, b.default_limit * 4),
            kinds=[args.kind] if args.kind in _KINDS else None,
            embedder=build_embedder(b.settings),
        )
        return _render(hits)

    return define_tool(
        name="recall",
        description=(
            "Search everything you have remembered, across all past sessions. Finds "
            "memories by meaning as well as wording, so ask in your own words."
        ),
        args=RecallArgs,
        handler=handler,
        source="memory",
    )


def _forget_tool(b: _Binding) -> Tool:
    async def handler(args: ForgetArgs, _ctx: ToolInvocationCtx | None = None) -> str:
        # Names itself, so the store can refuse an operator-curated row. This tool
        # is reachable by a prompt injection: list_memories prints every id.
        ok = await memory_store.forget(b.settings, b.tenant_id, args.id, source="remember_tool")
        return f"forgot:{args.id}" if ok else f"no such memory: {args.id}"

    return define_tool(
        name="forget",
        description=(
            "Mark a memory obsolete so it stops being recalled. It is hidden, not "
            "deleted. To correct a fact, prefer remembering the new value under the "
            "same topic_key."
        ),
        args=ForgetArgs,
        handler=handler,
        source="memory",
    )


def _list_tool(b: _Binding) -> Tool:
    async def handler(args: ListMemoriesArgs, _ctx: ToolInvocationCtx | None = None) -> str:
        rows = await memory_store.list_active(
            b.settings,
            b.tenant_id,
            manifest_id=b.manifest_id,
            kind=args.kind if args.kind in _KINDS else None,
            limit=args.limit,
        )
        if not rows:
            return "(no memories stored)"
        return "\n".join(f"- ({r['id']}) [{r.get('kind')}] {r.get('content')}" for r in rows)

    return define_tool(
        name="list_memories",
        description="List what you have remembered, most recent first.",
        args=ListMemoriesArgs,
        handler=handler,
        source="memory",
    )


def make_memory_tools(
    *,
    settings: Settings,
    tenant_id: str,
    manifest_id: str,
    default_limit: int = 5,
    thread_id: str = "",
) -> list[Tool]:
    """The four memory tools, bound to one tenant and manifest."""
    binding = _Binding(
        settings=settings,
        tenant_id=tenant_id,
        manifest_id=manifest_id,
        default_limit=default_limit,
        thread_id=thread_id,
    )
    return [
        _remember_tool(binding),
        _recall_tool(binding),
        _forget_tool(binding),
        _list_tool(binding),
    ]


__all__ = ["MEMORY_TOOL_NAMES", "make_memory_tools"]
