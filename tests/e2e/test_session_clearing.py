"""Stale tool results through the stack: the model is sent a stub, the thread keeps the result."""

from __future__ import annotations

from typing import Any

from felix.manifests.loader import parse_manifest
from felix_ai.providers.scripted import ScriptedTurn
from felix_ai.types import ToolCall

THREAD = "e2e-clearing"


def _agent() -> Any:
    spec = {
        "pattern": "react",
        "auth": {"inbound": {"allow_anonymous": True}},
        "tools": ["calculator"],
        "session": {"clear_tool_results": {"keep_turns": 1, "min_chars": 0}},
    }
    return parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": "e2e-clear"}, "spec": spec}
    )


def _calc(n: int) -> ScriptedTurn:
    return ScriptedTurn(
        tool_calls=[ToolCall(id=f"c{n}", name="calculator", args={"expression": f"{n}*1000+7"})]
    )


async def _turn(app: Any, text: str) -> None:
    resp = await app.client.post(
        "/chat",
        json={"manifest": "e2e-clear", "thread_id": THREAD, "messages": [{"role": "user", "content": text}]},
    )
    assert resp.status_code == 200, resp.text


async def test_an_old_result_is_stubbed_for_the_model_and_kept_in_the_thread(boot: Any) -> None:
    script = [
        _calc(1),
        ScriptedTurn(content="one"),
        _calc(2),
        ScriptedTurn(content="two"),
        ScriptedTurn(content="three"),
    ]
    async with boot(script, manifests={"e2e-clear": _agent()}) as app:
        await _turn(app, "first")
        await _turn(app, "second")
        # The call that answered turn two: turn one's result is stale, turn two's is the current turn's.
        in_turn_two = {m.tool_call_id: m.content for m in app.spy.prompts[-1] if m.role == "tool"}
        await _turn(app, "third")
        in_turn_three = {m.tool_call_id: m.content for m in app.spy.prompts[-1] if m.role == "tool"}
        history = (await app.client.get(f"/chat/history/{THREAD}")).text
    assert in_turn_two["c1"].startswith("[cleared to save context:"), in_turn_two
    assert in_turn_two["c2"] == "2007", "the current turn's result is whole"
    assert in_turn_three["c2"].startswith("[cleared to save context:"), in_turn_three
    assert "1007" in history and "2007" in history, "the thread keeps what the model was spared"


async def test_a_side_question_reads_the_thread_as_its_next_turn_would(boot: Any) -> None:
    """`/chat/ask` renders through its own copy of the strategy: it must be wrapped the same way."""
    script = [
        _calc(1),
        ScriptedTurn(content="one"),
        _calc(2),
        ScriptedTurn(content="two"),
        ScriptedTurn(content="ok"),
    ]
    async with boot(script, manifests={"e2e-clear": _agent()}) as app:
        await _turn(app, "first")
        await _turn(app, "second")
        resp = await app.client.post("/chat/ask", json={"thread_id": THREAD, "question": "what was it?"})
        sent = " ".join(m.content or "" for m in app.spy.prompts[-1])
    assert resp.status_code == 200, resp.text
    assert "[cleared to save context:" in sent and "1007" not in sent, sent
