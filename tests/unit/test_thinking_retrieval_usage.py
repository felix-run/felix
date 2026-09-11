"""thinking_budget / cache request shaping, tool retrieval, OpenAI usage."""

from __future__ import annotations

import pytest
from felix.context import AuthContext, LimitState, RequestContext
from felix.manifests.schema import ModelSpec, ToolsRetrievalSpec
from felix.patterns.model import (
    apply_anthropic_thinking_cache,
    apply_openai_thinking_cache,
    reasoning_effort_from_budget,
)
from felix.patterns.types import ChatMessage, ToolCall
from felix.tools.retrieval import select_tools
from felix.tools.types import define_tool


def test_reasoning_effort_buckets() -> None:
    assert reasoning_effort_from_budget(1024) == "low"
    assert reasoning_effort_from_budget(8000) == "medium"
    assert reasoning_effort_from_budget(20000) == "high"


def test_openai_thinking_and_cache() -> None:
    spec = ModelSpec(thinking_budget=8000, cache=True)
    body: dict = {"model": "gpt-4.1", "messages": []}
    apply_openai_thinking_cache(body, spec)
    assert body["reasoning_effort"] == "medium"
    assert body["prompt_cache_key"] == "felix"
    # `thinking` is an Anthropic field and used to be sent here too, on the grounds that a
    # LiteLLM proxy to Anthropic honours it — but the same body goes to api.openai.com and
    # to every self-hosted gateway, and a server that validates its schema rejects the
    # unknown key. It now goes only to models whose native wire format is Anthropic's.
    assert "thinking" not in body


@pytest.mark.asyncio
async def test_openai_cache_key_uses_thread_id() -> None:
    from felix.config import Settings
    from felix.context import AuthContext, RequestContext, async_run_with_context

    spec = ModelSpec(cache=True)
    body: dict = {"model": "gpt-4.1", "messages": []}
    ctx = RequestContext(
        settings=Settings(allow_insecure=True, database_url="memory://c", object_store="memory"),
        auth=AuthContext(),
        thread_id="tenant:abc",
    )
    async with async_run_with_context(ctx):
        apply_openai_thinking_cache(body, spec)
    assert body["prompt_cache_key"] == "felix:tenant:abc"


def _thinking_body(model: str) -> dict:
    body: dict = {
        "model": model,
        "system": "You are Felix.",
        "temperature": 0,
        "max_tokens": 1024,
        "tools": [{"name": "calculator", "input_schema": {}}],
    }
    apply_anthropic_thinking_cache(body, ModelSpec(thinking_budget=5000, cache=True), model)
    return body


def test_anthropic_thinking_legacy_model_uses_budget_tokens() -> None:
    """Pre-4.6 models still take a fixed thinking budget."""
    body = _thinking_body("claude-sonnet-4-5")
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 5000}
    assert body["temperature"] == 1
    assert body["max_tokens"] > 5000


def test_anthropic_thinking_current_model_uses_adaptive() -> None:
    """`budget_tokens` and `temperature` are both removed on the current generation and
    return HTTP 400, so emitting them broke thinking against every current model."""
    body = _thinking_body("claude-opus-5")
    assert body["thinking"] == {"type": "adaptive"}
    assert "budget_tokens" not in body["thinking"]
    assert "temperature" not in body, "sampling params are rejected on 4.6+"
    assert body["output_config"]["effort"] == "medium"


def test_anthropic_cache_control_is_applied_either_way() -> None:
    for model in ("claude-sonnet-4-5", "claude-opus-5"):
        body = _thinking_body(model)
        assert body["system"][0]["cache_control"]["type"] == "ephemeral"
        assert body["tools"][-1]["cache_control"]["type"] == "ephemeral"


def test_max_tokens_is_clamped_to_the_model_ceiling() -> None:
    body: dict = {"model": "claude-haiku-4-5", "max_tokens": 500_000}
    apply_anthropic_thinking_cache(body, None, "claude-haiku-4-5")
    assert body["max_tokens"] == 64_000


def test_select_tools_keeps_used_and_ranks() -> None:
    async def _h(_a=None, _c=None):
        return "ok"

    calc = define_tool(name="calculator", description="arithmetic expressions", handler=_h)
    search = define_tool(name="web_search", description="search the public web", handler=_h)
    weather = define_tool(name="weather", description="forecast temperature rain", handler=_h)
    extra = define_tool(name="unused_blob", description="zzzz", handler=_h)
    tools = [calc, search, weather, extra]
    messages = [
        ChatMessage(role="user", content="search the web for felix"),
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="1", name="calculator", args={})],
        ),
    ]
    selected = select_tools(tools, messages, ToolsRetrievalSpec(enabled=True, top_k=2))
    names = [t.name for t in selected]
    assert "calculator" in names  # already used
    assert "web_search" in names
    assert "unused_blob" not in names
    assert len(selected) == 2


def test_select_tools_disabled_returns_all() -> None:
    async def _h(_a=None, _c=None):
        return "ok"

    tools = [define_tool(name=f"t{i}", description=f"tool {i}", handler=_h) for i in range(5)]
    out = select_tools(
        tools,
        [ChatMessage(role="user", content="hi")],
        ToolsRetrievalSpec(enabled=False, top_k=1),
    )
    assert len(out) == 5


def test_usage_payload_from_limit_state() -> None:
    from felix.config import Settings
    from felix_api.routes.openai_compat import _usage_payload

    ctx = RequestContext(
        settings=Settings(database_url="memory://u", object_store="memory", allow_insecure=True),
        auth=AuthContext(),
        limit_state=LimitState(tokens_input=12, tokens_output=8, tokens_cached=5),
    )
    usage = _usage_payload(ctx)
    assert usage == {
        "prompt_tokens": 12,
        "completion_tokens": 8,
        "total_tokens": 20,
        "prompt_tokens_details": {"cached_tokens": 5},
    }
