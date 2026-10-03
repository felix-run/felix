"""`ChatMessage.model_validate` reading tool calls in Felix's shape and OpenAI's."""

from __future__ import annotations

from typing import Any

import pytest
from felix_ai.types import ChatMessage, MessageFormatError


def _parse(call: dict[str, Any]) -> Any:
    (tc,) = (
        ChatMessage.model_validate({"role": "assistant", "content": "", "tool_calls": [call]}).tool_calls
        or []
    )
    return tc


@pytest.mark.parametrize(
    "call",
    [
        {"id": "c", "name": "calc", "args": {"x": 1}},
        {"id": "c", "name": "calc", "arguments": {"x": 1}},
        {"id": "c", "type": "function", "function": {"name": "calc", "arguments": '{"x": 1}'}},
        {"id": "c", "function": {"name": "calc", "arguments": {"x": 1}}},
    ],
    ids=["felix", "arguments-object", "openai-string", "function-object"],
)
def test_every_shape_reads_to_the_same_call(call: dict[str, Any]) -> None:
    tc = _parse(call)
    assert (tc.id, tc.name, tc.args) == ("c", "calc", {"x": 1})


@pytest.mark.parametrize("arguments", ["", "   ", None], ids=["empty", "blank", "absent"])
def test_no_arguments_is_an_empty_object(arguments: Any) -> None:
    function = {"name": "calc"} if arguments is None else {"name": "calc", "arguments": arguments}
    assert _parse({"id": "c", "function": function}).args == {}


@pytest.mark.parametrize(
    ("arguments", "reason"),
    [
        ("{nope", "not valid JSON"),
        ('"text"', "must be a JSON object"),
        ("[1]", "must be a JSON object"),
        (7, "must be a JSON object"),
    ],
    ids=["invalid", "string", "array", "number"],
)
def test_arguments_that_are_not_an_object_say_which_call(arguments: Any, reason: str) -> None:
    good = {"id": "a", "name": "calc", "args": {}}
    bad = {"id": "b", "function": {"name": "calc", "arguments": arguments}}
    with pytest.raises(MessageFormatError, match=rf"tool_calls\[1\]: .*{reason}"):
        ChatMessage.model_validate({"role": "assistant", "content": "", "tool_calls": [good, bad]})
