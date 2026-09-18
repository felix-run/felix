"""`spec.plan_execute` fields that read as controls, now doing what they say.

Five of the seven validated and were read by nothing: a manifest could set a planner model,
ask for replanning, or cap replans, and `felix validate-manifest` would bless all of it while
the harness ignored every one. That shape is worse than a missing feature, because the only
way to learn the truth was to grep the harness — `test_inert_manifest_fields.py` tracks the
class of bug and its list is four names shorter for this.

Asserted against what actually reached a model rather than against the reply, because the
reply is scripted: a test that checked the answer would pass with every field still inert.
"""

from __future__ import annotations

import json
from typing import Any, NamedTuple

import pytest
from felix.manifests.schema import ModelSpec, PlanExecuteSpec
from felix.patterns.model import _spec_with_model
from felix.patterns.plan_execute import _FAILED_STOP_REASONS, _step_failed
from felix.patterns.types import Event, InvokeInput, InvokeOutput
from felix_ai.types import ChatMessage, ModelChatResult, StreamDelta, TokenUsage

# Both arms, because `_generate` reaches the model through two different calls and the
# streaming half is where this repo has repeatedly found drift.
STREAMING = [pytest.param(False, id="invoke"), pytest.param(True, id="stream")]


class _Run(NamedTuple):
    events: list[Event]
    output: InvokeOutput


async def _run(agent: Any, *, streaming: bool, model_id: str | None = None) -> _Run:
    """One turn through whichever arm, handing back both what it emitted and what it said.

    The output matters as much as the events: several assertions below are about *which
    client produced the answer*, and a spy recording the specs `_model_for` was handed
    cannot see that — both clients are constructed before either is used.
    """
    input = InvokeInput(messages=[ChatMessage(role="user", content="do the thing")], model_id=model_id)
    events: list[Event] = []
    output: Any = None
    if streaming:
        async for ev in agent.stream_events(input):
            events.append(ev)
            # `stream_events` yields events and nothing else, so the answer comes off the
            # terminal frame -- the same place `/v1` and the SSE route read it from.
            if ev.event == "on_chain_end" and isinstance(ev.data.get("output"), InvokeOutput):
                output = ev.data["output"]
    else:
        output = await agent.invoke(input)
    return _Run(events, output)


def test_a_planner_route_is_swapped_onto_the_spec_and_nothing_else_is() -> None:
    """The id is a route name; everything else on the spec is the manifest's own choice.

    Carrying the rest across is the difference between "plan on this route" and "opt out of
    my model configuration" — a manifest that set a price override or a fallback chain did
    not ask to lose them when it named a planner.
    """
    base = ModelSpec(id="default", temperature=0.4, fallbacks=["backup"], price={"input": 1.0})

    planner = _spec_with_model(base, "strong")

    assert planner.id == "strong"
    assert planner.temperature == 0.4
    assert planner.fallbacks == ["backup"]
    assert planner.price == {"input": 1.0}
    assert base.id == "default", "the caller's spec was mutated"


def test_an_empty_model_id_leaves_the_spec_exactly_as_it_was() -> None:
    """Unset is the default, so this is the path every existing manifest takes."""
    base = ModelSpec(id="default")

    assert _spec_with_model(base, "") is base


@pytest.mark.parametrize(
    ("stop_reason", "failed"),
    [
        ("refusal", True),
        ("max_tokens", True),
        ("end_turn", False),
        ("tool_use", False),
        ("stop_sequence", False),
        ("unknown", False),
    ],
)
def test_only_an_early_ending_counts_as_a_failed_subtask(stop_reason: str, failed: bool) -> None:
    """Narrow on purpose, and both directions matter.

    `refusal` is governance replacing the reply and `max_tokens` is the model cut off
    mid-answer: in both the note the synthesiser would record is not an answer. `unknown` is
    the one worth pinning on the other side — it is what a provider that said nothing useful
    reports, and treating it as failure would replan on every such turn.
    """
    assert _step_failed(stop_reason) is failed


