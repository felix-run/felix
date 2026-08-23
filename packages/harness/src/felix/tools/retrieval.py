"""Select a subset of tools per turn when ``spec.tools_retrieval`` is enabled."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from felix.manifests.schema import ToolsRetrievalSpec
from felix.patterns.types import ChatMessage
from felix.tools.types import Tool

_WORD = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall(text.lower().replace("_", " ")))


def query_from_messages(messages: list[ChatMessage]) -> str:
    parts: list[str] = []
    for m in messages:
        if m.role in {"user", "assistant"} and m.content:
            parts.append(m.content)
        if m.tool_calls:
            for tc in m.tool_calls:
                parts.append(tc.name)
                parts.append(str(tc.args))
    return " ".join(parts)


def used_tool_names(messages: list[ChatMessage]) -> set[str]:
    names: set[str] = set()
    for m in messages:
        if m.name and m.role == "tool":
            names.add(m.name)
        if m.tool_calls:
            for tc in m.tool_calls:
                names.add(tc.name)
    return names


def score_tool(tool: Tool, query_tokens: set[str]) -> int:
    blob = f"{tool.name} {tool.description}"
    return len(query_tokens & _tokens(blob))


def select_tools(
    tools: list[Tool],
    messages: list[ChatMessage],
    spec: ToolsRetrievalSpec | None,
) -> list[Tool]:
    """Return at most ``top_k`` tools, always keeping tools already used this thread.

    Ranking uses embeddings when ``felix-harness[embeddings]`` is installed,
    otherwise keyword overlap on name+description.
    """
    if spec is None or not spec.enabled:
        return tools
    if len(tools) <= spec.top_k:
        return tools

    used = used_tool_names(messages)
    query = query_from_messages(messages)
    query_tokens = _tokens(query)
    by_name = {t.name: t for t in tools}

    selected: list[Tool] = []
    seen: set[str] = set()
    for name in used:
        tool = by_name.get(name)
        if tool is not None and name not in seen:
            selected.append(tool)
            seen.add(name)

    remaining = [t for t in tools if t.name not in seen]
    ranked: list[Tool]
    order = None
    model = str(getattr(spec, "model", "") or "")
    if model:
        try:
            from felix.embeddings import rank_indices_by_query

            blobs = [f"{t.name} {t.description}" for t in remaining]
            order = rank_indices_by_query(query, blobs, model)
        except Exception:
            order = None
    if order is None:
        ranked = sorted(
            remaining,
            key=lambda t: score_tool(t, query_tokens),
            reverse=True,
        )
    else:
        ranked = [remaining[i] for i in order]
    for tool in ranked:
        if len(selected) >= spec.top_k:
            break
        selected.append(tool)
        seen.add(tool.name)
    return selected


def _coerce_spec(spec: Any | None) -> ToolsRetrievalSpec | None:
    if spec is None or isinstance(spec, ToolsRetrievalSpec):
        return spec
    return ToolsRetrievalSpec(
        enabled=bool(getattr(spec, "enabled", False)),
        top_k=int(getattr(spec, "top_k", 20) or 20),
        model=str(getattr(spec, "model", "") or "bge-base-en-v1.5"),
    )


def will_embed(tools: list[Tool], spec: Any | None) -> bool:
    """Whether :func:`select_tools` will reach the encoder for this call.

    Mirrors the early returns in ``select_tools`` and the ``if model`` branch inside
    it. Kept next to them on purpose, and pinned by a test that runs both against the
    same inputs — a predicate that drifts from the code it guards is worse than no
    predicate, because it silently puts a blocking encode back on the event loop.
    """
    spec = _coerce_spec(spec)
    if spec is None or not spec.enabled:
        return False
    if len(tools) <= spec.top_k:
        return False
    return bool(str(getattr(spec, "model", "") or ""))


def select_tools_from_ctx(
    tools: list[Tool],
    messages: list[ChatMessage],
    spec: Any | None,
) -> list[Tool]:
    spec = _coerce_spec(spec)
    if spec is None:
        return tools
    return select_tools(tools, messages, spec)


async def select_tools_from_ctx_async(
    tools: list[Tool],
    messages: list[ChatMessage],
    spec: Any | None,
) -> list[Tool]:
    """:func:`select_tools_from_ctx`, off the event loop when it will embed.

    Ranking is keyword overlap — cheap, and worth doing inline — until a model is
    configured, at which point it encodes the query and every candidate description.
    That is CPU-bound, and the first call for a model also loads it from disk. This
    runs up to four times per loop step, so the cheap path deliberately does not pay
    for an executor hop; `tools_retrieval` is off by default and that is the path
    almost every deployment takes.
    """
    if not will_embed(tools, spec):
        return select_tools_from_ctx(tools, messages, spec)
    return await asyncio.to_thread(select_tools_from_ctx, tools, messages, spec)


__all__ = [
    "query_from_messages",
    "score_tool",
    "select_tools",
    "select_tools_from_ctx",
    "select_tools_from_ctx_async",
    "used_tool_names",
    "will_embed",
]
