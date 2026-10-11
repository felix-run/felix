"""`done` carries the final model call's usage, so a client can meter a turn as it ends.

Before this, no frame on `POST /chat/stream` carried usage. `on_chain_end` holds the
in-process `InvokeOutput`, which reaches the wire as its `repr` through `default=str`, and
`done` had no usage field, so a client learned a turn's token counts only by re-reading
the session snapshot. The block is the one `_append_produced` already stores on the final
message, so the stream and the snapshot report the same numbers.
"""

from __future__ import annotations

from typing import Any

from felix.config import Settings
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.manifests.schema import ModelSpec
from felix.patterns.model import StreamDelta
from felix.patterns.react import _ReactAgent
from felix.patterns.types import ChatMessage, Event, InvokeInput, ToolCall
from felix.tools.types import Tool, ToolInput, ToolInvocationCtx, ToolOutput
from felix_ai.types import ModelChatResult, TokenUsage


class _Echo:
    transport = "local"

    async def execute(self, args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
        return "ran"


class _ToolThenAnswer:
    """Calls a tool, then answers. Each call reports different usage, so the test can
    tell which call's block reached `done`."""

    model_id = "test-model"

    def __init__(self, final_usage: TokenUsage) -> None:
        self.turns = 0
        self.final_usage = final_usage

    async def chat(self, messages: list[ChatMessage], tools: list, opts: Any = None) -> ModelChatResult:
        self.turns += 1
        if self.turns == 1:
            return ModelChatResult(
                message=ChatMessage(
                    role="assistant", content="", tool_calls=[ToolCall(id="c1", name="echo", args={})]
                ),
                stop_reason="tool_use",
                usage=TokenUsage(input=500, output=20),
            )
        return ModelChatResult(
            message=ChatMessage(role="assistant", content="done"),
            stop_reason="end_turn",
            usage=self.final_usage,
        )

    async def stream_turn(self, messages: list[ChatMessage], tools: list, opts: Any = None) -> Any:
        result = await self.chat(messages, tools, opts)
        if result.message.content:
            yield StreamDelta(kind="text", text=result.message.content)
        yield result


def _settings(name: str) -> Settings:
    return Settings(
        allow_insecure=True,
        auth_mode="none",
        environment="development",
        database_url=f"memory://{name}",
        object_store="memory",
        host="127.0.0.1",
    )


async def _done(model: Any, name: str) -> Event:
    settings = _settings(name)
    agent = _ReactAgent(
        tools=[Tool(name="echo", description="d", args_schema=None, executor=_Echo())],
        pattern="react",
        manifest_id="test-manifest",
        manifest_version="1.0.0",
        system_prompt="Test",
        model_spec=ModelSpec(id="test-model"),
        settings=settings,
        recursion_limit=10,
    )
    agent._resolve_model = lambda _input: model  # type: ignore[method-assign]
    ctx = RequestContext(
        settings=settings, auth=AuthContext(tenant_id="default"), manifest_id="test-manifest"
    )
    events: list[Event] = []
    async with async_run_with_context(ctx):
        async for item in agent.stream_events(
            InvokeInput(messages=[ChatMessage(role="user", content="go")], tenant_id="default")
        ):
            if isinstance(item, Event):
                events.append(item)
    (done,) = [e for e in events if e.event == "done"]
    return done


async def test_done_carries_the_final_calls_usage_including_cache() -> None:
    """The final call's block, not the tool call's and not a sum.

    The final call's prompt already contains everything before it, so its block is how
    full the context is. Cached tokens are reported apart from `input`, which is why a
    client reading `input` alone undercounts a cached prompt by orders of magnitude.
    """
    model = _ToolThenAnswer(TokenUsage(input=3, output=7, cache_creation=1025, cache_read=400))
    done = await _done(model, "done-usage")
    usage = done.data["usage"]
    assert (usage["input"], usage["output"], usage["cacheWrite"], usage["cacheRead"]) == (3, 7, 1025, 400)
    assert usage["totalTokens"] == 3 + 7 + 1025 + 400


async def test_done_omits_usage_when_the_provider_reported_none() -> None:
    """Absent, not zeros: a provider that reports nothing has not reported an empty context."""
    model = _ToolThenAnswer(TokenUsage())
    model.turns = 1  # straight to the answer
    done = await _done(model, "done-no-usage")
    assert "usage" not in done.data