def test_the_failure_set_is_the_whole_definition() -> None:
    """Nothing else may decide a step failed — the set is the single place that says so.

    Asserted as an equality rather than a membership, because the way this loosens is a
    later reader adding `tool_use` (an executor that ran out of steps) or `unknown` (a
    provider that said nothing useful) without weighing that both also describe successes.
    """
    assert set(_FAILED_STOP_REASONS) == {"refusal", "max_tokens"}


def test_planner_few_shots_is_gone_and_a_stored_manifest_that_set_it_still_loads() -> None:
    """Removed rather than wired: it named a count of examples with no corpus behind it
    anywhere, so there was nothing to make it mean.

    The schema is `extra=forbid`, so dropping a field retroactively invalidates every stored
    manifest that set it — which is what `RETIRED` exists for, and dropping it is inert here
    precisely because nothing read it.
    """
    from felix.manifests.compat import RETIRED, drop_retired

    assert ("spec", "plan_execute", "planner_few_shots") in RETIRED
    assert not hasattr(PlanExecuteSpec(), "planner_few_shots")

    body: dict[str, Any] = {
        "spec": {"pattern": "plan_execute", "plan_execute": {"planner_few_shots": 3, "max_subtasks": 4}}
    }
    cleaned, dropped = drop_retired(body)

    assert dropped == [("spec", "plan_execute", "planner_few_shots")]
    assert cleaned["spec"]["plan_execute"] == {"max_subtasks": 4}
    assert body["spec"]["plan_execute"]["planner_few_shots"] == 3, "the caller's body was mutated"


# --- through the pattern, not just the helpers ---------------------------------------
#
# Everything above tests a function in isolation, which proves a helper honours an argument
# and stays green if `_run_plan_execute` never passes one. That is this repo's named defect
# shape, so the rest of the file drives the pattern and watches what it does.


class _PlanSpy:
    """A model that returns a fixed plan, recording the spec it was built from.

    `_model_for` is the seam every composite reaches a model through, so patching it and
    recording its `model_spec` argument is how "which route planned" becomes visible —
    the returned text is identical whichever route produced it.
    """

    model_id = "spy"

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls = 0

    def _next(self) -> str:
        self.calls += 1
        return self.replies.pop(0) if self.replies else "done"

    def _result(self) -> ModelChatResult:
        return ModelChatResult(
            message=ChatMessage(role="assistant", content=self._next()),
            usage=TokenUsage(input=1, output=1),
        )

    async def chat(self, messages: list[ChatMessage], tools: list, opts: Any = None) -> ModelChatResult:
        return self._result()

    async def stream_turn(self, messages: list[ChatMessage], tools: list, opts: Any = None) -> Any:
        result = self._result()
        yield StreamDelta(kind="text", text=result.message.content)
        yield result


class _StepSpy:
    """A sub-agent whose steps end on a given stop reason, recording every subtask it saw."""

    def __init__(self, stop_reasons: list[str] | None = None, *, plan_execute: bool = True) -> None:
        self.stop_reasons = list(stop_reasons or [])
        self.subtasks: list[str] = []
        # `reflect` hands its child the caller's own conversation, not a subtask prompt, so
        # the shape guard below applies only to the pattern that builds one.
        self.plan_execute = plan_execute

    def _out(self, input: InvokeInput) -> InvokeOutput:
        # The step name only, not the whole prompt. The prompt also carries "Prior notes",
        # so a substring match against it counts every later step that mentions an earlier
        # one -- which reads as "the completed step ran again" when nothing ran twice.
        content = input.messages[-1].content if input.messages else ""
        head = content.split("\n", 1)[0]
        # Fail loudly rather than record a wrong name. The prompt is built in
        # `_run_plan_execute`, and reformatting it would otherwise make every assertion here
        # read "the replan never reached the executor" -- a fake failure that looks exactly
        # like a real one.
        if self.plan_execute:
            assert head.startswith("Subtask ") and ": " in head, f"the subtask prompt changed shape: {head!r}"
            self.subtasks.append(head.split(": ", 1)[1])
        stop = self.stop_reasons.pop(0) if self.stop_reasons else "end_turn"
        msg = ChatMessage(role="assistant", content="step output")
        return InvokeOutput(messages=[*input.messages, msg], final=msg, stop_reason=stop)  # type: ignore[arg-type]

    async def invoke(self, input: InvokeInput) -> InvokeOutput:
        return self._out(input)

    async def stream_events(self, input: InvokeInput) -> Any:
        out = self._out(input)
        yield Event(event="on_chain_end", data={"output": out})
        # `stop_reason` on `done` too, because `react.py` puts it there and `_pipe_stream`
        # keeps the *last* terminal event it sees. A spy that omitted it would report every
        # streamed step as `end_turn` however it really ended -- which is a fake test failure
        # that looks exactly like a real streaming bug.
        yield Event(
            event="done",
            data={"final": out.final.model_dump(), "stop_reason": out.stop_reason},
        )


