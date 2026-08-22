"""OpenAI-compatible surface — POST /v1/chat/completions, GET /v1/models."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from felix.context import AuthContext, RequestContext, async_run_with_context, try_get_context
from felix.manifests.loader import list_bundled
from felix.patterns.model import ModelGatewayError
from felix.patterns.types import ChatMessage, InvokeInput
from felix.runtime import build_tenant_agent, prepare_tenant_invoke, resolve_tenant_manifest
from pydantic import BaseModel, Field

router = APIRouter(tags=["OpenAI"])


class OpenAIMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    name: str | None = None
    tool_call_id: str | None = None


class ChatCompletionsRequest(BaseModel):
    model: str = Field(description="Manifest name (Felix uses model as the agent id).")
    messages: list[OpenAIMessage] = Field(min_length=1)
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    user: str | None = None


def _usage_payload(ctx: RequestContext) -> dict[str, int]:
    ls = ctx.limit_state
    return {
        "prompt_tokens": ls.tokens_input,
        "completion_tokens": ls.tokens_output,
        "total_tokens": ls.tokens_input + ls.tokens_output,
    }


def _auth(request: Request) -> AuthContext:
    ctx = try_get_context()
    if ctx is not None:
        return ctx.auth
    return AuthContext()


@router.get("/models")
async def list_models(request: Request) -> dict[str, Any]:
    """List bundled + discoverable manifests as OpenAI models with Felix catalog metadata."""
    from felix.usage.catalog import catalog_from_manifest

    settings = request.app.state.settings
    auth = _auth(request)
    names = list_bundled()
    data: list[dict[str, Any]] = []
    for name in names:
        manifest = None
        try:
            resolved = await resolve_tenant_manifest(settings, auth.tenant_id, name, thread_id=None)
            manifest = resolved.manifest
        except Exception:
            manifest = None
        data.append(catalog_from_manifest(name, manifest))
    return {"object": "list", "data": data}


@router.post("/chat/completions")
async def chat_completions(body: ChatCompletionsRequest, request: Request) -> Any:
    settings = request.app.state.settings
    tools = request.app.state.tools
    auth = _auth(request)
    thread = f"{auth.tenant_id}:{body.user}" if body.user else None

    try:
        resolved = await resolve_tenant_manifest(settings, auth.tenant_id, body.model, thread_id=thread)
        await prepare_tenant_invoke(settings, resolved=resolved, auth=auth, thread_id=thread)
    except Exception as exc:
        from felix.manifests.inbound_auth import InboundAuthError
        from felix.manifests.pin import ManifestDriftError

        if isinstance(exc, InboundAuthError):
            return JSONResponse(
                {"error": {"message": exc.detail, "type": "auth_error", "code": exc.detail}},
                status_code=exc.status_code,
            )
        if isinstance(exc, ManifestDriftError):
            return JSONResponse(
                {"error": {"message": str(exc), "type": "manifest_drift", "code": "conflict"}},
                status_code=409,
            )
        raise

    messages = [
        ChatMessage.model_validate({"role": m.role, "content": m.content or ""}) for m in body.messages
    ]
    try:
        from felix.governance.inbound import apply_inbound_screening

        messages = await apply_inbound_screening(resolved.manifest, messages, settings)
    except Exception as exc:
        from felix.governance.inbound import InboundScreeningError as _ISE

        if isinstance(exc, _ISE):
            return JSONResponse(
                {"error": {"message": exc.detail, "type": "content_filter", "code": exc.detail}},
                status_code=exc.status_code,
            )
        raise
    req_ctx = RequestContext(
        settings=settings,
        auth=auth,
        manifest_id=body.model,
        thread_id=thread,
    )
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    invoke_input = InvokeInput(messages=messages, thread_id=thread)

    if body.stream:

        async def event_gen():
            async with async_run_with_context(req_ctx):
                agent = await build_tenant_agent(
                    settings,
                    manifest=resolved.manifest,
                    tools=tools,
                    tenant_id=auth.tenant_id,
                )
                async for event in agent.stream_events(invoke_input):
                    text = getattr(event, "text", "") or ""
                    chunk = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": body.model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": text} if text else {},
                                "finish_reason": None,
                            }
                        ],
                    }
                    yield f"data: {json.dumps(chunk)}\n\n"
            done = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": body.model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": _usage_payload(req_ctx),
            }
            yield f"data: {json.dumps(done)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(event_gen(), media_type="text/event-stream")

    async with async_run_with_context(req_ctx):
        try:
            agent = await build_tenant_agent(
                settings,
                manifest=resolved.manifest,
                tools=tools,
                tenant_id=auth.tenant_id,
            )
            result = await agent.invoke(invoke_input)
        except ModelGatewayError as exc:
            return JSONResponse(
                {
                    "error": {
                        "message": str(exc),
                        "type": "model_gateway_error",
                        "code": "model_unavailable",
                    }
                },
                status_code=502,
            )

    content = result.final.content if result.final else ""
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": body.model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": _usage_payload(req_ctx),
    }
