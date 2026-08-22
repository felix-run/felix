"""A2A JSON-RPC surface."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from felix.a2a.server import handle_rpc
from felix.context import try_get_context
from pydantic import BaseModel, Field

router = APIRouter(tags=["A2A"])


class JsonRpcRequest(BaseModel):
    jsonrpc: str = "2.0"
    id: str | int | None = None
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


@router.post("")
@router.post("/")
async def a2a_rpc(body: JsonRpcRequest, request: Request) -> dict[str, Any]:
    from felix.context import AuthContext

    ctx = try_get_context()
    auth = ctx.auth if ctx else AuthContext()
    return await handle_rpc(
        settings=request.app.state.settings,
        tools=request.app.state.tools,
        tenant_id=auth.tenant_id,
        auth=auth,
        method=body.method,
        params=body.params,
        rpc_id=body.id,
    )