def _plan_agent(**kw: Any) -> Any:
    from felix.manifests.schema import ModelSpec
    from felix.patterns import delegating

    agent = delegating._DelegatingAgent(
        tools=[],
        pattern="plan_execute",
        manifest_id="pe",
        manifest_version="1",
        model_spec=ModelSpec(id="manifest-model"),
        **kw,
    )
    agent.settings = None
    return agent


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", STREAMING)
async def test_both_routes_are_built_and_only_one_is_the_planner(streaming: bool) -> None:
    """The cheap shape check: two clients, built from the right two specs, in order.

    It stops there on purpose. Both are constructed before either is used, so this says
    nothing about which one *performed* which turn —
    `test_each_route_does_its_own_half_of_the_run` is the test that can enforce that, and it
    does it by the text that came back rather than by a captured argument.
    """
    import felix.patterns.delegating as delegating
    from felix.manifests.schema import PlanExecuteSpec

    model = _PlanSpy(["1. alpha", "final answer"])
    seen_ids: list[str] = []

    def _capture(input: Any, settings: Any, model_spec: Any) -> Any:
        seen_ids.append(getattr(model_spec, "id", None))
        return model

    orig = delegating._model_for
    delegating._model_for = _capture  # type: ignore[assignment]
    try:
        agent = _plan_agent(
            inner=_StepSpy(),
            plan_cfg=PlanExecuteSpec(planner_model="strong-planner"),
        )
        await _run(agent, streaming=streaming)
    finally:
        delegating._model_for = orig  # type: ignore[assignment]

    # Order, not membership. Both ids appear whichever turn used which, so `in` is satisfied
    # by the precise inversion of the feature -- plan on the manifest model, synthesise on
    # the planner. `_run_plan_execute` builds the run's own model first and the planner
    # second, so the sequence is the assertion.
    assert seen_ids == ["manifest-model", "strong-planner"], (
        f"the planner and answering routes are not where they belong: {seen_ids}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", STREAMING)
