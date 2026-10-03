"""Tool-call history sent to `/v1/chat/completions`, the way an OpenAI SDK sends it.

The request model declared no `tool_calls`, and pydantic ignores an undeclared field, so an
assistant turn's calls were dropped without a word: the `tool` messages that followed answered
ids the model was never shown, and the turn read as a reply nothing had asked for. The
assertions are on what reached the model.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix_ai.providers.scripted import ScriptedTurn

CALL = {
    "id": "call_1",
    "type": "function",
    "function": {"name": "calculator", "arguments": '{"expression": "2+2"}'},
}


def _history(call: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": "what is 2+2?"},
        {"role": "assistant", "content": None, "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "call_1", "content": "4"},
        {"role": "user", "content": "and doubled?"},
    ]


async def test_an_assistant_turns_tool_calls_reach_the_model(boot: Any) -> None:
    async with boot([ScriptedTurn(content="8")]) as app:
        resp = await app.client.post(
            "/v1/chat/completions", json={"model": "quick", "messages": _history(CALL)}
        )
        assert resp.status_code == 200, resp.text

        prompt = app.spy.prompts[0]
        (assistant,) = [m for m in prompt if m.role == "assistant"]
        (seen,) = assistant.tool_calls or []
        assert (seen.id, seen.name, seen.args) == ("call_1", "calculator", {"expression": "2+2"})
        (tool,) = [m for m in prompt if m.role == "tool"]
        assert tool.tool_call_id == "call_1", "the result still answers the call it belongs to"


@pytest.mark.parametrize("arguments", ["{not json", "[1, 2]"], ids=["invalid-json", "json-array"])
async def test_tool_call_arguments_that_are_not_an_object_are_a_400(boot: Any, arguments: str) -> None:
    bad = {**CALL, "function": {"name": "calculator", "arguments": arguments}}
    async with boot([ScriptedTurn(content="unused")]) as app:
        resp = await app.client.post(
            "/v1/chat/completions", json={"model": "quick", "messages": _history(bad)}
        )
        assert resp.status_code == 400, resp.text
        error = resp.json()["error"]
        assert error["type"] == "invalid_request_error" and "tool_calls[0]" in error["message"]
        assert app.spy.calls == [], "a malformed history reaches no model"
