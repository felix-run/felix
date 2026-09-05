"""The reply-path controls: final-response judges and PII guardrails on the agent's reply.

`wrap_final_response_judges` passed events straight through on `stream_events`, so the
only outbound model-call control was inert on the primary chat surface, and
`apply_guardrails` wrapped tools only, so `guardrails.targets: [input, output]` scrubbed
user input and tool output and let the reply through untouched. `ReplyControlsAgent`
wraps the agent: on `invoke` the reply is screened before it returns; on a stream every
frame carrying reply text is held until the run ends and released screened, while
structural frames stream as they happen.

The fake pattern here streams the way the react loop does: each text delta twice — as
`text_delta` and inside a `session_progress` envelope — a thinking delta, a tool event,
possibly several assistant turns, then `on_chain_end`, `done`, and the output itself.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from felix.governance.reply import (
    JUDGE_DENIED_PREFIX,
    PII_BLOCKED_REPLY,
    ReplyControlsAgent,
    carries_reply_text,
    reply_controls_enabled,
    reply_pii_enabled,
)
from felix.manifests.builder import apply_reply_controls
from felix.manifests.schema import Guardrails, JudgeRule
from felix.patterns.types import ChatMessage, Event, InvokeInput, InvokeOutput

EMAIL = "alice@example.com"
PII = f"Reach me at {EMAIL} any time."
THINKING = "let me think about the address"


def _delta(text: str) -> list[Event]:
    return [
        Event(event="text_delta", data={"chunk": {"content": text}, "delta": text}),
        Event(
            event="session_progress",
            data={"progress": {"type": "assistant_delta", "kind": "text", "delta": text}},
        ),
    ]


class _Inner:
    """One or more assistant turns; every turn but the last is followed by a tool call."""

    tools: list[Any] = []
    pattern = "react"
    manifest_id = "m"
    manifest_version = "1"

    def __init__(self, *turns: str) -> None:
        self.turns = list(turns)

    def _output(self) -> InvokeOutput:
        messages: list[ChatMessage] = [ChatMessage(role="user", content="hi")]
        for turn in self.turns[:-1]:
            messages.append(ChatMessage(role="assistant", content=turn))
            messages.append(ChatMessage(role="tool", content="42", tool_call_id="c1"))
        final = ChatMessage(role="assistant", content=self.turns[-1])
        messages.append(final)
        return InvokeOutput(messages=messages, final=final)

    async def invoke(self, input: InvokeInput) -> InvokeOutput:
        return self._output()

    async def stream_events(self, input: InvokeInput) -> AsyncIterator[Any]:
        yield Event(event="session_progress", data={"phase": "turn"})
        yield Event(event="thinking_delta", data={"chunk": {"content": THINKING}, "delta": THINKING})
        for turn in self.turns[:-1]:
            for ev in _delta(turn):
                yield ev
            yield Event(event="tool_call", data={"name": "calculator"})
        last = self.turns[-1]
        half = len(last) // 2
        for ev in [*_delta(last[:half]), *_delta(last[half:])]:
            yield ev
        out = self._output()
        yield Event(event="on_chain_end", data={"output": out})
        yield Event(
            event="done",
            data={"final": out.final.model_dump(), "messages": [m.model_dump() for m in out.messages]},
        )
        yield out


def _pii(**kw: Any) -> Guardrails:
    return Guardrails(providers=["pii"], **kw)


def _judge(threshold: float = 1.0) -> Guardrails:
    return Guardrails(
        judges=[JudgeRule(name="long", criteria="min_chars:200", threshold=threshold, final_response=True)]
    )


def _wrap(inner: Any, guardrails: Guardrails) -> ReplyControlsAgent:
    return ReplyControlsAgent(inner, guardrails, "m")


async def _collect(agent: Any) -> list[Any]:
    return [
        item
        async for item in agent.stream_events(InvokeInput(messages=[ChatMessage(role="user", content="hi")]))
    ]


def _events(items: list[Any], name: str) -> list[Event]:
    return [i for i in items if isinstance(i, Event) and i.event == name]


def _streamed_text(items: list[Any]) -> str:
    """Everything a client would render as the reply, from every frame that can carry it.

    Spelled out rather than asking `carries_reply_text`: the assertion must not share
    the production predicate's blind spots, or weakening the predicate blinds it too."""
    out = ""
    for i in items:
        if not isinstance(i, Event):
            continue
        if i.event in ("text_delta", "on_chat_model_stream"):
            out += i.text
        elif i.event == "session_progress":
            progress = i.data.get("progress") or {}
            if progress.get("type") == "assistant_delta" and progress.get("kind", "text") == "text":
                out += str(progress.get("delta") or "")
    return out