async def test_an_unset_planner_model_keeps_every_turn_on_the_manifests_model(
    streaming: bool,
) -> None:
    """The default, and therefore the path every existing manifest takes."""
    import felix.patterns.delegating as delegating
    from felix.manifests.schema import PlanExecuteSpec

    model = _PlanSpy(["1. alpha", "final answer"])
    seen_ids: list[str] = []

    def _capture(input: Any, settings: Any, model_spec: Any) -> Any:
        seen_ids.append(getattr(model_spec, "id", None))
        return model

    orig = delegating._model_for
    delegating._model_for = _capture  # type: ignore[assignment]
    try:
        await _run(_plan_agent(inner=_StepSpy(), plan_cfg=PlanExecuteSpec()), streaming=streaming)
    finally:
        delegating._model_for = orig  # type: ignore[assignment]

    assert set(seen_ids) == {"manifest-model"}, seen_ids


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", STREAMING)
async def test_a_refused_subtask_replans_the_remainder(streaming: bool) -> None:
    """The field said `replan_on_failure: true` and nothing replanned.

    The first subtask is refused, so the planner is asked again and the *remaining* work is
    replaced. Asserted on the subtasks the executor actually received, because that is the
    only place a replan is visible — the synthesised answer is the same either way.
    """
    import felix.patterns.delegating as delegating
    from felix.manifests.schema import PlanExecuteSpec

    # plan, then the replan, then the synthesis.
    model = _PlanSpy(["1. alpha\n2. beta", "1. gamma", "final answer"])
    steps = _StepSpy(stop_reasons=["refusal"])

    orig = delegating._model_for
    delegating._model_for = lambda *a, **k: model  # type: ignore[assignment]
    try:
        agent = _plan_agent(inner=steps, plan_cfg=PlanExecuteSpec(max_replans=2))
        await _run(agent, streaming=streaming)
    finally:
        delegating._model_for = orig  # type: ignore[assignment]

    # The exact sequence, not membership: `_PlanSpy` answers "done" once its script runs
    # out, so a spurious extra planning call shows up as an extra subtask that `in` would
    # not notice.
    assert steps.subtasks == ["alpha", "gamma"], steps.subtasks


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", STREAMING)
async def test_replanning_is_not_attempted_when_the_manifest_turns_it_off(
    streaming: bool,
) -> None:
    """Both sides of the switch, because a test that only saw replanning happen would pass
    against a pattern that replans unconditionally — which is the same bug wearing the
    opposite sign."""
    import felix.patterns.delegating as delegating
    from felix.manifests.schema import PlanExecuteSpec

    model = _PlanSpy(["1. alpha\n2. beta", "1. gamma", "final answer"])
    steps = _StepSpy(stop_reasons=["refusal"])

    orig = delegating._model_for
    delegating._model_for = lambda *a, **k: model  # type: ignore[assignment]
    try:
        agent = _plan_agent(inner=steps, plan_cfg=PlanExecuteSpec(replan_on_failure=False))
        await _run(agent, streaming=streaming)
    finally:
        delegating._model_for = orig  # type: ignore[assignment]

    assert steps.subtasks == ["alpha", "beta"], (
        f"the plan did not run exactly as planned with replanning off: {steps.subtasks}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", STREAMING)
async def test_max_replans_bounds_a_subtask_that_always_fails(streaming: bool) -> None:
    """Otherwise a step that cannot succeed spends the whole run replanning around itself."""
    import felix.patterns.delegating as delegating
    from felix.manifests.schema import PlanExecuteSpec

    model = _PlanSpy(["1. alpha"] + ["1. retry"] * 8 + ["final answer"])
    steps = _StepSpy(stop_reasons=["refusal"] * 8)

    orig = delegating._model_for
    delegating._model_for = lambda *a, **k: model  # type: ignore[assignment]
    try:
        agent = _plan_agent(inner=steps, plan_cfg=PlanExecuteSpec(max_replans=1))
        await _run(agent, streaming=streaming)
    finally:
        delegating._model_for = orig  # type: ignore[assignment]

    # One plan, one replan, one synthesis: the ceiling held.
    assert len(steps.subtasks) == 2, f"replanned past max_replans=1: {steps.subtasks}"


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", STREAMING)
async def test_a_replan_keeps_the_steps_already_done(streaming: bool) -> None:
    """Replanning replaces the *remainder*, not the plan.

    The distinction only shows up when a step after the first fails: at index 0 the two are
    the same expression, which is how a mutation replacing `lines[:index] + remainder` with
    `remainder` survived a test that failed step one. Here step two of three fails, so a
    whole-plan replacement would re-run step one and spend the budget twice.
    """
    import felix.patterns.delegating as delegating
    from felix.manifests.schema import PlanExecuteSpec

    model = _PlanSpy(["1. alpha\n2. beta\n3. gamma", "1. delta", "final answer"])
    steps = _StepSpy(stop_reasons=["end_turn", "refusal"])

    orig = delegating._model_for
    delegating._model_for = lambda *a, **k: model  # type: ignore[assignment]
    try:
        agent = _plan_agent(inner=steps, plan_cfg=PlanExecuteSpec(max_replans=2))
        await _run(agent, streaming=streaming)
    finally:
        delegating._model_for = orig  # type: ignore[assignment]

    assert steps.subtasks == ["alpha", "beta", "delta"], (
        f"a replan must keep what is done and replace only the remainder: {steps.subtasks}"
    )


