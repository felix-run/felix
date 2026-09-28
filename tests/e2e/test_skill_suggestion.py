"""`spec.skill_suggestion` through the stack: a hint on each turn's first call, in no transcript.

What makes the hint cheap is where it goes: last, on every model call of the turn, and never
into the session log — so the next turn's prompt, rendered from that log, has the same prefix
it would have had without a hint. These assert exactly that against the prompts the model was
handed and the thread's stored history.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from felix.manifests.loader import parse_manifest
from felix_ai.decide import ChoiceAnswer, NoulAnswer
from felix_ai.providers.scripted import ScriptedTurn
from felix_ai.types import ToolCall

ENV = {"FELIX_DECISION_ROUTES": json.dumps({"e2e-decider": {"provider": "scripted", "model": "jev-latest"}})}
CALC = ToolCall(id="call-1", name="calculator", args={"expression": "2+2"})


@pytest.fixture
def decider() -> Iterator[dict[str, Any]]:
    from felix.decisions import register_builtin_deciders
    from felix_ai.decide import reset_decision_provider_registry
    from felix_ai.decide.scripted import register_scripted_decider

    state: dict[str, Any] = {"calls": 0}

    def answer(_state: Any, questions: Any) -> dict[str, Any]:
        state["calls"] += 1
        out: dict[str, Any] = {}
        for key, q in questions.items():
            if key.startswith("rank_"):
                # Every bundled skill is in the catalog (spec.skills is not exclusive), so the
                # catalog outgrows the shortlist and the ranking stage runs first.
                out[key] = ChoiceAnswer("calculator-help", {"calculator-help": 0.9}, confidence=0.9)
            else:
                out[key] = NoulAnswer(0.9 if key == "gate" or "`calculator-help`" in q.instructions else 0.05)
        return out

    register_scripted_decider("scripted", answer)
    try:
        yield state
    finally:
        reset_decision_provider_registry()
        register_builtin_deciders()


def _manifest() -> Any:
    spec = {
        "pattern": "react",
        "tools": ["calculator"],
        "skills": [{"name": "calculator-help"}, {"name": "felix-testing"}],
        "auth": {"inbound": {"allow_anonymous": True}},
        "decider": {"id": "e2e-decider"},
        "skill_suggestion": {"enabled": True},
    }
    return parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": "e2e-skilled"}, "spec": spec}
    )


async def _turn(app: Any, text: str) -> Any:
    return await app.client.post(
        "/chat",
        json={
            "manifest": "e2e-skilled",
            "thread_id": "e2e-hint",
            "messages": [{"role": "user", "content": text}],
        },
    )


async def test_the_hint_rides_the_first_call_of_each_turn_and_no_transcript(boot: Any, decider: Any) -> None:
    script = [
        ScriptedTurn(content="", tool_calls=[CALC], stop_reason="tool_use"),
        ScriptedTurn(content="4"),
        ScriptedTurn(content="done"),
    ]
    async with boot(script, env=ENV, manifests={"e2e-skilled": _manifest()}) as app:
        assert (await _turn(app, "What is 2+2?")).status_code == 200
        assert (await _turn(app, "Thanks!")).status_code == 200
        first_step, second_step, next_turn = app.spy.prompts

        for prompt in (first_step, next_turn):
            tail = prompt[-1]
            assert tail.transient and "`calculator-help`" in tail.content, "last, on a turn's first call"
            assert sum(m.transient for m in prompt) == 1, "one hint — this turn's, not an earlier one"
        # Read once: the step after the tool call does not repeat "load the skill".
        assert not any(m.transient for m in second_step)
        assert second_step[-1].role == "tool"

        # The second turn renders turn one from the session log: no hint in it.
        history = [m.content for m in next_turn[:-1]]
        assert "What is 2+2?" in history and "4" in history
        assert not any("calculator-help`" in str(c) for c in history)

    assert decider["calls"] == 4, "rank + rerank once per turn, not once per model call"
