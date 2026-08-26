"""`_DelegatingAgent` — the composite patterns and the plumbing they share.

Split out of `patterns/__init__.py`. It lived there alongside six pattern builders and
the deep-pattern plan tools, which is also why the package entry point needed two
`noqa: E402` imports to get its registration order right.

`_model_for` lives here rather than in `__init__` because this is its only caller.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

# `felix.manifests.schema` is a leaf — it imports only `felix.security.ssrf` — so this is
# safe at module scope even though `manifests/builder.py` imports `felix.patterns`.
from felix.manifests.schema import PlanExecuteSpec, ReflectSpec
from felix.patterns.model import ModelChatResult, build_model, record_usage
from felix.patterns.react import build_react_agent
from felix.patterns.types import (
    Agent,
    ChatMessage,
    Event,
    InvokeInput,
    InvokeOutput,
)
from felix.tools.types import Tool

logger = logging.getLogger(__name__)

_SCORE_RE = re.compile(r"[-+]?\d*\.?\d+")

# What an unset `ReflectSpec.criteria` means to a *model*. Deliberately not passed to the
# heuristic scorer: it reads criteria as tokens to match, so "general helpfulness" scores
# ~0 against almost any real answer, and a verifier outage would then burn every one of
# `max_iterations` passes on a default-configured agent. `_heuristic_judge_score` has its
# own empty-criteria branch, which is the right answer when nothing was asked for.
_DEFAULT_REFLECT_CRITERIA = "general helpfulness"


def _parse_score(raw: str | None) -> float | None:
    """The first 0..1 number in a verifier reply. None when there is not one.

    Verifiers are told to reply with a bare number and frequently do not — "Score: 0.9",
    "0.9/1.0", a leading newline. Taking `.split()[0]` and calling `float()` on it turned
    every one of those into an exception, so the caller's fallback decided the gate.

    A number outside 0..1 is *not* clamped into range. Clamping read "7" — almost
    certainly a verifier answering out of ten — as a perfect 1.0 that cleared every
    threshold. That is the fail-open this control exists to remove, so an out-of-range
    reply is treated as unparseable and falls through to the heuristic, which measures
    something real.

    The residual gap is a reply whose first number is coincidentally a valid score
    ("Answer 1 of 3: ..." reads as 1.0). Detecting that needs prose parsing, which would
    trade a narrow, visible limitation for a wide, invisible one; the verifier is asked
    for a bare number and the two realistic deviations — a label before it, a
    denominator after it — are both handled.
    """
    if not raw:
        return None
    match = _SCORE_RE.search(raw)
    if match is None:
        return None
    try:
        score = float(match.group())
    except ValueError:  # pragma: no cover — the pattern only matches parseable numbers
        return None
    return score if 0.0 <= score <= 1.0 else None


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


def _empty_output(input: InvokeInput) -> InvokeOutput:
    """The result of a turn that produced nothing."""
    return InvokeOutput(
        messages=list(input.messages),
        final=ChatMessage(role="assistant", content=""),
    )


def _stub_output(input: InvokeInput, text: str) -> InvokeOutput:
    """A misconfigured pattern's answer — e.g. `parallel` with no sub-agents."""
    return InvokeOutput(
        messages=list(input.messages),
        final=ChatMessage(role="assistant", content=text),
    )


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
    model: Any, messages: list[ChatMessage], collected: list[str], *, manifest_id: str
) -> AsyncIterator[Event]:
    """Stream a text-only model call as display events, and meter it.

    This drove the model through `model.stream()`, which yields text and nothing else, and
    never called `record_usage`. `record_usage` is the only feed for
    `ctx.limit_state.tokens_input/tokens_output/cost_usd`, so every call routed through
    here was invisible to `limits.max_input_tokens`, `max_output_tokens` and
    `max_cost_usd`, and produced no usage row, metric, or sink record either. The
    streamed `parallel` and `plan_execute` paths were the callers, so their synthesis and
    planning inferences were unbilled and uncapped while the non-streaming twins of the
    same methods metered correctly — the same stream/non-stream drift `_run` in
    `patterns/react.py` was written to end.

    `stream_turn` yields display deltas and finishes with the authoritative
    `ModelChatResult`, so it gives incremental output *and* usage from one request; it is
    preferred. A provider that implements only `stream()` cannot report usage from a
    streamed request at all, so we call `chat()` instead and emit its text as a single
    delta: one request, correctly metered, at the cost of token-by-token display for that
    provider. Streaming for show is not worth an uncapped spend.
    """
    stream_turn = getattr(model, "stream_turn", None)
    if stream_turn is not None:
        async for item in stream_turn(messages, []):
            if isinstance(item, ModelChatResult):
                record_usage(item, manifest_id=manifest_id, model_id=model.model_id)
                continue
            if not item.text:
                continue
            if item.kind == "thinking":
                # Dropped entirely here, where react at least forwarded it as
                # progress. Not collected: `collected` becomes the delegate's
                # answer, and reasoning is not part of it.
                yield Event(
                    event="thinking_delta",
                    data={"chunk": {"content": item.text}, "delta": item.text},
                )
                continue
            collected.append(item.text)
            yield Event(
                event="text_delta",
                data={"chunk": {"content": item.text}, "delta": item.text},
            )
            yield Event(event="on_chat_model_stream", data={"chunk": {"content": item.text}})
        return

    result = await model.chat(messages, [])
    record_usage(result, manifest_id=manifest_id, model_id=model.model_id)
    text = result.message.content or ""
    if text:
        collected.append(text)
        yield Event(
            event="text_delta",
            data={"chunk": {"content": text}, "delta": text},
        )
        yield Event(event="on_chat_model_stream", data={"chunk": {"content": text}})


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


