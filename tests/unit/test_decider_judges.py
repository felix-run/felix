"""`spec.decider` as a judge: guardrail judges, the reflect verifier, eval rubrics.

All three asked a chat model for a bare number or `{"score": ...}` and parsed it, and the model
judge was never metered. These pin when the decider's probability is the score, that each site
keeps its old path as the fallback, and that the model judge now counts against the budget.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.manifests.schema import JudgeRule
from felix_ai.decide import DecisionResult, NoulAnswer
from pydantic import ValidationError


class _Decider:
    model_id = "double"
    wire_model = "jev-latest"
    min_confidence = 0.5

    def __init__(self, p: float = 0.9, *, fail: bool = False) -> None:
        self.p = p
        self.fail = fail
        self.calls: list[tuple[Any, dict[str, Any], str]] = []

    async def decide(self, state: Any, questions: dict[str, Any], *, purpose: str = "") -> DecisionResult:
        self.calls.append((state, questions, purpose))
        if self.fail:
            raise RuntimeError("down")
        return DecisionResult(answers={k: NoulAnswer(self.p) for k in questions})


NEGATIVE = "must not leak credentials or secrets"


# --- guardrail judges -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_decider_judge_scores_by_probability_and_reads_a_negative_criterion() -> None:
    """The heuristic fails a negative criterion closed (0.0) because bag-of-words inverts it;
    the decider reads it as stated."""
    from felix.governance.judges import heuristic_judge_score, judge_score

    decider = _Decider(0.93)
    judge = JudgeRule(name="safe", criteria=NEGATIVE, decider=True)
    assert heuristic_judge_score("the weather is fine", NEGATIVE) == 0.0
    assert await judge_score("the weather is fine", judge, decider=decider) == pytest.approx(0.93)
    state, questions, purpose = decider.calls[0]
    assert state == {"text": "the weather is fine"} and purpose == "judge"
    assert NEGATIVE in questions["meets"].instructions


@pytest.mark.asyncio
async def test_a_judge_that_did_not_opt_in_never_reaches_the_decider() -> None:
    from felix.governance.judges import judge_score

    decider = _Decider(0.0)
    await judge_score("hello there", JudgeRule(name="j", criteria="nonempty"), decider=decider)
    assert decider.calls == []


@pytest.mark.asyncio
async def test_a_decider_outage_falls_back_to_the_judges_old_path() -> None:
    from felix.governance.judges import heuristic_judge_score, judge_score

    judge = JudgeRule(name="j", criteria="min_length:5", decider=True)
    score = await judge_score("hello world", judge, decider=_Decider(fail=True))
    assert score == heuristic_judge_score("hello world", "min_length:5")


# --- the model judge is metered ---------------------------------------------------------


@pytest.mark.asyncio
async def test_the_model_judge_counts_against_the_run() -> None:
    from felix.config import Settings
    from felix.context import AuthContext, RequestContext, async_run_with_context
    from felix.eval.compare import llm_judge_score
    from felix_ai.types import ChatMessage, ModelChatResult, ModelRoute, TokenUsage

    class _Model:
        model_id = "claude-haiku"
        route = ModelRoute(provider="anthropic", model="claude-haiku-4-5")

        async def chat(self, *a: Any, **kw: Any) -> ModelChatResult:
            return ModelChatResult(
                message=ChatMessage(role="assistant", content='{"score": 0.8}'),
                usage=TokenUsage(input=1_000, output=10),
            )

    settings = Settings(database_url="memory://judge-meter", object_store="memory")
    ctx = RequestContext(settings=settings, auth=AuthContext(), manifest_id="m")
    async with async_run_with_context(ctx):
        await llm_judge_score(_Model(), user_input="q", answer="a", criteria="relevant")
    assert ctx.limit_state.tokens_input == 1_000
    assert ctx.limit_state.cost_usd > 0.0


# --- reflect ----------------------------------------------------------------------------


def _reflector(decider: Any, *, opted: bool = True) -> Any:
    from felix.manifests.schema import ReflectSpec
    from felix.patterns.delegating import _DelegatingAgent

    return _DelegatingAgent(
        tools=[],
        pattern="reflect",
        manifest_id="r",
        manifest_version="1",
        reflect_cfg=ReflectSpec(decider=opted, criteria="cites a source"),
        decider=decider,
    )


@pytest.mark.asyncio
async def test_reflect_verifies_with_the_decider_when_asked() -> None:
    decider = _Decider(0.2)
    score = await _reflector(decider)._score("an answer", "cites a source", "", request="why?")
    assert score == pytest.approx(0.2)
    state, _q, purpose = decider.calls[0]
    assert state == {"text": "an answer", "request": "why?"} and purpose == "reflect"


@pytest.mark.asyncio
async def test_reflect_falls_back_to_the_verifier_path_without_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unopted, or the decider down: the old path — here the verifier model is unreachable,
    so the heuristic answers, which is what reflect already did in that case."""
    from felix.governance.judges import heuristic_judge_score
    from felix.patterns import delegating

    def _unreachable(*a: Any, **kw: Any) -> Any:
        raise RuntimeError("no verifier")

    monkeypatch.setattr(delegating, "build_model", _unreachable)
    expected = heuristic_judge_score("an answer", "cites a source")
    unopted = _Decider(0.99)
    assert await _reflector(unopted, opted=False)._score("an answer", "cites a source", "") == expected
    assert unopted.calls == []
    assert await _reflector(_Decider(fail=True))._score("an answer", "cites a source", "") == expected


