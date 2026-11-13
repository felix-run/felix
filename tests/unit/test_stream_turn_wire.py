"""Parsing one streamed turn off the wire: text, thinking, tool calls, usage, stop.

The streamed request previously carried no tools and reported no usage, which is why a
second non-streaming call was needed to find out what the model actually did.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.patterns.model import ModelChatResult, StreamDelta
from felix.patterns.types import ChatMessage
from felix_ai.wire.base import _repair_json
from felix_ai.wire.base import parse_tool_arguments as _parse_tool_arguments


class _FakeStreamResponse:
    status_code = 200

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self) -> bytes:
        return b""


class _FakeAsyncClient:
    sent: dict[str, Any] = {}

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    def __call__(self, *a: Any, **kw: Any) -> _FakeAsyncClient:
        return self

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    def stream(self, method: str, url: str, json: dict[str, Any] | None = None, headers: Any = None):
        type(self).sent = dict(json or {})
        return _FakeStream(self._lines)


class _FakeStream:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def __aenter__(self) -> _FakeStreamResponse:
        return _FakeStreamResponse(self._lines)

    async def __aexit__(self, *exc: Any) -> None:
        return None


def _client(monkeypatch: Any, lines: list[str], style: str):
    import httpx
    from felix.config import Settings
    from felix_ai import AnthropicMessagesClient, ModelRoute, OpenAICompletionsClient

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient(lines))
    cls = AnthropicMessagesClient if style == "anthropic" else OpenAICompletionsClient
    return cls(
        model_id="m",
        route=ModelRoute(provider=style, model="m"),
        settings=Settings(allow_insecure=True, auth_mode="none", environment="development"),
        spec=None,
        base_url="https://example.invalid",
        api_key="k",
    )


async def _collect(client, tools=None):
    deltas: list[StreamDelta] = []
    result: ModelChatResult | None = None
    async for item in client.stream_turn([ChatMessage(role="user", content="hi")], tools or []):
        if isinstance(item, ModelChatResult):
            result = item
        else:
            deltas.append(item)
    return deltas, result


ANTHROPIC_LINES = [
    'data: {"type":"message_start","message":{"usage":{"input_tokens":120,"cache_read_input_tokens":40}}}',
    'data: {"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":""}}',
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"weigh it"}}',
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"signature_delta","signature":"sig-1"}}',
    'data: {"type":"content_block_start","index":1,"content_block":{"type":"text","text":""}}',
    'data: {"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"Look"}}',
    'data: {"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"ing"}}',
    'data: {"type":"content_block_start","index":2,"content_block":{"type":"tool_use","id":"c1","name":"search"}}',
    'data: {"type":"content_block_delta","index":2,"delta":{"type":"input_json_delta","partial_json":"{\\"q\\":"}}',
    'data: {"type":"content_block_delta","index":2,"delta":{"type":"input_json_delta","partial_json":"\\"felix\\"}"}}',
    'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":33}}',
    'data: {"type":"message_stop"}',
]


async def test_anthropic_stream_yields_text_and_returns_the_full_turn(monkeypatch: Any) -> None:
    deltas, result = await _collect(_client(monkeypatch, ANTHROPIC_LINES, "anthropic"))

    assert "".join(d.text for d in deltas if d.kind == "text") == "Looking"
    assert "".join(d.text for d in deltas if d.kind == "thinking") == "weigh it"

    assert result is not None
    assert result.message.content == "Looking"
    assert result.stop_reason == "tool_use"
    assert result.message.tool_calls is not None
    call = result.message.tool_calls[0]
    assert (call.id, call.name, call.args) == ("c1", "search", {"q": "felix"})
    assert result.message.thinking == [{"type": "thinking", "thinking": "weigh it", "signature": "sig-1"}]
    assert result.usage is not None
    assert (result.usage.input, result.usage.output, result.usage.cache_read) == (120, 33, 40)


async def test_streamed_request_carries_tools(monkeypatch: Any) -> None:
    """The old streamed request omitted tools entirely, which forced the second call."""
    from felix.tools.types import define_tool

    async def _h(a: dict[str, Any], ctx: Any = None) -> str:
        return "x"

    client = _client(monkeypatch, ANTHROPIC_LINES, "anthropic")
    await _collect(client, [define_tool(name="search", description="d", handler=_h)])
    assert _FakeAsyncClient.sent["stream"] is True
    assert [t["name"] for t in _FakeAsyncClient.sent["tools"]] == ["search"]


OPENAI_LINES = [
    'data: {"choices":[{"delta":{"content":"Hel"}}]}',
    'data: {"choices":[{"delta":{"content":"lo"}}]}',
    'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c9","function":{"name":"lookup","arguments":"{\\"n\\":"}}]}}]}',
    'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"7}"}}]}}]}',
    'data: {"choices":[{"finish_reason":"tool_calls","delta":{}}]}',
    'data: {"usage":{"prompt_tokens":11,"completion_tokens":4},"choices":[]}',
    "data: [DONE]",
]


async def test_openai_stream_accumulates_tool_arguments(monkeypatch: Any) -> None:
    deltas, result = await _collect(_client(monkeypatch, OPENAI_LINES, "openai"))

    assert "".join(d.text for d in deltas) == "Hello"
    assert result is not None
    assert result.stop_reason == "tool_use"
    call = (result.message.tool_calls or [])[0]
    assert (call.id, call.name, call.args) == ("c9", "lookup", {"n": 7})
    assert result.usage is not None and result.usage.input == 11


async def test_openai_stream_asks_for_usage(monkeypatch: Any) -> None:
    """Without stream_options a streamed turn reports no usage and meters as zero."""
    await _collect(_client(monkeypatch, OPENAI_LINES, "openai"))
    assert _FakeAsyncClient.sent["stream_options"] == {"include_usage": True}


# --- a stream that did not finish -----------------------------------------------
#
# A stream cut off after a 200 used to be stored as a finished turn: half an answer as
# `end_turn`, and a tool call whose arguments had not all arrived dispatched with `{}`.


async def _fails(client) -> tuple[list[StreamDelta], Any]:
    """Drain `stream_turn`, expecting it to raise: (the deltas shown, the error)."""
    from felix_ai import ModelGatewayError

    items: list[Any] = []
    with pytest.raises(ModelGatewayError) as caught:
        async for item in client.stream_turn([ChatMessage(role="user", content="hi")], []):
            items.append(item)
    assert not [i for i in items if isinstance(i, ModelChatResult)], "a cut stream yielded a result"
    return items, caught.value


def _advances_a_fallback(err: Any) -> bool:
    from felix.patterns.model_composites import _is_provider_error

    return _is_provider_error(err)


async def test_anthropic_text_cut_mid_answer_is_not_a_finished_turn(monkeypatch: Any) -> None:
    shown, err = await _fails(_client(monkeypatch, ANTHROPIC_LINES[:6], "anthropic"))
    assert [d.text for d in shown if d.kind == "text"] == ["Look"]
    assert _advances_a_fallback(err)


async def test_anthropic_tool_call_cut_mid_json_is_not_dispatched(monkeypatch: Any) -> None:
    """Ends on `{"q":` -- unparseable, so the call would have run with `{}`."""
    _, err = await _fails(_client(monkeypatch, ANTHROPIC_LINES[:9], "anthropic"))
    assert _advances_a_fallback(err)


async def test_anthropic_in_band_error_event_raises(monkeypatch: Any) -> None:
    lines = [
        *ANTHROPIC_LINES[:7],
        "event: error",
        'data: {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}',
    ]
    _, err = await _fails(_client(monkeypatch, lines, "anthropic"))
    assert err.status == 529
    assert "Overloaded" in err.body and "Overloaded" not in str(err)


async def test_anthropic_error_after_the_stop_reason_still_raises(monkeypatch: Any) -> None:
    """An error event is not outranked by a stop reason that arrived ahead of it."""
    lines = [*ANTHROPIC_LINES[:11], 'data: {"type":"error","error":{"type":"api_error"}}']
    _, err = await _fails(_client(monkeypatch, lines, "anthropic"))
    assert err.status == 500


async def test_openai_text_cut_mid_answer_is_not_a_finished_turn(monkeypatch: Any) -> None:
    shown, err = await _fails(_client(monkeypatch, OPENAI_LINES[:1], "openai"))
    assert [d.text for d in shown] == ["Hel"]
    assert _advances_a_fallback(err)


async def test_openai_tool_call_cut_mid_json_is_not_dispatched(monkeypatch: Any) -> None:
    _, err = await _fails(_client(monkeypatch, OPENAI_LINES[:3], "openai"))
    assert _advances_a_fallback(err)


async def test_openai_in_band_error_chunk_raises(monkeypatch: Any) -> None:
    lines = [
        *OPENAI_LINES[:2],
        'data: {"error":{"message":"upstream went away","code":502},"choices":[{"delta":{},"finish_reason":"error"}]}',
        "data: [DONE]",
    ]
    _, err = await _fails(_client(monkeypatch, lines, "openai"))
    assert err.status == 502
    assert "upstream went away" in err.body and "upstream went away" not in str(err)


async def test_openai_stream_may_end_on_either_terminator(monkeypatch: Any) -> None:
    """Compatible servers omit one or the other; only a stream with neither was cut."""
    without_done = [line for line in OPENAI_LINES if "[DONE]" not in line]
    _, result = await _collect(_client(monkeypatch, without_done, "openai"))
    assert result is not None and result.stop_reason == "tool_use"

    without_finish = [line for line in OPENAI_LINES[:2]] + ["data: [DONE]"]
    _, result = await _collect(_client(monkeypatch, without_finish, "openai"))
    assert result is not None and (result.message.content, result.stop_reason) == ("Hello", "end_turn")


async def test_anthropic_stream_may_end_on_its_stop_reason(monkeypatch: Any) -> None:
    """`message_stop` carries nothing: a turn whose stop reason arrived is whole without it."""
    _, result = await _collect(_client(monkeypatch, ANTHROPIC_LINES[:-1], "anthropic"))
    assert result is not None and result.stop_reason == "tool_use"


# --- argument repair ------------------------------------------------------------


def test_clean_json_is_untouched() -> None:
    assert _parse_tool_arguments('{"a": 1}') == {"a": 1}


def test_raw_control_characters_inside_strings_are_repaired() -> None:
    """Models emit literal newlines and tabs inside JSON string literals constantly."""
    assert _parse_tool_arguments('{"a": "one\ntwo"}') == {"a": "one\ntwo"}
    assert _parse_tool_arguments('{"a": "one\ttwo"}') == {"a": "one\ttwo"}


def test_invalid_backslash_escape_is_repaired() -> None:
    assert _parse_tool_arguments(r'{"a": "50\% done"}') == {"a": "50\\% done"}


def test_valid_escapes_survive_repair() -> None:
    assert _parse_tool_arguments('{"a": "line\\nbreak"}') == {"a": "line\nbreak"}
    assert _parse_tool_arguments('{"a": "\\u00e9"}') == {"a": "é"}


def test_unrepairable_fragment_yields_empty_args_rather_than_raising() -> None:
    """Losing the turn to an exception is worse than a call the schema will reject."""
    assert _parse_tool_arguments('{"a": ') == {}
    assert _parse_tool_arguments("") == {}
    assert _parse_tool_arguments("not json at all") == {}


def test_non_object_arguments_are_rejected() -> None:
    assert _parse_tool_arguments("[1, 2]") == {}
    assert _parse_tool_arguments('"a string"') == {}


def test_repair_leaves_structure_outside_strings_alone() -> None:
    assert _repair_json('{"a": 1,\n "b": 2}') == '{"a": 1,\n "b": 2}'


@pytest.mark.parametrize(
    ("error", "status"),
    [
        ({"type": "overloaded_error"}, 529),
        ({"type": "rate_limit_error"}, 429),
        # A request wrong for every model: as a 500 it read as this provider's fault, and a
        # fallback chain advanced past it.
        ({"type": "invalid_request_error"}, 400),
        ({"type": "authentication_error"}, 401),
        ({"code": 503, "message": "upstream"}, 503),
        ({"code": "rate_limited"}, 500),
        (None, 500),
    ],
)
def test_an_in_band_error_takes_the_status_the_provider_gives_it_up_front(error: Any, status: int) -> None:
    from felix_ai.wire.base import stream_error

    assert stream_error("p", error).status == status
