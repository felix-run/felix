"""Extended thinking has to survive the round trip, or tool-using turns lose it.

The provider signs each thinking block, and a later turn replaying a tool call must
replay the signed reasoning alongside it. Felix sent `thinking` on the request but read
nothing back: `_chat_anthropic` skipped `thinking` content blocks, and the session log
dropped the field entirely. A thinking-enabled manifest therefore lost its reasoning at
the first tool call.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.patterns.types import ChatMessage, ToolCall
from felix.session.types import SessionEvent, chat_message_to_event, event_to_chat_message
from felix_ai.wire.anthropic_messages import _anthropic_thinking_blocks

SIGNED = {"type": "thinking", "thinking": "weigh the options", "signature": "sig-abc"}
REDACTED = {"type": "redacted_thinking", "data": "opaque-payload"}


def _assistant(**kw: Any) -> ChatMessage:
    return ChatMessage(role="assistant", content="calling a tool", **kw)


def test_signed_blocks_are_replayed_verbatim() -> None:
    blocks = _anthropic_thinking_blocks(_assistant(thinking=[SIGNED]))
    assert blocks == [{"type": "thinking", "thinking": "weigh the options", "signature": "sig-abc"}]


def test_redacted_blocks_are_echoed_on_data_alone() -> None:
    assert _anthropic_thinking_blocks(_assistant(thinking=[REDACTED])) == [
        {"type": "redacted_thinking", "data": "opaque-payload"}
    ]


def test_unsigned_thinking_is_dropped_not_sent() -> None:
    """An unsigned block fails signature verification and rejects the whole turn."""
    unsigned = {"type": "thinking", "thinking": "no signature here"}
    assert _anthropic_thinking_blocks(_assistant(thinking=[unsigned])) == []


def test_block_order_is_preserved() -> None:
    out = _anthropic_thinking_blocks(_assistant(thinking=[SIGNED, REDACTED]))
    assert [b["type"] for b in out] == ["thinking", "redacted_thinking"]


def test_no_thinking_is_not_an_error() -> None:
    assert _anthropic_thinking_blocks(_assistant()) == []
    assert _anthropic_thinking_blocks(_assistant(thinking=[])) == []


def test_thinking_survives_the_session_round_trip() -> None:
    original = _assistant(
        thinking=[SIGNED],
        tool_calls=[ToolCall(id="c1", name="search", args={"q": "x"})],
    )
    appendable = chat_message_to_event(original)
    assert appendable.metadata is not None
    assert appendable.metadata["thinking"] == [SIGNED]

    # Rebuild the way a session strategy does when replaying history.
    restored = event_to_chat_message(
        SessionEvent(
            seq=1,
            ts=0.0,
            kind=appendable.kind,
            role=appendable.role,
            content=appendable.content,
            tool_calls=appendable.tool_calls,
            metadata=appendable.metadata,
        )
    )
    assert restored.thinking == [SIGNED]
    assert _anthropic_thinking_blocks(restored) == _anthropic_thinking_blocks(original)


def test_message_without_thinking_carries_no_metadata() -> None:
    """Empty metadata stays None so existing events are byte-identical."""
    assert chat_message_to_event(ChatMessage(role="user", content="hi")).metadata is None


# --- capture off the wire ---------------------------------------------------------


class _FakeResponse:
    status_code = 200

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.text = ""
        self.headers: dict[str, str] = {}

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeAsyncClient:
    """Stands in for httpx.AsyncClient and records the body that was sent."""

    sent: dict[str, Any] = {}

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def __call__(self, *a: Any, **kw: Any) -> _FakeAsyncClient:
        return self

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def post(self, url: str, json: dict[str, Any] | None = None, headers: Any = None):
        type(self).sent = dict(json or {})
        return _FakeResponse(self._payload)


def _client(monkeypatch: Any, payload: dict[str, Any]):
    import httpx
    from felix.config import Settings
    from felix_ai import AnthropicMessagesClient, ModelRoute

    fake = _FakeAsyncClient(payload)
    monkeypatch.setattr(httpx, "AsyncClient", fake)
    return AnthropicMessagesClient(
        model_id="claude-test",
        route=ModelRoute(provider="anthropic", model="claude-test"),
        settings=Settings(allow_insecure=True, auth_mode="none", environment="development"),
        spec=None,
        base_url="https://example.invalid",
        api_key="k",
    )


@pytest.mark.asyncio
async def test_thinking_blocks_are_captured_off_the_response(monkeypatch: Any) -> None:
    client = _client(
        monkeypatch,
        {
            "content": [
                {"type": "thinking", "thinking": "reason about it", "signature": "sig-1"},
                {"type": "text", "text": "here goes"},
                {"type": "tool_use", "id": "c1", "name": "search", "input": {"q": "x"}},
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 5, "output_tokens": 6},
        },
    )
    result = await client.chat([ChatMessage(role="user", content="hi")], [])

    assert result.message.thinking == [
        {"type": "thinking", "thinking": "reason about it", "signature": "sig-1"}
    ]
    assert result.message.content == "here goes"
    assert result.message.tool_calls and result.message.tool_calls[0].id == "c1"


@pytest.mark.asyncio
async def test_captured_thinking_is_sent_back_on_the_next_turn(monkeypatch: Any) -> None:
    """The round trip that was broken: reason, call a tool, then replay both."""
    client = _client(
        monkeypatch,
        {"content": [{"type": "text", "text": "done"}], "stop_reason": "end_turn", "usage": {}},
    )
    prior = ChatMessage(
        role="assistant",
        content="here goes",
        thinking=[{"type": "thinking", "thinking": "reason about it", "signature": "sig-1"}],
        tool_calls=[ToolCall(id="c1", name="search", args={"q": "x"})],
    )
    await client.chat(
        [
            ChatMessage(role="user", content="hi"),
            prior,
            ChatMessage(role="tool", tool_call_id="c1", name="search", content="result"),
        ],
        [],
    )

    assistant_turn = next(m for m in _FakeAsyncClient.sent["messages"] if m["role"] == "assistant")
    kinds = [b["type"] for b in assistant_turn["content"]]
    assert kinds == ["thinking", "text", "tool_use"], "signed reasoning leads the turn"
    assert assistant_turn["content"][0]["signature"] == "sig-1"
