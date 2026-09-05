"""`/v1/chat/completions` streaming behaves like `/chat/stream`, on the OpenAI wire.

Before this the stream had no try/except (an error mid-stream ended a 200 body with no
error and no `[DONE]`), no heartbeat, none of the headers a proxied SSE stream needs,
hard-coded `finish_reason: "stop"`, and silently ignored `temperature`/`max_tokens`.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import felix_api.routes.openai_compat as oc
import pytest
from felix.config import Settings
from felix.patterns.model import ModelGatewayError
from felix.patterns.types import ChatMessage, Event, InvokeInput, InvokeOutput
from felix_api.app import create_app
from httpx import ASGITransport, AsyncClient

BODY = {"model": "quick", "messages": [{"role": "user", "content": "hi"}], "stream": True}


def _settings() -> Settings:
    return Settings(
        database_url="memory://v1-stream",
        object_store="memory",
        allow_insecure=True,
        auth_mode="none",
        host="127.0.0.1",
        environment="development",
        # Every request here comes from one client; the per-IP limiter is not under test.
        rate_limit=100_000,
    )


def _frames(text: str) -> list[str]:
    """Data frames only: `: keep-alive` comments are part of the wire, not of the payload."""
    return [f for f in text.split("\n\n") if f.strip() and not f.startswith(":")]


def _data(frame: str) -> Any:
    assert frame.startswith("data: "), frame
    body = frame[len("data: ") :]
    return body if body == "[DONE]" else json.loads(body)


class _Agent:
    """A stand-in for the compiled agent: scripted events, then a stop reason."""

    def __init__(
        self, events: list[Event], *, stop_reason: str = "end_turn", raises: Exception | None = None
    ):
        self.events, self.stop_reason, self.raises = events, stop_reason, raises
        self.inputs: list[InvokeInput] = []

    async def stream_events(self, input: InvokeInput) -> AsyncIterator[Event]:
        self.inputs.append(input)
        for ev in self.events:
            yield ev
        if self.raises is not None:
            raise self.raises
        yield Event(event="done", data={"final": {}, "messages": [], "stop_reason": self.stop_reason})

    async def invoke(self, input: InvokeInput) -> InvokeOutput:
        self.inputs.append(input)
        return InvokeOutput(
            messages=[], final=ChatMessage(role="assistant", content="hello"), stop_reason=self.stop_reason
        )


@pytest.fixture
def agent(monkeypatch: pytest.MonkeyPatch):
    holder: dict[str, _Agent] = {}

    async def build(*a: Any, **k: Any) -> _Agent:
        return holder["agent"]

    monkeypatch.setattr(oc, "build_tenant_agent", build)

    def install(a: _Agent) -> _Agent:
        holder["agent"] = a
        return a

    return install


async def _post(body: dict[str, Any]) -> Any:
    app = create_app(settings=_settings(), plugins=[])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post("/v1/chat/completions", json=body)


@pytest.mark.asyncio
async def test_the_stream_carries_the_headers_a_proxied_sse_stream_needs(agent) -> None:
    agent(_Agent([Event(event="text_delta", data={"delta": "hi"})]))
    resp = await _post(BODY)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["x-accel-buffering"] == "no", "nginx buffers the stream without this"
    assert resp.headers["cache-control"] == "no-cache"


@pytest.mark.asyncio
async def test_an_error_mid_stream_yields_an_error_chunk_and_done(agent) -> None:
    agent(
        _Agent(
            [Event(event="text_delta", data={"delta": "par"})],
            raises=RuntimeError("session store unavailable"),
        )
    )
    resp = await _post(BODY)
    frames = [_data(f) for f in _frames(resp.text)]
    assert frames[0]["choices"][0]["delta"] == {"content": "par"}
    error = frames[-2]
    assert error["error"]["type"] == "server_error" and error["error"]["code"] == "stream_failed"
    assert "session store unavailable" not in resp.text, "the raw exception text is not client-safe"
    assert frames[-1] == "[DONE]"
    chunks = [f for f in frames if isinstance(f, dict) and "choices" in f]
    assert all(c["choices"][0]["finish_reason"] is None for c in chunks), (
        "a failed stream must not claim a finish_reason"
    )


@pytest.mark.asyncio
async def test_a_gateway_error_mid_stream_is_typed_and_does_not_leak_the_body(agent) -> None:
    agent(_Agent([], raises=ModelGatewayError("anthropic", 429, "org_abc123 req_sensitive_9f3")))
    resp = await _post(BODY)
    frames = [_data(f) for f in _frames(resp.text)]
    assert frames[-2]["error"]["type"] == "model_gateway_error"
    assert frames[-2]["error"]["code"] == "model_unavailable"
    assert "req_sensitive_9f3" not in resp.text
    assert frames[-1] == "[DONE]"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stop_reason", "finish_reason"),
    [("end_turn", "stop"), ("max_tokens", "length"), ("refusal", "content_filter"), ("tool_use", "stop")],
)
async def test_finish_reason_reflects_the_stop_reason(agent, stop_reason: str, finish_reason: str) -> None:
    agent(_Agent([Event(event="text_delta", data={"delta": "x"})], stop_reason=stop_reason))
    resp = await _post(BODY)
    frames = [_data(f) for f in _frames(resp.text)]
    final = frames[-2]
    assert final["choices"][0]["finish_reason"] == finish_reason
    assert "usage" in final and "prompt_tokens_details" in final["usage"]
    assert frames[-1] == "[DONE]"

    plain = await _post({**BODY, "stream": False})
    assert plain.json()["choices"][0]["finish_reason"] == finish_reason


@pytest.mark.asyncio
async def test_sampling_parameters_reach_the_agent(agent) -> None:
    a = agent(_Agent([Event(event="text_delta", data={"delta": "x"})]))
    await _post({**BODY, "temperature": 0.2, "max_tokens": 77})
    await _post({**BODY, "stream": False, "temperature": 0.9, "max_tokens": 5})
    assert [(i.model_options.temperature, i.model_options.max_tokens) for i in a.inputs] == [
        (0.2, 77),
        (0.9, 5),
    ]

    await _post(BODY)
    assert a.inputs[-1].model_options is None, "absent means the manifest's values"

    assert (await _post({**BODY, "temperature": 3.0})).status_code == 422
    assert (await _post({**BODY, "max_tokens": 0})).status_code == 422


@pytest.mark.asyncio
async def test_thinking_is_not_emitted_as_content(agent) -> None:
    agent(
        _Agent(
            [
                Event(event="thinking_delta", data={"delta": "secret plan"}),
                Event(event="text_delta", data={"delta": "answer"}),
            ]
        )
    )
    resp = await _post(BODY)
    assert "secret plan" not in resp.text
    assert "answer" in resp.text


def _innermost(agent: Any) -> Any:
    while hasattr(agent, "_inner"):
        agent = agent._inner
    return agent


async def _bare_agent(
    limits: dict[str, Any] | None = None,
    model: dict[str, Any] | None = None,
    guardrails: dict[str, Any] | None = None,
    reply: str = "ok",
) -> tuple[Any, Any]:
    from felix.manifests.loader import parse_manifest
    from felix.runtime import build_tenant_agent
    from felix.tools.provider import InMemoryToolProvider
    from felix_ai.types import ModelChatResult, TokenUsage

    class _Model:
        model_id = "mock"

        def __init__(self) -> None:
            self.calls: list[Any] = []

        async def chat(self, messages: Any, tools: Any, opts: Any = None) -> ModelChatResult:
            self.calls.append(opts)
            return ModelChatResult(
                message=ChatMessage(role="assistant", content=reply),
                stop_reason="max_tokens",
                usage=TokenUsage(input=1, output=1),
            )

        async def stream(self, messages: Any, tools: Any, opts: Any = None) -> AsyncIterator[str]:
            """The loop streams through `stream()` for a client without `stream_turn`, then
            asks `chat()` for the authoritative turn."""
            yield reply

    spec: dict[str, Any] = {"pattern": "react"}
    if limits:
        spec["limits"] = limits
    if model:
        spec["model"] = model
    if guardrails:
        spec["guardrails"] = guardrails
    bare = parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": "bare"}, "spec": spec}
    )
    agent = await build_tenant_agent(_settings(), manifest=bare, tools=InMemoryToolProvider(), tenant_id="t")
    fake = _Model()
    _innermost(agent)._resolve_model = lambda _i: fake  # type: ignore[attr-defined]
    return agent, fake


def _turn(**options: Any) -> InvokeInput:
    from felix_ai.types import ModelChatOptions

    return InvokeInput(
        messages=[ChatMessage(role="user", content="hi")],
        model_options=ModelChatOptions(**options) if options else None,
    )


@pytest.mark.asyncio
async def test_sampling_options_reach_the_model_call_and_the_stop_reason_the_caller() -> None:
    """The react loop passes the caller's `ModelChatOptions` to the model call, and `None`
    when nobody asked — the Protocol's optional argument, so a client written against it
    sees exactly what the caller sent."""
    agent, model = await _bare_agent()

    out = await agent.invoke(_turn(temperature=0.3, max_tokens=9))
    plain = await agent.invoke(_turn())

    given, none = model.calls
    assert (given.temperature, given.max_tokens) == (0.3, 9)
    assert none is None
    assert out.stop_reason == "max_tokens" and plain.stop_reason == "max_tokens", (
        "the stop reason reaches the caller"
    )


@pytest.mark.asyncio
async def test_a_caller_may_only_lower_max_tokens() -> None:
    """The wire prefers the caller's `max_tokens` over `spec.model.max_tokens`, and the
    output budget is checked at the top of a turn — so an unclamped value sizes a whole
    turn past the ceiling the operator declared. The loop clamps to the manifest."""
    agent, model = await _bare_agent(model={"max_tokens": 1024})
    await agent.invoke(_turn(max_tokens=10_000_000))
    await agent.invoke(_turn(max_tokens=100))
    assert [o.max_tokens for o in model.calls] == [1024, 100]

    agent, model = await _bare_agent(limits={"max_output_tokens": 300}, model={"max_tokens": 1024})
    await agent.invoke(_turn(max_tokens=10_000_000))
    assert model.calls[-1].max_tokens == 300, "limits.max_output_tokens is the tighter ceiling"

    agent, model = await _bare_agent()
    await agent.invoke(_turn(max_tokens=10_000_000))
    from felix.manifests.schema import ABSOLUTE_LIMITS

    assert model.calls[-1].max_tokens == ABSOLUTE_LIMITS["max_output_tokens"], (
        "no manifest ceiling: the absolute one"
    )
    await agent.invoke(_turn(temperature=0.5))
    assert model.calls[-1].max_tokens is None, "no caller max_tokens means the manifest's value at the wire"


def test_every_stop_reason_has_a_finish_reason() -> None:
    """`_FINISH_REASON` is a literal keyed by `StopReason`; a dict literal may omit a key
    and `finish_reason_for` defaults to `stop`, so an eighth member would silently read
    as a normal completion. This is what makes the map's comment true."""
    from typing import get_args

    from felix_ai.types import StopReason
    from felix_ai.wire.openai_completions import _FINISH_REASON

    assert set(_FINISH_REASON) == set(get_args(StopReason))


