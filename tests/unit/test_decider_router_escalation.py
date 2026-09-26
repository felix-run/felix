"""`spec.decider` as the router's classifier and as confidence escalation's judge.

Both sites had a decision made in prose — "reply with only the agent name", and "is this
reply shorter than 40 characters or does it say *unclear*" — and both keep that as the
fallback. What these pin is when the decider's answer is taken, when it is not, and that the
production wiring (`build_model(..., decider=)`, `_build_router`) reaches it.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.patterns.model_composites import _EscalationClient
from felix_ai.decide import ChoiceAnswer, DecisionResult, NoulAnswer
from felix_ai.types import ChatMessage, ModelChatResult, ModelRoute, TokenUsage
from pydantic import ValidationError

ROUTE = ModelRoute(provider="anthropic", model="m")


class _Model:
    def __init__(self, name: str, text: str) -> None:
        self.model_id = name
        self.route = ROUTE
        self._text = text
        self.calls = 0

    async def chat(self, messages: Any, tools: Any, opts: Any = None) -> ModelChatResult:
        self.calls += 1
        return ModelChatResult(
            message=ChatMessage(role="assistant", content=self._text), usage=TokenUsage(input=1, output=1)
        )


class _Decider:
    model_id = "double"
    wire_model = "jev-latest"

    def __init__(self, answer: Any = None, *, fail: bool = False, min_confidence: float = 0.5) -> None:
        self.answer = answer
        self.fail = fail
        self.min_confidence = min_confidence
        self.calls: list[tuple[Any, dict[str, Any], str]] = []

    async def decide(self, state: Any, questions: dict[str, Any], *, purpose: str = "") -> DecisionResult:
        self.calls.append((state, questions, purpose))
        if self.fail:
            raise RuntimeError("down")
        return DecisionResult(answers={key: self.answer for key in questions})


def _escalating(primary: _Model, target: _Model, decider: Any) -> _EscalationClient:
    return _EscalationClient(
        primary=primary,
        escalate_to=target,
        markers=["not sure"],
        min_response_chars=40,
        model_id=primary.model_id,
        route=ROUTE,
        decider=decider,
    )


ASK = [ChatMessage(role="user", content="What is 2+2?")]


# --- escalation -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_short_right_answer_is_kept_when_the_decider_says_it_answers() -> None:
    """The heuristic escalates "4" for being under 40 characters; the decider is asked."""
    primary, target = _Model("weak", "4"), _Model("strong", "Four.")
    decider = _Decider(NoulAnswer(0.95))
    result = await _escalating(primary, target, decider).chat(ASK, [])
    assert result.message.content == "4"
    assert target.calls == 0
    state, _q, purpose = decider.calls[0]
    assert state == {"request": "What is 2+2?", "reply": "4"} and purpose == "escalation"


@pytest.mark.asyncio
async def test_a_fluent_non_answer_escalates_when_the_decider_says_so() -> None:
    fluent = "That is a fascinating question about arithmetic that people have long debated."
    primary, target = _Model("weak", fluent), _Model("strong", "4")
    result = await _escalating(primary, target, _Decider(NoulAnswer(0.1))).chat(ASK, [])
    assert result.message.content == "4"
    assert target.calls == 1


@pytest.mark.asyncio
async def test_the_threshold_is_the_manifests_min_confidence() -> None:
    primary, target = _Model("weak", "4"), _Model("strong", "Four.")
    strict = _Decider(NoulAnswer(0.7), min_confidence=0.8)
    assert (await _escalating(primary, target, strict).chat(ASK, [])).message.content == "Four."


@pytest.mark.asyncio
async def test_a_decider_error_falls_back_to_the_heuristic_not_to_never() -> None:
    primary, target = _Model("weak", "4"), _Model("strong", "Four.")
    result = await _escalating(primary, target, _Decider(fail=True)).chat(ASK, [])
    assert result.message.content == "Four.", "a 1-character reply still escalates by the heuristic"


@pytest.mark.asyncio
async def test_no_request_text_means_the_heuristic_decides() -> None:
    decider = _Decider(NoulAnswer(0.95))
    primary, target = _Model("weak", "4"), _Model("strong", "Four.")
    await _escalating(primary, target, decider).chat([ChatMessage(role="user", content="")], [])
    assert decider.calls == []
    assert target.calls == 1


def test_build_model_hands_escalation_the_decider_only_when_asked() -> None:
    from felix.config import Settings
    from felix.manifests.schema import ModelSpec
    from felix.patterns.model import build_model

    settings = Settings(database_url="memory://esc", object_store="memory")
    decider = _Decider(NoulAnswer(1.0))
    esc = {"enabled": True, "escalate_to": "claude-opus"}
    opted = build_model(
        settings,
        ModelSpec(id="claude-haiku", confidence_escalation={**esc, "decider": True}),
        decider=decider,
    )
    plain = build_model(settings, ModelSpec(id="claude-haiku", confidence_escalation=esc), decider=decider)
    assert isinstance(opted, _EscalationClient) and opted.decider is decider
    assert isinstance(plain, _EscalationClient) and plain.decider is None


# --- router -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_confident_decider_picks_the_child_without_a_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from felix.patterns import delegating
    from felix.patterns.types import InvokeInput

    def _no_model(*a: Any, **kw: Any) -> Any:
        raise AssertionError("the classifier model must not be called")

    monkeypatch.setattr(delegating, "_model_for", _no_model)
    decider = _Decider(ChoiceAnswer("b", {"a": 0.1, "b": 0.9}, confidence=0.9))
    router = await _build_router_async(decider)
    child = await router._choose_child(
        InvokeInput(messages=[ChatMessage(role="user", content="what is DNA?")])
    )
    assert child.name == "b"
    _state, questions, purpose = decider.calls[0]
    assert purpose == "router"
    assert list(questions["route"].criteria) == ["a", "b"]
    assert "a: arithmetic. b: biology." in questions["route"].instructions


@pytest.mark.parametrize(
    "decider",
    [
        _Decider(ChoiceAnswer("b", {"a": 0.45, "b": 0.55}, confidence=0.2)),
        _Decider(fail=True),
    ],
    ids=["unsure", "error"],
)
@pytest.mark.asyncio
async def test_an_unsure_or_failed_decider_leaves_the_choice_to_the_classifier(
    decider: _Decider, monkeypatch: pytest.MonkeyPatch
) -> None:
    from felix.patterns import delegating
    from felix.patterns.types import InvokeInput

    classifier = _Model("classifier", "a")
    monkeypatch.setattr(delegating, "_model_for", lambda *a, **kw: classifier)
    monkeypatch.setattr(delegating, "record_model_usage", lambda *a, **kw: {})
    router = await _build_router_async(decider)
    child = await router._choose_child(InvokeInput(messages=[ChatMessage(role="user", content="2+2?")]))
    assert child.name == "a"
    assert classifier.calls == 1


@pytest.mark.asyncio
async def test_a_classifier_that_names_no_child_is_counted_not_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from felix.patterns import delegating
    from felix.patterns.types import InvokeInput

    counted: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        delegating, "record_counter", lambda name, labels: counted.append((name, dict(labels)))
    )
    monkeypatch.setattr(delegating, "_model_for", lambda *a, **kw: _Model("classifier", "zebra"))
    monkeypatch.setattr(delegating, "record_model_usage", lambda *a, **kw: {})
    router = await _build_router_async(None)
    child = await router._choose_child(InvokeInput(messages=[ChatMessage(role="user", content="?")]))
    assert child.name == "a", "a router still has to send the request somewhere"
    assert counted == [("felix_router_choice", {"manifest_id": "r", "method": "unmatched"})]


async def _build_router_async(decider: Any) -> Any:
    from felix.patterns import _build_router

    class _Child:
        def __init__(self, name: str) -> None:
            self.name = name

    return await _build_router(
        {
            "manifest_id": "r",
            "sub_agents": {"a": _Child("a"), "b": _Child("b")},
            "system_prompt": "a: arithmetic. b: biology.",
            "decider": decider,
        }
    )


# --- schema -----------------------------------------------------------------------------


def test_escalation_by_decider_needs_a_decider_and_an_escalation() -> None:
    from felix.manifests.schema import Spec

    esc = {"enabled": True, "escalate_to": "claude-opus", "decider": True}
    with pytest.raises(ValidationError, match=r"spec\.decider\.id"):
        Spec.model_validate({"model": {"confidence_escalation": esc}})
    with pytest.raises(ValidationError, match="escalate_to"):
        Spec.model_validate({"model": {"confidence_escalation": {"decider": True}}, "decider": {"id": "jev"}})
    Spec.model_validate({"model": {"confidence_escalation": esc}, "decider": {"id": "jev"}})


@pytest.mark.parametrize(
    ("decider", "expected"),
    [
        # The `llm` backend names a pick with no confidence: taken, not treated as unsure.
        (_Decider(ChoiceAnswer("b", {"b": 1.0}, confidence=None)), "b"),
        # The manifest's threshold, not the default: 0.7 clears 0.5 and misses 0.8.
        (_Decider(ChoiceAnswer("b", {"a": 0.3, "b": 0.7}, confidence=0.7), min_confidence=0.8), "a"),
        # A pick that is not a child falls back to the classifier rather than KeyError-ing.
        (_Decider(ChoiceAnswer("zebra", {"zebra": 1.0}, confidence=1.0)), "a"),
    ],
    ids=["no-confidence", "manifest-threshold", "not-a-child"],
)
@pytest.mark.asyncio
async def test_when_the_routers_decider_is_taken(
    decider: _Decider, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from felix.patterns import delegating
    from felix.patterns.types import InvokeInput

    classifier = _Model("classifier", "a")
    monkeypatch.setattr(delegating, "_model_for", lambda *a, **kw: classifier)
    monkeypatch.setattr(delegating, "record_model_usage", lambda *a, **kw: {})
    router = await _build_router_async(decider)
    child = await router._choose_child(
        InvokeInput(messages=[ChatMessage(role="user", content="what is DNA?")])
    )
    assert child.name == expected
    assert classifier.calls == (0 if expected == "b" else 1)


@pytest.mark.asyncio
async def test_a_tool_step_does_not_ask_the_decider() -> None:
    from felix_ai.types import ToolCall

    class _Tooling(_Model):
        async def chat(self, messages: Any, tools: Any, opts: Any = None) -> ModelChatResult:
            self.calls += 1
            call = ToolCall(id="c", name="calculator", args={})
            return ModelChatResult(message=ChatMessage(role="assistant", content="", tool_calls=[call]))

    decider = _Decider(NoulAnswer(0.0))
    await _escalating(_Tooling("weak", ""), _Model("strong", "x"), decider).chat(ASK, [])
    assert decider.calls == []


@pytest.mark.asyncio
async def test_a_side_request_is_judged_by_the_heuristic_not_the_decider() -> None:
    """A compaction summary is not an answer to its "request" (the transcript), so asking
    would escalate every compaction to the expensive model and ship the transcript out."""
    from felix_ai.types import ModelChatOptions

    long_summary = "The user asked about arithmetic and was told the answer was four, twice."
    decider = _Decider(NoulAnswer(0.0))
    primary, target = _Model("weak", long_summary), _Model("strong", "x")
    result = await _escalating(primary, target, decider).chat(ASK, [], ModelChatOptions(isolate_cache=True))
    assert decider.calls == []
    assert result.message.content == long_summary, "the heuristic keeps a long summary"


def test_escalation_by_decider_is_refused_where_it_would_not_run() -> None:
    from felix.manifests.schema import Spec

    esc = {"enabled": True, "escalate_to": "claude-opus", "decider": True}
    with pytest.raises(ValidationError, match="reflect"):
        Spec.model_validate(
            {"pattern": "reflect", "model": {"confidence_escalation": esc}, "decider": {"id": "jev"}}
        )
    Spec.model_validate(
        {"pattern": "deep", "model": {"confidence_escalation": esc}, "decider": {"id": "jev"}}
    )
