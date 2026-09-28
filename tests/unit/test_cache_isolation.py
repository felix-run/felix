"""One-off requests made during a turn shared the conversation's prompt cache.

Compaction summarises, memory extracts facts, inbound screening scores, branch
summarisation condenses an abandoned path — each is a side request issued in the middle
of somebody's turn, carrying a completely different prefix. They inherited the thread's
cache identity anyway:

* on an OpenAI-style endpoint `prompt_cache_key` defaults to `felix:<thread_id>`, so the
  side request churns the prefix the conversation had cached and the next real turn misses;
* on Anthropic the `cache_control: ephemeral` marker writes a fresh cache entry — billed
  above base input — for a prompt that is never read again.

`ModelChatOptions.isolate_cache` opts a request out of both.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from felix.patterns.model import (
    ModelChatOptions,
    apply_anthropic_thinking_cache,
    apply_openai_thinking_cache,
)


class _Spec:
    """A manifest model spec with caching switched on."""

    cache = True
    thinking_budget = None
    temperature = 0
    max_tokens = None


# --- the request builders -------------------------------------------------------


def test_anthropic_caches_the_system_block_by_default() -> None:
    body: dict[str, Any] = {"system": "you are felix", "max_tokens": 1024}
    apply_anthropic_thinking_cache(body, _Spec(), "claude-sonnet-4-5")
    assert isinstance(body["system"], list)
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_anthropic_writes_no_cache_entry_when_isolated() -> None:
    body: dict[str, Any] = {"system": "summarise this", "max_tokens": 1024}
    apply_anthropic_thinking_cache(body, _Spec(), "claude-sonnet-4-5", isolate_cache=True)
    assert body["system"] == "summarise this", "a one-shot prompt must not be cached"
    assert "cache_control" not in json.dumps(body)


def test_anthropic_isolation_leaves_tools_uncached() -> None:
    body: dict[str, Any] = {
        "system": "s",
        "max_tokens": 1024,
        "tools": [{"name": "a"}, {"name": "b"}],
    }
    apply_anthropic_thinking_cache(body, _Spec(), "claude-sonnet-4-5", isolate_cache=True)
    assert all("cache_control" not in t for t in body["tools"])


def test_openai_sets_a_cache_key_by_default() -> None:
    body: dict[str, Any] = {}
    apply_openai_thinking_cache(body, _Spec())
    assert "prompt_cache_key" in body


def test_openai_sends_no_cache_key_when_isolated() -> None:
    """Sharing the key is what churns the conversation's cached prefix."""
    body: dict[str, Any] = {}
    apply_openai_thinking_cache(body, _Spec(), isolate_cache=True)
    assert "prompt_cache_key" not in body


def test_isolation_does_not_disable_thinking() -> None:
    """Only caching is opted out of; a summariser may still need to reason."""

    class _Thinking(_Spec):
        thinking_budget = 8192

    body: dict[str, Any] = {}
    # The model is named because `reasoning_effort` is now gated on the catalog saying the
    # target accepts it; it used to be sent to every OpenAI-compatible endpoint, including
    # the ones that 400 on it.
    apply_openai_thinking_cache(body, _Thinking(), "gpt-4.1", isolate_cache=True)
    assert body["reasoning_effort"] == "medium"
    assert "prompt_cache_key" not in body


def test_isolation_is_off_by_default() -> None:
    assert ModelChatOptions().isolate_cache is False


# --- end to end through the client ----------------------------------------------


class _FakeResponse:
    status_code = 200
    text = ""
    headers: dict[str, str] = {}

    def json(self) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn", "usage": {}}


class _FakeClient:
    sent: dict[str, Any] = {}

    def __call__(self, *a: Any, **kw: Any) -> _FakeClient:
        return self

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def post(self, url: str, json: dict[str, Any] | None = None, headers: Any = None):
        type(self).sent = dict(json or {})
        return _FakeResponse()


