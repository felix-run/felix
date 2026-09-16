"""Which turn of a composite pattern carries the answer contract.

`spec.output_schema` is the shape every answer must have, and a composite reaches a model
several times per run — routing, planning, critiquing, scoring, synthesizing. Exactly one
of those produces what the caller receives. Putting the schema on all of them shapes a
planner's scratch output; putting it on none returns free text from a manifest that
declared a contract. Both are the same defect from opposite sides, and `build_agent`
refused the whole combination rather than pick wrong.

So these tests are about *placement*, not plumbing: for each pattern, the answering turn
must carry the schema and every other turn must not. A spy on `opts` is the only way to
see that — the returned text looks identical either way, which is precisely why this went
unnoticed long enough to need a compile-time refusal.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.patterns.types import Event, InvokeInput, InvokeOutput
from felix_ai.types import ChatMessage, ModelChatResult, StreamDelta, TokenUsage

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


class _OptionSpy:
    """Records the `opts` of every turn, in order, so placement is visible."""

    model_id = "spy"

    def __init__(self, text: str = "ok") -> None:
        self.text = text
        self.seen: list[Any] = []

    def _result(self) -> ModelChatResult:
        return ModelChatResult(
            message=ChatMessage(role="assistant", content=self.text),
            usage=TokenUsage(input=1, output=1),
        )

    async def chat(self, messages: list[ChatMessage], tools: list, opts: Any = None) -> ModelChatResult:
        self.seen.append(opts)
        return self._result()

    async def stream_turn(self, messages: list[ChatMessage], tools: list, opts: Any = None) -> Any:
        self.seen.append(opts)
        yield StreamDelta(kind="text", text=self.text)
        yield self._result()

    @property
    def schemas(self) -> list[Any]:
        """The `output_schema` of each turn, `None` where none was passed."""
        return [getattr(o, "output_schema", None) for o in self.seen]


class _ChildSpy:
    """A sub-agent that records the `model_options` on the input it was handed."""

    def __init__(self, text: str = "child") -> None:
        self.text = text
        self.seen: list[Any] = []

    async def invoke(self, input: InvokeInput) -> InvokeOutput:
        self.seen.append(input.model_options)
        msg = ChatMessage(role="assistant", content=self.text)
        return InvokeOutput(messages=[*input.messages, msg], final=msg)

    async def stream_events(self, input: InvokeInput) -> Any:
        self.seen.append(input.model_options)
        msg = ChatMessage(role="assistant", content=self.text)
        out = InvokeOutput(messages=[*input.messages, msg], final=msg)
        yield Event(event="on_chain_end", data={"output": out})
        yield Event(event="done", data={"final": msg.model_dump()})

    @property
    def schemas(self) -> list[Any]:
        return [getattr(o, "output_schema", None) for o in self.seen]


def _agent(pattern: str, **kw: Any) -> Any:
    from felix.patterns import delegating

    return delegating._DelegatingAgent(
        tools=[],
        pattern=pattern,
        manifest_id="composite",
        manifest_version="1",
        output_schema=SCHEMA,
        **kw,
    )


async def _run(agent: Any, *, streaming: bool) -> None:
    input = InvokeInput(messages=[ChatMessage(role="user", content="hi")])
    if streaming:
        async for _ in agent.stream_events(input):
            pass
    else:
        await agent.invoke(input)


# Both arms, because `_generate` reaches the model through two different calls and the
# streaming half is where this repo has repeatedly found drift.
STREAMING = [pytest.param(False, id="invoke"), pytest.param(True, id="stream")]


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", STREAMING)
async def test_parallel_shapes_the_synthesis_and_not_the_specialists(streaming: bool) -> None:
    """The specialists' answers are raw material for the synthesis prompt.

    Shaping them would hand the aggregator JSON fragments where it asked for prose, and
    the reply the caller sees would still be whatever the aggregator felt like emitting.
    """
    model = _OptionSpy("synthesis")
    children = {"a": _ChildSpy(), "b": _ChildSpy()}
    agent = _agent("parallel", sub_agents=children, model_spec=None)
    agent.settings = None
    import felix.patterns.delegating as delegating

    orig = delegating._model_for
    delegating._model_for = lambda *a, **k: model  # type: ignore[assignment]
    try:
        await _run(agent, streaming=streaming)
    finally:
        delegating._model_for = orig  # type: ignore[assignment]

    assert model.schemas == [SCHEMA], "the synthesis turn did not carry the contract"
    for name, child in children.items():
        assert child.schemas == [None], f"specialist {name} was shaped"


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", STREAMING)
async def test_plan_execute_shapes_the_synthesis_and_not_the_plan(streaming: bool) -> None:
    """A plan shaped like the answer schema is not a plan.

    Two model turns go through the spy — planning first, synthesis last — so their order
    in `schemas` is what distinguishes "the contract landed on the right one" from "the
    contract landed on a turn".
    """
    from felix.manifests.schema import PlanExecuteSpec

    model = _OptionSpy("text")
    executor = _ChildSpy("step done")
    agent = _agent("plan_execute", inner=executor, plan_cfg=PlanExecuteSpec())
    agent.settings = None

    import felix.patterns.delegating as delegating

    orig = delegating._model_for
    delegating._model_for = lambda *a, **k: model  # type: ignore[assignment]
    try:
        await _run(agent, streaming=streaming)
    finally:
        delegating._model_for = orig  # type: ignore[assignment]

    assert len(model.schemas) >= 2, f"expected a planning turn and a synthesis: {model.schemas}"
    assert model.schemas[0] is None, "the planning turn was shaped by the answer contract"
    assert model.schemas[-1] == SCHEMA, "the synthesis turn did not carry the contract"
    assert executor.schemas == [None] * len(executor.schemas), "an executor step was shaped"


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", STREAMING)
async def test_router_shapes_the_child_and_not_the_classifier(streaming: bool) -> None:
    """The router's own turn picks a name; the child writes the answer.

    A router that replied with its classifier's JSON would satisfy the schema and answer
    nothing, which is the failure worth naming.
    """
    model = _OptionSpy("a")
    child = _ChildSpy("routed answer")
    agent = _agent("router", sub_agents={"a": child})
    agent.settings = None

    import felix.patterns.delegating as delegating

    orig = delegating._model_for
    delegating._model_for = lambda *a, **k: model  # type: ignore[assignment]
    try:
        await _run(agent, streaming=streaming)
    finally:
        delegating._model_for = orig  # type: ignore[assignment]

    assert model.schemas == [None], "the routing classifier was shaped by the answer contract"
    assert child.schemas == [SCHEMA], "the child that answers did not receive the contract"


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", STREAMING)
async def test_reflect_shapes_every_draft_because_any_can_be_the_answer(streaming: bool) -> None:
    """The loop exits as soon as a draft clears the threshold.

    So "the last iteration" is not knowable in advance, and a contract applied only to a
    final turn would miss every early exit — which is the common case.
    """
    from felix.manifests.schema import ReflectSpec

    inner = _ChildSpy("draft")
    agent = _agent("reflect", inner=inner, reflect_cfg=ReflectSpec(max_iterations=2, threshold=0.0))
    agent.settings = None

    await _run(agent, streaming=streaming)

    assert inner.schemas, "the inner agent was never reached"
    assert all(s == SCHEMA for s in inner.schemas), f"a draft went unshaped: {inner.schemas}"


@pytest.mark.asyncio
async def test_a_caller_s_own_schema_wins_over_the_manifest_s() -> None:
    """`/v1` `response_format` is a per-request override, and react already works this way.

    Before this change `_child_input` dropped `model_options` outright, so a caller's
    schema could not reach a child at all.
    """
    from felix_ai.types import ModelChatOptions

    caller: dict[str, Any] = {"type": "object", "properties": {"n": {"type": "number"}}}
    child = _ChildSpy()
    agent = _agent("router", sub_agents={"a": child})
    agent.settings = None

    import felix.patterns.delegating as delegating

    model = _OptionSpy("a")
    orig = delegating._model_for
    delegating._model_for = lambda *a, **k: model  # type: ignore[assignment]
    try:
        await agent.invoke(
            InvokeInput(
                messages=[ChatMessage(role="user", content="hi")],
                model_options=ModelChatOptions(output_schema=caller),
            )
        )
    finally:
        delegating._model_for = orig  # type: ignore[assignment]

    assert child.schemas == [SCHEMA], "a request overrode the manifest's published contract"


@pytest.mark.asyncio
async def test_a_composite_without_a_schema_passes_no_options_at_all() -> None:
    """The overwhelmingly common case must not start sending an empty options object.

    A `ModelChatOptions()` with every field defaulted is not the same as `None` to a wire
    format that branches on its presence.
    """
    from felix.patterns import delegating

    model = _OptionSpy("synthesis")
    child = _ChildSpy()
    agent = delegating._DelegatingAgent(
        tools=[],
        pattern="parallel",
        manifest_id="composite",
        manifest_version="1",
        sub_agents={"a": child},
    )
    agent.settings = None

    orig = delegating._model_for
    delegating._model_for = lambda *a, **k: model  # type: ignore[assignment]
    try:
        await _run(agent, streaming=False)
    finally:
        delegating._model_for = orig  # type: ignore[assignment]

    assert model.seen == [None], f"an unschema'd composite sent options: {model.seen}"
    assert child.schemas == [None]


# --- through the registered builders, which is what decides placement ----------------
#
# Everything above constructs `_DelegatingAgent` directly and hands it a `_ChildSpy`. That
# cannot see the thing that actually shapes an inner agent: `patterns/__init__.py` builds
# the real react executor from the shared build context, and `build_react_agent` reads
# `output_schema` straight off it. So `plan_execute`'s executor was schema'd by ctx while a
# test asserting `executor.schemas == [None]` passed — the assertion was structurally
# incapable of failing, which is this repo's named defect shape: exercise the production
# call, not a convenient one.


def _ctx(**kw: Any) -> dict[str, Any]:
    from felix.config import Settings

    base: dict[str, Any] = {
        "manifest_id": "composite",
        "manifest_version": "1",
        "system_prompt": "",
        "tools": [],
        "settings": Settings(database_url="memory://composite", object_store="memory"),
        "output_schema": SCHEMA,
    }
    base.update(kw)
    return base


def _inner_schema(agent: Any) -> Any:
    """The schema the built inner react agent will apply to its own turns."""
    return getattr(agent, "output_schema", None)


@pytest.mark.asyncio
async def test_the_plan_execute_executor_is_built_without_the_answer_contract() -> None:
    """Subtask answers become `notes` for the synthesis prompt, so they must stay prose.

    `_build_plan_execute` passes the build context to `build_react_agent`, and that context
    carries the manifest's `output_schema`. Passed through unchanged, every executor step
    returned a JSON envelope and `notes` became a list of `1. <step> → {"answer": …}` — the
    exact failure `_child_input`'s `options=None` default was written to prevent, arriving
    by a route that default cannot reach.
    """
    from felix.patterns import _build_plan_execute

    agent = await _build_plan_execute(_ctx())
    assert _inner_schema(agent.inner) is None, "the executor inherited the answer contract from ctx"
    assert agent.output_schema == SCHEMA, "the composite lost the contract for its synthesis"


@pytest.mark.asyncio
async def test_the_reflect_inner_agent_is_built_with_the_answer_contract() -> None:
    """The mirror decision, made deliberately rather than by accident.

    Reflect's drafts *are* the answer and the loop exits as soon as one clears the
    threshold, so each draft has to satisfy the contract — the opposite of plan_execute's
    executor, from the same ctx.
    """
    from felix.patterns import _build_reflect

    agent = await _build_reflect(_ctx(manifest=None))
    assert _inner_schema(agent.inner) == SCHEMA, "a reflect draft would have gone unshaped"


@pytest.mark.asyncio
async def test_the_manifest_contract_outranks_a_caller_s_on_every_pattern() -> None:
    """`react` resolves `self.output_schema or opts.output_schema`, and the composites must
    agree: an agent published with an answer contract keeps answering to it rather than to
    whichever shape the last request preferred.

    This was inverted, and inconsistently — `reflect` took the manifest's (through its inner
    agent) while `router`, `parallel` and `plan_execute` took the caller's, so the same
    field was a governance control on three patterns and a default on three others.
    """
    from felix.patterns import _build_parallel, _build_plan_execute, _build_router
    from felix_ai.types import ModelChatOptions

    caller: dict[str, Any] = {"type": "object", "properties": {"n": {"type": "number"}}}
    request = InvokeInput(
        messages=[ChatMessage(role="user", content="hi")],
        model_options=ModelChatOptions(output_schema=caller),
    )

    for build in (_build_router, _build_parallel, _build_plan_execute):
        agent = await build(_ctx(manifest=None))
        resolved = agent._answer_options(request)
        assert resolved is not None
        assert resolved.output_schema == SCHEMA, f"{build.__name__}: the caller overrode the manifest"


@pytest.mark.asyncio
async def test_a_caller_s_contract_is_used_when_the_manifest_declares_none() -> None:
    """The other half, and the reason `_child_input` stopped dropping `model_options`:
    a `/v1` `response_format` could not reach a composite's answering turn at all before."""
    from felix.patterns import _build_parallel
    from felix_ai.types import ModelChatOptions

    caller: dict[str, Any] = {"type": "object", "properties": {"n": {"type": "number"}}}
    agent = await _build_parallel(_ctx(manifest=None, output_schema=None))
    resolved = agent._answer_options(
        InvokeInput(
            messages=[ChatMessage(role="user", content="hi")],
            model_options=ModelChatOptions(output_schema=caller),
        )
    )
    assert resolved is not None and resolved.output_schema == caller


@pytest.mark.asyncio
async def test_caller_options_alone_do_not_reach_a_synthesis_turn() -> None:
    """A composite with no contract must behave exactly as it did, and this is not pedantry.

    `_DelegatingAgent` has no `limits`, so unlike `react` it cannot clamp `max_tokens` to
    `limits.max_output_tokens`. Forwarding a request's options to the synthesis turn would
    let `max_tokens: 200000` size the turn that composes the answer on a manifest capping
    output at 2000 — react's clamp exists to close exactly that, and there is no equivalent
    here to inherit.
    """
    from felix.patterns import _build_parallel
    from felix_ai.types import ModelChatOptions

    agent = await _build_parallel(_ctx(manifest=None, output_schema=None))
    resolved = agent._answer_options(
        InvokeInput(
            messages=[ChatMessage(role="user", content="hi")],
            model_options=ModelChatOptions(max_tokens=200_000),
        )
    )
    assert resolved is None, "an unschema'd composite forwarded caller options unclamped"
