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
