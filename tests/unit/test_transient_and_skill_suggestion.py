"""Transient messages, and the skill suggestion that rides on one.

A per-request message in the prompt costs the conversation its cache: present in one request
and gone from the next, it splits the prefix at the point it stood. `ChatMessage.transient`
is sent after the cache breakpoint and after everything persistent, so the prefix a later
request reuses never contains it. These pin that on both wires, and the suggester's decisions.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix_ai import AnthropicMessagesClient, ModelRoute, OpenAICompletionsClient
from felix_ai.decide import ChoiceAnswer, DecisionResult, NoulAnswer
from felix_ai.types import ChatMessage


class _Spec:
    cache = True
    thinking_budget = None
    temperature = 0
    max_tokens = None


def _settings() -> Any:
    from felix.config import Settings

    return Settings(allow_insecure=True, auth_mode="none", environment="development")


CONVERSATION = [
    ChatMessage(role="system", content="you are felix"),
    ChatMessage(role="user", content="first"),
    ChatMessage(role="assistant", content="answer"),
    ChatMessage(role="user", content="second"),
]
HINT = ChatMessage(role="user", content="(hint)", transient=True)


def test_anthropic_puts_the_breakpoint_on_the_last_persistent_message() -> None:
    client = AnthropicMessagesClient(
        model_id="claude-sonnet-5",
        route=ModelRoute(provider="anthropic", model="claude-sonnet-5"),
        settings=_settings(),
        spec=_Spec(),
        base_url="https://example.invalid",
        api_key="k",
    )
    body = client._body([*CONVERSATION, HINT], [], 0.0, None)
    *persistent, tail = body["messages"]
    assert tail == {"role": "user", "content": "(hint)"}, "sent last, carrying no breakpoint"
    assert persistent[-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert persistent[-1]["content"][-1]["text"] == "second"
    assert "(hint)" not in str(body["system"]), "never folded into the cached system block"
    # The prefix is byte-identical to the request with no hint at all.
    assert persistent == client._body(CONVERSATION, [], 0.0, None)["messages"]


def test_a_transient_system_message_is_sent_as_a_user_turn_not_folded_into_system() -> None:
    client = AnthropicMessagesClient(
        model_id="claude-sonnet-5",
        route=ModelRoute(provider="anthropic", model="claude-sonnet-5"),
        settings=_settings(),
        spec=_Spec(),
        base_url="https://example.invalid",
        api_key="k",
    )
    body = client._body(
        [*CONVERSATION, ChatMessage(role="system", content="(note)", transient=True)], [], 0.0, None
    )
    assert "(note)" not in str(body["system"])
    assert body["messages"][-1] == {"role": "user", "content": "(note)"}


def test_openai_sends_transient_messages_last_so_the_prefix_is_unchanged() -> None:
    client = OpenAICompletionsClient(
        model_id="gpt-4.1",
        route=ModelRoute(provider="openai", model="gpt-4.1"),
        settings=_settings(),
        spec=_Spec(),
        base_url="https://example.invalid/v1",
        api_key="k",
    )
    # Even out of position: nothing persistent may follow a transient message on the wire.
    with_hint = client._body([*CONVERSATION[:2], HINT, *CONVERSATION[2:]], [], 0.0, None)
    without = client._body(CONVERSATION, [], 0.0, None)
    assert with_hint["messages"][:-1] == without["messages"]
    assert with_hint["messages"][-1] == {"role": "user", "content": "(hint)"}


def test_the_request_a_decision_is_about_is_never_the_harnesss_own_hint() -> None:
    from felix.decisions import latest_request

    assert latest_request([*CONVERSATION, HINT]) == "second"


# --- the suggester ----------------------------------------------------------------------


class _Decider:
    model_id = "double"
    wire_model = "jev-latest"
    min_confidence = 0.5

    def __init__(self, answer: Any, *, fail: bool = False) -> None:
        self.answer = answer
        self.fail = fail
        self.calls: list[tuple[Any, dict[str, Any], str]] = []

    async def decide(self, state: Any, questions: dict[str, Any], *, purpose: str = "") -> DecisionResult:
        self.calls.append((state, questions, purpose))
        if self.fail:
            raise RuntimeError("down")
        return DecisionResult(answers={k: self.answer(k, q) for k, q in questions.items()})


def _skills(n: int) -> list[Any]:
    from felix.skills.types import Skill

    return [Skill(name=f"skill-{i}", description=f"does thing {i}", body=f"steps for {i}") for i in range(n)]


def _suggester(decider: Any, n: int = 3, **spec: Any) -> Any:
    from felix.manifests.schema import SkillSuggestionSpec
    from felix.skills.suggest import SkillSuggester

    return SkillSuggester(_skills(n), decider, SkillSuggestionSpec(enabled=True, **spec))


ASK = [ChatMessage(role="user", content="export my report to pdf")]


def _fits(best: str, p_best: float = 0.8, gate: float = 0.9) -> Any:
    def answer(key: str, q: Any) -> Any:
        if key == "gate":
            return NoulAnswer(gate)
        if key.startswith("fit_"):
            return NoulAnswer(p_best if f"`{best}`" in q.instructions else 0.05)
        return ChoiceAnswer(best, {best: 0.9}, confidence=0.9)

    return answer


@pytest.mark.asyncio
async def test_a_small_catalog_is_reranked_directly_and_the_best_fit_is_hinted() -> None:
    decider = _Decider(_fits("skill-1"))
    hint = await _suggester(decider).hint(ASK)
    assert hint is not None and "`skill-1`" in hint and "activate_skill" in hint
    assert [purpose for *_, purpose in decider.calls] == ["skill_rerank"], "3 skills <= shortlist: no ranking"


@pytest.mark.asyncio
async def test_a_large_catalog_is_ranked_first_and_only_the_shortlist_reranked() -> None:
    spread = {"skill-7": 0.5, "skill-2": 0.2, "skill-4": 0.15, "skill-9": 0.1, "skill-0": 0.05}
    fits = _fits("skill-7")

    def answer(key: str, q: Any) -> Any:
        return ChoiceAnswer("skill-7", spread, confidence=0.5) if key.startswith("rank_") else fits(key, q)

    decider = _Decider(answer)
    hint = await _suggester(decider, n=10, shortlist=3).hint(ASK)
    assert hint is not None and "`skill-7`" in hint
    assert [purpose for *_, purpose in decider.calls] == ["skill_rank", "skill_rerank"]
    reranked = decider.calls[1][1]
    assert len([k for k in reranked if k.startswith("fit_")]) == 3, "five weighted, three carried"
    assert "gate" not in reranked, "the gate was already asked in the ranking"


@pytest.mark.parametrize(
    "answer",
    [_fits("skill-1", gate=0.1), _fits("skill-1", p_best=0.2)],
    ids=["not-a-task", "nothing-fits"],
)
@pytest.mark.asyncio
async def test_no_hint_unless_the_request_is_a_task_and_a_skill_fits(answer: Any) -> None:
    assert await _suggester(_Decider(answer)).hint(ASK) is None


@pytest.mark.asyncio
async def test_a_decider_outage_means_no_hint_not_a_failed_turn() -> None:
    assert await _suggester(_Decider(None, fail=True)).hint(ASK) is None


@pytest.mark.asyncio
async def test_a_turn_with_no_request_text_asks_nothing() -> None:
    decider = _Decider(_fits("skill-1"))
    assert await _suggester(decider).hint([ChatMessage(role="user", content="")]) is None
    assert decider.calls == []


def test_skill_suggestion_needs_a_decider_and_a_react_loop() -> None:
    from felix.manifests.schema import Spec
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match=r"spec\.decider\.id"):
        Spec.model_validate({"skill_suggestion": {"enabled": True}})
    with pytest.raises(ValidationError, match="router"):
        Spec.model_validate(
            {"pattern": "router", "skill_suggestion": {"enabled": True}, "decider": {"id": "jev"}}
        )
    Spec.model_validate({"skill_suggestion": {"enabled": True}, "decider": {"id": "jev"}})


@pytest.mark.asyncio
async def test_a_large_catalog_stops_at_the_ranking_when_the_request_is_not_a_task() -> None:
    """The gate is asked in the ranking when there is one, and a failed gate ends it there —
    no rerank call, no hint."""
    decider = _Decider(_fits("skill-7", gate=0.1))
    assert await _suggester(decider, n=10, shortlist=3).hint(ASK) is None
    assert [purpose for *_, purpose in decider.calls] == ["skill_rank"]


@pytest.mark.asyncio
async def test_a_ranking_that_picks_no_skill_ends_without_a_rerank() -> None:
    """Every chunk chose "no skill": a shortlist by catalog order would pay for a rerank of
    whichever skills came first, and a lenient fit could suggest one of them."""
    from felix.skills.suggest import NO_SKILL

    def answer(key: str, q: Any) -> Any:
        if key == "gate":
            return NoulAnswer(0.9)
        if key.startswith("rank_"):
            return ChoiceAnswer(NO_SKILL, {}, confidence=None)
        return NoulAnswer(0.9)

    decider = _Decider(answer)
    assert await _suggester(decider, n=10, shortlist=3).hint(ASK) is None
    assert [purpose for *_, purpose in decider.calls] == ["skill_rank"]