def _client(monkeypatch: Any):
    import httpx
    from felix.config import Settings
    from felix_ai import AnthropicMessagesClient, ModelRoute

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient())
    return AnthropicMessagesClient(
        model_id="claude-sonnet-4-5",
        route=ModelRoute(provider="anthropic", model="claude-sonnet-4-5"),
        settings=Settings(allow_insecure=True, auth_mode="none", environment="development"),
        spec=_Spec(),
        base_url="https://example.invalid",
        api_key="k",
    )


@pytest.mark.asyncio
async def test_a_normal_turn_still_asks_for_caching(monkeypatch: Any) -> None:
    from felix.patterns.types import ChatMessage

    client = _client(monkeypatch)
    await client.chat([ChatMessage(role="system", content="s"), ChatMessage(role="user", content="hi")], [])
    assert "cache_control" in json.dumps(_FakeClient.sent)


@pytest.mark.asyncio
async def test_an_isolated_request_reaches_the_wire_uncached(monkeypatch: Any) -> None:
    from felix.patterns.types import ChatMessage

    client = _client(monkeypatch)
    await client.chat(
        [ChatMessage(role="system", content="summarise"), ChatMessage(role="user", content="log")],
        [],
        ModelChatOptions(isolate_cache=True),
    )
    assert "cache_control" not in json.dumps(_FakeClient.sent)


# --- the conversation, which is where the money is ------------------------------


def _conversation_body() -> dict[str, Any]:
    return {
        "system": "you are felix",
        "max_tokens": 1024,
        "tools": [{"name": "read_file"}],
        "messages": [
            {"role": "user", "content": "fix the bug"},
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "t1", "name": "read_file", "input": {}}],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "...50 KiB..."}],
            },
        ],
    }


def test_the_newest_message_carries_a_breakpoint_so_the_turns_before_it_are_read() -> None:
    """Without this the whole transcript is re-billed at full input price every turn: a
    212-call run metered 16.25M uncached input tokens against 2.7M cache reads, and 95% of
    its cost was that uncached input."""
    body = _conversation_body()
    apply_anthropic_thinking_cache(body, _Spec(), "claude-sonnet-4-5")

    tail = body["messages"][-1]["content"][-1]
    assert tail["cache_control"] == {"type": "ephemeral"}
    assert tail["tool_use_id"] == "t1", "the block is marked, not replaced"
    assert "cache_control" not in json.dumps(body["messages"][:-1]), "one breakpoint, at the end"


def test_a_string_message_becomes_a_text_block_to_carry_the_marker() -> None:
    body: dict[str, Any] = {
        "system": "s",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": "hi"}],
    }
    apply_anthropic_thinking_cache(body, _Spec(), "claude-sonnet-4-5")

    assert body["messages"][-1]["content"] == [
        {"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}
    ]


def test_an_isolated_request_caches_no_part_of_the_conversation() -> None:
    body = _conversation_body()
    apply_anthropic_thinking_cache(body, _Spec(), "claude-sonnet-4-5", isolate_cache=True)

    assert "cache_control" not in json.dumps(body)


def test_a_thinking_block_is_left_alone() -> None:
    """Signed reasoning is replayed verbatim and may not carry a breakpoint; marking it
    would fail the request rather than save anything."""
    body: dict[str, Any] = {
        "system": "s",
        "max_tokens": 1024,
        "messages": [
            {"role": "assistant", "content": [{"type": "thinking", "thinking": "...", "signature": "x"}]}
        ],
    }
    apply_anthropic_thinking_cache(body, _Spec(), "claude-sonnet-4-5")

    assert "cache_control" not in json.dumps(body["messages"])


def test_never_more_breakpoints_than_anthropic_allows() -> None:
    body = _conversation_body()
    apply_anthropic_thinking_cache(body, _Spec(), "claude-sonnet-4-5")

    assert json.dumps(body).count('"cache_control"') <= 4, "system, tools, conversation — and the cap is four"


def test_an_empty_conversation_is_left_alone() -> None:
    for messages in ([], [{"role": "user", "content": ""}], [{"role": "user", "content": []}]):
        body: dict[str, Any] = {"system": "s", "max_tokens": 1024, "messages": messages}
        apply_anthropic_thinking_cache(body, _Spec(), "claude-sonnet-4-5")
        assert "cache_control" not in json.dumps(body["messages"])
