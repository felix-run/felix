"""MCP JSON-RPC surface — tools/list + tools/call."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
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
    return await handle_rpc(
        settings=request.app.state.settings,
        tools=request.app.state.tools,
        method=body.method,
        params=body.params,
        rpc_id=body.id,
    )


@router.get("")
@router.get("/")
async def mcp_info() -> dict[str, str]:
    return {
        "status": "ok",
        "message": "POST JSON-RPC (initialize, tools/list, tools/call).",
    }