@pytest.mark.asyncio
async def test_the_compile_hands_the_executor_route_to_the_agent_it_builds() -> None:
    """The test that was missing, and its absence let `executor_model` ship inert.

    `_build_plan_execute` always passes `inner`, so `_DelegatingAgent`'s
    `self.inner or self._base_agent(...)` fallback never evaluates for a compiled manifest.
    Wiring the field only there left it doing nothing -- and *quieter* than before, because
    the mention satisfied the textual ratchet in `test_inert_manifest_fields.py`, which then
    reported it as fixed.

    So this runs the real builder and asserts on the executor it actually produced, rather
    than on the dict it was handed: `build_react_agent` reading `model_spec` is half of the
    wiring, and a test that stops at the argument cannot see that half stop working.
    """
    import felix.patterns as patterns_pkg
    from felix.manifests.loader import parse_manifest

    manifest = parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "pe"},
            "spec": {
                "pattern": "plan_execute",
                "model": {"id": "manifest-model"},
                "plan_execute": {"executor_model": "cheap-executor", "executor_recursion_limit": 4},
            },
        }
    )

    agent = await patterns_pkg._build_plan_execute(
        {
            "manifest": manifest,
            "tools": [],
            "model_spec": manifest.spec.model,
            "manifest_id": "pe",
            "settings": None,
        }
    )

    assert getattr(agent.inner.model_spec, "id", None) == "cheap-executor", (
        f"the executor route never reached the agent: {agent.inner.model_spec}"
    )
    # The sibling field, asserted alongside so the pair cannot drift apart again.
    assert agent.inner.recursion_limit == 4
    # The composite itself stays on the manifest's model: `executor_model` routes the steps,
    # not the synthesis that answers.
    assert agent.model_spec.id == "manifest-model"
    assert manifest.spec.model.id == "manifest-model", "the manifest's own spec was mutated"


@pytest.mark.asyncio
async def test_a_plan_execute_agent_built_without_an_inner_agent_still_runs() -> None:
    """The fallback branch, which is where `executor_model` was wired first and did nothing.

    It carries no plan_execute field on purpose — `_build_plan_execute` always passes
    `inner`, so anything applied only here is inert for every compiled manifest. What the
    branch still owes is an executor at all, and nothing else constructs this shape.
    """
    from felix.manifests.schema import PlanExecuteSpec
    from felix.patterns import delegating

    agent = delegating._DelegatingAgent(
        tools=[],
        pattern="plan_execute",
        manifest_id="pe",
        manifest_version="1",
        model_spec=ModelSpec(id="manifest-model"),
        plan_cfg=PlanExecuteSpec(executor_recursion_limit=3),
    )
    agent.settings = None

    built: list[Any] = []
    model = _PlanSpy(["1. alpha", "final answer"])
    orig = delegating.build_react_agent
    delegating.build_react_agent = lambda ctx: built.append(ctx) or _StepSpy()  # type: ignore[assignment]
    orig_model = delegating._model_for
    delegating._model_for = lambda *a, **k: model  # type: ignore[assignment]
    try:
        await _run(agent, streaming=False)
    finally:
        delegating.build_react_agent = orig  # type: ignore[assignment]
        delegating._model_for = orig_model  # type: ignore[assignment]

    assert len(built) == 1, "the fallback built no executor"
    assert built[0]["model_spec"].id == "manifest-model"
    assert built[0]["recursion_limit"] == 3