def _done(items: list[Any]) -> dict[str, Any]:
    (done,) = _events(items, "done")
    return done.data


def _no_email_anywhere(items: list[Any]) -> None:
    assert EMAIL not in _streamed_text(items), "the raw reply reached a client-facing frame"
    done = _done(items)
    assert EMAIL not in done["final"]["content"]
    assert all(EMAIL not in (m.get("content") or "") for m in done["messages"]), done["messages"]
    (chain_end,) = _events(items, "on_chain_end")
    output = chain_end.data["output"]
    assert EMAIL not in output.final.content
    assert all(EMAIL not in (m.content or "") for m in output.messages)
    trailing = next(i for i in items if isinstance(i, InvokeOutput))
    assert EMAIL not in trailing.final.content
    assert all(EMAIL not in (m.content or "") for m in trailing.messages)


# --- which manifests enable it ---------------------------------------------------------


def test_reply_pii_follows_the_targets() -> None:
    assert reply_pii_enabled(None) is False
    assert reply_pii_enabled(Guardrails()) is False, "no provider, nothing to apply"
    assert reply_pii_enabled(_pii()) is True, "the default targets include output"
    assert reply_pii_enabled(_pii(targets=["input", "output"])) is True
    assert reply_pii_enabled(_pii(targets=["final_response"])) is True
    assert reply_pii_enabled(_pii(targets=["input"])) is False


def test_the_builder_seam_wraps_only_when_a_reply_control_is_configured() -> None:
    inner = _Inner("fine")
    assert apply_reply_controls(inner, None, "m") is inner
    assert apply_reply_controls(inner, Guardrails(), "m") is inner
    assert apply_reply_controls(inner, _pii(targets=["input"]), "m") is inner
    assert isinstance(apply_reply_controls(inner, _pii(), "m"), ReplyControlsAgent)
    assert isinstance(apply_reply_controls(inner, _judge(), "m"), ReplyControlsAgent)
    assert reply_controls_enabled(_pii(targets=["final_response"])) is True


def test_which_frames_carry_reply_text() -> None:
    assert all(carries_reply_text(ev) for ev in _delta("x"))
    assert carries_reply_text(Event(event="on_chat_model_stream", data={"chunk": {"content": "x"}}))
    assert not carries_reply_text(Event(event="thinking_delta", data={"delta": "x"}))
    assert not carries_reply_text(Event(event="session_progress", data={"phase": "turn"}))
    assert not carries_reply_text(
        Event(
            event="session_progress",
            data={"progress": {"type": "assistant_delta", "kind": "thinking", "delta": "x"}},
        )
    )
    assert not carries_reply_text(Event(event="tool_call", data={}))


# --- invoke ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invoke_redacts_pii_from_every_assistant_message() -> None:
    out = await _wrap(_Inner(PII, "done: " + PII), _pii()).invoke(InvokeInput(messages=[]))
    assert EMAIL not in out.final.content
    assert all(EMAIL not in (m.content or "") for m in out.messages)
    assert out.messages[-1] is out.final, "the final is the last message, by identity"
    assert out.messages[2].role == "tool", "non-assistant messages are untouched"


