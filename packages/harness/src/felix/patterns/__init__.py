"""Built-in patterns — register on import: react, deep, router, parallel, groupchat, reflect, plan_execute."""

from __future__ import annotations

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

# Ensure react is registered — providers registered below after _model_for.


def _model_for(input: InvokeInput, settings: Any, model_spec: Any) -> Any:
    """Build a model client, applying request-level model_id override when present."""
    spec = model_spec
    if input.model_id:
        from copy import deepcopy

        from felix.manifests.schema import ModelSpec

        if isinstance(model_spec, ModelSpec):
            data = model_spec.model_dump()
            data["id"] = input.model_id
            spec = ModelSpec.model_validate(data)
        else:
            try:
                spec = deepcopy(model_spec)
                spec.id = input.model_id
            except Exception:
                spec = model_spec
    return build_model(settings, spec)


import felix.patterns.react  # noqa: E402, F401
from felix.patterns.model import register_builtin_providers  # noqa: E402

register_builtin_providers()

_TERMINAL_EVENTS = frozenset({"done", "on_chain_end"})


@dataclass
class _Tap:
    output: InvokeOutput | None = None


def _coerce_output(data: Any) -> InvokeOutput | None:
    if isinstance(data, InvokeOutput):
        return data
    if not isinstance(data, dict):
        return None
    nested = data.get("output")
    if isinstance(nested, InvokeOutput):
        return nested
    if isinstance(nested, dict):
        inner = _coerce_output(nested)
        if inner is not None:
            return inner
    final_raw = data.get("final")
    if final_raw is None:
        return None
    final = final_raw if isinstance(final_raw, ChatMessage) else ChatMessage.model_validate(final_raw)
    msgs_raw = data.get("messages") or []
    messages = [m if isinstance(m, ChatMessage) else ChatMessage.model_validate(m) for m in msgs_raw]
    return InvokeOutput(messages=messages or [final], final=final)


def _output_from_event(ev: Event) -> InvokeOutput | None:
    if ev.event not in _TERMINAL_EVENTS:
        return None
    return _coerce_output(ev.data)


async def _pipe_stream(
    agent: Agent,
    input: InvokeInput,
    tap: _Tap,
    *,
    swallow_terminal: bool,
) -> AsyncIterator[Event]:
    async for ev in agent.stream_events(input):
        captured = _output_from_event(ev)
        if captured is not None:
            tap.output = captured
        if swallow_terminal and ev.event in _TERMINAL_EVENTS:
            continue
        yield ev


def _terminal_events(result: InvokeOutput) -> list[Event]:
    return [
        Event(event="on_chain_end", data={"output": result}),
        Event(
            event="done",
            data={
                "final": result.final.model_dump(),
                "messages": [m.model_dump() for m in result.messages],
            },
        ),
    ]


