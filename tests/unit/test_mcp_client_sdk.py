"""The outbound MCP client against real MCP servers: the official SDK's, over HTTP and stdio.

Felix's client is the SDK too, so these run both sides' idea of the protocol against each other
instead of a hand-written JSON-RPC fake. HTTP goes through the egress guard (`allow_http=True` is
what lets loopback through), stdio through the stdio allowlist.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.manifests.schema import McpServerRef
from felix.mcp import client as mcp_client
from felix.mcp.client import tools_from_mcp_servers
from felix.tools.types import ToolInvocationCtx
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS

from tests.support.mcp_peer import PNG_1X1, STDIO_PEER, STDIO_PYTHON, build_peer, serve_http_peer


def _text(out: Any) -> str:
    return out if isinstance(out, str) else out.content


async def _run(tool: Any, args: dict[str, Any] | None = None) -> Any:
    return await tool.executor.execute(args or {}, ToolInvocationCtx(manifest_id="m", tool_call_id="c"))


def _http_ref(name: str, url: str, **fields: Any) -> McpServerRef:
    """A ref for a loopback URL. The schema refuses `http://` outright, as a stored manifest must;
    `model_copy` sets it after validation, so what these exercise is the client's `allow_http`."""
    return McpServerRef(
        name=name, url="https://placeholder.invalid/mcp", transport="http", **fields
    ).model_copy(update={"url": url})


def _by_name(tools: list[Any]) -> dict[str, Any]:
    return {t.name: t for t in tools}


async def test_an_http_server_is_discovered_bound_and_called_with_its_credential() -> None:
    async with serve_http_peer() as peer:
        ref = _http_ref("peer", peer.url, auth="tok-1")
        tools = _by_name(await tools_from_mcp_servers([ref], allow_http=True))
        out = await _run(tools["peer__echo"], {"text": "hi"})
    assert set(tools) == {"peer__echo", "peer__fail", "peer__strict", "peer__slow", "peer__picture"}
    assert tools["peer__echo"].executor.transport == "mcp"
    assert tools["peer__echo"].source == "mcp:peer"
    assert _text(out) == "echo:hi"
    # Discovery and the call each send it: every request the server saw carried the credential.
    assert peer.headers and all(h.get("authorization") == "Bearer tok-1" for h in peer.headers)


async def test_a_failed_call_and_an_image_come_back_as_felix_tool_output() -> None:
    async with serve_http_peer() as peer:
        ref = _http_ref("peer", peer.url)
        tools = _by_name(await tools_from_mcp_servers([ref], allow_http=True))
        failed = await _run(tools["peer__fail"])
        picture = await _run(tools["peer__picture"])
    assert _text(failed).startswith("[mcp_error] ")
    assert not isinstance(picture, str), "the image block was dropped"
    (image,) = picture.attachments
    assert image.url.endswith(PNG_1X1), "not the bytes the server sent"


async def test_server_instructions_reach_the_tools_only_when_opted_in() -> None:
    async with serve_http_peer() as peer:
        plain = _http_ref("peer", peer.url)
        opted = _http_ref("peer", peer.url, use_instructions=True)
        without = await tools_from_mcp_servers([plain], allow_http=True)
        mcp_client.clear_discovery_cache()
        with_guidance = await tools_from_mcp_servers([opted], allow_http=True)
    # Counted first: an `all()` over no tools passes, and a discovery that broke binds none.
    assert len(without) == len(with_guidance) == 5
    assert all(not t.prompt_guidance for t in without)
    assert all(
        t.prompt_guidance == "peer (from the server): Use echo to repeat text back." for t in with_guidance
    )


async def test_loopback_is_refused_without_allow_http(caplog: pytest.LogCaptureFixture) -> None:
    """The guard sits in front of the SDK: with the development switch off, nothing binds."""
    async with serve_http_peer() as peer:
        ref = _http_ref("peer", peer.url)
        tools = await tools_from_mcp_servers([ref], allow_http=False)
    assert tools == []
    assert peer.headers == [], "a request reached the server past the egress guard"


async def test_a_stdio_server_on_the_allowlist_is_spawned_and_called(monkeypatch: pytest.MonkeyPatch) -> None:
    from felix.config import get_settings

    monkeypatch.setenv("FELIX_MCP_STDIO_ALLOWED_COMMANDS", STDIO_PYTHON)
    get_settings.cache_clear()
    try:
        ref = McpServerRef(
            name="local",
            transport="stdio",
            command=STDIO_PYTHON,
            args=[str(STDIO_PEER)],
            use_instructions=True,
        )
        tools = _by_name(await tools_from_mcp_servers([ref]))
        echoed = await _run(tools["local__echo"], {"text": "yo"})
        failed = await _run(tools["local__fail"])
    finally:
        get_settings.cache_clear()
    assert set(tools) == {"local__echo", "local__fail"}
    assert _text(echoed) == "echo:yo"
    assert _text(failed).startswith("[mcp_error]")
    assert tools["local__echo"].prompt_guidance == "local (from the server): Use echo to repeat text back."


