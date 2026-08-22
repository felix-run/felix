"""POST /chat and /chat/stream — direct REST + SSE agent invocation."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from felix.context import AuthContext, RequestContext, async_run_with_context, try_get_context
from felix.patterns.model import ModelGatewayError
from felix.patterns.types import ChatMessage, InvokeInput
from felix.runtime import build_tenant_agent, resolve_tenant_manifest
from pydantic import BaseModel, Field

router = APIRouter(tags=["Threads"])

_SUFFIX_DELIMS = frozenset(":#")


class ChatRequest(BaseModel):
    model_config = {"extra": "forbid"}

    manifest: str = Field(description="Manifest name to invoke.")
    messages: list[dict[str, Any]] = Field(min_length=1)
    thread_id: str | None = Field(
        default=None,
        description="Optional thread-id suffix; server prefixes the tenant id.",
    )


def effective_thread_id(tenant_id: str, suffix: str | None) -> str | None:
    if not suffix:
        return None
    if any(c in suffix for c in _SUFFIX_DELIMS):
        return None
    return f"{tenant_id}:{suffix}"


def _auth_from_request(request: Request) -> AuthContext:
    ctx = try_get_context()
    if ctx is not None:
        return ctx.auth
    return AuthContext()


@router.post("")
@router.post("/")
async def chat(body: ChatRequest, request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    tools = request.app.state.tools
    auth = _auth_from_request(request)
    thread = effective_thread_id(auth.tenant_id, body.thread_id)
    if body.thread_id and thread is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")

    resolved = await resolve_tenant_manifest(
        settings, auth.tenant_id, body.manifest, thread_id=thread
    )
    messages = [ChatMessage.model_validate(m) for m in body.messages]
    req_ctx = RequestContext(
        settings=settings,
        auth=auth,
        manifest_id=body.manifest,
        thread_id=thread,
    )
    async with async_run_with_context(req_ctx):
        try:
            agent = await build_tenant_agent(
                settings,
                manifest=resolved.manifest,
                tools=tools,
                tenant_id=auth.tenant_id,
            )
            result = await agent.invoke(InvokeInput(messages=messages, thread_id=thread))
        except ModelGatewayError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    final = result.final
    return {
        "messages": [m.model_dump() for m in result.messages],
        "final": final.model_dump() if hasattr(final, "model_dump") else final,
        "thread_id": thread,
    }


@router.post("/stream")
async def chat_stream(body: ChatRequest, request: Request) -> StreamingResponse:
    settings = request.app.state.settings
    tools = request.app.state.tools
    auth = _auth_from_request(request)
    thread = effective_thread_id(auth.tenant_id, body.thread_id)
    if body.thread_id and thread is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")

    resolved = await resolve_tenant_manifest(
        settings, auth.tenant_id, body.manifest, thread_id=thread
    )
    messages = [ChatMessage.model_validate(m) for m in body.messages]
    req_ctx = RequestContext(
        settings=settings,
        auth=auth,
        manifest_id=body.manifest,
        thread_id=thread,
    )

    async def event_gen():
        import json

        async with async_run_with_context(req_ctx):
            agent = await build_tenant_agent(
                settings,
                manifest=resolved.manifest,
                tools=tools,
                tenant_id=auth.tenant_id,
            )
            async for event in agent.stream_events(
                InvokeInput(messages=messages, thread_id=thread)
            ):
                payload = event.model_dump() if hasattr(event, "model_dump") else event
                yield f"data: {json.dumps(payload, default=str)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream")
