"""Tests for outbound MCP client and A2A peer tool binding."""

from __future__ import annotations

import json

import pytest
from felix.a2a.peers import make_peer_tool, tools_from_peers
from felix.manifests.schema import A2APeerRef, McpServerRef
from felix.mcp.client import tools_from_mcp_servers
from felix.tools.types import ToolInvocationCtx


@pytest.mark.asyncio
async def test_mcp_rpc_and_tool_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class _Resp:
        status_code = 200
        headers = {"content-type": "application/json"}

        def __init__(self, body: dict):
            self._body = body

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self._body

        @property
        def text(self) -> str:
            return json.dumps(self._body)

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            method = (json or {}).get("method")
            calls.append(str(method))
            if method == "initialize":
                return _Resp({"jsonrpc": "2.0", "id": 1, "result": {}})
            if method == "tools/list":
                return _Resp(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "result": {
                            "tools": [
                                {
                                    "name": "search",
                                    "description": "Search docs",
                                    "inputSchema": {
                                        "type": "object",
                                        "properties": {"q": {"type": "string"}},
                                    },
                                }
                            ]
                        },
                    }
                )
            if method == "tools/call":
                return _Resp(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "result": {
                            "content": [{"type": "text", "text": "found:42"}],
                            "isError": False,
                        },
                    }
                )
            return _Resp({"jsonrpc": "2.0", "id": 0, "error": {"code": -1, "message": "nope"}})

    import felix.mcp.client as client_mod

    monkeypatch.setattr(client_mod.httpx, "AsyncClient", _Client)

    ref = McpServerRef(name="docs", url="https://mcp.example.com/mcp", transport="http")
    tools = await tools_from_mcp_servers([ref], allow_http=False)
    assert len(tools) == 1
    assert tools[0].name == "docs__search"
    assert tools[0].executor.transport == "mcp"
    out = await tools[0].executor.execute({"q": "felix"}, ToolInvocationCtx())
    text = out if isinstance(out, str) else out.content
    assert "found:42" in text
    assert "tools/list" in calls
    assert "tools/call" in calls


@pytest.mark.asyncio
async def test_peer_tool_message_send(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Resp:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "status": {
                        "message": {
                            "parts": [{"type": "text", "text": "peer says hi"}],
                        }
                    }
                },
            }

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            assert url.endswith("/a2a")
            assert json["method"] == "message/send"
            return _Resp()

    import felix.a2a.peers as peers_mod

    monkeypatch.setattr(peers_mod.httpx, "AsyncClient", _Client)

    ref = A2APeerRef(name="helper", url="https://peer.example.com")
    tool = make_peer_tool(ref)
    assert tool.name == "peer__helper"
    assert tool.is_peer
    assert tool.executor.transport == "a2a"
    out = await tool.executor.execute({"message": "hello"}, ToolInvocationCtx())
    text = out if isinstance(out, str) else out.content
    assert "peer says hi" in text

    tools = tools_from_peers([ref])
    assert len(tools) == 1


@pytest.mark.asyncio
async def test_mcp_timeout_is_per_server_over_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """`McpServerRef.timeout_ms` must reach discovery and the tool call alike.

    30s was unraisable, so a slow-but-working server was unusable and the only symptom was a
    tool result that read like the server had refused. `ContainerRef` and `SandboxRef` both
    already carried a timeout; this closed the parity gap.
    """
    from felix.mcp import client as mcp_client

    seen: list[float] = []

    async def _fake_rpc(url, method, params=None, *, auth="", allow_http=False, wait_s=30.0):
        seen.append(wait_s)
        if method == "tools/list":
            return {"tools": [{"name": "echo", "description": "e", "inputSchema": {}}]}
        return {"content": [{"type": "text", "text": "ok"}]}

    monkeypatch.setattr(mcp_client, "mcp_rpc", _fake_rpc)
    ref = McpServerRef(name="slow", url="https://example.com/mcp", transport="http", timeout_ms=90_000)
    tools = await tools_from_mcp_servers([ref])
    assert [t.name for t in tools] == ["slow__echo"]

    await tools[0].executor.execute({}, ToolInvocationCtx(manifest_id="m", tool_call_id="c"))
    # Arity as well as value: a subset assertion would stay green if a call site lost its
    # `wait_s` entirely. initialize + tools/list + tools/call.
    assert seen == [90.0, 90.0, 90.0]