async def _yield_model_stream(
    model: Any, messages: list[ChatMessage], collected: list[str]
) -> AsyncIterator[Event]:
    stream = getattr(model, "stream", None)
    if stream is None:
        result = await model.chat(messages, [])
        text = result.message.content or ""
        if text:
            collected.append(text)
            yield Event(
                event="text_delta",
                data={"chunk": {"content": text}, "delta": text},
            )
            yield Event(event="on_chat_model_stream", data={"chunk": {"content": text}})
        return
    async for delta in stream(messages, []):
        if not delta:
            continue
        collected.append(delta)
        yield Event(
            event="text_delta",
            data={"chunk": {"content": delta}, "delta": delta},
        )
        yield Event(event="on_chat_model_stream", data={"chunk": {"content": delta}})


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
        body = {
            "title": title or goal or "untitled",
            "goal": goal,
            "steps": steps,
            "status": "active",
        }
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
        if self.pattern == "deep" and self.inner is not None:
            async for ev in self.inner.stream_events(input):
                yield ev
            return
        if self.pattern == "router":
            async for ev in self._stream_router(input):
                yield ev
            return
        if self.pattern == "parallel":
            async for ev in self._stream_parallel(input):
                yield ev
            return
        if self.pattern == "groupchat":
            async for ev in self._stream_groupchat(input):
                yield ev
            return
        if self.pattern == "reflect":
            async for ev in self._stream_reflect(input):
                yield ev
            return
        if self.pattern == "plan_execute":
            async for ev in self._stream_plan_execute(input):
                yield ev
            return
        if self.inner is not None:
            async for ev in self.inner.stream_events(input):
                yield ev
            return
        result = await self.invoke(input)
        if result.final.content:
            yield Event(
                event="text_delta",
                data={"chunk": {"content": result.final.content}, "delta": result.final.content},
            )
            yield Event(
                event="on_chat_model_stream",
                data={"chunk": {"content": result.final.content}},
            )
        for ev in _terminal_events(result):
            yield ev

    async def _choose_child(self, input: InvokeInput) -> Agent:
        names = list(self.sub_agents.keys())
        model = _model_for(input, self.settings, self.model_spec)
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
        return self.sub_agents.get(choice) or self.sub_agents[names[0]]

    async def _stream_router(self, input: InvokeInput) -> AsyncIterator[Event]:
        if not self.sub_agents:
            result = await self._router(input)
            for ev in _terminal_events(result):
                yield ev
            return
        child = await self._choose_child(input)
        async for ev in child.stream_events(input):
            yield ev

    async def _stream_parallel(self, input: InvokeInput) -> AsyncIterator[Event]:
        import asyncio

        if not self.sub_agents:
            result = await self._parallel(input)
            for ev in _terminal_events(result):
                yield ev
            return
        child_input = InvokeInput(
            messages=list(input.messages),
            thread_id=None,
            model_id=input.model_id,
            tenant_id=input.tenant_id,
        )
        results = await asyncio.gather(*[a.invoke(child_input) for a in self.sub_agents.values()])
        synthesis_bits = [
            f"### {name}\n{r.final.content}" for name, r in zip(self.sub_agents.keys(), results, strict=True)
        ]
        model = _model_for(input, self.settings, self.model_spec)
        prompt = self.aggregator_prompt or self.system_prompt or "Synthesize the answers."
        collected: list[str] = []
        async for ev in _yield_model_stream(
            model,
            [
                ChatMessage(role="system", content=prompt),
                ChatMessage(
                    role="user",
                    content="Combine these specialist answers:\n\n" + "\n\n".join(synthesis_bits),
                ),
            ],
            collected,
        ):
            yield ev
        text = "".join(collected)
        final = ChatMessage(role="assistant", content=text)
        result = InvokeOutput(messages=[*input.messages, final], final=final)
        for ev in _terminal_events(result):
            yield ev

    async def _stream_groupchat(self, input: InvokeInput) -> AsyncIterator[Event]:
        if not self.sub_agents:
            result = await self._groupchat(input)
            for ev in _terminal_events(result):
                yield ev
            return
        transcript = list(input.messages)
        agents = list(self.sub_agents.values())
        names = list(self.sub_agents.keys())
        final = ChatMessage(role="assistant", content="")
        for turn in range(self.max_turns):
            agent = agents[turn % len(agents)]
            child_input = InvokeInput(
                messages=list(transcript),
                thread_id=None,
                model_id=input.model_id,
                tenant_id=input.tenant_id,
            )
            tap = _Tap()
            async for ev in _pipe_stream(agent, child_input, tap, swallow_terminal=True):
                yield ev
            content = tap.output.final.content if tap.output is not None else ""
            stamped = ChatMessage(
                role="assistant",
                content=f"[{names[turn % len(names)]}] {content}",
            )
            transcript.append(stamped)
            final = stamped
        result = InvokeOutput(messages=transcript, final=final)
        for ev in _terminal_events(result):
            yield ev

    async def _stream_reflect(self, input: InvokeInput) -> AsyncIterator[Event]:
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
        current = input
        messages = list(input.messages)
        last = InvokeOutput(
            messages=list(input.messages),
            final=ChatMessage(role="assistant", content=""),
        )
        for i in range(max_iter):
            tap = _Tap()
            async for ev in _pipe_stream(base, current, tap, swallow_terminal=True):
                yield ev
            last = tap.output or await base.invoke(current)
            if i == max_iter - 1:
                break
            score = await self._score(last.final.content, criteria, verifier_id)
            if score >= threshold:
                break
            critique = (
                f"Previous answer scored {score:.2f} (need ≥{threshold}). "
                f"Improve against: {criteria}\n\nPrior answer:\n{last.final.content}"
            )
            current = InvokeInput(
                messages=[*messages, ChatMessage(role="user", content=critique)],
                thread_id=input.thread_id,
                model_id=input.model_id,
                tenant_id=input.tenant_id,
            )
        for ev in _terminal_events(last):
            yield ev

    async def _stream_plan_execute(self, input: InvokeInput) -> AsyncIterator[Event]:
        cfg = self.plan_cfg
        max_subtasks = int(getattr(cfg, "max_subtasks", 8) or 8)
        model = _model_for(input, self.settings, self.model_spec)
        plan_prompt = [
            ChatMessage(
                role="system",
                content=self.system_prompt or "Break the user goal into a numbered list of subtasks.",
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
            step_input = InvokeInput(
                messages=[
                    ChatMessage(
                        role="user",
                        content=f"Subtask {i}/{len(lines)}: {step}\nPrior notes:\n" + "\n".join(notes),
                    )
                ],
                thread_id=None,
                model_id=input.model_id,
                tenant_id=input.tenant_id,
            )
            tap = _Tap()
            async for ev in _pipe_stream(executor, step_input, tap, swallow_terminal=True):
                yield ev
            step_text = tap.output.final.content if tap.output is not None else ""
            notes.append(f"{i}. {step} → {step_text}")

        collected: list[str] = []
        synth_messages = [
            ChatMessage(role="system", content="Synthesize the final answer from subtask notes."),
            *input.messages,
            ChatMessage(role="user", content="Notes:\n" + "\n".join(notes)),
        ]
        async for ev in _yield_model_stream(model, synth_messages, collected):
            yield ev
        text = "".join(collected)
        final = ChatMessage(role="assistant", content=text)
        result = InvokeOutput(messages=[*input.messages, final], final=final)
        for ev in _terminal_events(result):
            yield ev

    async def _router(self, input: InvokeInput) -> InvokeOutput:
        if not self.sub_agents:
            return InvokeOutput(
                messages=list(input.messages),
                final=ChatMessage(role="assistant", content="[router] no sub_agents"),
            )
        child = await self._choose_child(input)
        return await child.invoke(input)

    async def _parallel(self, input: InvokeInput) -> InvokeOutput:
        import asyncio

        if not self.sub_agents:
            return InvokeOutput(
                messages=list(input.messages),
                final=ChatMessage(role="assistant", content="[parallel] no sub_agents"),
            )
        # Children run without thread_id to avoid session races; keep model/tenant.
        child_input = InvokeInput(
            messages=list(input.messages),
            thread_id=None,
            model_id=input.model_id,
            tenant_id=input.tenant_id,
        )
        results = await asyncio.gather(*[a.invoke(child_input) for a in self.sub_agents.values()])
        synthesis_bits = [
            f"### {name}\n{r.final.content}" for name, r in zip(self.sub_agents.keys(), results, strict=True)
        ]
        model = _model_for(input, self.settings, self.model_spec)
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
            child_input = InvokeInput(
                messages=list(transcript),
                thread_id=None,
                model_id=input.model_id,
                tenant_id=input.tenant_id,
            )
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
                    model_id=input.model_id,
                    tenant_id=input.tenant_id,
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
                        content=(
                            "Score 0.0-1.0 whether the answer meets the criteria. Reply with a number only."
                        ),
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
        model = _model_for(input, self.settings, self.model_spec)
        plan_prompt = [
            ChatMessage(
                role="system",
                content=self.system_prompt or "Break the user goal into a numbered list of subtasks.",
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
                            content=f"Subtask {i}/{len(lines)}: {step}\nPrior notes:\n" + "\n".join(notes),
                        )
                    ],
                    thread_id=None,
                    model_id=input.model_id,
                    tenant_id=input.tenant_id,
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
