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
from dataclasses import dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING, Any

from felix_ai.decide import Choice

# `felix.manifests.schema` is a leaf — it imports only `felix.security.ssrf` — so this is
# safe at module scope even though `manifests/builder.py` imports `felix.patterns`.
from felix.decisions import latest_request
from felix.logging_setup import loggable
from felix.manifests.schema import PlanExecuteSpec, ReflectSpec
from felix.observability.metrics import record_counter
from felix.patterns.model import (
    ModelChatOptions,
    ModelChatResult,
    ModelClient,
    _spec_with_model,
    build_model,
    record_model_usage,
    supports_stream_turn,
)
from felix.patterns.plan_execute import _plan_subtasks, _replan_request, _step_failed
from felix.patterns.react import build_react_agent
from felix.patterns.types import (
    Agent,
    ChatMessage,
    Event,
    InvokeInput,
    InvokeOutput,
)
from felix.tools.types import Tool

if TYPE_CHECKING:
    from felix.decisions import MeteredDecider

logger = logging.getLogger(__name__)

_SCORE_RE = re.compile(r"[-+]?\d*\.?\d+")

# What an unset `ReflectSpec.criteria` means to a *model*. Deliberately not passed to the
# heuristic scorer: it reads criteria as tokens to match, so "general helpfulness" scores
# ~0 against almost any real answer, and a verifier outage would then burn every one of
# `max_iterations` passes on a default-configured agent. `heuristic_judge_score` has its
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
    return InvokeOutput(
        messages=messages or [final], final=final, stop_reason=data.get("stop_reason") or "end_turn"
    )


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
    """The pair a composite emits when it finishes, shaped like the one `react` emits.

    `stop_reason` rides on `done` as well as on `on_chain_end`, and both halves matter.
    `_pipe_stream` keeps the *last* terminal event it sees, so a parent composite reading a
    child's `done` would otherwise record `end_turn` however the child really ended -- which
    is what makes `plan_execute` replan on the `invoke` path and not the streamed one when a
    delegate is itself a composite. And `routes/openai_compat.py` reads `stop_reason` off
    this event to fill `finish_reason`, so without it a streamed composite run reported a
    default finish even after a truncation or a provider refusal. (A *reply-guard* denial
    already came out right: `ReplyControlsAgent` rewrites the terminal event it rewrote the
    reply on -- but only a manifest with reply controls configured is wrapped in it.)
    """
    return [
        Event(event="on_chain_end", data={"output": result}),
        Event(
            event="done",
            data={
                "final": result.final.model_dump(),
                "messages": [m.model_dump() for m in result.messages],
                "stop_reason": result.stop_reason,
            },
        ),
    ]