# --- the two seams the wiring above leans on ------------------------------------------
#
# Both were changed by this branch and neither had a test: `_model_for` collapsed onto
# `_spec_with_model`, and the composite terminal pair grew the `stop_reason` that
# `_step_failed` reads. Every test above patches `_model_for` out and drives the pattern's
# own doubles, so a mutation to either survived the whole file.


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", STREAMING)
async def test_a_model_named_on_the_request_outranks_the_planner_route(streaming: bool) -> None:
    """`_model_for` applies the request override *last*, so it wins over `planner_model`.

    Asserted through `build_model`, one layer below the seam every other test in this file
    patches: `_model_for` itself has to run for the ordering to be observable at all. An
    explicit per-request choice outranking a manifest default is the deliberate half — and
    it means a request override moves the planning turn too, not only the answering one,
    which is the part a reader would otherwise have to take on faith.
    """
    import felix.patterns.delegating as delegating
    from felix.manifests.schema import PlanExecuteSpec

    model = _PlanSpy(["1. alpha", "final answer"])
    built: list[Any] = []

    def _capture(settings: Any, spec: Any) -> Any:
        built.append(getattr(spec, "id", None))
        return model

    orig = delegating.build_model
    delegating.build_model = _capture  # type: ignore[assignment]
    try:
        agent = _plan_agent(
            inner=_StepSpy(),
            plan_cfg=PlanExecuteSpec(planner_model="strong-planner"),
        )
        await _run(agent, streaming=streaming, model_id="request-override")
    finally:
        delegating.build_model = orig  # type: ignore[assignment]

    assert built, "no model was built at all"
    assert set(built) == {"request-override"}, f"the request-level model did not reach every turn: {built}"


@pytest.mark.asyncio
async def test_a_streamed_composite_says_how_it_really_ended() -> None:
    """`stop_reason` rides on the composite's `done` event, not only on `on_chain_end`.

    Two things read it there. `_pipe_stream` keeps the *last* terminal event it sees, so a
    composite delegating to another composite recorded `end_turn` however the child really
    ended — which makes `plan_execute` replan on the `invoke` path and not the streamed one.
    And `routes/openai_compat.py` fills `finish_reason` from exactly this field, so every
    streamed composite run reported a default finish even after a refusal.

    `reflect` is the pattern that shows it: it finishes on its child's own `InvokeOutput`,
    where `plan_execute` and `parallel` synthesize a fresh one and always end `end_turn`.
    """
    from felix.manifests.schema import ReflectSpec
    from felix.patterns import delegating

    agent = delegating._DelegatingAgent(
        tools=[],
        pattern="reflect",
        manifest_id="r",
        manifest_version="1",
        model_spec=ModelSpec(id="manifest-model"),
        inner=_StepSpy(stop_reasons=["refusal"], plan_execute=False),
        reflect_cfg=ReflectSpec(max_iterations=1),
    )
    agent.settings = None

    events = (await _run(agent, streaming=True)).events

    done = [e for e in events if e.event == "done"]
    assert len(done) == 1, f"the child's terminal events leaked: {[e.event for e in events]}"
    assert done[0].data.get("stop_reason") == "refusal", done[0].data


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", STREAMING)
async def test_max_replans_zero_turns_replanning_off(streaming: bool) -> None:
    """The other way an operator switches it off, and the one with no behavioural test.

    `replan_on_failure: false` and `max_replans: 0` are documented as equivalent, so both
    need driving: a manifest that reaches for the ceiling rather than the boolean gets the
    same run, and a `<=` creeping into the bound would keep exactly one of them working.
    """
    import felix.patterns.delegating as delegating
    from felix.manifests.schema import PlanExecuteSpec

    model = _PlanSpy(["1. alpha\n2. beta", "1. gamma", "final answer"])
    steps = _StepSpy(stop_reasons=["refusal"])

    orig = delegating._model_for
    delegating._model_for = lambda *a, **k: model  # type: ignore[assignment]
    try:
        agent = _plan_agent(inner=steps, plan_cfg=PlanExecuteSpec(max_replans=0))
        await _run(agent, streaming=streaming)
    finally:
        delegating._model_for = orig  # type: ignore[assignment]

    assert steps.subtasks == ["alpha", "beta"], f"a ceiling of zero still replanned: {steps.subtasks}"


