"""Model calls share a connection pool, and the streamed open retries like the plain POST.

Every model call used to open its own `httpx.AsyncClient`, so a tool loop paid a TCP
connect and TLS handshake per step. The connection test below runs a real HTTP server and
counts the sockets it accepts, because the claim is about connections, not about which
object a call site happened to pass.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from felix.config import Settings
from felix_ai import AnthropicMessagesClient, ChatMessage, ModelChatResult, ModelRoute
from felix_ai.wire.transport import aclose_shared_transports

_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


@pytest.fixture(autouse=True)
def _no_environment_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Under a proxy the pool is bypassed on purpose; these tests are about the pool."""
    for name in _PROXY_VARS:
        monkeypatch.delenv(name, raising=False)


_REPLY = json.dumps(
    {
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
).encode()


async def _serve() -> tuple[asyncio.Server, list[int]]:
    """An HTTP/1.1 keep-alive server that answers every POST with `_REPLY`."""
    accepted: list[int] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        accepted.append(1)
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                length = 0
                for line in head.decode().split("\r\n"):
                    if line.lower().startswith("content-length:"):
                        length = int(line.split(":", 1)[1])
                await reader.readexactly(length)
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    + f"Content-Length: {len(_REPLY)}\r\n\r\n".encode()
                    + _REPLY
                )
                await writer.drain()
        except asyncio.IncompleteReadError, ConnectionError:
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, accepted


def _anthropic(base_url: str) -> AnthropicMessagesClient:
    return AnthropicMessagesClient(
        model_id="m",
        route=ModelRoute(provider="anthropic", model="m"),
        settings=Settings(database_url="memory://pooling", object_store="memory"),
        spec=None,
        base_url=base_url,
        api_key="k",
    )


async def test_consecutive_model_calls_reuse_one_connection() -> None:
    server, accepted = await _serve()
    port = server.sockets[0].getsockname()[1]
    client = _anthropic(f"http://127.0.0.1:{port}")
    try:
        for _ in range(3):
            result = await client.chat([ChatMessage(role="user", content="hi")], [])
            assert result.message.content == "ok"
    finally:
        await aclose_shared_transports()
        server.close()
        await server.wait_closed()
    assert len(accepted) == 1


class _Response:
    def __init__(self, status: int, lines: list[str]) -> None:
        self.status_code = status
        self._lines = lines
        self.text = ""
        self.headers: dict[str, str] = {}

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self) -> bytes:
        return b""


class _Opener:
    def __init__(self, resp: _Response, log: list[str]) -> None:
        self._resp = resp
        self._log = log

    async def __aenter__(self) -> _Response:
        self._log.append(f"open {self._resp.status_code}")
        return self._resp

    async def __aexit__(self, *exc: Any) -> None:
        self._log.append(f"close {self._resp.status_code}")


class _ScriptedHttp:
    """Stands in for `httpx.AsyncClient`; each `stream` call takes the next scripted reply."""

    def __init__(self, replies: list[_Response]) -> None:
        self.replies = replies
        self.log: list[str] = []

    def __call__(self, *a: Any, **kw: Any) -> _ScriptedHttp:
        return self

    async def __aenter__(self) -> _ScriptedHttp:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    def stream(self, method: str, url: str, **kw: Any) -> _Opener:
        return _Opener(self.replies.pop(0), self.log)


_OK_LINES = [
    'data: {"type":"message_start","message":{"usage":{"input_tokens":1}}}',
    'data: {"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":""}}',
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"a"}}',
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"b"}}',
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"signature_delta","signature":"s1"}}',
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"signature_delta","signature":"s2"}}',
    'data: {"type":"content_block_start","index":1,"content_block":{"type":"text","text":""}}',
    'data: {"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"done"}}',
    'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":2}}',
]


async def _stream(client: AnthropicMessagesClient) -> ModelChatResult:
    result: ModelChatResult | None = None
    async for item in client.stream_turn([ChatMessage(role="user", content="hi")], []):
        if isinstance(item, ModelChatResult):
            result = item
    assert result is not None
    return result


