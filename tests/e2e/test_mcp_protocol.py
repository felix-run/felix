"""`/mcp` speaks both protocol eras, through the zero-argument app production boots.

The server is the official `mcp` SDK's stateless Streamable HTTP transport. A legacy client opens
with `initialize`; a `2026-07-28` client sends its version, identity and capabilities in every
request's `_meta`, mirrors the method (and a tool's name) into headers, and may ask
`server/discover` first. The path names the manifest, so a stock MCP host -- which has no way to
add Felix's `params.manifest` -- can reach any agent.
"""

from __future__ import annotations

from typing import Any

from felix.manifests.loader import parse_manifest

MODERN = "2026-07-28"
ACCEPT = "application/json, text/event-stream"


def _agent(name: str, *, tools: list[str], anonymous: bool = True) -> Any:
    spec = {"pattern": "react", "auth": {"inbound": {"allow_anonymous": anonymous}}, "tools": tools}
    return parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": name}, "spec": spec}
    )


MANIFESTS = {
    "e2e-calc": _agent("e2e-calc", tools=["calculator"]),
    "e2e-bare": _agent("e2e-bare", tools=[]),
    "e2e-closed": _agent("e2e-closed", tools=["calculator"], anonymous=False),
}


def _modern(method: str, **params: Any) -> tuple[dict[str, str], dict[str, Any]]:
    headers = {"MCP-Protocol-Version": MODERN, "Mcp-Method": method, "Accept": ACCEPT}
    if "name" in params:
        headers["Mcp-Name"] = params["name"]
    meta = {
        "io.modelcontextprotocol/protocolVersion": MODERN,
        "io.modelcontextprotocol/clientInfo": {"name": "e2e", "version": "1"},
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    return headers, {"jsonrpc": "2.0", "id": 7, "method": method, "params": {**params, "_meta": meta}}


async def _post_modern(client: Any, path: str, method: str, **params: Any) -> Any:
    headers, body = _modern(method, **params)
    return await client.post(path, json=body, headers=headers)


def _names(resp: Any) -> set[str]:
    return {t["name"] for t in resp.json()["result"]["tools"]}


async def test_a_modern_client_discovers_then_calls_a_tool_on_the_manifest_in_the_path(boot: Any) -> None:
    async with boot([], manifests=MANIFESTS) as app:
        discovered = await _post_modern(app.client, "/mcp/e2e-calc", "server/discover")
        listed = await _post_modern(app.client, "/mcp/e2e-calc", "tools/list")
        called = await _post_modern(
            app.client, "/mcp/e2e-calc", "tools/call", name="calculator", arguments={"expression": "6*7"}
        )
    result = discovered.json()["result"]
    assert MODERN in result["supportedVersions"]
    assert "tools" in result["capabilities"]
    assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "felix"
    # Who asks decides what compiles, so nothing may be cached across callers.
    assert result["cacheScope"] == "private"
    assert listed.json()["result"]["resultType"] == "complete"
    assert listed.json()["result"]["cacheScope"] == "private"
    assert "calculator" in _names(listed)
    assert called.status_code == 200
    call = called.json()["result"]
    assert call["content"][0]["text"] == "42" and call["isError"] is False


async def test_the_path_selects_the_manifest(boot: Any) -> None:
    async with boot([], manifests=MANIFESTS) as app:
        calc = await _post_modern(app.client, "/mcp/e2e-calc", "tools/list")
        bare = await _post_modern(app.client, "/mcp/e2e-bare", "tools/list")
        # A legacy client naming a different manifest in params loses to the path.
        legacy = await app.client.post(
            "/mcp/e2e-bare",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"manifest": "e2e-calc"}},
            headers={"Accept": ACCEPT},
        )
    assert "calculator" in _names(calc)
    assert "calculator" not in _names(bare)
    assert "calculator" not in _names(legacy)


async def test_a_legacy_client_still_handshakes_and_names_its_manifest_in_params(boot: Any) -> None:
    async with boot([], manifests=MANIFESTS) as app:
        init = await app.client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "legacy", "version": "1"},
                },
            },
            headers={"Accept": ACCEPT},
        )
        initialized = await app.client.post(
            "/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers={"Accept": ACCEPT}
        )
        listed = await app.client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"manifest": "e2e-bare"}},
            headers={"Accept": ACCEPT, "MCP-Protocol-Version": "2025-06-18"},
        )
    assert init.json()["result"]["protocolVersion"] == "2025-06-18"
    assert init.json()["result"]["serverInfo"]["name"] == "felix"
    assert initialized.status_code == 202 and initialized.content == b""
    # The default manifest lists `calculator`; the one named in params does not.
    assert "calculator" not in _names(listed)