@pytest.mark.asyncio
async def test_mcp_timeout_is_per_server_over_stdio(monkeypatch: pytest.MonkeyPatch) -> None:
    """stdio is the arm with the most machinery behind it, and it was the untested one."""
    from felix.mcp import stdio as mcp_stdio

    seen: list[float] = []

    async def _fake_stdio_rpc(ref, method, params=None, *, wait_s=30.0, settings=None):
        seen.append(wait_s)
        if method == "tools/list":
            return {"tools": [{"name": "echo", "description": "e", "inputSchema": {}}]}
        return {"content": [{"type": "text", "text": "ok"}]}

    monkeypatch.setattr(mcp_stdio, "stdio_rpc", _fake_stdio_rpc)
    ref = McpServerRef(name="local", transport="stdio", command="/usr/bin/true", timeout_ms=90_000)
    tools = await tools_from_mcp_servers([ref])
    assert [t.name for t in tools] == ["local__echo"]

    await tools[0].executor.execute({}, ToolInvocationCtx(manifest_id="m", tool_call_id="c"))
    assert seen == [90.0, 90.0], "discovery and the call must both carry the timeout"


def test_mcp_timeout_default_and_floor() -> None:
    from felix.mcp.client import DEFAULT_MCP_TIMEOUT_S, _timeout_s

    base = McpServerRef(name="a", url="https://e.com/m")
    # Assert the contract, not the constant against itself.
    assert DEFAULT_MCP_TIMEOUT_S == 30.0
    assert _timeout_s(base) == 30.0
    assert _timeout_s(McpServerRef(name="a", url="https://e.com/m", timeout_ms=5_000)) == 5.0
    # A sub-second timeout would fail every real call; floor it rather than honour it.
    # 0 and negatives no longer reach here at all — the schema rejects them.
    assert _timeout_s(McpServerRef(name="a", url="https://e.com/m", timeout_ms=10)) == 1.0


def test_peer_timeout_is_per_peer() -> None:
    """`A2APeerRef` was the ref left without a timeout once MCP gained one.

    A peer call runs a whole agent turn on the far side, so a hardcoded 60s was the tightest
    ceiling on the longest operation.
    """
    from felix.a2a.peers import DEFAULT_PEER_TIMEOUT_S, _peer_timeout

    assert _peer_timeout(A2APeerRef(name="p", url="https://e.com")).read == DEFAULT_PEER_TIMEOUT_S
    t = _peer_timeout(A2APeerRef(name="p", url="https://e.com", timeout_ms=300_000))
    assert t.read == 300.0
    # Connect must not scale with the request ceiling.
    assert t.connect == 10.0


def test_every_outbound_client_pins_connect_separately() -> None:
    """Connect must not scale with the request ceiling, on every outbound client.

    The container executor was the one that missed it, and it is the one carrying a
    tenant-supplied `timeout_ms` — so a gateway on a public address that blackholes SYN
    could park a socket for the full request ceiling, per tool call.
    """
    from felix.manifests.schema import ContainerRef
    from felix.memory.embedder import OpenAIEmbedder
    from felix.timeouts import DEFAULT_CONNECT_TIMEOUT_S, request_timeout
    from felix.tools.sandboxes import _ContainerExecutor
    from felix.tools.transports import HttpExecutor

    ref = ContainerRef(name="c", gateway_url="https://example.com", image="i", timeout_ms=3_600_000)
    ex = _ContainerExecutor(
        gateway_url=ref.gateway_url,
        image=ref.image,
        timeout_ms=ref.timeout_ms,
        auth="",
        allow_http=False,
    )
    assert ex._timeout_s == 3600.0

    # The shared helper is what guarantees the split; assert it directly for each default.
    for default in (30.0, 60.0):
        t = request_timeout(None, default_s=default)
        assert t.read == default
        assert t.connect == DEFAULT_CONNECT_TIMEOUT_S

    assert HttpExecutor("https://example.com/x")._timeout_s == 30.0
    assert OpenAIEmbedder(model="m", api_key="", base_url="https://e.com")._timeout_s == 60.0
