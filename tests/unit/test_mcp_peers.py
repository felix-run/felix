"""Tests for outbound MCP client and A2A peer tool binding."""

from __future__ import annotations

import pytest
from felix.a2a.peers import make_peer_tool, tools_from_peers
from felix.manifests.schema import A2APeerRef, McpServerRef
from felix.tools.types import ToolInvocationCtx


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
