"""`final_response` counts every refused call in the run, in `payload.denied_calls`.

Its `status` says whether the run *ended* on a refusal (#311): a run that recovered after one reads
`ok`, and that is deliberate. But the row could not tell that run from one that was never refused,
because the flag behind it is the last batch's and each clean round overwrote it. The count
accumulates across rounds, so both questions are answerable from the one row.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.config import Settings
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.hooks import get_agent_hooks
from felix.manifests.schema import ModelSpec
from felix.patterns.react import _ReactAgent
from felix.patterns.types import ChatMessage, InvokeInput, ToolCall
from felix.tools.types import Tool, ToolInput, ToolInvocationCtx, ToolOutput
from felix_ai.types import ModelChatResult, StreamDelta, TokenUsage


def _reply(content: str = "", *, call: str | None = None) -> ModelChatResult:
    calls = [ToolCall(id=call, name="lookup", args={})] if call else []
    return ModelChatResult(
        message=ChatMessage(role="assistant", content=content, tool_calls=calls),
        stop_reason="tool_use" if call else "end_turn",
        usage=TokenUsage(),
    )


class _Script:
    """Three rounds of tool calls, `c1` `c2` `c3`, then an answer."""

    model_id = "denials-fake"

    def __init__(self) -> None:
        self.replies = [_reply(call="c1"), _reply(call="c2"), _reply(call="c3"), _reply("done")]

    async def chat(self, messages: list[ChatMessage], tools: list, opts: Any = None) -> ModelChatResult:
        return self.replies.pop(0)

    async def stream_turn(self, messages: list[ChatMessage], tools: list, opts: Any = None) -> Any:
        result = self.replies.pop(0)
        if result.message.content:
            yield StreamDelta(kind="text", text=result.message.content)
        yield result


class _Lookup:
    transport = "local"

    async def execute(self, args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
        return "found"


def _agent() -> _ReactAgent:
    agent = _ReactAgent(
        tools=[Tool(name="lookup", description="d", args_schema=None, executor=_Lookup())],
        pattern="react",
        manifest_id="denials",
        manifest_version="1",
        system_prompt="s",
        model_spec=ModelSpec(id="denials-fake"),
        settings=None,
        recursion_limit=10,
    )
    model = _Script()
    agent._resolve_model = lambda _input: model  # type: ignore[method-assign]
    return agent


def _ctx() -> RequestContext:
    settings = Settings(
        allow_insecure=True, auth_mode="none", environment="development", database_url="memory://denials"
    )
    return RequestContext(settings=settings, auth=AuthContext(tenant_id="default", scopes=frozenset()))


async def _final_response(
    monkeypatch: pytest.MonkeyPatch, refuse: set[str], streaming: bool
) -> dict[str, Any]:
    import felix.patterns.react as react_mod

    finals: list[dict[str, Any]] = []
    monkeypatch.setattr(
        react_mod,
        "emit_agent_audit",
        lambda kind, **kw: finals.append(kw) if kind == "final_response" else None,
    )
    get_agent_hooks().register_before_tool(
        lambda call, ctx: {"block": True, "reason": "no"} if call["id"] in refuse else None
    )
    input = InvokeInput(messages=[ChatMessage(role="user", content="go")], thread_id="default:denials")
    async with async_run_with_context(_ctx()):
        if streaming:
            async for _ in _agent().stream_events(input):
                pass
        else:
            await _agent().invoke(input)
    assert len(finals) == 1, finals
    return finals[0]


@pytest.mark.parametrize("streaming", [False, True], ids=["invoke", "stream"])
@pytest.mark.parametrize(
    ("refuse", "status", "count"),
    [
        (set(), "ok", 0),
        ({"c1"}, "ok", 1),
        ({"c1", "c2"}, "ok", 2),
        ({"c1", "c3"}, "error", 2),
    ],
    ids=["none", "early-then-recovered", "two-early", "early-and-last"],
)
async def test_final_response_counts_every_refusal_and_its_status_is_the_last_rounds(
    monkeypatch: pytest.MonkeyPatch, refuse: set[str], status: str, count: int, streaming: bool
) -> None:
    row = await _final_response(monkeypatch, refuse, streaming)

    assert row["status"] == status, "status is whether the run ended on a refusal (#311)"
    assert row["payload"]["denied_calls"] == count, row["payload"]
