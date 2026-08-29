"""Outbound MCP client — discover remote tools and bind them as Felix Tools."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from felix.manifests.schema import McpServerRef
from felix.security.ssrf import assert_safe_outbound_url
from felix.timeouts import DEFAULT_CONNECT_TIMEOUT_S, timeout_seconds
from felix.tools.types import Tool, ToolInvocationCtx, define_tool

logger = logging.getLogger("felix.mcp.client")

_RPC_ID = 0


def _next_id() -> int:
    global _RPC_ID
    _RPC_ID += 1
    return _RPC_ID


DEFAULT_MCP_TIMEOUT_S = 30.0


def _timeout_s(ref: McpServerRef) -> float:
    """Per-server request timeout in seconds."""
    return timeout_seconds(ref.timeout_ms, default_s=DEFAULT_MCP_TIMEOUT_S)


def _headers(auth: str) -> dict[str, str]:
    headers = {
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
    }
    if not auth:
        return headers
    if auth.lower().startswith("bearer ") or auth.lower().startswith("basic "):
        headers["authorization"] = auth
    else:
        headers["authorization"] = f"Bearer {auth}"
    return headers


async def mcp_rpc(
    url: str,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    auth: str = "",
    allow_http: bool = False,
    wait_s: float = DEFAULT_MCP_TIMEOUT_S,
) -> dict[str, Any]:
    """POST a JSON-RPC request to an MCP HTTP endpoint."""
    assert_safe_outbound_url(url, allow_http=allow_http)
    payload = {
        "jsonrpc": "2.0",
        "id": _next_id(),
        "method": method,
        "params": params or {},
    }
    timeout = httpx.Timeout(wait_s, connect=DEFAULT_CONNECT_TIMEOUT_S)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        resp = await client.post(url, json=payload, headers=_headers(auth))
        resp.raise_for_status()
        # Some MCP HTTP servers return SSE; take the first JSON data line if needed.
        ctype = (resp.headers.get("content-type") or "").lower()
        if "text/event-stream" in ctype:
            data = None
            for line in resp.text.splitlines():
                if line.startswith("data:"):
                    import json

                    raw = line[5:].strip()
                    if raw and raw != "[DONE]":
                        data = json.loads(raw)
                        break
            if data is None:
                raise RuntimeError(f"MCP SSE response had no data frames from {url}")
            body = data
        else:
            body = resp.json()
    if not isinstance(body, dict):
        raise RuntimeError(f"MCP response was not an object: {type(body)}")
    if body.get("error"):
        err = body["error"]
        raise RuntimeError(f"MCP error {err.get('code')}: {err.get('message')}")
    return body.get("result") if "result" in body else body


async def list_remote_tools(
    ref: McpServerRef,
    *,
    allow_http: bool = False,
) -> list[dict[str, Any]]:
    if ref.transport == "stdio":
        from felix.mcp.stdio import list_stdio_tools

        return await list_stdio_tools(ref, wait_s=_timeout_s(ref))
    try:
        await mcp_rpc(
            ref.url,
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "felix", "version": "0.1.0"},
            },
            auth=ref.auth,
            allow_http=allow_http,
            wait_s=_timeout_s(ref),
        )
    except Exception:
        logger.debug("MCP initialize failed for %s (continuing)", ref.name, exc_info=True)
    result = await mcp_rpc(
        ref.url, "tools/list", {}, auth=ref.auth, allow_http=allow_http, wait_s=_timeout_s(ref)
    )
    tools = result.get("tools") if isinstance(result, dict) else None
    if not isinstance(tools, list):
        return []
    return [t for t in tools if isinstance(t, dict) and t.get("name")]


def _bind_remote_tool(
    ref: McpServerRef,
    remote: dict[str, Any],
    *,
    allow_http: bool,
    name_prefix: bool = True,
) -> Tool:
    remote_name = str(remote["name"])
    local_name = f"{ref.name}__{remote_name}" if name_prefix else remote_name
    description = str(remote.get("description") or f"MCP tool {remote_name} via {ref.name}")
    schema = (
        remote.get("inputSchema")
        or remote.get("input_schema")
        or {
            "type": "object",
            "properties": {},
        }
    )

    async def handler(args: dict[str, Any], _ctx: ToolInvocationCtx | None = None) -> str:
        if ref.transport == "stdio":
            from felix.mcp.stdio import stdio_rpc

            result = await stdio_rpc(
                ref, "tools/call", {"name": remote_name, "arguments": args or {}}, wait_s=_timeout_s(ref)
            )
        else:
            result = await mcp_rpc(
                ref.url,
                "tools/call",
                {"name": remote_name, "arguments": args or {}},
                auth=ref.auth,
                allow_http=allow_http,
                wait_s=_timeout_s(ref),
            )
        if isinstance(result, dict):
            content = result.get("content")
            if isinstance(content, list):
                texts = [
                    str(c.get("text") or "")
                    for c in content
                    if isinstance(c, dict) and c.get("type") == "text"
                ]
                joined = "\n".join(t for t in texts if t)
                if joined:
                    if result.get("isError"):
                        return f"[mcp_error] {joined}"
                    return joined
            return str(result)
        return str(result)

    return define_tool(
        name=local_name,
        description=description,
        handler=handler,
        raw_input_schema=schema if isinstance(schema, dict) else None,
        source=f"mcp:{ref.name}",
        transport="mcp",
    )


async def tools_from_mcp_servers(
    refs: list[McpServerRef],
    *,
    allow_http: bool = False,
) -> list[Tool]:
    """Discover and bind tools from each MCP server ref."""
    out: list[Tool] = []
    for ref in refs:
        try:
            remotes = await list_remote_tools(ref, allow_http=allow_http)
        except Exception:
            logger.warning("failed to list MCP tools from %s", ref.name, exc_info=True)
            continue
        for remote in remotes:
            try:
                out.append(_bind_remote_tool(ref, remote, allow_http=allow_http))
            except Exception:
                logger.debug("skip remote tool %s from %s", remote.get("name"), ref.name, exc_info=True)
    return out


__all__ = ["DEFAULT_MCP_TIMEOUT_S", "list_remote_tools", "mcp_rpc", "tools_from_mcp_servers"]