@dataclass
class _DelegatingAgent:
    """The composite patterns: deep, router, parallel, groupchat, reflect, plan_execute.

    Each pattern is written once, as `_run_*(input, *, emit_events)` — an async generator
    that yields display events when asked and always ends with exactly one `InvokeOutput`.
    `invoke` drains it for the output; `stream_events` drains it for the events.

    Every pattern used to exist twice, as `_x` and `_stream_x`. The copies drifted, and
    the drift shipped: `_stream_parallel` and `_stream_plan_execute` never called
    `record_usage`, so streamed runs of those patterns were unbilled and escaped
    `limits.max_cost_usd` while their non-streaming twins metered correctly. That is the
    second time this shape has produced a defect here — `patterns/react.py:_run` was
    written to end the same duplication after the streaming half stopped emitting audit
    records. One implementation per pattern is the point; keep it that way.
    """

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
    # Typed, not `Any`: these are the same strict pydantic models the governance
    # wrappers were just converted away from reading through `getattr` defaults. Every
    # such read restated a schema default at the read site, free to disagree with the
    # schema, and turned a renamed field into a silent fall back to the local default
    # rather than an error. `max_iterations` is `ge=1, le=5`; a rename to `max_passes`
    # would have left reflect quietly running two passes forever.
    reflect_cfg: ReflectSpec | None = None
    plan_cfg: PlanExecuteSpec | None = None

    # --- the two public entry points, both draining the one loop -------------------

    async def invoke(self, input: InvokeInput) -> InvokeOutput:
        """Run the pattern to completion and return its result, discarding display events."""
        out: InvokeOutput | None = None
        async for item in self._run(input, emit_events=False):
            if isinstance(item, InvokeOutput):
                out = item
        return out if out is not None else _empty_output(input)

    async def stream_events(self, input: InvokeInput) -> AsyncIterator[Event]:
        """Run the pattern, emitting display events as they happen."""
        async for item in self._run(input, emit_events=True):
            if isinstance(item, Event):
                yield item

    async def _run(self, input: InvokeInput, *, emit_events: bool) -> AsyncIterator[Event | InvokeOutput]:
        """Dispatch to the pattern. Yields display events, then one `InvokeOutput`."""
        # No `deep` branch: the dispatch table has no "deep" key, so it falls to the
        # `self.inner` forward below — which is what a dedicated branch did anyway. Having
        # both read as though `deep` were special.
        runner = {
            "router": self._run_router,
            "parallel": self._run_parallel,
            "groupchat": self._run_groupchat,
            "reflect": self._run_reflect,
            "plan_execute": self._run_plan_execute,
        }.get(self.pattern)
        if runner is not None:
            async for item in runner(input, emit_events=emit_events):
                yield item
            return

        if self.inner is not None:
            async for item in self._forward(self.inner, input, emit_events=emit_events):
                yield item
            return

        async for item in self._finish(_empty_output(input), emit_events=emit_events):
            yield item

    # --- shared plumbing ------------------------------------------------------------

    async def _finish(
        self, result: InvokeOutput, *, emit_events: bool
    ) -> AsyncIterator[Event | InvokeOutput]:
        """Close out a pattern that composes its own answer."""
        if emit_events:
            for ev in _terminal_events(result):
                yield ev
        yield result

    async def _forward(
        self, agent: Agent, input: InvokeInput, *, emit_events: bool
    ) -> AsyncIterator[Event | InvokeOutput]:
        """Hand the turn to a child wholesale — its events *and* its terminal events."""
        if not emit_events:
            yield await agent.invoke(input)
            return
        tap = _Tap()
        async for ev in _pipe_stream(agent, input, tap, swallow_terminal=False):
            yield ev
        # Never re-invoke to fill this in: `stream_events` discards it, and a second
        # `invoke()` here would silently double the cost of a forwarded turn.
        yield tap.output if tap.output is not None else _empty_output(input)

    async def _delegate(
        self, agent: Agent, input: InvokeInput, *, emit_events: bool
    ) -> AsyncIterator[Event | InvokeOutput]:
        """Run a child as one step of a larger pattern, swallowing its terminal events."""
        if not emit_events:
            yield await agent.invoke(input)
            return
        tap = _Tap()
        async for ev in _pipe_stream(agent, input, tap, swallow_terminal=True):
            yield ev
        # As in `_forward`: never re-invoke to fill a missing output. `_stream_reflect`
        # did (`tap.output or await base.invoke(current)`), silently doubling the cost of
        # a turn whose child emitted no terminal event.
        yield tap.output if tap.output is not None else _empty_output(input)

    async def _generate(
        self, model: Any, messages: list[ChatMessage], *, emit_events: bool
    ) -> AsyncIterator[Event | ChatMessage]:
        """Produce an assistant message from `model`, ending with the complete one.

        Both arms record usage — this is the single place a composite pattern reaches a
        model for text, so metering cannot differ between streaming and not.

        The non-streaming arm yields the model's own `ChatMessage` rather than rebuilding
        one from its text. Collapsing the stream/non-stream pair briefly did rebuild it,
        which silently dropped `thinking` from the synthesized answer of a `parallel` or
        `plan_execute` run — `session/types.py` persists and replays those blocks, so an
        extended-thinking manifest lost its reasoning on exactly the turn that composed
        the answer. Streaming still rebuilds, because `stream()` yields text and the wire
        gives it nothing else to carry.
        """
        if not emit_events:
            result = await model.chat(messages, [])
            record_usage(result, manifest_id=self.manifest_id, model_id=model.model_id)
            yield result.message
            return
        collected: list[str] = []
        async for ev in _yield_model_stream(model, messages, collected, manifest_id=self.manifest_id):
            yield ev
        yield ChatMessage(role="assistant", content="".join(collected))

    def _base_agent(self, *, recursion_limit: Any = None) -> Agent:
        """The react agent a single-agent composite wraps."""
        ctx: dict[str, Any] = {
            "tools": self.tools,
            "system_prompt": self.system_prompt,
            "model_spec": self.model_spec,
            "manifest_id": self.manifest_id,
            "manifest_version": self.manifest_version,
            "settings": self.settings,
        }
        if recursion_limit is not None:
            ctx["recursion_limit"] = recursion_limit
        return build_react_agent(ctx)

    def _child_input(self, input: InvokeInput, messages: list[ChatMessage]) -> InvokeInput:
        """A turn for a sub-agent: no thread_id, so children cannot race the session."""
        return InvokeInput(
            messages=messages,
            thread_id=None,
            model_id=input.model_id,
            tenant_id=input.tenant_id,
        )

    # --- router ---------------------------------------------------------------------

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

    async def _run_router(
        self, input: InvokeInput, *, emit_events: bool
    ) -> AsyncIterator[Event | InvokeOutput]:
        if not self.sub_agents:
            async for item in self._finish(
                _stub_output(input, "[router] no sub_agents"), emit_events=emit_events
            ):
                yield item
            return
        child = await self._choose_child(input)
        async for item in self._forward(child, input, emit_events=emit_events):
            yield item

    # --- parallel -------------------------------------------------------------------

    async def _run_parallel(
        self, input: InvokeInput, *, emit_events: bool
    ) -> AsyncIterator[Event | InvokeOutput]:
        import asyncio

        if not self.sub_agents:
            async for item in self._finish(
                _stub_output(input, "[parallel] no sub_agents"), emit_events=emit_events
            ):
                yield item
            return

        child_input = self._child_input(input, list(input.messages))
        results = await asyncio.gather(*[a.invoke(child_input) for a in self.sub_agents.values()])
        synthesis_bits = [
            f"### {name}\n{r.final.content}" for name, r in zip(self.sub_agents.keys(), results, strict=True)
        ]
        model = _model_for(input, self.settings, self.model_spec)
        prompt = self.aggregator_prompt or self.system_prompt or "Synthesize the answers."

        final = ChatMessage(role="assistant", content="")
        async for item in self._generate(
            model,
            [
                ChatMessage(role="system", content=prompt),
                ChatMessage(
                    role="user",
                    content="Combine these specialist answers:\n\n" + "\n\n".join(synthesis_bits),
                ),
            ],
            emit_events=emit_events,
        ):
            if isinstance(item, ChatMessage):
                final = item
            else:
                yield item

        async for item in self._finish(
            InvokeOutput(messages=[*input.messages, final], final=final), emit_events=emit_events
        ):
            yield item

    # --- groupchat ------------------------------------------------------------------

    async def _run_groupchat(
        self, input: InvokeInput, *, emit_events: bool
    ) -> AsyncIterator[Event | InvokeOutput]:
        if not self.sub_agents:
            async for item in self._finish(
                _stub_output(input, "[groupchat] no sub_agents"), emit_events=emit_events
            ):
                yield item
            return

        transcript = list(input.messages)
        agents = list(self.sub_agents.values())
        names = list(self.sub_agents.keys())
        final = ChatMessage(role="assistant", content="")

        for turn in range(self.max_turns):
            agent = agents[turn % len(agents)]
            content = ""
            async for item in self._delegate(
                agent, self._child_input(input, list(transcript)), emit_events=emit_events
            ):
                if isinstance(item, InvokeOutput):
                    content = item.final.content
                else:
                    yield item
            stamped = ChatMessage(
                role="assistant",
                content=f"[{names[turn % len(names)]}] {content}",
            )
            transcript.append(stamped)
            final = stamped

        async for item in self._finish(
            InvokeOutput(messages=transcript, final=final), emit_events=emit_events
        ):
            yield item

    # --- reflect --------------------------------------------------------------------

    async def _run_reflect(
        self, input: InvokeInput, *, emit_events: bool
    ) -> AsyncIterator[Event | InvokeOutput]:
        base = self.inner or self._base_agent()
        cfg = self.reflect_cfg or ReflectSpec()
        max_iter = cfg.max_iterations
        threshold = cfg.threshold
        criteria = cfg.criteria
        verifier_id = cfg.verifier_model

        messages = list(input.messages)
        current = input
        draft = _empty_output(input)

        for iteration in range(max_iter):
            async for item in self._delegate(base, current, emit_events=emit_events):
                if isinstance(item, InvokeOutput):
                    draft = item
                else:
                    yield item
            if iteration == max_iter - 1:
                break
            score = await self._score(draft.final.content, criteria, verifier_id)
            if score >= threshold:
                break
            critique = (
                f"Previous answer scored {score:.2f} (need ≥{threshold}). "
                f"Improve against: {criteria or _DEFAULT_REFLECT_CRITERIA}\n\n"
                f"Prior answer:\n{draft.final.content}"
            )
            # Reflect re-runs the *same* conversation, so unlike a sub-agent step it keeps
            # the caller's thread_id.
            current = InvokeInput(
                messages=[*messages, ChatMessage(role="user", content=critique)],
                thread_id=input.thread_id,
                model_id=input.model_id,
                tenant_id=input.tenant_id,
            )

        async for item in self._finish(draft, emit_events=emit_events):
            yield item

    async def _score(self, answer: str, criteria: str, verifier_id: str) -> float:
        """Score an answer 0..1 against the reflect criteria.

        `criteria` is passed through raw. The model prompt substitutes
        `_DEFAULT_REFLECT_CRITERIA` when it is empty, because a model needs something to
        judge against; the heuristic does not, because it would match those words as
        tokens. One string, two consumers that must read it differently.

        Degrades to `_heuristic_judge_score` — the same fallback `_judge_score` uses in
        `manifests/builder.py` — when the verifier is unavailable or unparseable. It used
        to return `0.8 if len(answer) > 40 else 0.4`, which is above the default
        `ReflectSpec.threshold` of 0.7: an unreachable verifier, a rate-limited one, or a
        reply of "Score: 0.9" that `float()` rejects all silently *passed* the gate that
        exists to catch bad answers. A quality gate that cannot reach its judge must fall
        back to a real measurement, not to a constant chosen to clear the bar.
        """
        if not answer.strip():
            return 0.0

        from felix.manifests.builder import _heuristic_judge_score

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
                        content=f"Criteria: {criteria or _DEFAULT_REFLECT_CRITERIA}\n\nAnswer:\n{answer}",
                    ),
                ],
                [],
            )
        except Exception:
            logger.warning("reflect verifier call failed; falling back to the heuristic score", exc_info=True)
            return _heuristic_judge_score(answer, criteria)

        # The verifier is billed whether or not its reply parses.
        record_usage(result, manifest_id=self.manifest_id, model_id=model.model_id)

        score = _parse_score(result.message.content)
        if score is None:
            logger.warning(
                "reflect verifier returned an unparseable score %r; falling back to the heuristic",
                (result.message.content or "")[:120],
            )
            return _heuristic_judge_score(answer, criteria)
        return score

    # --- plan_execute ---------------------------------------------------------------

    async def _run_plan_execute(
        self, input: InvokeInput, *, emit_events: bool
    ) -> AsyncIterator[Event | InvokeOutput]:
        cfg = self.plan_cfg or PlanExecuteSpec()
        max_subtasks = cfg.max_subtasks
        model = _model_for(input, self.settings, self.model_spec)

        plan_result = await model.chat(
            [
                ChatMessage(
                    role="system",
                    content=self.system_prompt or "Break the user goal into a numbered list of subtasks.",
                ),
                *input.messages,
                ChatMessage(
                    role="user",
                    content=f"Return at most {max_subtasks} numbered subtasks, one per line.",
                ),
            ],
            [],
        )
        record_usage(plan_result, manifest_id=self.manifest_id, model_id=model.model_id)
        lines = [
            ln.strip().lstrip("0123456789.-) ").strip()
            for ln in plan_result.message.content.splitlines()
            if ln.strip()
        ][:max_subtasks]
        if not lines:
            lines = [input.messages[-1].content if input.messages else "complete the task"]

        executor = self.inner or self._base_agent(recursion_limit=cfg.executor_recursion_limit)

        notes: list[str] = []
        for i, step in enumerate(lines, 1):
            step_input = self._child_input(
                input,
                [
                    ChatMessage(
                        role="user",
                        content=f"Subtask {i}/{len(lines)}: {step}\nPrior notes:\n" + "\n".join(notes),
                    )
                ],
            )
            step_text = ""
            async for item in self._delegate(executor, step_input, emit_events=emit_events):
                if isinstance(item, InvokeOutput):
                    step_text = item.final.content
                else:
                    yield item
            notes.append(f"{i}. {step} → {step_text}")

        final = ChatMessage(role="assistant", content="")
        async for item in self._generate(
            model,
            [
                ChatMessage(role="system", content="Synthesize the final answer from subtask notes."),
                *input.messages,
                ChatMessage(role="user", content="Notes:\n" + "\n".join(notes)),
            ],
            emit_events=emit_events,
        ):
            if isinstance(item, ChatMessage):
                final = item
            else:
                yield item

        async for item in self._finish(
            InvokeOutput(messages=[*input.messages, final], final=final), emit_events=emit_events
        ):
            yield item
