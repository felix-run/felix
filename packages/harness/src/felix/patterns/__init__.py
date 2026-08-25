"""Built-in patterns — register on import: react, deep, router, parallel, groupchat, reflect, plan_execute.

The package entry point wires the patterns together; it does not implement them.
`react` lives in `patterns/react.py`, the five composite patterns in
`patterns/delegating.py`, and the deep pattern's plan tools in `patterns/plan_tools.py`.

Importing this module is what makes the built-ins resolvable: `register_pattern` runs at
import time, and nothing in core enumerates patterns. `felix.patterns.react` is imported
for the same reason — it registers `react` as a side effect.
"""

from __future__ import annotations

import felix.patterns.react  # noqa: F401  — registers the `react` pattern on import
from felix.patterns.delegating import _DelegatingAgent
from felix.patterns.model import register_builtin_providers
from felix.patterns.plan_tools import _plan_tools
from felix.patterns.react import build_react_agent
from felix.patterns.registry import (
    PatternBuildContext,
    get_pattern,
    list_patterns,
    register_pattern,
)
from felix.patterns.types import (
    Agent,
    ChatMessage,
    Event,
    InvokeInput,
    InvokeOutput,
    ToolCall,
)

register_builtin_providers()


async def _build_deep(ctx: PatternBuildContext) -> Agent:
    tools = list(ctx.get("tools") or [])
    seen = {t.name for t in tools}
    for t in _plan_tools():
        if t.name not in seen:
            tools.append(t)
            seen.add(t.name)
    inner_ctx = {**ctx, "tools": tools}
    inner = build_react_agent(inner_ctx)
    return _DelegatingAgent(
        tools=tools,
        pattern="deep",
        manifest_id=str(ctx.get("manifest_id") or ""),
        manifest_version=str(ctx.get("manifest_version") or "1.0.0"),
        inner=inner,
        system_prompt=str(ctx.get("system_prompt") or ""),
        model_spec=ctx.get("model_spec"),
        settings=ctx.get("settings"),
    )


async def _build_router(ctx: PatternBuildContext) -> Agent:
    return _DelegatingAgent(
        tools=[],
        pattern="router",
        manifest_id=str(ctx.get("manifest_id") or ""),
        manifest_version=str(ctx.get("manifest_version") or "1.0.0"),
        sub_agents=dict(ctx.get("sub_agents") or {}),
        system_prompt=str(ctx.get("system_prompt") or ""),
        model_spec=ctx.get("model_spec"),
        settings=ctx.get("settings"),
    )


async def _build_parallel(ctx: PatternBuildContext) -> Agent:
    return _DelegatingAgent(
        tools=[],
        pattern="parallel",
        manifest_id=str(ctx.get("manifest_id") or ""),
        manifest_version=str(ctx.get("manifest_version") or "1.0.0"),
        sub_agents=dict(ctx.get("sub_agents") or {}),
        system_prompt=str(ctx.get("system_prompt") or ""),
        model_spec=ctx.get("model_spec"),
        settings=ctx.get("settings"),
        aggregator_prompt=str(ctx.get("aggregator_prompt") or ""),
    )


async def _build_groupchat(ctx: PatternBuildContext) -> Agent:
    return _DelegatingAgent(
        tools=[],
        pattern="groupchat",
        manifest_id=str(ctx.get("manifest_id") or ""),
        manifest_version=str(ctx.get("manifest_version") or "1.0.0"),
        sub_agents=dict(ctx.get("sub_agents") or {}),
        system_prompt=str(ctx.get("system_prompt") or ""),
        model_spec=ctx.get("model_spec"),
        settings=ctx.get("settings"),
        max_turns=int(ctx.get("max_turns") or 4),
    )


async def _build_reflect(ctx: PatternBuildContext) -> Agent:
    manifest = ctx.get("manifest")
    reflect_cfg = getattr(getattr(manifest, "spec", None), "reflect", None)
    inner = build_react_agent(ctx)
    return _DelegatingAgent(
        tools=list(ctx.get("tools") or []),
        pattern="reflect",
        manifest_id=str(ctx.get("manifest_id") or ""),
        manifest_version=str(ctx.get("manifest_version") or "1.0.0"),
        inner=inner,
        system_prompt=str(ctx.get("system_prompt") or ""),
        model_spec=ctx.get("model_spec"),
        settings=ctx.get("settings"),
        reflect_cfg=reflect_cfg,
    )


async def _build_plan_execute(ctx: PatternBuildContext) -> Agent:
    manifest = ctx.get("manifest")
    plan_cfg = getattr(getattr(manifest, "spec", None), "plan_execute", None)
    recursion = getattr(plan_cfg, "executor_recursion_limit", 6)
    inner = build_react_agent({**ctx, "recursion_limit": recursion})
    return _DelegatingAgent(
        tools=list(ctx.get("tools") or []),
        pattern="plan_execute",
        manifest_id=str(ctx.get("manifest_id") or ""),
        manifest_version=str(ctx.get("manifest_version") or "1.0.0"),
        inner=inner,
        system_prompt=str(ctx.get("system_prompt") or ""),
        model_spec=ctx.get("model_spec"),
        settings=ctx.get("settings"),
        plan_cfg=plan_cfg,
    )


register_pattern("deep", _build_deep, kind="single-agent")
register_pattern("router", _build_router, kind="multi-agent")
register_pattern("parallel", _build_parallel, kind="multi-agent")
register_pattern("groupchat", _build_groupchat, kind="multi-agent")
register_pattern("reflect", _build_reflect, kind="single-agent")
register_pattern("plan_execute", _build_plan_execute, kind="single-agent")


__all__ = [
    "Agent",
    "ChatMessage",
    "Event",
    "InvokeInput",
    "InvokeOutput",
    "ToolCall",
    "get_pattern",
    "list_patterns",
    "register_pattern",
]