@pytest.mark.asyncio
async def test_a_quiet_run_is_kept_alive_for_the_proxy(agent, monkeypatch: pytest.MonkeyPatch) -> None:
    """A long tool call emits nothing and proxies cut idle streams; the sentinel becomes a
    `: keep-alive` comment, which an OpenAI SSE client ignores."""
    import asyncio
    import functools

    from felix_api.routes._sse import with_heartbeat

    class _Slow(_Agent):
        async def stream_events(self, input: InvokeInput) -> AsyncIterator[Event]:
            yield Event(event="text_delta", data={"delta": "a"})
            await asyncio.sleep(0.15)
            yield Event(event="text_delta", data={"delta": "b"})
            yield Event(event="done", data={"final": {}, "messages": [], "stop_reason": "end_turn"})

    monkeypatch.setattr(oc, "with_heartbeat", functools.partial(with_heartbeat, interval=0.02))
    agent(_Slow([]))
    resp = await _post(BODY)
    raw = [f for f in resp.text.split("\n\n") if f.strip()]
    assert any(f.startswith(": keep-alive") for f in raw), "no heartbeat during the quiet stretch"
    frames = [_data(f) for f in _frames(resp.text)]
    assert frames[-1] == "[DONE]" and frames[-2]["choices"][0]["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_finish_reason_survives_the_reply_controls_on_both_arms() -> None:
    """Through the real chain — a governed manifest wrapping the loop in reply controls —
    rather than a double: the wrapper rebuilds the output and the `done` frame, and on
    `invoke` it used to drop the stop reason (every reply said `stop`)."""
    from felix.governance.reply import PII_BLOCKED_REPLY

    agent, _ = await _bare_agent(
        guardrails={"providers": ["pii"], "targets": ["output"], "block_on_match": False}
    )
    out = await agent.invoke(_turn())
    assert out.stop_reason == "max_tokens", "invoke through the reply wrapper lost the stop reason"
    done = [e async for e in agent.stream_events(_turn()) if getattr(e, "event", "") == "done"]
    assert done and done[0].data.get("stop_reason") == "max_tokens", "the done frame lost the stop reason"

    # A judge or PII block that replaces the reply is a refusal to the caller.
    agent, _ = await _bare_agent(
        guardrails={"providers": ["pii"], "targets": ["output"], "block_on_match": True},
        reply="my email is someone@example.com",
    )
    out = await agent.invoke(_turn())
    assert out.final.content == PII_BLOCKED_REPLY and out.stop_reason == "refusal"


@pytest.mark.asyncio
async def test_sampling_options_reach_the_streaming_call_sites() -> None:
    """`stream: true` takes `stream_turn` (or `stream` for a client without it), not `chat`."""
    from felix_ai.types import ModelChatResult, StreamDelta, TokenUsage

    result = ModelChatResult(
        message=ChatMessage(role="assistant", content="ok"),
        stop_reason="end_turn",
        usage=TokenUsage(input=1, output=1),
    )

    class _Turn:
        model_id = "mock"

        def __init__(self) -> None:
            self.calls: list[Any] = []

        async def chat(self, messages: Any, tools: Any, opts: Any = None) -> ModelChatResult:
            raise AssertionError("a streaming client is not asked for a chat turn")

        async def stream_turn(self, messages: Any, tools: Any, opts: Any = None) -> AsyncIterator[Any]:
            self.calls.append(opts)
            yield StreamDelta(text="ok")
            yield result

    class _StreamOnly:
        model_id = "mock"

        def __init__(self) -> None:
            self.calls: list[Any] = []

        async def chat(self, messages: Any, tools: Any, opts: Any = None) -> ModelChatResult:
            self.calls.append(opts)
            return result

        async def stream(self, messages: Any, tools: Any, opts: Any = None) -> AsyncIterator[str]:
            self.calls.append(opts)
            yield "ok"

    for fake in (_Turn(), _StreamOnly()):
        agent, _ = await _bare_agent()
        _innermost(agent)._resolve_model = lambda _i, fake=fake: fake  # type: ignore[attr-defined]
        async for _ in agent.stream_events(_turn(temperature=0.4, max_tokens=8)):
            pass
        assert fake.calls, type(fake).__name__
        assert all(o is not None and (o.temperature, o.max_tokens) == (0.4, 8) for o in fake.calls), (
            fake.calls
        )


@pytest.mark.asyncio
async def test_a_bare_pattern_context_without_limits_still_takes_sampling() -> None:
    """`PatternBuildContext` is a plain dict and `limits` may be absent from a third-party
    builder's context; `_over_budget` tolerates that, and the clamp must too."""
    from felix.patterns.react import build_react_agent
    from felix_ai.types import ModelChatResult, TokenUsage

    class _Model:
        model_id = "mock"
        calls: list[Any] = []

        async def chat(self, messages: Any, tools: Any, opts: Any = None) -> ModelChatResult:
            self.calls.append(opts)
            return ModelChatResult(
                message=ChatMessage(role="assistant", content="ok"),
                stop_reason="end_turn",
                usage=TokenUsage(),
            )

    agent = build_react_agent(
        {
            "tools": [],
            "manifest_id": "bare",
            "manifest_version": "1",
            "system_prompt": "s",
            "model_spec": None,
            "settings": _settings(),
            "recursion_limit": 2,
        }
    )
    model = _Model()
    agent._resolve_model = lambda _i: model  # type: ignore[attr-defined]
    await agent.invoke(_turn(max_tokens=50))
    assert model.calls[-1].max_tokens == 50