@pytest.mark.asyncio
async def test_a_replan_cannot_grow_the_run_past_max_subtasks() -> None:
    """`max_subtasks` is a promise about the whole run, not about each plan separately.

    `_plan_subtasks` already truncates what the planner returns, so the second slice in the replan
    only bites when steps are already done: three planned, one done, a three-line replan —
    without the bound that is four steps for a manifest that declared three.
    """
    import felix.patterns.delegating as delegating
    from felix.manifests.schema import PlanExecuteSpec

    model = _PlanSpy(["1. a\n2. b\n3. c", "1. x\n2. y\n3. z", "final answer"])
    # `a` completes, `b` is refused: the replan then has one step behind it.
    steps = _StepSpy(stop_reasons=["end_turn", "refusal"])

    orig = delegating._model_for
    delegating._model_for = lambda *a, **k: model  # type: ignore[assignment]
    try:
        agent = _plan_agent(inner=steps, plan_cfg=PlanExecuteSpec(max_subtasks=3, max_replans=1))
        await _run(agent, streaming=False)
    finally:
        delegating._model_for = orig  # type: ignore[assignment]

    assert steps.subtasks == ["a", "b", "x", "y"], steps.subtasks
    # `b` was attempted and replaced, so the *plan* is three long even though four subtasks
    # were handed to the executor. That is the ceiling the field names.
    assert len(steps.subtasks) - 1 == 3, f"the run grew past max_subtasks: {steps.subtasks}"


def test_a_spec_that_cannot_carry_an_id_is_left_alone_rather_than_crashing() -> None:
    """The plugin seam: core always supplies a real `ModelSpec`, and a plugin need not.

    Ignoring the route is the right failure — a composite built by a third party keeps
    running on whatever model it was given — but silently is not, so the warning is part of
    the contract. Without it the field is inert again for exactly the caller who cannot see
    why.
    """
    import logging

    for shape in (None, {"id": "a-dict-is-not-a-spec"}):
        assert _spec_with_model(shape, "cheap") is shape

    # The parent, not the module: `_spec_with_model` has already moved once (delegating ->
    # model), and a handler on the old module name catches nothing after a move like that
    # while still reading as an assertion about the warning.
    logger = logging.getLogger("felix.patterns")
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    logger.addHandler(handler)
    try:
        _spec_with_model({"id": "x"}, "cheap")
    finally:
        logger.removeHandler(handler)

    assert any("cheap" in r.getMessage() for r in records), (
        f"an ignored model route said nothing: {[r.getMessage() for r in records]}"
    )


# --- the whole chain, with nothing patched out ----------------------------------------
#
# Every test above reaches a model by replacing `_model_for` or `build_model`, which proves
# the pattern asked for a route and not that the route resolves. `register_scripted_provider`
# is the supported no-infrastructure path for the rest: `_spec_with_model` -> `build_model`
# -> `build_one_model` -> `parse_model_routes` -> a metered turn. "Which route planned" then
# becomes observable by the text that came back rather than by a captured argument.


@pytest.fixture
def two_routes():
    """A planner route and an answering route, each answering in its own words."""
    from felix_ai import registry
    from felix_ai.providers.scripted import ScriptedTurn, register_scripted_provider

    saved = dict(registry._providers)
    register_scripted_provider("planner-provider", [ScriptedTurn(content="1. from-the-planner")])
    register_scripted_provider("answer-provider", [ScriptedTurn(content="from-the-answerer")])
    try:
        yield
    finally:
        # Restore the whole dict the way `tests/e2e/conftest.py` does: leaving a fake
        # registered would let a later test route to it and pass on canned text.
        registry._providers.clear()
        registry._providers.update(saved)