async def test_a_stdio_command_off_the_allowlist_binds_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    from felix.config import get_settings

    monkeypatch.delenv("FELIX_MCP_STDIO_ALLOWED_COMMANDS", raising=False)
    get_settings.cache_clear()
    try:
        ref = McpServerRef(name="local", transport="stdio", command=STDIO_PYTHON, args=[str(STDIO_PEER)])
        assert await tools_from_mcp_servers([ref]) == []
    finally:
        get_settings.cache_clear()


class _TimeoutSpy:
    """Records each SDK client's `read_timeout_seconds`, then connects for real."""

    def __init__(self) -> None:
        self.seen: list[float | None] = []
        self.real = mcp_client.Client

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.seen.append(kwargs.get("read_timeout_seconds"))
        return self.real(*args, **kwargs)


async def test_the_per_server_timeout_reaches_discovery_and_the_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """`McpServerRef.timeout_ms` must reach discovery and the tool call alike: 30s was once
    unraisable, so a slow-but-working server read like one that refused."""
    spy = _TimeoutSpy()
    monkeypatch.setattr(mcp_client, "Client", spy)
    async with serve_http_peer() as peer:
        ref = _http_ref("slow", peer.url, timeout_ms=90_000)
        tools = _by_name(await tools_from_mcp_servers([ref], allow_http=True))
        await _run(tools["slow__echo"], {"text": "x"})
    # Arity as well as value: one session to discover, one to call.
    assert spy.seen == [90.0, 90.0]


async def test_the_per_server_timeout_reaches_stdio_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """stdio is the arm with the most machinery behind it, and it was the untested one."""
    from felix.config import get_settings

    spy = _TimeoutSpy()
    monkeypatch.setattr(mcp_client, "Client", spy)
    monkeypatch.setenv("FELIX_MCP_STDIO_ALLOWED_COMMANDS", STDIO_PYTHON)
    get_settings.cache_clear()
    try:
        ref = McpServerRef(
            name="local", transport="stdio", command=STDIO_PYTHON, args=[str(STDIO_PEER)], timeout_ms=90_000
        )
        tools = _by_name(await tools_from_mcp_servers([ref]))
        await _run(tools["local__echo"], {"text": "x"})
    finally:
        get_settings.cache_clear()
    assert spy.seen == [90.0, 90.0]


async def test_a_modern_server_is_called_without_forcing_the_legacy_handshake() -> None:
    async with serve_http_peer(build_peer(), stateless=True) as peer:
        ref = _http_ref("peer", peer.url)
        handshake: dict[str, Any] = {}
        await mcp_client.list_remote_tools(ref, allow_http=True, handshake=handshake)
    assert handshake["protocolVersion"] not in HANDSHAKE_PROTOCOL_VERSIONS
    assert mcp_client._call_mode(handshake) == "auto"
    assert mcp_client._call_mode({"protocolVersion": "2025-06-18"}) == "legacy"


async def test_the_sdk_dials_only_through_the_pinned_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """An https name passes the syntactic check; only the transport's pin can refuse it.

    A client that handed the SDK its default httpx2 client would dial this name; the guarded one
    resolves it once, finds a private address, and refuses before connecting.
    """
    from felix.security import ssrf
    from felix.security.ssrf import EgressBlocked

    monkeypatch.setattr(ssrf, "resolve_host", lambda host: ["10.0.0.5"])
    ref = McpServerRef(name="inner", url="https://internal.example.com/mcp", transport="http")

    def blocked(exc: BaseException) -> bool:
        if isinstance(exc, EgressBlocked):
            return True
        if isinstance(exc, BaseExceptionGroup):
            return any(blocked(e) for e in exc.exceptions)
        return any(blocked(e) for e in (exc.__cause__, exc.__context__) if e is not None)

    with pytest.raises(BaseException) as raised:
        await mcp_client.list_remote_tools(ref)
    assert blocked(raised.value), f"refused for another reason: {raised.value!r}"


async def test_a_servers_protocol_error_reaches_the_model_by_its_message_as_output() -> None:
    """The SDK's task groups wrap a server's JSON-RPC error in nested `ExceptionGroup`s; unwrapped,
    the model read "unhandled errors in a TaskGroup" and never saw what the server said. It comes
    back as output, not an exception, because the server wrote it: content screening and secret
    masking read a tool's output and never its exceptions."""
    from felix.tools.types import is_failure_content

    async with serve_http_peer() as peer:
        tools = _by_name(await tools_from_mcp_servers([_http_ref("peer", peer.url)], allow_http=True))
        out = await _run(tools["peer__strict"])
    assert _text(out) == "[mcp_error] MCP error -32602: server says bad params"
    assert is_failure_content(_text(out)), "a failed call read as a success"


