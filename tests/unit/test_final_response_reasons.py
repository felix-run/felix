"""A failed `final_response` says why, and a run that dies still writes one.

#543: the row's `status` was `error` for a fatal tool and for a refusal alike, with nothing on it
to tell the two apart. `payload.reasons` names each cause, and a fatal one names the call, whose
own `tool_call` row carries the same `error_code` — a tool that *raised* wrote no row at all.

#305: a model call that raised (`httpx.ReadTimeout`, which the transport deliberately does not
retry) propagated out of the turn loop past the code that writes `final_response`, so the run
read in the audit log as a `user_input` that never finished.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from felix.config import Settings
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.hooks import get_agent_hooks, reset_agent_hooks
from felix.manifests.schema import ModelSpec
from felix.patterns.react import _ReactAgent
from felix.patterns.types import ChatMessage, InvokeInput, ToolCall
from felix.tools.types import Tool, ToolInput, ToolInvocationCtx, ToolOutput
from felix_ai.types import ModelChatResult, StreamDelta, TokenUsage


@pytest.fixture(autouse=True)
def _clean_hooks():
    reset_agent_hooks()
    yield
    reset_agent_hooks()


def _reply(content: str = "", *, call: str | None = None) -> ModelChatResult:
    calls = [ToolCall(id=call, name="lookup", args={})] if call else []
    return ModelChatResult(
        message=ChatMessage(role="assistant", content=content, tool_calls=calls),
        stop_reason="tool_use" if call else "end_turn",
        usage=TokenUsage(),
    )


class _Script:
    """Replies in order; an exception in the list is raised in its place."""

    model_id = "reasons-fake"

    def __init__(self, replies: list[ModelChatResult | BaseException]) -> None:
        self.replies = replies

    def _next(self) -> ModelChatResult:
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    async def chat(self, messages: list[ChatMessage], tools: list, opts: Any = None) -> ModelChatResult:
        return self._next()

    async def stream_turn(self, messages: list[ChatMessage], tools: list, opts: Any = None) -> Any:
        result = self._next()
        if result.message.content:
            yield StreamDelta(kind="text", text=result.message.content)
        yield result


class _Lookup:
    transport = "local"

    def __init__(self, raises: Exception | None) -> None:
        self.raises = raises

    async def execute(self, args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
        if self.raises is not None:
            raise self.raises
        return "found"


def _agent(replies: list[ModelChatResult | BaseException], *, raises: Exception | None = None) -> _ReactAgent:
    tool = Tool(name="lookup", description="d", args_schema=None, executor=_Lookup(raises), fatal=True)
    agent = _ReactAgent(
        tools=[tool],
        pattern="react",
        manifest_id="reasons",
        manifest_version="1",
        system_prompt="s",
        model_spec=ModelSpec(id="reasons-fake"),
        settings=None,
        recursion_limit=10,
    )
    model = _Script(replies)
    agent._resolve_model = lambda _input: model  # type: ignore[method-assign]
    return agent


def _ctx() -> RequestContext:
    settings = Settings(
        allow_insecure=True, auth_mode="none", environment="development", database_url="memory://reasons"
    )
    return RequestContext(settings=settings, auth=AuthContext(tenant_id="default", scopes=frozenset()))


async def _rows(
    monkeypatch: pytest.MonkeyPatch,
    agent: _ReactAgent,
    *,
    streaming: bool,
    rows: list[tuple[str, dict[str, Any]]] | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    """Every audit row the run wrote, from both the loop and the tool runner, into `rows`."""
    import felix.patterns.react as react_mod
    import felix.patterns.tool_runner as runner_mod

    rows = [] if rows is None else rows
    for mod in (react_mod, runner_mod):
        monkeypatch.setattr(mod, "emit_agent_audit", lambda kind, **kw: rows.append((kind, kw)))
    input = InvokeInput(messages=[ChatMessage(role="user", content="go")], thread_id="default:reasons")
    async with async_run_with_context(_ctx()):
        if streaming:
            async for _ in agent.stream_events(input):
                pass
        else:
            await agent.invoke(input)
    return rows


def _final(rows: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    finals = [kw for kind, kw in rows if kind == "final_response"]
    assert len(finals) == 1, finals
    return finals[0]


STREAMING = pytest.mark.parametrize("streaming", [False, True], ids=["invoke", "stream"])


@STREAMING
async def test_a_clean_run_carries_no_reasons(monkeypatch: pytest.MonkeyPatch, streaming: bool) -> None:
    row = _final(await _rows(monkeypatch, _agent([_reply(call="c1"), _reply("done")]), streaming=streaming))

    assert row["status"] == "ok"
    assert "reasons" not in row["payload"], row["payload"]


@STREAMING
async def test_a_fatal_tool_names_the_call_and_its_row_carries_the_code(
    monkeypatch: pytest.MonkeyPatch, streaming: bool
) -> None:
    agent = _agent(
        [_reply(call="c1"), _reply("never reached")], raises=PermissionError("forbidden: /srv/secret")
    )
    rows = await _rows(monkeypatch, agent, streaming=streaming)
    row = _final(rows)

    assert row["status"] == "error"
    assert row["payload"]["reasons"] == ["fatal"]
    assert row["payload"]["fatal_call"] == {"tool_call_id": "c1", "error_code": "permission_denied"}
    calls = [kw for kind, kw in rows if kind == "tool_call"]
    assert [(kw["status"], kw["payload"]["tool_call_id"], kw["payload"]["error_code"]) for kw in calls] == [
        ("error", "c1", "permission_denied")
    ], "the call that ended the run has its own row to lead to"
    assert "/srv/secret" not in repr(rows), "the exception's message stays out of the audit log"


@STREAMING
async def test_a_refusal_in_the_last_round_reads_denied(
    monkeypatch: pytest.MonkeyPatch, streaming: bool
) -> None:
    get_agent_hooks().register_before_tool(lambda call, ctx: {"block": True, "reason": "no"})
    rows = await _rows(monkeypatch, _agent([_reply(call="c1"), _reply("could not")]), streaming=streaming)
    row = _final(rows)

    assert row["status"] == "error"
    assert row["payload"]["reasons"] == ["denied"]
    assert "fatal_call" not in row["payload"]


@STREAMING
async def test_read_timeout_writes_final_response(monkeypatch: pytest.MonkeyPatch, streaming: bool) -> None:
    agent = _agent([httpx.ReadTimeout("upstream at 10.0.0.7 timed out")])
    rows: list[tuple[str, dict[str, Any]]] = []
    with pytest.raises(httpx.ReadTimeout):
        await _rows(monkeypatch, agent, streaming=streaming, rows=rows)
    row = _final(rows)

    assert row["status"] == "error"
    assert row["payload"]["reasons"] == ["exception"]
    assert row["payload"]["error_type"] == "ReadTimeout"
    assert "10.0.0.7" not in repr(row), "the exception's message stays out of the audit log"
