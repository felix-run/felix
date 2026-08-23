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
    apply_openai_thinking_cache(body, _Thinking(), isolate_cache=True)
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
    from felix.config import Settings
    from felix.patterns import model as model_mod

    monkeypatch.setattr(model_mod.httpx, "AsyncClient", _FakeClient())
    return model_mod._HttpModelClient(
        model_id="claude-sonnet-4-5",
        route=model_mod.ModelRoute(provider="anthropic", model="claude-sonnet-4-5"),
        settings=Settings(allow_insecure=True, auth_mode="none", environment="development"),
        spec=_Spec(),
        base_url="https://example.invalid",
        api_key="k",
        style="anthropic",
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