async def test_a_timeout_stays_a_timeout() -> None:
    """Retry and abort logic keys on the error code: a wrapped timeout inferred as INTERNAL."""
    from felix.tools.errors import ToolErrorCode, infer_error_code

    async with serve_http_peer() as peer:
        ref = _http_ref("peer", peer.url, timeout_ms=1_000)
        tools = _by_name(await tools_from_mcp_servers([ref], allow_http=True))
        with pytest.raises(Exception) as raised:
            await _run(tools["peer__slow"])
    assert not isinstance(raised.value, BaseExceptionGroup)
    assert infer_error_code(raised.value) == ToolErrorCode.TIMEOUT


async def test_a_call_sends_nothing_after_tools_call() -> None:
    """The SDK validates a result against the tool's output schema and lists the tools *after* the
    call to find a schema it has not seen. A failure there would report a call whose side effects
    already happened as failed, and the model would repeat it. The listing comes first."""
    async with serve_http_peer(stateless=True) as peer:
        tools = _by_name(await tools_from_mcp_servers([_http_ref("peer", peer.url)], allow_http=True))
        before = len(peer.methods)
        await _run(tools["peer__echo"], {"text": "once"})
        sent = peer.methods[before:]
    assert sent, "the call sent no 2026-07-28 requests at all"
    assert sent[-1] == "tools/call", f"requests after the call: {sent}"
    assert sent.count("tools/call") == 1


def test_only_the_sdks_own_timeout_is_read_as_one() -> None:
    """`-32001` is also a server's to send -- Felix's own server refuses inbound auth with it."""
    from felix.tools.errors import ToolError, ToolErrorCode
    from mcp.shared.exceptions import MCPError

    local = mcp_client._from_mcp_error(MCPError(-32001, "Request 'tools/call' timed out"))
    remote = mcp_client._from_mcp_error(MCPError(-32001, "unauthorized"))
    assert isinstance(local, ToolError) and local.code == ToolErrorCode.TIMEOUT
    assert isinstance(remote, RuntimeError) and str(remote) == "MCP error -32001: unauthorized"


async def test_a_whole_session_has_a_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each request is bounded on its own; the session as a whole is bounded too, so a server that
    answers every page just in time, or dribbles the closing DELETE, cannot hold a call open."""
    import time

    from felix.tools.errors import ToolError, ToolErrorCode

    async with serve_http_peer() as peer:
        # A per-request timeout longer than the slow tool takes, and a session budget of 0.5s: without
        # the deadline the call returns "late" and no `ToolError` is raised at all.
        ref = _http_ref("peer", peer.url, timeout_ms=10_000)
        tools = _by_name(await tools_from_mcp_servers([ref], allow_http=True))
        monkeypatch.setattr(mcp_client, "SESSION_BUDGET_FACTOR", 0.05)
        started = time.monotonic()
        with pytest.raises(ToolError) as raised:
            await _run(tools["peer__slow"])
        elapsed = time.monotonic() - started
    assert raised.value.code == ToolErrorCode.TIMEOUT
    assert elapsed < 4, f"took {elapsed:.2f}s: the deadline did not end it"


class _ModeSpy:
    """Records each SDK client's `mode`, then connects for real."""

    def __init__(self) -> None:
        self.seen: list[str] = []
        self.real = mcp_client.Client

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.seen.append(kwargs.get("mode", "auto"))
        return self.real(*args, **kwargs)


@pytest.mark.parametrize("negotiated", ["modern", "2025-06-18"])
async def test_a_bound_tool_calls_in_the_era_discovery_negotiated(
    monkeypatch: pytest.MonkeyPatch, negotiated: str
) -> None:
    """A legacy server's calls go straight to `initialize`; a modern one's probe as discovery did.
    The SDK server speaks both, so either mode reaches it -- the spy is what tells them apart."""
    real_list = mcp_client.list_remote_tools

    async def listing(ref: McpServerRef, **kwargs: Any) -> list[dict[str, Any]]:
        tools = await real_list(ref, **kwargs)
        if negotiated != "modern":
            kwargs["handshake"]["protocolVersion"] = negotiated
        return tools

    monkeypatch.setattr(mcp_client, "list_remote_tools", listing)
    spy = _ModeSpy()
    monkeypatch.setattr(mcp_client, "Client", spy)
    async with serve_http_peer(stateless=True) as peer:
        tools = _by_name(await tools_from_mcp_servers([_http_ref("peer", peer.url)], allow_http=True))
        out = await _run(tools["peer__echo"], {"text": "m"})
    assert _text(out) == "echo:m"
    # Discovery always probes; the call follows what it found.
    assert spy.seen == ["auto", "auto" if negotiated == "modern" else "legacy"]
