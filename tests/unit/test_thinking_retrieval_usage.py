"""thinking_budget / cache request shaping, tool retrieval, OpenAI usage."""

from __future__ import annotations

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
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 8000}
    assert body["prompt_cache_key"] == "felix"


def test_anthropic_thinking_and_cache() -> None:
    spec = ModelSpec(thinking_budget=5000, cache=True)
    body: dict = {
        "model": "claude",
        "system": "You are Felix.",
        "temperature": 0,
        "max_tokens": 1024,
        "tools": [{"name": "calculator", "input_schema": {}}],
    }
    apply_anthropic_thinking_cache(body, spec)
    assert body["thinking"]["budget_tokens"] == 5000
    assert body["temperature"] == 1
    assert body["max_tokens"] > 5000
    assert body["system"][0]["cache_control"]["type"] == "ephemeral"
    assert body["tools"][-1]["cache_control"]["type"] == "ephemeral"


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
    selected = select_tools(
        tools, messages, ToolsRetrievalSpec(enabled=True, top_k=2)
    )
    names = [t.name for t in selected]
    assert "calculator" in names  # already used
    assert "web_search" in names
    assert "unused_blob" not in names
    assert len(selected) == 2


def test_select_tools_disabled_returns_all() -> None:
    async def _h(_a=None, _c=None):
        return "ok"

    tools = [
        define_tool(name=f"t{i}", description=f"tool {i}", handler=_h) for i in range(5)
    ]
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
        limit_state=LimitState(tokens_input=12, tokens_output=8),
    )
    usage = _usage_payload(ctx)
    assert usage == {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}
