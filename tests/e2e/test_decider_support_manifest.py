"""The bundled `decider-support` manifest, compiled and run the way a deployment would.

It is the example every decider consumer is copied from, so the test is that each one it
switches on is actually reached: one turn, a scripted decider standing in for Jev, and the
questions it was asked. A consumer that validates and then asks nothing is the defect shape
this repo keeps producing — and an example is where it would be copied from.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from felix_ai.decide import Choice, ChoiceAnswer, NoulAnswer
from felix_ai.providers.scripted import ScriptedTurn

ENV = {"FELIX_DECISION_ROUTES": json.dumps({"jev": {"provider": "scripted", "model": "jev-latest"}})}
SCREENING = {"override", "jailbreak", "exfiltrate"}


@pytest.fixture
def asked() -> Iterator[list[str]]:
    """Every question key the decider was asked, in order. Clean, confident answers throughout."""
    from felix.decisions import register_builtin_deciders
    from felix_ai.decide import reset_decision_provider_registry
    from felix_ai.decide.scripted import register_scripted_decider

    keys: list[str] = []

    def answer(_state: Any, questions: Any) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, q in questions.items():
            keys.append(key)
            if isinstance(q, Choice):
                first = next(iter(q.criteria))
                out[key] = ChoiceAnswer(first, {first: 0.9}, confidence=0.9)
            else:
                out[key] = NoulAnswer(0.02 if key in SCREENING else 0.9)
        return out

    register_scripted_decider("scripted", answer)
    try:
        yield keys
    finally:
        reset_decision_provider_registry()
        register_builtin_deciders()


async def test_every_consumer_the_example_switches_on_is_asked(boot: Any, asked: list[str]) -> None:
    async with boot(
        [ScriptedTurn(content="Restart the worker with `felix worker restart`.")], env=ENV
    ) as app:
        resp = await app.client.post(
            "/v1/chat/completions",
            json={
                "model": "decider-support",
                "messages": [{"role": "user", "content": "How do I restart the worker?"}],
            },
        )
        assert resp.status_code == 200, resp.text
        [offered] = app.spy.tools
        [prompt] = app.spy.prompts

    assert any(k in SCREENING for k in asked), "content_screening.decider screened the turn"
    assert any(k.startswith("tool_") for k in asked), "tools_retrieval.decider ranked the tools"
    assert any(k.startswith(("rank_", "fit_")) for k in asked), "skill_suggestion ranked the skills"
    assert "answers" in asked, "confidence_escalation.decider judged the reply"
    assert len(offered) <= 4, f"tools_retrieval.top_k narrowed the catalogue: {offered}"
    assert prompt[-1].transient, "the skill hint rode the call, transiently"


async def test_without_a_route_the_example_refuses_to_compile_and_says_why(boot: Any) -> None:
    """The documented failure: no key, no compile — never an agent that decides nothing."""
    async with boot() as app:
        with pytest.raises(ValueError, match="api_key"):
            await app.client.post(
                "/v1/chat/completions",
                json={"model": "decider-support", "messages": [{"role": "user", "content": "hi"}]},
            )