async def _yield_model_stream(
    model: ModelClient,
    messages: list[ChatMessage],
    collected: list[str],
    *,
    manifest_id: str,
    options: ModelChatOptions | None = None,
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
    if supports_stream_turn(model):
        stream_turn = model.stream_turn
        async for item in stream_turn(messages, [], opts=options):
            if isinstance(item, ModelChatResult):
                record_model_usage(item, model, manifest_id=manifest_id)
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

    result = await model.chat(messages, [], opts=options)
    record_model_usage(result, model, manifest_id=manifest_id)
    text = result.message.content or ""
    if text:
        collected.append(text)
        yield Event(
            event="text_delta",
            data={"chunk": {"content": text}, "delta": text},
        )
        yield Event(event="on_chat_model_stream", data={"chunk": {"content": text}})


def _model_for(input: InvokeInput, settings: Any, model_spec: Any) -> Any:
    """Build a model client, applying a request-level `model_id` override when present.

    The override is applied *last*, so a caller who names a model on the request wins over
    `plan_execute.planner_model`. That ordering is deliberate -- an explicit per-request
    choice outranks a manifest default -- and it means a request override moves the planning
    turn too, not only the answering one.
    """
    return build_model(settings, _spec_with_model(model_spec, input.model_id or ""))


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
    # `spec.output_schema`: the shape the *final* answer must have. Held here rather than
    # read from the manifest at call time for the same reason react holds it — one place
    # that knows the contract, and a build that cannot silently forget to apply it.
    output_schema: dict[str, Any] | None = None
    # Typed, not `Any`: these are the same strict pydantic models the governance
    # wrappers were just converted away from reading through `getattr` defaults. Every
    # such read restated a schema default at the read site, free to disagree with the
    # schema, and turned a renamed field into a silent fall back to the local default
    # rather than an error. `max_iterations` is `ge=1, le=5`; a rename to `max_passes`
    # would have left reflect quietly running two passes forever.
    reflect_cfg: ReflectSpec | None = None
    plan_cfg: PlanExecuteSpec | None = None
    # `spec.decider`, built. The router reads it whenever it is set — naming a decider on a
    # router manifest is its opt-in (see `DeciderSpec`); reflect reads it under
    # `reflect.decider`; the other composite patterns ignore it.
    decider: MeteredDecider | None = None

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
        self,
        model: ModelClient,
        messages: list[ChatMessage],
        *,
        emit_events: bool,
        options: ModelChatOptions | None = None,
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
            result = await model.chat(messages, [], opts=options)
            record_model_usage(result, model, manifest_id=self.manifest_id)
            yield result.message
            return
        collected: list[str] = []
        async for ev in _yield_model_stream(
            model, messages, collected, manifest_id=self.manifest_id, options=options
        ):
            yield ev
        yield ChatMessage(role="assistant", content="".join(collected))

    def _base_agent(self, *, recursion_limit: Any = None) -> Agent:
        """The react agent a single-agent composite wraps when the caller supplied none."""
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

    def _answer_options(self, input: InvokeInput) -> ModelChatOptions | None:
        """Options for the one turn whose output becomes the answer.

        A composite reaches a model several times — routing, planning, critiquing,
        scoring — and only one of those produces what the caller receives. Applying the
        schema to all of them would shape a planner's scratch output and leave the
        synthesis free text, which is the failure `honours_output_schema` exists to
        prevent, arrived at from the other direction.

        So this is never applied implicitly: each call site says whether its turn is the
        answering one. `_run_parallel` and `_run_plan_execute` pass it to their synthesis;
        `_run_router` and `_run_reflect` pass it to the child whose draft *is* the answer;
        `_choose_child`, `_score` and the planning turn deliberately do not.

        The manifest wins where both specify one, matching `react._chat_options`: an agent
        published with an answer contract keeps answering to it rather than to whichever
        shape the last request preferred. A caller's `response_format` still reaches a
        composite's answering turn — and, through `_child_input`, a child — whenever the
        manifest declares none, which it could not do at all before.

        Returns `None` unless a schema is actually in play, so a composite without one
        behaves exactly as it did. Forwarding a caller's `temperature`/`max_tokens` to a
        synthesis turn that never received them would be a quiet behaviour change, and
        worse than quiet: `_DelegatingAgent` has no `limits`, so unlike `react` it cannot
        clamp `max_tokens` down to `limits.max_output_tokens`, and the turn that composes
        the answer would size itself to whatever the request asked for.
        """
        caller = input.model_options
        schema = self.output_schema or (caller.output_schema if caller else None)
        if schema is None:
            return None
        return replace(caller or ModelChatOptions(), output_schema=schema)

    def _child_input(
        self,
        input: InvokeInput,
        messages: list[ChatMessage],
        *,
        options: ModelChatOptions | None = None,
    ) -> InvokeInput:
        """A turn for a sub-agent: no thread_id, so children cannot race the session.

        `options` defaults to None rather than forwarding `input.model_options`, and that
        is the whole design. A parallel specialist and a plan_execute step produce *input
        to* the answer, not the answer; forcing a schema on them would return JSON
        fragments to a synthesis prompt that wanted prose. Only a call site whose child
        output is returned verbatim passes it.
        """
        return InvokeInput(
            messages=messages,
            thread_id=None,
            model_id=input.model_id,
            tenant_id=input.tenant_id,
            model_options=options,
        )

    # --- router ---------------------------------------------------------------------

    async def _choose_child(self, input: InvokeInput) -> Agent:
        names = list(self.sub_agents.keys())
        if self.decider is not None:
            chosen = await self._decide_child(input, names)
            # Membership is checked here too, not only by the decider's own validation: a
            # pick that is not a child must fall back to the classifier, never KeyError the turn.
            if chosen is not None and chosen in self.sub_agents:
                return self.sub_agents[chosen]
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
        record_model_usage(result, model, manifest_id=self.manifest_id)
        choice = result.message.content.strip().split()[0] if result.message.content else ""
        if choice in self.sub_agents:
            record_counter("felix_router_choice", {"manifest_id": self.manifest_id, "method": "llm"})
            return self.sub_agents[choice]
        # Still the first child — a router with nowhere else to send a request has to send it
        # somewhere — but said, rather than indistinguishable from a deliberate choice.
        logger.warning(
            "router %s: classifier named no sub-agent (%s); sending to %s",
            self.manifest_id,
            loggable(choice, limit=64),
            names[0],
        )
        record_counter("felix_router_choice", {"manifest_id": self.manifest_id, "method": "unmatched"})
        return self.sub_agents[names[0]]

    async def _decide_child(self, input: InvokeInput, names: list[str]) -> str | None:
        """The decider's pick of sub-agent, or `None` to classify with the model as before.

        The routes are described where the operator already describes them — the router's
        system prompt — so that is the question's instructions and the names are its options.
        A pick below `spec.decider.min_confidence` is not taken: a router that is unsure
        between two agents is exactly the case the chat classifier is kept for.
        """
        assert self.decider is not None
        request = latest_request(input.messages)
        if request is None:
            return None
        instructions = f"{self.system_prompt}\n\nWhich agent should handle this request?".strip()
        question = {"route": Choice(instructions=instructions, criteria={n: None for n in names})}
        labels = {"manifest_id": self.manifest_id}
        try:
            result = await self.decider.decide({"request": request}, question, purpose="router")
        except Exception as exc:
            logger.warning(
                "router %s: decider failed (%s); classifying with the model",
                self.manifest_id,
                type(exc).__name__,
            )
            record_counter("felix_router_choice", {**labels, "method": "decider_error"})
            return None
        answer = result.answers["route"]
        confidence = getattr(answer, "confidence", None)
        if confidence is not None and confidence < self.decider.min_confidence:
            record_counter("felix_router_choice", {**labels, "method": "decider_unsure"})
            return None
        record_counter("felix_router_choice", {**labels, "method": "decider"})
        return str(getattr(answer, "choice", ""))

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
        # The child answers the caller directly — `_forward` hands over its terminal
        # events too — so the contract belongs on its turn. `_choose_child` above routes
        # and deliberately does not carry it: a router that replied with the classifier's
        # JSON would satisfy the schema and answer nothing.
        async for item in self._forward(
            child, replace(input, model_options=self._answer_options(input)), emit_events=emit_events
        ):
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
            # The answering turn. The specialists above deliberately get none: their
            # answers are raw material for this prompt, not the reply.
            options=self._answer_options(input),
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
        # Every iteration's draft can be the answer — the loop exits early once the score
        # clears the threshold — so the contract goes on all of them rather than on a
        # "last" one that is not known in advance. The critique then reviews a shaped
        # draft, which is the honest trade: the alternative is a free-text loop whose
        # winner is reshaped by an extra turn nobody asked for.
        current = replace(input, model_options=self._answer_options(input))
        draft = _empty_output(input)

        for iteration in range(max_iter):
            async for item in self._delegate(base, current, emit_events=emit_events):
                if isinstance(item, InvokeOutput):
                    draft = item
                else:
                    yield item
            if iteration == max_iter - 1:
                break
            score = await self._score(
                draft.final.content, criteria, verifier_id, request=latest_request(messages) or ""
            )
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
                model_options=self._answer_options(input),
            )

        async for item in self._finish(draft, emit_events=emit_events):
            yield item

    async def _score(self, answer: str, criteria: str, verifier_id: str, *, request: str = "") -> float:
        """Score an answer 0..1 against the reflect criteria.

        `criteria` is passed through raw. The model prompt substitutes
        `_DEFAULT_REFLECT_CRITERIA` when it is empty, because a model needs something to
        judge against; the heuristic does not, because it would match those words as
        tokens. One string, two consumers that must read it differently.

        Degrades to `heuristic_judge_score` — the same fallback `judge_score` uses in
        `manifests/builder.py` — when the verifier is unavailable or unparseable. It used
        to return `0.8 if len(answer) > 40 else 0.4`, which is above the default
        `ReflectSpec.threshold` of 0.7: an unreachable verifier, a rate-limited one, or a
        reply of "Score: 0.9" that `float()` rejects all silently *passed* the gate that
        exists to catch bad answers. A quality gate that cannot reach its judge must fall
        back to a real measurement, not to a constant chosen to clear the bar.
        """
        if not answer.strip():
            return 0.0

        from felix.governance.judges import heuristic_judge_score

        cfg = self.reflect_cfg
        if cfg is not None and cfg.decider and self.decider is not None:
            from felix.decisions import meets_criterion

            try:
                return await meets_criterion(
                    self.decider,
                    answer,
                    criteria or _DEFAULT_REFLECT_CRITERIA,
                    request=request,
                    purpose="reflect",
                )
            except Exception as exc:
                logger.warning("reflect: decider failed (%s); asking the verifier model", type(exc).__name__)

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
            return heuristic_judge_score(answer, criteria)

        # The verifier is billed whether or not its reply parses.
        record_model_usage(result, model, manifest_id=self.manifest_id)

        score = _parse_score(result.message.content)
        if score is None:
            logger.warning(
                "reflect verifier returned an unparseable score %r; falling back to the heuristic",
                (result.message.content or "")[:120],
            )
            return heuristic_judge_score(answer, criteria)
        return score

    # --- plan_execute ---------------------------------------------------------------

    async def _run_plan_execute(
        self, input: InvokeInput, *, emit_events: bool
    ) -> AsyncIterator[Event | InvokeOutput]:
        cfg = self.plan_cfg or PlanExecuteSpec()
        max_subtasks = cfg.max_subtasks
        replans = 0
        model = _model_for(input, self.settings, self.model_spec)
        # The planner is its own route when the manifest names one. The point of the field
        # is the asymmetry: planning is one call whose quality shapes everything after it,
        # execution is many calls that each do a narrow thing, so "plan with the expensive
        # model, execute with the cheap one" is the lever a plan/execute split exists for.
        # Unset keeps the manifest's model, so a spec that says nothing behaves as before.
        planner = (
            _model_for(input, self.settings, _spec_with_model(self.model_spec, cfg.planner_model))
            if cfg.planner_model
            else model
        )

        # `max_subtasks` is deliberately *not* bound here. It is the one argument that
        # differs between the two calls -- the opening plan may use the whole ceiling, a
        # replan only what is left of it -- and freezing it meant asking the planner for
        # eight subtasks when two slots remained and then cutting its answer to fit, which
        # truncates a revised plan mid-plan. Everything the partial does hold genuinely does
        # not vary across a run.
        plan_subtasks = partial(
            _plan_subtasks, planner, system_prompt=self.system_prompt, manifest_id=self.manifest_id
        )

        lines = await plan_subtasks(list(input.messages), max_subtasks=max_subtasks)
        if not lines:
            lines = [input.messages[-1].content if input.messages else "complete the task"]

        # `executor_model` is *not* applied here, and the omission is the point.
        # `patterns/__init__.py:_build_plan_execute` always passes `inner`, so the right-hand
        # side never evaluates for a compiled manifest -- the first attempt at this field
        # wired it here, which left it as inert as before and, worse, satisfied the textual
        # ratchet in `test_inert_manifest_fields.py`. A second copy of the rule on a branch
        # no test can reach is how it would come back, so there is one copy, where the
        # executor is built.
        executor = self.inner or self._base_agent(recursion_limit=cfg.executor_recursion_limit)

        notes: list[str] = []
        index = 0
        while index < len(lines):
            step = lines[index]
            step_input = self._child_input(
                input,
                [
                    ChatMessage(
                        role="user",
                        content=f"Subtask {index + 1}/{len(lines)}: {step}\nPrior notes:\n"
                        + "\n".join(notes),
                    )
                ],
            )
            step_text = ""
            step_stop: str = "end_turn"
            async for item in self._delegate(executor, step_input, emit_events=emit_events):
                if isinstance(item, InvokeOutput):
                    step_text = item.final.content
                    step_stop = str(item.stop_reason or "end_turn")
                else:
                    yield item

            if _step_failed(step_stop) and cfg.replan_on_failure and replans < cfg.max_replans:
                replans += 1
                logger.info(
                    "plan_execute replanning after subtask %d stopped on %s (replan %d/%d, manifest=%s)",
                    index + 1,
                    loggable(step_stop, limit=32),
                    replans,
                    cfg.max_replans,
                    loggable(self.manifest_id, limit=64),
                )
                # The budget a replan is asked for is what the ceiling has left, so the
                # planner is told the truth and its answer is used whole. `index` is at most
                # `len(lines) - 1`, and `lines` is already bounded by `max_subtasks`, so the
                # remaining budget is never below one.
                remainder = await plan_subtasks(
                    _replan_request(list(input.messages), notes, step, step_stop, index),
                    max_subtasks=max_subtasks - index,
                )
                if remainder:
                    # Keep what is done, replace what is not.
                    lines = lines[:index] + remainder
                    continue
                # A planner with nothing to say is not a reason to stop: fall through and
                # record the step as it ended, which is what the pre-replan loop always did.

            notes.append(f"{index + 1}. {step} → {step_text}")
            index += 1

        final = ChatMessage(role="assistant", content="")
        async for item in self._generate(
            model,
            [
                ChatMessage(role="system", content="Synthesize the final answer from subtask notes."),
                *input.messages,
                ChatMessage(role="user", content="Notes:\n" + "\n".join(notes)),
            ],
            emit_events=emit_events,
            # The answering turn. The planning call above and each executor step below
            # stay free-form — a plan shaped like the answer schema is not a plan.
            options=self._answer_options(input),
        ):
            if isinstance(item, ChatMessage):
                final = item
            else:
                yield item

        async for item in self._finish(
            InvokeOutput(messages=[*input.messages, final], final=final), emit_events=emit_events
        ):
            yield item