def _routed_settings(**routes: dict[str, str]) -> Any:
    from felix.config import Settings

    return Settings(
        database_url="memory://test",
        auth_mode="none",
        model_routes=json.dumps(routes),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", STREAMING)
async def test_each_route_does_its_own_half_of_the_run(streaming: bool, two_routes: None) -> None:
    """Which client *performed* which turn, which is the half a spy cannot see.

    Every other test here records the specs `_model_for` was handed, and both clients are
    built at the top of `_run_plan_execute` before either is used — so that list is
    construction order, not use. Moving synthesis onto the planner client leaves it green
    while every answering turn silently bills the expensive route, which is this repo's
    named defect shape.

    Two providers with distinct canned text answer it by value instead: the plan must be in
    the planner's words and the answer in the answerer's. Both arms, because route selection
    drifting between `invoke` and the streamed path is the other thing this repo keeps
    finding.
    """
    from felix.manifests.schema import PlanExecuteSpec

    settings = _routed_settings(
        **{
            "manifest-model": {"provider": "answer-provider", "model": "a"},
            "strong-planner": {"provider": "planner-provider", "model": "p"},
        }
    )
    steps = _StepSpy()
    agent = _plan_agent(inner=steps, plan_cfg=PlanExecuteSpec(planner_model="strong-planner"))
    agent.settings = settings

    out = (await _run(agent, streaming=streaming)).output

    assert steps.subtasks == ["from-the-planner"], (
        f"the plan did not come from the planner route: {steps.subtasks}"
    )
    assert out.final.content == "from-the-answerer", (
        f"synthesis did not stay on the manifest's route: {out.final.content!r}"
    )


@pytest.mark.asyncio
async def test_a_planner_route_that_does_not_resolve_fails_the_run(two_routes: None) -> None:
    """Loudly, and that is the intended half.

    An unknown `planner_model` raising from `build_one_model` is the same failure a bad
    `spec.model.id` gives. The alternative — falling back to the manifest's model — is how
    this field becomes inert again: the run answers, nothing says the route was ignored, and
    the only symptom is a bill on the wrong model.
    """
    from felix.manifests.schema import PlanExecuteSpec

    settings = _routed_settings(**{"manifest-model": {"provider": "answer-provider", "model": "a"}})
    agent = _plan_agent(inner=_StepSpy(), plan_cfg=PlanExecuteSpec(planner_model="no-such-route"))
    agent.settings = settings

    with pytest.raises(ValueError, match="MODEL_ROUTES"):
        await _run(agent, streaming=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", STREAMING)
async def test_a_planner_with_nothing_to_say_leaves_the_plan_alone(streaming: bool) -> None:
    """A replan that comes back empty is not a reason to stop.

    The run falls through, records the step as it ended, and carries on with the plan it
    has — which is exactly what the pre-replan loop always did. Treating "no revised plan"
    as "no remaining work" would truncate the run on a planner hiccup and still synthesise
    an answer, which is the quiet half of this failure: the caller gets a reply built from
    one note and nothing says why.
    """
    import felix.patterns.delegating as delegating
    from felix.manifests.schema import PlanExecuteSpec

    # plan, then a replan that yields no subtasks, then the synthesis.
    model = _PlanSpy(["1. alpha\n2. beta", "", "final answer"])
    steps = _StepSpy(stop_reasons=["refusal"])

    orig = delegating._model_for
    delegating._model_for = lambda *a, **k: model  # type: ignore[assignment]
    try:
        agent = _plan_agent(inner=steps, plan_cfg=PlanExecuteSpec(max_replans=2))
        await _run(agent, streaming=streaming)
    finally:
        delegating._model_for = orig  # type: ignore[assignment]

    assert steps.subtasks == ["alpha", "beta"], f"an empty replan truncated the run: {steps.subtasks}"
