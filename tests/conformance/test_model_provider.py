"""One contract, run against every model provider wire format.

`tests/conformance/test_session_store.py` exists because an in-memory twin that nobody
compares to the real store drifts from it. The model layer had the same shape of gap and
worse consequences: twelve test files each build their own model double, every one of them
re-decides what a provider owes its caller, and **nothing exercised the chain a real
provider travels** — `register_model_provider` → `FELIX_MODEL_ROUTES` → `build_one_model`
→ a turn → `record_usage`.

That gap is why `stream_turn` went missing from the published `ModelProvider` Protocol for
so long: a double that implemented it and a double that did not both looked correct in
isolation, and the difference only shows up in metering, where getting it wrong fails
*open* on `limits.max_cost_usd`.

The contract is written once here and every arm runs it. Add an arm to `WIRE_FORMATS` and
it inherits every assertion. Unlike the store suite, no arm needs infrastructure — the HTTP
wire formats run against a fake transport and the scripted provider needs none — so a skip
in this file would be a bug rather than a missing database.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from felix_ai.types import ChatMessage, ModelChatOptions, ModelRoute, TokenUsage, ToolCall
from felix_ai.wire.transport import ModelGatewayError

WIRE_FORMATS = ["scripted", "openai", "anthropic"]
parametrized = pytest.mark.parametrize("arm", WIRE_FORMATS, indirect=True)

# What every arm is programmed to report, so the metering assertions can be exact.
EXPECT_INPUT = 11
EXPECT_OUTPUT = 7


# --- fakes ------------------------------------------------------------------------------


class _Resp:
    def __init__(self, status: int, payload: dict[str, Any] | None = None, text: str = "") -> None:
        self.status_code = status
        self._payload = payload or {}
        self.text = text
        self.headers: dict[str, str] = {}

    def json(self) -> dict[str, Any]:
        return self._payload

    async def aread(self) -> bytes:
        return self.text.encode()


class _StreamResp:
    def __init__(self, status: int, lines: list[str]) -> None:
        self.status_code = status
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self) -> bytes:
        return b"upstream detail"


class _Transport:
    """Stands in for `httpx.AsyncClient`, recording what the wire format sent."""

    def __init__(self) -> None:
        self.response: Any = _Resp(200, {})
        self.lines: list[str] = []
        self.status = 200
        self.sent: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        # True for wire formats where a streamed response carries usage only on request.
        self.usage_needs_asking = False

    def __call__(self, *a: Any, **kw: Any) -> _Transport:
        return self

    async def __aenter__(self) -> _Transport:
        return self

    async def __aexit__(self, *a: Any) -> None:
        return None

    async def post(self, url: str, json: Any = None, headers: Any = None) -> Any:
        self.sent.append(json or {})
        self.headers.append(dict(headers or {}))
        return self.response

    def stream(self, method: str, url: str, json: Any = None, headers: Any = None) -> Any:
        body = json or {}
        self.sent.append(body)
        self.headers.append(dict(headers or {}))
        outer = self
        # Model the endpoint, not a tape recorder: OpenAI omits usage from a streamed
        # response unless `stream_options.include_usage` asked for it. A fake that hands
        # usage back regardless cannot see a provider that forgets to ask — and that
        # provider meters every streamed run as zero.
        lines = list(outer.lines)
        if outer.usage_needs_asking and not (body.get("stream_options") or {}).get("include_usage"):
            lines = [line for line in lines if '"usage"' not in line]

        class _Ctx:
            async def __aenter__(self) -> _StreamResp:
                return _StreamResp(outer.status, lines)

            async def __aexit__(self, *a: Any) -> None:
                return None

        return _Ctx()


def _openai_finish(stop: str) -> str:
    return {"end_turn": "stop", "max_tokens": "length", "tool_use": "tool_calls"}[stop]


class _Arm:
    """A client plus the wire-format-specific way to program its next reply.

    The contract below never names a payload shape; it only says what a *turn* is. That is
    what makes it a contract rather than three copies of the same test.
    """

    def __init__(self, wire: str, client: Any, transport: _Transport | None) -> None:
        self.wire = wire
        self.client = client
        self.transport = transport

    def program_turn(
        self, *, content: str = "hello", tool_calls: list[ToolCall] | None = None, stop: str = "end_turn"
    ) -> None:
        if self.wire == "scripted":
            from felix_ai.providers.scripted import ScriptedTurn

            self.client.script.append(
                ScriptedTurn(
                    content=content,
                    tool_calls=tool_calls,
                    stop_reason="tool_use" if tool_calls else stop,
                    usage=TokenUsage(input=EXPECT_INPUT, output=EXPECT_OUTPUT),
                )
            )
            return
        assert self.transport is not None
        if self.wire == "openai":
            message: dict[str, Any] = {"content": content}
            if tool_calls:
                message["tool_calls"] = [
                    {"id": tc.id, "function": {"name": tc.name, "arguments": json.dumps(tc.args)}}
                    for tc in tool_calls
                ]
            self.transport.response = _Resp(
                200,
                {
                    "choices": [
                        {
                            "message": message,
                            "finish_reason": _openai_finish("tool_use" if tool_calls else stop),
                        }
                    ],
                    "usage": {"prompt_tokens": EXPECT_INPUT, "completion_tokens": EXPECT_OUTPUT},
                },
            )
        else:
            blocks: list[dict[str, Any]] = [{"type": "text", "text": content}]
            for tc in tool_calls or []:
                blocks.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.args})
            self.transport.response = _Resp(
                200,
                {
                    "content": blocks,
                    "stop_reason": "tool_use" if tool_calls else stop,
                    "usage": {"input_tokens": EXPECT_INPUT, "output_tokens": EXPECT_OUTPUT},
                },
            )

    def program_stream(self, *, content: str = "hello", stop: str = "end_turn") -> None:
        if self.wire == "scripted":
            self.program_turn(content=content, stop=stop)
            return
        assert self.transport is not None
        if self.wire == "openai":
            self.transport.lines = [
                f'data: {{"choices":[{{"delta":{{"content":"{content}"}}}}]}}',
                f'data: {{"choices":[{{"delta":{{}},"finish_reason":"{_openai_finish(stop)}"}}]}}',
                f'data: {{"usage":{{"prompt_tokens":{EXPECT_INPUT},"completion_tokens":{EXPECT_OUTPUT}}}}}',
                "data: [DONE]",
            ]
        else:
            self.transport.lines = [
                f'data: {{"type":"message_start","message":{{"usage":{{"input_tokens":{EXPECT_INPUT}}}}}}}',
                f'data: {{"type":"content_block_delta","delta":{{"type":"text_delta","text":"{content}"}}}}',
                f'data: {{"type":"message_delta","delta":{{"stop_reason":"{stop}"}},"usage":{{"output_tokens":{EXPECT_OUTPUT}}}}}',
            ]
        self.transport.status = 200

    def program_error(self, status: int) -> None:
        if self.wire == "scripted":
            from felix_ai.providers.scripted import ScriptedTurn

            self.client.script.append(
                ScriptedTurn(error=ModelGatewayError(self.wire, status, "upstream detail"))
            )
            return
        assert self.transport is not None
        self.transport.response = _Resp(status, {}, text="upstream detail")
        self.transport.status = status
        self.transport.lines = []


@pytest.fixture
def arm(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> _Arm:
    import httpx
    from felix.config import Settings

    wire = request.param
    if wire == "scripted":
        from felix_ai.providers.scripted import ScriptedClient

        return _Arm(
            wire,
            ScriptedClient(model_id="s", route=ModelRoute(provider="scripted", model="s-1")),
            None,
        )

    transport = _Transport()
    transport.usage_needs_asking = wire == "openai"
    monkeypatch.setattr(httpx, "AsyncClient", transport)
    settings = Settings(database_url="memory://conformance-model", object_store="memory")

    if wire == "openai":
        from felix_ai.wire.openai_completions import OpenAICompletionsClient

        client: Any = OpenAICompletionsClient(
            model_id="gpt-4o",
            route=ModelRoute(provider="openai", model="gpt-4o"),
            settings=settings,
            spec=None,
            base_url="https://example.invalid/v1",
            api_key="k",
        )
    else:
        from felix_ai.wire.anthropic_messages import AnthropicMessagesClient

        client = AnthropicMessagesClient(
            model_id="claude-sonnet-5",
            route=ModelRoute(provider="anthropic", model="claude-sonnet-5"),
            settings=settings,
            spec=None,
            base_url="https://example.invalid",
            api_key="k",
        )
    return _Arm(wire, client, transport)


def _user(text: str = "hi") -> list[ChatMessage]:
    return [ChatMessage(role="user", content=text)]


# --- the published shape ------------------------------------------------------------------


@parametrized
def test_a_provider_exposes_model_id_and_route(arm: _Arm) -> None:
    """Both are read as bare attributes — `model_id` in every metering line, `route`
    whenever `fallbacks` or `confidence_escalation` is configured — so a provider missing
    either raises `AttributeError` mid-turn rather than failing to build."""
    assert isinstance(arm.client.model_id, str) and arm.client.model_id
    assert isinstance(arm.client.route, ModelRoute)
    assert arm.client.route.provider and arm.client.route.model


@parametrized
@pytest.mark.asyncio
async def test_chat_takes_messages_and_tools_positionally(arm: _Arm) -> None:
    arm.program_turn(content="hello")
    result = await arm.client.chat(_user(), [])
    assert result.message.role == "assistant"
    assert "hello" in result.message.content


@parametrized
@pytest.mark.asyncio
async def test_options_are_accepted_as_the_third_positional_argument(arm: _Arm) -> None:
    """Six call sites pass `opts` positionally — compaction, branch summaries, session
    strategies, memory extraction, inbound screening and eval — so a keyword-only `opts`
    is a `TypeError` on every side request. The Protocol does not say this; the codebase
    does."""
    arm.program_turn(content="hello")
    result = await arm.client.chat(_user(), [], ModelChatOptions(isolate_cache=True))
    assert result.message.content


# --- metering, where getting it wrong fails open --------------------------------------------


@parametrized
@pytest.mark.asyncio
async def test_chat_reports_usage(arm: _Arm) -> None:
    """`record_usage` is the only feed for `max_input_tokens`, `max_output_tokens` and
    `max_cost_usd`. A provider reporting nothing leaves the run uncapped."""
    arm.program_turn()
    result = await arm.client.chat(_user(), [])
    assert result.usage is not None
    assert result.usage.input == EXPECT_INPUT
    assert result.usage.output == EXPECT_OUTPUT


@parametrized
@pytest.mark.asyncio
async def test_stream_turn_ends_with_an_authoritative_result(arm: _Arm) -> None:
    """One request must yield display deltas *and* the turn's real answer. A provider that
    only implements `stream()` forfeits tool calls and usage from a streamed request and
    costs a second inference to meter."""
    from felix_ai.types import ModelChatResult, StreamDelta

    arm.program_stream(content="hello")
    items = [item async for item in arm.client.stream_turn(_user(), [])]
    assert items, "stream_turn yielded nothing"
    assert isinstance(items[-1], ModelChatResult), "the last item must be the authoritative result"
    assert any(isinstance(i, StreamDelta) for i in items[:-1]), "no display deltas"
    assert "hello" in items[-1].message.content


@parametrized
@pytest.mark.asyncio
async def test_a_streamed_turn_is_metered_too(arm: _Arm) -> None:
    """The OpenAI wire format needs `stream_options.include_usage` for this; a provider
    that forgets the equivalent makes every streamed run free to the budgets."""
    from felix_ai.types import ModelChatResult

    arm.program_stream()
    items = [item async for item in arm.client.stream_turn(_user(), [])]
    final = items[-1]
    assert isinstance(final, ModelChatResult)
    assert final.usage is not None
    assert final.usage.input == EXPECT_INPUT
    assert final.usage.output == EXPECT_OUTPUT


# --- the neutral vocabularies ----------------------------------------------------------------


@parametrized
@pytest.mark.asyncio
async def test_stop_reasons_map_to_the_shared_vocabulary(arm: _Arm) -> None:
    """Each wire format names these differently; callers must never see the wire spelling."""
    for stop in ("end_turn", "max_tokens"):
        arm.program_turn(stop=stop)
        result = await arm.client.chat(_user(), [])
        assert result.stop_reason == stop


@parametrized
@pytest.mark.asyncio
async def test_tool_calls_come_back_parsed(arm: _Arm) -> None:
    """Arguments arrive as a dict, whatever the wire sent — a JSON string on one path and
    an object on the other — and the stop reason says `tool_use`."""
    arm.program_turn(content="", tool_calls=[ToolCall(id="c1", name="search", args={"q": "felix"})])
    result = await arm.client.chat(_user(), [])
    calls = result.message.tool_calls or []
    assert [c.name for c in calls] == ["search"]
    assert calls[0].args == {"q": "felix"}
    assert result.stop_reason == "tool_use"


# --- errors the harness has to be able to read -----------------------------------------------


@parametrized
@pytest.mark.asyncio
async def test_an_upstream_failure_raises_a_readable_gateway_error(arm: _Arm) -> None:
    """`_is_provider_error` and `_FallbackClient` branch on `.status`, so a provider that
    raises something opaque never fails over. The body stays off `str(exc)` because that
    string is relayed to API clients."""
    from felix.patterns.model import _is_provider_error

    arm.program_error(503)
    with pytest.raises(ModelGatewayError) as excinfo:
        await arm.client.chat(_user(), [])
    err = excinfo.value
    assert err.status == 503
    assert _is_provider_error(err), "a 5xx must be retryable/failoverable"
    assert "upstream detail" not in str(err), "the upstream body must not reach the client message"


@parametrized
@pytest.mark.asyncio
async def test_a_client_error_is_not_treated_as_transient(arm: _Arm) -> None:
    from felix.patterns.model import _is_provider_error

    arm.program_error(400)
    with pytest.raises(ModelGatewayError) as excinfo:
        await arm.client.chat(_user(), [])
    assert not _is_provider_error(excinfo.value)


# --- the chain a real provider travels ---------------------------------------------------------


@pytest.mark.asyncio
async def test_registration_to_metered_turn() -> None:
    """The end-to-end nothing covered: register a provider, route to it through
    `FELIX_MODEL_ROUTES`, build it with `build_one_model`, take a turn, and see the spend
    land on `ctx.limit_state`. Every link here was individually tested and the chain was
    not, which is how a plugin provider could satisfy the whole published contract and
    still land in the unmetered path."""
    from felix.config import Settings
    from felix.context import AuthContext, RequestContext, async_run_with_context
    from felix.patterns.model import build_one_model, record_usage, register_builtin_providers, wire_model_id
    from felix.patterns.model_registry import reset_model_provider_registry
    from felix_ai.providers.scripted import ScriptedTurn, register_scripted_provider

    try:
        register_scripted_provider(
            "scripted", [ScriptedTurn(content="ok", usage=TokenUsage(input=1_000_000, output=0))]
        )
        settings = Settings(
            database_url="memory://conformance-chain",
            object_store="memory",
            # The wire model is a real Claude id, so the turn prices at a rate the catalog
            # knows — which is the point: pricing keys on the wire model, not this route name.
            model_routes='{"cheap":{"provider":"scripted","model":"claude-sonnet-5"}}',
        )
        client = build_one_model(settings, None, "cheap")
        assert client.model_id == "cheap"
        assert wire_model_id(client) == "claude-sonnet-5"

        ctx = RequestContext(settings=settings, auth=AuthContext(), manifest_id="m")
        async with async_run_with_context(ctx):
            result = await client.chat(_user(), [])
            record_usage(
                result,
                manifest_id="m",
                model_id=client.model_id,
                wire_model_id=wire_model_id(client),
            )
        assert ctx.limit_state.tokens_input == 1_000_000
        assert ctx.limit_state.cost_usd > 0.0, "a metered turn must accrue spend"
    finally:
        reset_model_provider_registry()
        register_builtin_providers()


@pytest.mark.asyncio
async def test_the_scripted_provider_is_not_registered_by_default() -> None:
    """Shipping a fake in the production registry would let a typo in `FELIX_MODEL_ROUTES`
    succeed silently and answer every prompt with canned text."""
    from felix.patterns.model_registry import get_model_provider

    assert get_model_provider("scripted") is None


@pytest.mark.asyncio
async def test_a_provider_with_only_chat_and_stream_still_runs() -> None:
    """The Protocol declares `stream_turn` optional, so the contract states what omitting
    it costs rather than pretending it is forbidden.

    A provider with only `stream()` cannot report tool calls *or* usage from a streamed
    request, so the agent loop streams for display and calls `chat()` again to get the
    authoritative, metered turn — two inferences for one turn, billed twice and sampled
    twice. `_FallbackClient.stream_turn` skips such a client entirely.
    """
    from felix_ai.types import ChatMessage as CM
    from felix_ai.types import ModelChatResult

    class _ChatOnly:
        model_id = "legacy"
        route = ModelRoute(provider="legacy", model="legacy-1")

        def __init__(self) -> None:
            self.calls: list[str] = []

        async def chat(self, messages, tools, opts=None):
            self.calls.append("chat")
            return ModelChatResult(
                message=CM(role="assistant", content="done"),
                usage=TokenUsage(input=EXPECT_INPUT, output=EXPECT_OUTPUT),
            )

        async def stream(self, messages, tools, opts=None):
            self.calls.append("stream")
            yield "done"

    client = _ChatOnly()
    assert getattr(client, "stream_turn", None) is None, "the probe callers use"

    chunks = [c async for c in client.stream(_user(), [])]
    result = await client.chat(_user(), [])
    assert chunks == ["done"]
    assert result.usage is not None and result.usage.input == EXPECT_INPUT
    # The cost of the missing capability, stated: the turn took two calls.
    assert client.calls == ["stream", "chat"]
