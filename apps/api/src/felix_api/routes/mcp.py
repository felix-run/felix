"""MCP JSON-RPC surface — tools/list + tools/call."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from felix.context import AuthContext, try_get_context
from felix.mcp.server import handle_rpc
from pydantic import BaseModel, Field

router = APIRouter(tags=["MCP"])


class McpJsonRpc(BaseModel):
    jsonrpc: str = "2.0"
    id: str | int | None = None
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


@router.post("")
@router.post("/")
async def mcp_rpc(body: McpJsonRpc, request: Request) -> dict[str, Any]:
    ctx = try_get_context()
    auth = ctx.auth if ctx else AuthContext()
    return await handle_rpc(
        settings=request.app.state.settings,
        tools=request.app.state.tools,
        method=body.method,
        params=body.params,
        rpc_id=body.id,
        auth=auth,
    )


@router.get("")
@router.get("/")
async def mcp_info() -> dict[str, str]:
    return {
        "status": "ok",
        "message": "POST JSON-RPC (initialize, tools/list, tools/call). "
        "Pass params.manifest to select an agent (default: FELIX_DEFAULT_MANIFEST).",
    }