async def test_modern_requests_are_held_to_the_transport_rules(boot: Any) -> None:
    async with boot([], manifests=MANIFESTS) as app:
        headers, body = _modern("tools/list")
        headers["Mcp-Method"] = "tools/call"
        mismatched = await app.client.post("/mcp/e2e-calc", json=body, headers=headers)
        unknown = await _post_modern(app.client, "/mcp/e2e-calc", "prompts/list")
        headers, body = _modern("tools/list")
        headers["MCP-Protocol-Version"] = "1900-01-01"
        body["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"] = "1900-01-01"
        unsupported = await app.client.post("/mcp/e2e-calc", json=body, headers=headers)
        stream = await app.client.get("/mcp/e2e-calc")
    assert mismatched.status_code == 400 and mismatched.json()["error"]["code"] == -32020
    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == -32601
    assert unsupported.status_code == 400 and unsupported.json()["error"]["code"] == -32022
    assert MODERN in unsupported.json()["error"]["data"]["supported"]
    assert stream.status_code == 405 and stream.headers["allow"] == "POST"


async def test_inbound_auth_still_refuses_an_anonymous_caller(boot: Any) -> None:
    async with boot([], manifests=MANIFESTS) as app:
        refused = await _post_modern(app.client, "/mcp/e2e-closed", "tools/list")
        missing = await _post_modern(app.client, "/mcp/e2e-nowhere", "tools/list")
    # Felix's own codes are outside the SDK's status table, so they stay HTTP 200 on either era.
    assert refused.status_code == 200 and missing.status_code == 200
    assert refused.json()["error"]["code"] == -32001
    assert "result" not in refused.json()
    assert missing.json()["error"] == {"code": -32004, "message": "Unknown manifest: e2e-nowhere"}


async def test_a_modern_tool_call_compiles_the_manifest_once(boot: Any, monkeypatch: Any) -> None:
    """The SDK validates `Mcp-Param-*` headers against the called tool's schema, and without a
    lookup it finds the schema by running `tools/list` -- a whole second compile per call, and for
    a refused caller a logged traceback per request. Felix routes on no such header."""
    from felix.mcp import server

    compiles: list[str] = []
    real = server._compiled_tools

    async def counted(**kwargs: Any) -> Any:
        compiles.append(kwargs["manifest_name"])
        return await real(**kwargs)

    monkeypatch.setattr(server, "_compiled_tools", counted)
    async with boot([], manifests=MANIFESTS) as app:
        called = await _post_modern(
            app.client, "/mcp/e2e-calc", "tools/call", name="calculator", arguments={"expression": "1+1"}
        )
    assert called.json()["result"]["content"][0]["text"] == "2"
    assert compiles == ["e2e-calc"]


async def test_an_oversized_chunked_body_is_refused_on_the_modern_path(boot: Any) -> None:
    """Felix's body cap cuts a chunked body the SDK is reading; that must stay a 413, not a 500."""
    headers, _body = _modern("tools/list")

    async def chunks() -> Any:
        for _ in range(64):
            yield b" " * 65_536

    async with boot([], manifests=MANIFESTS) as app:
        resp = await app.client.post("/mcp/e2e-calc", content=chunks(), headers=headers)
    assert resp.status_code == 413


async def test_modern_tool_failures_are_results_or_400s(boot: Any) -> None:
    async with boot([], manifests=MANIFESTS) as app:
        unknown = await _post_modern(app.client, "/mcp/e2e-calc", "tools/call", name="nope", arguments={})
        failed = await _post_modern(
            app.client, "/mcp/e2e-calc", "tools/call", name="calculator", arguments={"expression": "1/0"}
        )
    assert unknown.status_code == 400 and unknown.json()["error"]["code"] == -32602
    assert failed.status_code == 200
    assert failed.json()["result"]["isError"] is True


async def test_concurrent_requests_each_compile_their_own_manifest(boot: Any) -> None:
    """One cached SDK `Server` serves every request; the manifest rides each exchange's context."""
    import asyncio

    async with boot([], manifests=MANIFESTS) as app:
        replies = await asyncio.gather(
            *(_post_modern(app.client, f"/mcp/{name}", "tools/list") for name in ["e2e-calc", "e2e-bare"] * 4)
        )
    for name, reply in zip(["e2e-calc", "e2e-bare"] * 4, replies, strict=True):
        assert ("calculator" in _names(reply)) is (name == "e2e-calc")
