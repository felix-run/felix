"""`before_model` / `after_model` on the calls a composite pattern makes itself.

The children of a composite are react agents and run the hooks on their own turns; these are
the calls in between — the router's classifier, reflect's verifier, the plan_execute planner,
and the synthesis that composes a `parallel` or `plan_execute` answer. Each test changes a
call through a hook and then asserts the *pattern* acted on the change, not merely that the
hook ran: the router went to the other child, the executor ran the replaced plan.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.config import Settings
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.hooks import get_agent_hooks
from felix.patterns.model import ModelChatResult, StreamDelta, TokenUsage
from felix.patterns.types import ChatMessage, Event, InvokeInput, InvokeOutput

THREAD = "default:composite-hooks"


def _ctx() -> RequestContext:
    settings = Settings(
        database_url="memory://composite-hooks",
        object_store="memory",
        allow_insecure=True,
        environment="development",
    )
    return RequestContext(settings=settings, auth=AuthContext(tenant_id="default"))


class _Model:
    """Answers each call with the next of `replies`; records what every call was sent."""

    model_id = "composite-fake"

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.seen: list[list[ChatMessage]] = []

    def _next(self, messages: list[ChatMessage]) -> ModelChatResult:
        self.seen.append(list(messages))
        text = self.replies[min(len(self.seen), len(self.replies)) - 1]
        return ModelChatResult(message=ChatMessage(role="assistant", content=text), usage=TokenUsage())

    async def chat(self, messages: list[ChatMessage], tools: list, opts: Any = None) -> ModelChatResult:
        return self._next(messages)


class _StreamingModel(_Model):
    async def stream_turn(self, messages: list[ChatMessage], tools: list, opts: Any = None) -> Any:
        result = self._next(messages)
        yield StreamDelta(kind="text", text=result.message.content)
        yield result


class _Child:
    """A sub-agent that records the last user message of every turn it is given."""

    pattern = "react"
    manifest_id = "child"
    manifest_version = "1"

    def __init__(self, text: str) -> None:
        self.text = text
        self.asked: list[str] = []

    def _out(self, input: InvokeInput) -> InvokeOutput:
        self.asked.append(str(input.messages[-1].content))
        msg = ChatMessage(role="assistant", content=self.text)
        return InvokeOutput(messages=[*input.messages, msg], final=msg)

    async def invoke(self, input: InvokeInput) -> InvokeOutput:
        return self._out(input)

    async def stream_events(self, input: InvokeInput) -> Any:
        out = self._out(input)
        yield Event(event="on_chain_end", data={"output": out})
        yield Event(event="done", data={"final": out.final.model_dump()})


def _agent(pattern: str, **kw: Any) -> Any:
    from felix.patterns import delegating

    return delegating._DelegatingAgent(
        tools=[], pattern=pattern, manifest_id="composite", manifest_version="1", **kw
    )


async def _run(agent: Any, *, streaming: bool = False) -> InvokeOutput | None:
    input = InvokeInput(messages=[ChatMessage(role="user", content="the request")], thread_id=THREAD)
    async with async_run_with_context(_ctx()):
        if not streaming:
            return await agent.invoke(input)
        out = None
        async for ev in agent.stream_events(input):
            if ev.event == "on_chain_end":
                out = ev.data.get("output")
        return out


def _on(purpose: str, reply: str):
    """An after_model hook that replaces the reply of every `purpose` call with `reply`."""

    def hook(response: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any] | None:
        if ctx["purpose"] != purpose:
            return None
        return {"message": ChatMessage(role="assistant", content=reply)}

    return hook


async def test_the_router_follows_a_replaced_classification(monkeypatch: pytest.MonkeyPatch) -> None:
    from felix.patterns import delegating

    model = _Model("billing")
    monkeypatch.setattr(delegating, "_model_for", lambda *a, **k: model)
    billing, support = _Child("from billing"), _Child("from support")
    get_agent_hooks().register_after_model(_on("router", "support"))

    out = await _run(_agent("router", sub_agents={"billing": billing, "support": support}))

    assert out is not None and out.final.content == "from support"
    assert (billing.asked, support.asked) == ([], ["the request"])


async def test_plan_execute_runs_the_replaced_plan_and_the_planner_is_sent_the_hooked_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from felix.patterns import delegating

    model = _Model("1. the model's step", "the answer")
    monkeypatch.setattr(delegating, "_model_for", lambda *a, **k: model)
    executor = _Child("step done")

    def mark_the_plan(request: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any] | None:
        if ctx["purpose"] != "plan":
            return None
        return {"messages": [*request["messages"], ChatMessage(role="user", content="planner marker")]}

    hooks = get_agent_hooks()
    hooks.register_before_model(mark_the_plan)
    hooks.register_after_model(_on("plan", "1. alpha\n2. beta"))

    await _run(_agent("plan_execute", inner=executor))

    assert model.seen[0][-1].content == "planner marker"
    assert all(m.content != "planner marker" for m in model.seen[1]), (
        "the plan's marker reached the synthesis"
    )
    assert [a.splitlines()[0] for a in executor.asked] == ["Subtask 1/2: alpha", "Subtask 2/2: beta"]


async def test_reflect_reads_the_replaced_score(monkeypatch: pytest.MonkeyPatch) -> None:
    """0.1 from the verifier would ask for a second draft; the hook's 0.95 clears the bar."""
    from felix.manifests.schema import ReflectSpec
    from felix.patterns import delegating

    verifier = _Model("0.1")
    monkeypatch.setattr(delegating, "build_model", lambda *a, **k: verifier)
    base = _Child("draft")
    get_agent_hooks().register_after_model(_on("reflect", "0.95"))

    await _run(_agent("reflect", inner=base, reflect_cfg=ReflectSpec(max_iterations=3)))

    assert len(verifier.seen) == 1
    assert len(base.asked) == 1, "reflect drafted again, so it scored the verifier's 0.1"


@pytest.mark.parametrize(
    ("model_cls", "streaming"),
    [(_Model, False), (_StreamingModel, True), (_Model, True)],
    ids=["invoke", "stream_turn", "chat-only-stream"],
)
async def test_the_synthesis_answer_is_the_hooks(
    monkeypatch: pytest.MonkeyPatch, model_cls: type[_Model], streaming: bool
) -> None:
    from felix.patterns import delegating

    model = model_cls("the model's synthesis")
    monkeypatch.setattr(delegating, "_model_for", lambda *a, **k: model)
    hooks = get_agent_hooks()
    hooks.register_before_model(
        lambda request, ctx: (
            {"messages": [ChatMessage(role="user", content="synthesis marker")]}
            if ctx["purpose"] == "synthesis"
            else None
        )
    )
    hooks.register_after_model(_on("synthesis", "the hook's synthesis"))

    out = await _run(_agent("parallel", sub_agents={"a": _Child("one")}), streaming=streaming)

    assert [m.content for m in model.seen[0]] == ["synthesis marker"]
    assert out is not None and out.final.content == "the hook's synthesis"


async def test_each_call_names_its_purpose_and_the_callers_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    from felix.patterns import delegating

    model = _Model("1. only step", "the answer")
    monkeypatch.setattr(delegating, "_model_for", lambda *a, **k: model)
    seen: list[tuple[str, str | None, str | None]] = []
    get_agent_hooks().register_before_model(
        lambda request, ctx: seen.append((ctx["purpose"], ctx["thread_id"], ctx["model_id"]))
    )

    await _run(_agent("plan_execute", inner=_Child("done")))

    assert seen == [("plan", THREAD, "composite-fake"), ("synthesis", THREAD, "composite-fake")]