@pytest.mark.asyncio
async def test_invoke_blocks_the_reply_when_block_on_match_is_set() -> None:
    out = await _wrap(_Inner(PII), _pii(block_on_match=True)).invoke(InvokeInput(messages=[]))
    assert out.final.content == PII_BLOCKED_REPLY


@pytest.mark.asyncio
async def test_invoke_judge_denies_a_reply_below_threshold() -> None:
    out = await _wrap(_Inner("short"), _judge()).invoke(InvokeInput(messages=[]))
    assert out.final.content.startswith(f"{JUDGE_DENIED_PREFIX} long")


@pytest.mark.asyncio
async def test_invoke_passes_a_clean_reply_through_unchanged() -> None:
    inner = _Inner("nothing to see here")
    out = await _wrap(inner, _pii()).invoke(InvokeInput(messages=[]))
    assert out.final.content == "nothing to see here"


# --- stream: the branch that was inert ---------------------------------------------------


@pytest.mark.asyncio
async def test_stream_releases_the_reply_redacted_and_patches_every_terminal_frame() -> None:
    items = await _collect(_wrap(_Inner(PII), _pii()))
    _no_email_anywhere(items)
    assert _streamed_text(items), "the redacted reply is still delivered"


@pytest.mark.asyncio
async def test_stream_screens_every_assistant_turn_not_only_the_final() -> None:
    """A preamble before a tool call is a reply the model already made. It went out raw
    when only `final.content` was screened."""
    items = await _collect(_wrap(_Inner(PII, "all clean now"), _pii()))
    _no_email_anywhere(items)
    assert "all clean now" in _streamed_text(items)


@pytest.mark.asyncio
async def test_stream_holds_reply_text_in_every_envelope_but_not_structure() -> None:
    """The reply rides `text_delta` and the `session_progress` envelope; both wait. The
    phase frame, thinking and the tool event arrive as they happen, before any reply."""
    items = await _collect(_wrap(_Inner(PII, "fine"), _pii()))
    kinds = [i.event for i in items if isinstance(i, Event)]
    first_reply = next(k for k, i in enumerate(items) if isinstance(i, Event) and carries_reply_text(i))
    assert kinds[:3] == ["session_progress", "thinking_delta", "tool_call"]
    assert all(not carries_reply_text(i) for i in items[:first_reply])
    assert kinds[-2:] == ["on_chain_end", "done"]
    assert THINKING in "".join(i.text for i in _events(items, "thinking_delta"))


@pytest.mark.asyncio
async def test_stream_judge_denial_replaces_the_reply_and_never_leaks_it() -> None:
    items = await _collect(_wrap(_Inner("short"), _judge()))
    assert "short" not in _streamed_text(items)
    assert _streamed_text(items).startswith(f"{JUDGE_DENIED_PREFIX} long")
    assert _done(items)["final"]["content"].startswith(f"{JUDGE_DENIED_PREFIX} long")


@pytest.mark.asyncio
async def test_a_judge_denial_withholds_earlier_turns_too() -> None:
    """The judge scored the final reply; a preamble before the tool calls was never
    judged, so the denial is the whole reply and the preamble does not ship."""
    items = await _collect(_wrap(_Inner("preamble before tools", "short"), _judge()))
    assert "preamble" not in _streamed_text(items)
    assert _streamed_text(items).startswith(JUDGE_DENIED_PREFIX)
    assert all("preamble" not in (m.get("content") or "") for m in _done(items)["messages"])
    out = await _wrap(_Inner("preamble before tools", "short"), _judge()).invoke(InvokeInput(messages=[]))
    assert all("preamble" not in (m.content or "") for m in out.messages)


