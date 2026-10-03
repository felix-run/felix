"""Tool-call history sent to `/chat` in either shape a client uses.

`ChatMessage.model_validate` read a tool call's arguments with `dict(...)`, which is right for
Felix's own `{"id", "name", "args": {...}}` and wrong for OpenAI's
`{"id", "type": "function", "function": {"name", "arguments": "<json string>"}}` -- `dict()` of a
string raised, and the request was a 500. A malformed call is the caller's mistake and is now a
422 that says which one, before any model is called.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix_ai.providers.scripted import ScriptedTurn

OPENAI_SHAPE = {
    "id": "c1",
    "type": "function",
    "function": {"name": "calculator", "arguments": '{"expression": "2+2"}'},
}
FELIX_SHAPE = {"id": "c1", "name": "calculator", "args": {"expression": "2+2"}}


def _history(call: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": "what is 2+2?"},
        {"role": "assistant", "content": "", "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "c1", "name": "calculator", "content": "4"},
        {"role": "user", "content": "and doubled?"},
    ]


@pytest.mark.parametrize("call", [OPENAI_SHAPE, FELIX_SHAPE], ids=["openai", "felix"])
async def test_tool_call_history_in_either_shape_reaches_the_model(boot: Any, call: dict[str, Any]) -> None:
    async with boot([ScriptedTurn(content="8")]) as app:
        resp = await app.client.post("/chat", json={"manifest": "quick", "messages": _history(call)})
        assert resp.status_code == 200, resp.text

        (assistant,) = [m for m in app.spy.prompts[0] if m.role == "assistant" and m.tool_calls]
        (seen,) = assistant.tool_calls
        assert (seen.id, seen.name, seen.args) == ("c1", "calculator", {"expression": "2+2"})


@pytest.mark.parametrize(
    "arguments",
    ["{not json", '"a string"', "[1, 2]"],
    ids=["invalid-json", "json-string", "json-array"],
)
async def test_a_tool_call_whose_arguments_are_not_an_object_is_a_422(boot: Any, arguments: str) -> None:
    bad = {**OPENAI_SHAPE, "function": {"name": "calculator", "arguments": arguments}}
    async with boot([ScriptedTurn(content="unused")]) as app:
        for route in ("/chat", "/chat/stream"):
            resp = await app.client.post(route, json={"manifest": "quick", "messages": _history(bad)})
            assert resp.status_code == 422, (route, resp.text)
            assert "tool_calls" in resp.text
        assert app.spy.calls == [], "a malformed history reaches no model"
