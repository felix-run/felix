"""Built-in patterns — register on import: react, deep, router, parallel, groupchat, reflect, plan_execute."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from felix.patterns.model import build_model, record_usage
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
from felix.tools.types import Tool, define_tool

logger = logging.getLogger("felix.patterns")

# Ensure react is registered.
import felix.patterns.react  # noqa: E402, F401
from felix.patterns.model import register_builtin_providers  # noqa: E402

register_builtin_providers()


# --- plan tools (deep pattern) — persist via plans/store ----------------------


def _plan_tools() -> list[Tool]:
    async def plan_create(args: dict[str, Any], _ctx: Any = None) -> str:
        import json
        import uuid

        from felix.context import try_get_context
        from felix.plans import store as plans_store

        req = try_get_context()
        if req is None:
            return "error: no request context for plan_create"
        plan_id = str(args.get("plan_id") or uuid.uuid4().hex[:12])
        title = str(args.get("title") or "")
        goal = str(args.get("goal") or "")
        raw_steps = args.get("steps")
        steps: list[dict[str, Any]]
        if isinstance(raw_steps, list):
            steps = []
            for i, s in enumerate(raw_steps):
                if isinstance(s, dict):
                    steps.append(
                        {
                            "id": str(s.get("id") or i + 1),
                            "title": str(s.get("title") or s.get("text") or ""),
                            "status": str(s.get("status") or "pending"),
                        }
                    )
                else:
                    steps.append({"id": str(i + 1), "title": str(s), "status": "pending"})
        elif goal:
            steps = [{"id": "1", "title": goal, "status": "pending"}]
        else:
            steps = []
        body = {"title": title or goal or "untitled", "goal": goal, "steps": steps, "status": "active"}
        row = await plans_store.put_plan(
            req.settings,
            req.auth.tenant_id,
            plan_id,
            plan=body,
            manifest_id=req.manifest_id or "",
        )
        return json.dumps({"id": row["id"], "plan": row["plan"]}, separators=(",", ":"))

    async def plan_update_step(args: dict[str, Any], _ctx: Any = None) -> str:
        import json

        from felix.context import try_get_context
        from felix.plans import store as plans_store

        req = try_get_context()
        if req is None:
            return "error: no request context for plan_update_step"
        plan_id = str(args.get("plan_id") or "")
        step_id = str(args.get("step_id") or "")
        if not plan_id or not step_id:
            return "error: plan_id and step_id required"
        row = await plans_store.get_plan(req.settings, req.auth.tenant_id, plan_id)
        if row is None:
            return f"error: plan not found: {plan_id}"
        plan = dict(row["plan"] or {})
        steps = list(plan.get("steps") or [])
        found = False
        for step in steps:
            if str(step.get("id")) == step_id:
                step["status"] = str(args.get("status") or "done")
                if args.get("note"):
                    step["note"] = str(args["note"])
                found = True
                break
        if not found:
            return f"error: step not found: {step_id}"
        plan["steps"] = steps
        updated = await plans_store.put_plan(
            req.settings,
            req.auth.tenant_id,
            plan_id,
            plan=plan,
            manifest_id=row.get("manifest_id") or req.manifest_id or "",
            expires_at=row.get("expires_at"),
        )
        return json.dumps({"id": updated["id"], "plan": updated["plan"]}, separators=(",", ":"))

    async def plan_get(args: dict[str, Any], _ctx: Any = None) -> str:
        import json

        from felix.context import try_get_context
        from felix.plans import store as plans_store

        req = try_get_context()
        if req is None:
            return "error: no request context for plan_get"
        plan_id = str(args.get("plan_id") or "")
        if plan_id:
            row = await plans_store.get_plan(req.settings, req.auth.tenant_id, plan_id)
            if row is None:
                return f"error: plan not found: {plan_id}"
            return json.dumps({"id": row["id"], "plan": row["plan"]}, separators=(",", ":"))
        items = await plans_store.list_plans(req.settings, req.auth.tenant_id, limit=1)
        if not items:
            return "error: no plans for tenant"
        row = items[0]
        return json.dumps({"id": row["id"], "plan": row["plan"]}, separators=(",", ":"))

    return [
        define_tool(
            name="plan_create",
            description="Create a multi-step plan for a complex task.",
            args_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "goal": {"type": "string"},
                    "plan_id": {"type": "string"},
                    "steps": {"type": "array"},
                },
            },
            handler=plan_create,
        ),
        define_tool(
            name="plan_update_step",
            description="Update a plan step status.",
            args_schema={
                "type": "object",
                "properties": {
                    "plan_id": {"type": "string"},
                    "step_id": {"type": "string"},
                    "status": {"type": "string"},
                    "note": {"type": "string"},
                },
            },
            handler=plan_update_step,
        ),
        define_tool(
            name="plan_get",
            description="Fetch a plan by id, or the most recently updated plan.",
            args_schema={
                "type": "object",
                "properties": {"plan_id": {"type": "string"}},
            },
            handler=plan_get,
        ),
    ]


@dataclass
class _DelegatingAgent:
    tools: list[Tool]
    pattern: str
    manifest_id: str
    manifest_version: str
    inner: Agent | None = None
    sub_agents: dict[str, Agent] = field(default_factory=dict)
    system_prompt: str = ""
    model_spec: Any = None
    settings: Any = None
    max_turns: int = 4
    aggregator_prompt: str = ""
    reflect_cfg: Any = None
    plan_cfg: Any = None

    async def invoke(self, input: InvokeInput) -> InvokeOutput:
        if self.pattern == "deep" and self.inner is not None:
            return await self.inner.invoke(input)
        if self.pattern == "router":
            return await self._router(input)
        if self.pattern == "parallel":
            return await self._parallel(input)
        if self.pattern == "groupchat":
            return await self._groupchat(input)
        if self.pattern == "reflect":
            return await self._reflect(input)
        if self.pattern == "plan_execute":
            return await self._plan_execute(input)
        if self.inner is not None:
            return await self.inner.invoke(input)
        return InvokeOutput(
            messages=list(input.messages),
            final=ChatMessage(role="assistant", content=""),
        )

    async def stream_events(self, input: InvokeInput) -> AsyncIterator[Event]:
        result = await self.invoke(input)
        if result.final.content:
            yield Event(
                event="on_chat_model_stream",
                data={"chunk": {"content": result.final.content}},
            )
        yield Event(event="on_chain_end", data={"output": result})

    async def _router(self, input: InvokeInput) -> InvokeOutput:
        if not self.sub_agents:
            return InvokeOutput(
                messages=list(input.messages),
                final=ChatMessage(role="assistant", content="[router] no sub_agents"),
            )
        names = list(self.sub_agents.keys())
        model = build_model(self.settings, self.model_spec)
        classify = [
            ChatMessage(role="system", content=self.system_prompt or "Route to the best agent."),
            ChatMessage(
                role="user",
                content=(
                    f"Choose exactly one agent name from: {', '.join(names)}.\n"
                    f"User: {input.messages[-1].content if input.messages else ''}\n"
                    "Reply with only the agent name."
                ),
            ),
        ]
        result = await model.chat(classify, [])
        record_usage(result, manifest_id=self.manifest_id, model_id=model.model_id)
        choice = result.message.content.strip().split()[0] if result.message.content else names[0]
        child = self.sub_agents.get(choice) or self.sub_agents[names[0]]
        return await child.invoke(input)

    async def _parallel(self, input: InvokeInput) -> InvokeOutput:
        import asyncio

        if not self.sub_agents:
            return InvokeOutput(
                messages=list(input.messages),
                final=ChatMessage(role="assistant", content="[parallel] no sub_agents"),
            )
        # Children run without thread_id to avoid session races.
        child_input = InvokeInput(messages=list(input.messages), thread_id=None)
        results = await asyncio.gather(
            *[a.invoke(child_input) for a in self.sub_agents.values()]
        )
        synthesis_bits = [
            f"### {name}\n{r.final.content}"
            for name, r in zip(self.sub_agents.keys(), results, strict=True)
        ]
        model = build_model(self.settings, self.model_spec)
        prompt = self.aggregator_prompt or self.system_prompt or "Synthesize the answers."
        synth = await model.chat(
            [
                ChatMessage(role="system", content=prompt),
                ChatMessage(
                    role="user",
                    content="Combine these specialist answers:\n\n" + "\n\n".join(synthesis_bits),
                ),
            ],
            [],
        )
        record_usage(synth, manifest_id=self.manifest_id, model_id=model.model_id)
        return InvokeOutput(messages=[*input.messages, synth.message], final=synth.message)

    async def _groupchat(self, input: InvokeInput) -> InvokeOutput:
        if not self.sub_agents:
            return InvokeOutput(
                messages=list(input.messages),
                final=ChatMessage(role="assistant", content="[groupchat] no sub_agents"),
            )
        transcript = list(input.messages)
        agents = list(self.sub_agents.values())
        names = list(self.sub_agents.keys())
        final = ChatMessage(role="assistant", content="")
        for turn in range(self.max_turns):
            agent = agents[turn % len(agents)]
            child_input = InvokeInput(messages=list(transcript), thread_id=None)
            result = await agent.invoke(child_input)
            stamped = ChatMessage(
                role="assistant",
                content=f"[{names[turn % len(names)]}] {result.final.content}",
            )
            transcript.append(stamped)
            final = stamped
        return InvokeOutput(messages=transcript, final=final)

    async def _reflect(self, input: InvokeInput) -> InvokeOutput:
        base = self.inner or build_react_agent(
            {
                "tools": self.tools,
                "system_prompt": self.system_prompt,
                "model_spec": self.model_spec,
                "manifest_id": self.manifest_id,
                "manifest_version": self.manifest_version,
                "settings": self.settings,
            }
        )
        cfg = self.reflect_cfg
        max_iter = int(getattr(cfg, "max_iterations", 2) or 2)
        threshold = float(getattr(cfg, "threshold", 0.7) or 0.7)
        criteria = str(getattr(cfg, "criteria", "") or "general helpfulness")
        verifier_id = str(getattr(cfg, "verifier_model", "") or "")
        draft = await base.invoke(input)
        messages = list(input.messages)
        for _ in range(max_iter - 1):
            score = await self._score(draft.final.content, criteria, verifier_id)
            if score >= threshold:
                break
            critique = (
                f"Previous answer scored {score:.2f} (need ≥{threshold}). "
                f"Improve against: {criteria}\n\nPrior answer:\n{draft.final.content}"
            )
            draft = await base.invoke(
                InvokeInput(
                    messages=[*messages, ChatMessage(role="user", content=critique)],
                    thread_id=input.thread_id,
                )
            )
        return draft

    async def _score(self, answer: str, criteria: str, verifier_id: str) -> float:
        # Heuristic fallback when no model; otherwise ask verifier for 0..1.
        if not answer.strip():
            return 0.0
        try:
            from felix.manifests.schema import ModelSpec

            spec = ModelSpec(id=verifier_id or None)
            if self.model_spec is not None and not verifier_id:
                spec = self.model_spec
            model = build_model(self.settings, spec)
            result = await model.chat(
                [
                    ChatMessage(
                        role="system",
                        content="Score 0.0–1.0 whether the answer meets the criteria. Reply with a number only.",
                    ),
                    ChatMessage(
                        role="user",
                        content=f"Criteria: {criteria}\n\nAnswer:\n{answer}",
                    ),
                ],
                [],
            )
            text = result.message.content.strip().split()[0]
            return max(0.0, min(1.0, float(text)))
        except Exception:
            return 0.8 if len(answer) > 40 else 0.4

    async def _plan_execute(self, input: InvokeInput) -> InvokeOutput:
        cfg = self.plan_cfg
        max_subtasks = int(getattr(cfg, "max_subtasks", 8) or 8)
        model = build_model(self.settings, self.model_spec)
        plan_prompt = [
            ChatMessage(
                role="system",
                content=self.system_prompt
                or "Break the user goal into a numbered list of subtasks.",
            ),
            *input.messages,
            ChatMessage(
                role="user",
                content=f"Return at most {max_subtasks} numbered subtasks, one per line.",
            ),
        ]
        plan_result = await model.chat(plan_prompt, [])
        record_usage(plan_result, manifest_id=self.manifest_id, model_id=model.model_id)
        lines = [
            ln.strip().lstrip("0123456789.-) ").strip()
            for ln in plan_result.message.content.splitlines()
            if ln.strip()
        ][:max_subtasks]
        if not lines:
            lines = [input.messages[-1].content if input.messages else "complete the task"]

        executor = self.inner or build_react_agent(
            {
                "tools": self.tools,
                "system_prompt": self.system_prompt,
                "model_spec": self.model_spec,
                "manifest_id": self.manifest_id,
                "manifest_version": self.manifest_version,
                "settings": self.settings,
                "recursion_limit": getattr(cfg, "executor_recursion_limit", 6),
            }
        )
        notes: list[str] = []
        for i, step in enumerate(lines, 1):
            step_result = await executor.invoke(
                InvokeInput(
                    messages=[
                        ChatMessage(
                            role="user",
                            content=f"Subtask {i}/{len(lines)}: {step}\nPrior notes:\n"
                            + "\n".join(notes),
                        )
                    ],
                    thread_id=None,
                )
            )
            notes.append(f"{i}. {step} → {step_result.final.content}")

        synth = await model.chat(
            [
                ChatMessage(role="system", content="Synthesize the final answer from subtask notes."),
                *input.messages,
                ChatMessage(role="user", content="Notes:\n" + "\n".join(notes)),
            ],
            [],
        )
        record_usage(synth, manifest_id=self.manifest_id, model_id=model.model_id)
        return InvokeOutput(messages=[*input.messages, synth.message], final=synth.message)


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
    return _DelegatingAgent(
        tools=list(ctx.get("tools") or []),
        pattern="plan_execute",
        manifest_id=str(ctx.get("manifest_id") or ""),
        manifest_version=str(ctx.get("manifest_version") or "1.0.0"),
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