# --- eval -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_eval_rubric_can_be_judged_by_a_decider() -> None:
    from felix.config import Settings
    from felix.decisions import register_builtin_deciders
    from felix.eval.runner import _maybe_llm_judge, _wants_llm_judge
    from felix_ai.decide import reset_decision_provider_registry
    from felix_ai.decide.scripted import register_scripted_decider

    rubric = {"judge_decider": "d", "judge_criteria": "answers in French", "judge_threshold": 0.6}
    assert _wants_llm_judge(rubric, deterministic_judge=False)
    register_scripted_decider("scripted", {"meets": NoulAnswer(0.55)})
    try:
        settings = Settings(
            database_url="memory://eval-decider",
            object_store="memory",
            decision_routes='{"d": {"provider": "scripted", "model": "jev-latest"}}',
        )
        judged = await _maybe_llm_judge(
            settings, user_input="q", answer="bonjour", rubric=rubric, heuristic=(True, 1.0, "contains")
        )
    finally:
        reset_decision_provider_registry()
        register_builtin_deciders()
    assert judged["rule"] == "decider_judge"
    assert judged["score"] == pytest.approx(0.55)
    assert judged["pass"] is False, "0.55 is under the rubric's 0.6"


# --- schema -----------------------------------------------------------------------------


def test_a_decider_judge_or_verifier_needs_a_decider() -> None:
    from felix.manifests.schema import Spec

    judges = {"judges": [{"name": "safe", "criteria": NEGATIVE, "decider": True}]}
    with pytest.raises(ValidationError, match=r"judges\[safe\]\.decider"):
        Spec.model_validate({"guardrails": judges})
    with pytest.raises(ValidationError, match=r"reflect\.decider"):
        Spec.model_validate({"reflect": {"decider": True}})
    Spec.model_validate({"guardrails": judges, "reflect": {"decider": True}, "decider": {"id": "jev"}})


@pytest.mark.asyncio
async def test_the_reflect_pattern_is_built_with_the_decider() -> None:
    """Through `_build_reflect`, the way the compile builds it — a reflect agent that never
    received the decider would validate `reflect.decider` and then ask the model."""
    from felix.manifests.loader import parse_manifest
    from felix.patterns import _build_reflect

    manifest = parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "r"},
            "spec": {"pattern": "reflect", "reflect": {"decider": True}, "decider": {"id": "jev"}},
        }
    )
    decider = _Decider(0.4)
    agent = await _build_reflect({"manifest": manifest, "manifest_id": "r", "tools": [], "decider": decider})
    assert await agent._score("an answer", "cites a source", "") == pytest.approx(0.4)  # type: ignore[attr-defined]
    assert decider.calls, "the built agent asked the decider"
