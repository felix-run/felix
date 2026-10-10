"""Decider judges through the stack: the compile hands `spec.decider` to both judge slots.

A judge rule with `decider: true` is scored by the decider only if `build_agent` passed it to
`apply_judges` (tool output) and to the reply controls (the final answer). Neither is visible
from a unit test of `judge_score`, so these assert the effect a caller sees: a tool result the
model is shown as denied, and a reply replaced by the denial notice.
"""

from __future__ import annotations

from typing import Any

from felix_ai.providers.scripted import ScriptedTurn
from felix_ai.types import ToolCall

from tests.support.screening import DECIDER_ENV, judged_manifest

CALC = ToolCall(id="call-1", name="calculator", args={"expression": "2+2"})


async def _chat(app: Any) -> Any:
    return await app.client.post(
        "/v1/chat/completions",
        json={"model": "e2e-judged", "messages": [{"role": "user", "content": "What is 2+2?"}]},
    )


async def test_a_tool_result_the_decider_rejects_reaches_the_model_as_denied(boot: Any, verdict: Any) -> None:
    script = [ScriptedTurn(content="", tool_calls=[CALC], stop_reason="tool_use"), ScriptedTurn(content="4")]
    async with boot(script, env=DECIDER_ENV, manifests={"e2e-judged": judged_manifest()}) as app:
        resp = await _chat(app)
        assert resp.status_code == 200, resp.text
        shown = [str(m.content) for m in app.spy.prompts[1] if m.role == "tool"]
    assert verdict["judged"] == ["4"], "the calculator's output was what the decider judged"
    assert len(shown) == 1 and shown[0].startswith("[judge denied] on-topic")


async def test_a_reply_the_decider_rejects_is_replaced(boot: Any, verdict: Any) -> None:
    m = judged_manifest(final_response=True)
    async with boot(
        [ScriptedTurn(content="Let me tell you about cats.")], env=DECIDER_ENV, manifests={"e2e-judged": m}
    ) as app:
        resp = await _chat(app)
        assert resp.status_code == 200, resp.text
        reply = resp.json()["choices"][0]["message"]["content"]
    assert verdict["judged"] == ["Let me tell you about cats."]
    assert "on-topic" in reply and "cats" not in reply


async def test_a_reply_the_decider_accepts_ships(boot: Any, verdict: Any) -> None:
    verdict["p"] = 0.9
    m = judged_manifest(final_response=True)
    async with boot([ScriptedTurn(content="2+2 is 4.")], env=DECIDER_ENV, manifests={"e2e-judged": m}) as app:
        resp = await _chat(app)
        assert resp.json()["choices"][0]["message"]["content"] == "2+2 is 4."
