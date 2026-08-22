"""Select a subset of tools per turn when ``spec.tools_retrieval`` is enabled."""

from __future__ import annotations

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

    Ranking is keyword overlap on name+description (no embeddings required).
    When retrieval is disabled or the catalog is small, the full list is returned.
    """
    if spec is None or not spec.enabled:
        return tools
    if len(tools) <= spec.top_k:
        return tools

    used = used_tool_names(messages)
    query_tokens = _tokens(query_from_messages(messages))
    by_name = {t.name: t for t in tools}

    selected: list[Tool] = []
    seen: set[str] = set()
    for name in used:
        tool = by_name.get(name)
        if tool is not None and name not in seen:
            selected.append(tool)
            seen.add(name)

    ranked = sorted(
        [t for t in tools if t.name not in seen],
        key=lambda t: score_tool(t, query_tokens),
        reverse=True,
    )
    for tool in ranked:
        if len(selected) >= spec.top_k:
            break
        selected.append(tool)
        seen.add(tool.name)
    return selected


def select_tools_from_ctx(
    tools: list[Tool],
    messages: list[ChatMessage],
    spec: Any | None,
) -> list[Tool]:
    if spec is None:
        return tools
    if not isinstance(spec, ToolsRetrievalSpec):
        enabled = bool(getattr(spec, "enabled", False))
        top_k = int(getattr(spec, "top_k", 20) or 20)
        spec = ToolsRetrievalSpec(enabled=enabled, top_k=top_k)
    return select_tools(tools, messages, spec)


__all__ = [
    "query_from_messages",
    "score_tool",
    "select_tools",
    "select_tools_from_ctx",
    "used_tool_names",
]