def test_final_response_alone_no_longer_wraps_tools() -> None:
    """`output` is tool output and the reply; `final_response` is the reply alone. A tool
    wrapped under `final_response` would redact tool output the manifest never asked
    about — and the reply it did ask about is this module's job."""
    from felix.manifests.builder import apply_guardrails
    from felix.tools.types import define_tool

    async def _echo(args: dict) -> str:
        return str(args.get("text") or "")

    tool = define_tool(name="echo", description="echo", handler=_echo)
    assert apply_guardrails([tool], _pii(targets=["final_response"]), "m")[0] is tool
    assert apply_guardrails([tool], _pii(targets=["input"]), "m")[0] is tool
    assert apply_guardrails([tool], _pii(targets=["output"]), "m")[0] is not tool
    assert apply_guardrails([tool], _pii(), "m")[0] is not tool, "the default targets include output"


@pytest.mark.asyncio
async def test_v1_streams_only_reply_text_as_assistant_content() -> None:
    """The OpenAI wire has no reasoning channel: `thinking_delta` has a `.text`, and the
    route used to emit every event's text as `delta.content` — reasoning rendered as the
    answer, past the reply controls, which hold reply text only."""
    import felix_api.routes.openai_compat as oc
    from felix.config import Settings
    from felix_api.app import create_app
    from httpx import ASGITransport, AsyncClient

    settings = Settings(  # type: ignore[arg-type]
        database_url="memory://v1-reply",
        object_store="memory",
        allow_insecure=True,
        auth_mode="none",
        host="127.0.0.1",
        environment="development",
    )
    app = create_app(settings=settings, plugins=[])

    async def _agent(*a: Any, **k: Any) -> Any:
        return _Inner("the answer")

    orig = oc.build_tenant_agent
    oc.build_tenant_agent = _agent  # type: ignore[assignment]
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/v1/chat/completions",
                json={"model": "quick", "messages": [{"role": "user", "content": "hi"}], "stream": True},
            )
    finally:
        oc.build_tenant_agent = orig  # type: ignore[assignment]
    assert resp.status_code == 200
    import json

    deltas = [
        json.loads(line[len("data: ") :])["choices"][0]["delta"].get("content", "")
        for line in resp.text.splitlines()
        if line.startswith("data: {")
    ]
    assert "".join(deltas) == "the answer"
    assert THINKING not in resp.text


