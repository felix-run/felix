"""Built-in patterns — register on import: react, deep, router, parallel, groupchat, reflect, plan_execute.

The package entry point wires the patterns together; it does not implement them.
`react` lives in `patterns/react.py`, the five composite patterns in
`patterns/delegating.py`, and the deep pattern's plan tools in `patterns/plan_tools.py`.

Importing this module is what makes the built-ins resolvable: `register_pattern` runs at
import time, and nothing in core enumerates patterns. `react` registers itself as a side
effect of the `build_react_agent` import below — a `from`-import executes the module it
reads from, so a separate `import felix.patterns.react` alongside it would be redundant.
"""

from __future__ import annotations

from felix.decisions import register_builtin_deciders
from felix.patterns.delegating import _DelegatingAgent
from felix.patterns.model import _spec_with_model, register_builtin_providers
from felix.patterns.model_sinks import install_felix_ai_sinks
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

install_felix_ai_sinks()
register_builtin_providers()
register_builtin_deciders()


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
        output_schema=ctx.get("output_schema"),
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
        output_schema=ctx.get("output_schema"),
        # A router's one decision is which child answers, so naming a decider is opting in.
        decider=ctx.get("decider"),
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
        output_schema=ctx.get("output_schema"),
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
        output_schema=ctx.get("output_schema"),
    )


async def _build_reflect(ctx: PatternBuildContext) -> Agent:
    manifest = ctx.get("manifest")
    reflect_cfg = getattr(getattr(manifest, "spec", None), "reflect", None)
    # `output_schema` kept, unlike `plan_execute`'s executor: reflect's drafts *are* the
    # answer, and the loop exits as soon as one clears the threshold, so each draft has to
    # satisfy the contract. `_answer_options` on the delegated input is the belt for a
    # plugin-constructed agent with no `inner`; this is the braces for the built one.
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
        output_schema=ctx.get("output_schema"),
        decider=ctx.get("decider"),
    )


async def _build_plan_execute(ctx: PatternBuildContext) -> Agent:
    manifest = ctx.get("manifest")
    plan_cfg = getattr(getattr(manifest, "spec", None), "plan_execute", None)
    recursion = getattr(plan_cfg, "executor_recursion_limit", 6)
    # `output_schema` stripped deliberately. The executor runs subtasks whose answers
    # become `notes` for the synthesis prompt — shaped, they arrive as JSON envelopes
    # where that prompt wanted prose. `ctx` carries the manifest's schema and
    # `build_react_agent` reads it onto the agent itself, so passing ctx through
    # unchanged shaped every step no matter what `_child_input` passed. The only path
    # that may shape a turn is an explicit `options=` at the call site.
    # `executor_model` belongs here, beside `executor_recursion_limit`, because this is the
    # only place a plan_execute executor is built. `_DelegatingAgent` has a
    # `self.inner or self._base_agent(...)` fallback that looks like the natural home for it
    # and is dead from core: `inner` is always set, right here, so the right-hand side never
    # evaluates. Putting it there left the field as inert as it was before -- and quieter,
    # because the mention satisfied the textual ratchet in `test_inert_manifest_fields.py`.
    inner = build_react_agent(
        {
            **ctx,
            "output_schema": None,
            "recursion_limit": recursion,
            "model_spec": _spec_with_model(
                ctx.get("model_spec"), str(getattr(plan_cfg, "executor_model", "") or "")
            ),
        }
    )
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
        output_schema=ctx.get("output_schema"),
    )


# `deep` alone among the composites: `_run` has no `deep` branch, so it forwards to the inner
# react agent — which `_build_deep` builds from this same context — and composes nothing of its
# own afterwards. The other five reach a model for the answering turn through
# `_DelegatingAgent`, which passes no options, so a schema would shape an intermediate turn at
# best. Flipping one of these to `True` means threading `output_schema` onto that turn first.
register_pattern("deep", _build_deep, kind="single-agent", honours_output_schema=True)
register_pattern("router", _build_router, kind="multi-agent", honours_output_schema=True)
register_pattern("parallel", _build_parallel, kind="multi-agent", honours_output_schema=True)
# Not `honours_output_schema`, and not an oversight. `groupchat`'s answer is the last
# speaker's message *stamped with its name* -- `[researcher] ...` -- so even a child that
# returned perfect JSON would be handed back with a prefix in front of it, satisfying
# nobody. Supporting it means either dropping the stamp from the final message (losing who
# said it, which is the pattern's point) or adding a synthesis turn it does not have.
# Refusing is the honest answer until one of those is chosen.
register_pattern("groupchat", _build_groupchat, kind="multi-agent")
register_pattern("reflect", _build_reflect, kind="single-agent", honours_output_schema=True)
register_pattern("plan_execute", _build_plan_execute, kind="single-agent", honours_output_schema=True)


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
