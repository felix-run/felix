"""OpenAI-compatible surface — POST /v1/chat/completions, GET /v1/models."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from felix.context import AuthContext, RequestContext, async_run_with_context, try_get_context
from felix.logging_setup import loggable
from felix.manifests.inbound_auth import InboundAuthError
from felix.manifests.loader import list_bundled
from felix.manifests.pin import ManifestDriftError
from felix.patterns.model import ModelGatewayError
from felix.patterns.types import ChatMessage, InvokeInput
from felix.runtime import build_tenant_agent, prepare_tenant_invoke, resolve_tenant_manifest
from pydantic import BaseModel, Field

from felix_api.errors import client_safe_message
from felix_api.threads import effective_thread_id

logger = logging.getLogger("felix_api.routes.openai_compat")

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
    # Warm, the resolver cache hides this. Cold -- the first request after a deploy,
    # which is exactly when a client is probing -- it was one round trip per manifest,
    # in series.
    #
    # `return_exceptions=True` keeps the per-manifest fallback: a manifest that fails to
    # resolve is still listed, with whatever metadata `catalog_from_manifest` can derive
    # from its name alone. Without it, one bad manifest would empty the whole catalogue.
    resolved = await asyncio.gather(
        *(resolve_tenant_manifest(settings, auth.tenant_id, name, thread_id=None) for name in names),
        return_exceptions=True,
    )
    data: list[dict[str, Any]] = [
        catalog_from_manifest(
            name,
            None if isinstance(item, BaseException) else getattr(item, "manifest", None),
        )
        for name, item in zip(names, resolved, strict=True)
    ]
    return {"object": "list", "data": data}


@router.post("/chat/completions")
async def chat_completions(body: ChatCompletionsRequest, request: Request) -> Any:
    settings = request.app.state.settings
    tools = request.app.state.tools
    auth = _auth(request)
    # `effective_thread_id`, not a hand-rolled prefix. The tenant was applied, so this
    # was never cross-tenant — but `body.user` was never delimiter-screened, so a
    # client could send `user: "fiber:abc123"` or `"job:nightly"` and append its turns
    # to a durable fiber's or a scheduled job's session log, which that run then
    # replays as history. It also minted ids the chat routes can never address.
    thread = effective_thread_id(auth.tenant_id, body.user)
    if body.user and thread is None:
        return JSONResponse(
            {"error": {"message": "invalid user", "type": "invalid_request_error", "code": "invalid_user"}},
            status_code=400,
        )

    try:
        resolved = await resolve_tenant_manifest(settings, auth.tenant_id, body.model, thread_id=thread)
        await prepare_tenant_invoke(settings, resolved=resolved, auth=auth, thread_id=thread)
    # Two `except` clauses rather than one broad catch narrowed by `isinstance`. The
    # old shape caught everything and re-raised what it did not recognise, which meant
    # every exception in the request -- including ones with driver internals in their
    # message -- passed through a scope that formats messages for clients. Naming the
    # types keeps that reach as small as the intent always was.
    except InboundAuthError as exc:
        return JSONResponse(
            {"error": {"message": client_safe_message(exc), "type": "auth_error", "code": exc.detail}},
            status_code=exc.status_code,
        )
    except ManifestDriftError as exc:
        return JSONResponse(
            {"error": {"message": client_safe_message(exc), "type": "manifest_drift", "code": "conflict"}},
            status_code=409,
        )

    messages = [
        ChatMessage.model_validate({"role": m.role, "content": m.content or ""}) for m in body.messages
    ]
    # Imported here rather than at module scope to keep the governance package off the
    # import path of a lean install, but *before* the try so the handler can name the
    # type it means. The old shape caught everything, tested with `isinstance`, and
    # re-raised the rest -- which put every exception in this call through a scope whose
    # job is formatting messages for clients.
    from felix.governance.inbound import InboundScreeningError, apply_inbound_screening

    try:
        messages = await apply_inbound_screening(resolved.manifest, messages, settings)
    except InboundScreeningError as exc:
        return JSONResponse(
            {"error": {"message": client_safe_message(exc), "type": "content_filter", "code": exc.detail}},
            status_code=exc.status_code,
        )
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
            # `body` is an upstream response: untrusted and multi-line. Same
            # treatment as the two copies of this line in chat.py.
            logger.warning(
                "model gateway error label=%s status=%s body=%s",
                loggable(exc.label, limit=80),
                exc.status,
                loggable(exc.body),
            )
            return JSONResponse(
                {
                    "error": {
                        "message": client_safe_message(exc),
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