async def test_streamed_turn_retries_an_overloaded_open(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx
    from felix_ai.wire import transport

    monkeypatch.setattr(transport, "_backoff_delay", lambda attempt, retry_after: 0.0)
    http = _ScriptedHttp([_Response(529, []), _Response(200, _OK_LINES)])
    monkeypatch.setattr(httpx, "AsyncClient", http)

    result = await _stream(_anthropic("https://example.invalid"))

    assert result.message.content == "done"
    # The rejected response is closed before the retry opens, not leaked.
    assert http.log == ["open 529", "close 529", "open 200", "close 200"]
    # Fragments of one block arrive joined, in order.
    assert result.message.thinking == [{"type": "thinking", "thinking": "ab", "signature": "s1s2"}]


async def test_streamed_turn_does_not_retry_a_client_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx
    from felix_ai.wire.transport import ModelGatewayError

    http = _ScriptedHttp([_Response(400, []), _Response(200, _OK_LINES)])
    monkeypatch.setattr(httpx, "AsyncClient", http)

    with pytest.raises(ModelGatewayError) as err:
        await _stream(_anthropic("https://example.invalid"))
    assert err.value.status == 400
    assert http.log == ["open 400", "close 400"]


async def test_cancelled_retry_still_closes_the_rejected_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cancellation while reading a rejected body must not strand its pooled connection."""
    import httpx

    class _CancelledRead(_Response):
        async def aread(self) -> bytes:
            raise asyncio.CancelledError

    http = _ScriptedHttp([_CancelledRead(529, []), _Response(200, _OK_LINES)])
    monkeypatch.setattr(httpx, "AsyncClient", http)

    with pytest.raises(asyncio.CancelledError):
        await _stream(_anthropic("https://example.invalid"))
    assert http.log == ["open 529", "close 529"]


def test_model_call_sites_build_their_client_through_the_pool() -> None:
    """A bare `httpx.AsyncClient(` here is a handshake per call that nothing else would catch."""
    from pathlib import Path

    import felix_ai

    root = Path(felix_ai.__file__).parent
    offenders = [
        str(path.relative_to(root))
        for sub in ("wire", "decide")
        for path in sorted((root / sub).rglob("*.py"))
        if path.name != "transport.py" and "httpx.AsyncClient(" in path.read_text()
    ]
    assert offenders == []


def test_an_environment_proxy_keeps_its_route(monkeypatch: pytest.MonkeyPatch) -> None:
    """httpx ignores HTTPS_PROXY when handed a transport; the pool must not bypass the proxy."""
    import httpx
    from felix_ai.wire.transport import model_http_client

    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: seen.append(kw))

    async def build() -> None:
        model_http_client(httpx.Timeout(1.0))

    asyncio.run(build())
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")
    asyncio.run(build())

    assert "transport" in seen[0]
    assert "transport" not in seen[1]


async def test_streamed_spent_quota_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """The quota check needs the body; on a real streamed response it is unread until `aread`."""
    import httpx
    from felix_ai.wire import transport
    from felix_ai.wire.transport import ModelGatewayError

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(429, stream=httpx.ByteStream(b'{"error":{"message":"insufficient_quota"}}'))

    monkeypatch.setattr(transport, "shared_transport", lambda: httpx.MockTransport(handler))
    monkeypatch.setattr(transport, "_backoff_delay", lambda attempt, retry_after: 0.0)

    with pytest.raises(ModelGatewayError) as err:
        await _stream(_anthropic("https://example.invalid"))
    assert err.value.status == 429
    assert len(requests) == 1


async def test_streamed_retries_run_out_and_close_every_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx
    from felix_ai.wire import transport
    from felix_ai.wire.transport import MODEL_MAX_RETRIES, ModelGatewayError

    monkeypatch.setattr(transport, "_backoff_delay", lambda attempt, retry_after: 0.0)
    attempts = MODEL_MAX_RETRIES + 1
    http = _ScriptedHttp([_Response(529, []) for _ in range(attempts)])
    monkeypatch.setattr(httpx, "AsyncClient", http)

    with pytest.raises(ModelGatewayError) as err:
        await _stream(_anthropic("https://example.invalid"))
    assert err.value.status == 529
    assert http.log == ["open 529", "close 529"] * attempts


def test_each_event_loop_gets_its_own_pool() -> None:
    """httpcore connections belong to the loop that opened them; sharing one across loops breaks."""
    from felix_ai.wire.transport import shared_transport

    async def grab() -> tuple[Any, Any, Any]:
        first, again = shared_transport(), shared_transport()
        await aclose_shared_transports()
        return first, again, shared_transport()

    first, again, after_close = asyncio.run(grab())
    other_loop, _, _ = asyncio.run(grab())
    assert first is again
    assert after_close is not first
    assert other_loop is not first