@pytest.mark.asyncio
async def test_stream_passes_a_clean_reply_through_as_the_original_frames() -> None:
    reply = "nothing to see here"
    items = await _collect(_wrap(_Inner(reply), _pii()))
    deltas = [i.text for i in _events(items, "text_delta")]
    assert deltas == [reply[: len(reply) // 2], reply[len(reply) // 2 :]], "the original frames, unmerged"
    assert len(_events(items, "session_progress")) == 3, "the phase frame and both envelopes"
    assert _done(items)["final"]["content"] == reply


@pytest.mark.asyncio
async def test_a_reply_with_no_text_produces_no_phantom_frame() -> None:
    class _ToolOnly(_Inner):
        def _output(self) -> InvokeOutput:
            final = ChatMessage(role="assistant", content="", tool_calls=[])
            return InvokeOutput(messages=[final], final=final)

        async def stream_events(self, input: InvokeInput) -> AsyncIterator[Any]:
            out = self._output()
            yield Event(event="on_chain_end", data={"output": out})
            yield Event(
                event="done", data={"final": out.final.model_dump(), "messages": [out.final.model_dump()]}
            )
            yield out

    items = await _collect(_wrap(_ToolOnly(""), _pii(block_on_match=True)))
    assert _streamed_text(items) == ""
    assert [i.event for i in items if isinstance(i, Event)] == ["on_chain_end", "done"]


@pytest.mark.asyncio
async def test_a_stream_without_terminal_frames_still_releases_held_text() -> None:
    class _Bare(_Inner):
        async def stream_events(self, input: InvokeInput) -> AsyncIterator[Any]:
            for ev in _delta(self.turns[0]):
                yield ev

    items = await _collect(_wrap(_Bare(PII), _pii()))
    assert items
    assert EMAIL not in _streamed_text(items)
    assert _streamed_text(items)


@pytest.mark.asyncio
async def test_the_controls_emit_audit_events() -> None:
    from felix.audit import store as audit_store
    from felix.config import Settings
    from felix.context import AuthContext, RequestContext, async_run_with_context

    audit_store._pending.reset_for_tests()
    settings = Settings(  # type: ignore[arg-type]
        database_url="memory://reply-controls",
        object_store="memory",
        allow_insecure=True,
        auth_mode="none",
        host="127.0.0.1",
        environment="development",
    )
    ctx = RequestContext(settings=settings, auth=AuthContext(tenant_id="t"), manifest_id="m")
    async with async_run_with_context(ctx):
        await _wrap(_Inner(PII), _pii()).invoke(InvokeInput(messages=[]))
        await _wrap(_Inner("short"), _judge()).invoke(InvokeInput(messages=[]))
    types = [(e["event_type"], e.get("status")) for e in audit_store._pending]
    assert ("guardrails_reply", "redacted") in types
    assert ("judge_deny", "denied") in types


# --- through the compiler ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_governed_manifest_streams_its_reply_redacted_end_to_end() -> None:
    """Through `build_tenant_agent` and the real react loop, with the manifest that
    `governed.yaml` models: `providers: [pii], targets: [input, output]` and no judges.
    Pins the builder call site — a wrapper nobody applies is the shape this fixes."""
    from felix.config import Settings
    from felix.context import AuthContext, RequestContext, async_run_with_context
    from felix.manifests.loader import parse_manifest
    from felix.patterns.model import ModelChatResult, TokenUsage
    from felix.runtime import build_tenant_agent
    from felix.tools.builtins import default_tool_provider

    class _Model:
        model_id = "mock"

        async def chat(self, messages: Any, tools: Any, opts: Any = None) -> ModelChatResult:
            return ModelChatResult(
                message=ChatMessage(role="assistant", content=PII),
                stop_reason="end_turn",
                usage=TokenUsage(input=1, output=1),
            )

        async def stream(self, messages: Any, tools: Any, opts: Any = None) -> AsyncIterator[str]:
            """The react loop streams through `stream()` for a client without `stream_turn`."""
            for piece in (PII[:10], PII[10:]):
                yield piece

    settings = Settings(  # type: ignore[arg-type]
        database_url="memory://reply-e2e",
        object_store="memory",
        allow_insecure=True,
        auth_mode="none",
        host="127.0.0.1",
        environment="development",
    )
    manifest = parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "reply-e2e"},
            "spec": {
                "pattern": "react",
                "tools": ["calculator"],
                "guardrails": {"providers": ["pii"], "targets": ["input", "output"]},
            },
        }
    )
    ctx = RequestContext(settings=settings, auth=AuthContext(tenant_id="default"), manifest_id="reply-e2e")
    async with async_run_with_context(ctx):
        agent = await build_tenant_agent(
            settings, manifest=manifest, tools=default_tool_provider(), tenant_id="default"
        )
        # Inbound screening wraps outermost (the manifest targets `input`); the reply
        # controls sit inside it, around the pattern.
        from felix.governance.inbound import InboundScreeningAgent

        assert isinstance(agent, InboundScreeningAgent)
        reply = agent._inner
        assert isinstance(reply, ReplyControlsAgent)
        reply._inner._resolve_model = lambda _i: _Model()  # type: ignore[attr-defined]
        items = [
            item
            async for item in agent.stream_events(
                InvokeInput(messages=[ChatMessage(role="user", content="hi")])
            )
        ]
        out = await agent.invoke(InvokeInput(messages=[ChatMessage(role="user", content="hi")]))
    assert EMAIL not in _streamed_text(items) and _streamed_text(items)
    assert EMAIL not in _done(items)["final"]["content"]
    assert EMAIL not in out.final.content
