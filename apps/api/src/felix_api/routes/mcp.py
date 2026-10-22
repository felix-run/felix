"""MCP surface — `/mcp` (the default manifest) and `/mcp/{manifest_name}`, served by the `mcp` SDK.

Each route hands its raw ASGI exchange to `felix.mcp.server.serve_mcp`: the SDK reads the body,
picks the protocol era, validates the headers and writes the response. The routes declare no body
parameter, so FastAPI leaves the request stream for the SDK to read.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from felix.mcp.server import McpTarget, serve_mcp
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

router = APIRouter(tags=["MCP"])

_JSONRPC_BODY: dict[str, Any] = {
    "requestBody": {
        "required": True,
        "description": "One JSON-RPC 2.0 request or notification (Model Context Protocol, Streamable HTTP).",
        "content": {"application/json": {"schema": {"type": "object"}}},
    },
    "responses": {
        "200": {
            "description": "The JSON-RPC response.",
            "content": {"application/json": {"schema": {"type": "object"}}},
        },
        "202": {"description": "A notification, accepted."},
    },
}


class _McpExchange(Response):
    """Not a response body: the whole exchange, answered by the SDK."""

    def __init__(self, target: McpTarget) -> None:
        super().__init__()
        self._target = target

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await serve_mcp(self._target, scope, receive, send)


def _exchange(request: Request, manifest_name: str | None) -> Response:
    return _McpExchange(
        McpTarget(settings=request.app.state.settings, tools=request.app.state.tools, manifest=manifest_name)
    )


@router.post("", openapi_extra=_JSONRPC_BODY, response_class=Response)
@router.post("/", include_in_schema=False, response_class=Response)
async def mcp_default(request: Request) -> Response:
    """MCP for the default manifest (`FELIX_DEFAULT_MANIFEST`); a legacy client may name another
    in `params.manifest`."""
    return _exchange(request, None)


@router.post("/{manifest_name}", openapi_extra=_JSONRPC_BODY, response_class=Response)
async def mcp_manifest(manifest_name: str, request: Request) -> Response:
    """MCP for one manifest: its governed tools, compiled as the caller."""
    return _exchange(request, manifest_name)


@router.get("", include_in_schema=False)
@router.get("/", include_in_schema=False)
@router.get("/{manifest_name}", include_in_schema=False)
async def mcp_get() -> Response:
    """No standalone server-to-client stream: every exchange is one stateless POST.

    Not handed to the SDK, which would open a legacy GET stream that nothing ever writes to or
    closes. A 405 is what both eras specify for a server that offers no such stream.
    """
    return Response(status_code=405, headers={"Allow": "POST"})
