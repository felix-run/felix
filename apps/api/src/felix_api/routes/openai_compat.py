"""OpenAI-compatible surface — POST /v1/chat/completions, GET /v1/models."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from felix.context import AuthContext, RequestContext, async_run_with_context, try_get_context
from felix.governance.reply import REPLY_TEXT_EVENTS
from felix.logging_setup import loggable
from felix.manifests.inbound_auth import InboundAuthError
from felix.manifests.loader import list_bundled
from felix.manifests.pin import ManifestDriftError
from felix.patterns.model import ModelGatewayError
from felix.patterns.types import ChatMessage, InvokeInput
from felix.runtime import build_tenant_agent, prepare_tenant_invoke, resolve_tenant_manifest
from felix_ai.types import ModelChatOptions
from felix_ai.wire.openai_completions import finish_reason_for
from pydantic import BaseModel, Field

from felix_api.errors import client_safe_message, log_gateway_error
from felix_api.routes._sse import DONE, HEARTBEAT, KEEP_ALIVE, sse_response, with_heartbeat
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
    # Per-request sampling. `max_tokens` may only lower the manifest's ceiling (the react
    # loop clamps it); the bounds here keep NaN/inf and nonsense off the wire.
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, ge=1)
    user: str | None = None


def _usage_payload(ctx: RequestContext) -> dict[str, Any]:
    ls = ctx.limit_state
    return {
        "prompt_tokens": ls.tokens_input,
        "completion_tokens": ls.tokens_output,
        "total_tokens": ls.tokens_input + ls.tokens_output,
        "prompt_tokens_details": {"cached_tokens": ls.tokens_cached},
    }


def _error_body(message: str, kind: str, code: str) -> dict[str, Any]:
    """The OpenAI error envelope, bounded like the native stream's `error_frame`."""
    return {"error": {"message": message[:200], "type": kind, "code": code}}


def _error_json(message: str, kind: str, code: str, status_code: int) -> JSONResponse:
    return JSONResponse(_error_body(message, kind, code), status_code=status_code)


@dataclass(frozen=True, slots=True)
class _Completion:
    """One completion's identity, and the two envelopes it is rendered in."""

    id: str
    created: int
    model: str

    @classmethod
    def new(cls, model: str) -> _Completion:
        return cls(id=f"chatcmpl-{uuid.uuid4().hex[:24]}", created=int(time.time()), model=model)

    def chunk(self, delta: dict[str, Any], finish_reason: str | None, **extra: Any) -> str:
        payload = {
            "id": self.id,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
            **extra,
        }
        return f"data: {json.dumps(payload)}\n\n"

    def error_chunk(self, message: str, kind: str, code: str) -> str:
        """An error inside the stream: clients parse `data:` JSON with an `error` key,
        not the `event: error` frame the native stream uses."""
        return f"data: {json.dumps(_error_body(message, kind, code))}\n\n"

    def response(self, content: str, finish_reason: str, usage: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": self.id,
            "object": "chat.completion",
            "created": self.created,
            "model": self.model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": finish_reason,
                }
            ],
            "usage": usage,
        }


async def _stream_completion(
    settings: Any,
    tools: Any,
    resolved: Any,
    req_ctx: RequestContext,
    invoke_input: InvokeInput,
    completion: _Completion,
) -> AsyncIterator[str]:
    """The streamed turn, on the OpenAI wire, with the same guarantees as `/chat/stream`:
    every stream ends in `[DONE]`, an error is a frame rather than a truncated body, and
    a quiet tool call is kept alive for the proxy in between."""
    stop_reason: str | None = None
    try:
        async with async_run_with_context(req_ctx):
            agent = await build_tenant_agent(
                settings, manifest=resolved.manifest, tools=tools, tenant_id=req_ctx.auth.tenant_id
            )
            async for event in with_heartbeat(agent.stream_events(invoke_input)):
                if event is HEARTBEAT:
                    yield KEEP_ALIVE
                    continue
                name = getattr(event, "event", "")
                if name == "done":
                    stop_reason = event.data.get("stop_reason")
                # Only the reply is assistant content on this wire. `thinking_delta` has a
                # `.text` too, and emitting it here rendered reasoning as the answer — and
                # past the reply controls, which hold reply text only.
                text = event.text if name in REPLY_TEXT_EVENTS else ""
                yield completion.chunk({"content": text} if text else {}, None)
    except asyncio.CancelledError:
        # The client hung up: let the cancellation tear the run down.
        raise
    except ModelGatewayError as exc:
        log_gateway_error(logger, exc)
        yield completion.error_chunk(client_safe_message(exc), "model_gateway_error", "model_unavailable")
        yield DONE
        return
    except Exception as exc:
        # Without this the body simply stopped under an already-sent 200 OK, with no
        # error and no [DONE] — an OpenAI client could not tell success from failure.
        logger.exception("v1 stream failed manifest=%s", loggable(completion.model, limit=80))
        yield completion.error_chunk(client_safe_message(exc), "server_error", "stream_failed")
        yield DONE
        return
    yield completion.chunk({}, finish_reason_for(stop_reason), usage=_usage_payload(req_ctx))
    yield DONE


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
        return _error_json("invalid user", "invalid_request_error", "invalid_user", 400)

    try:
        resolved = await resolve_tenant_manifest(settings, auth.tenant_id, body.model, thread_id=thread)
        await prepare_tenant_invoke(settings, resolved=resolved, auth=auth, thread_id=thread)
    # Two `except` clauses rather than one broad catch narrowed by `isinstance`. The
    # old shape caught everything and re-raised what it did not recognise, which meant
    # every exception in the request -- including ones with driver internals in their
    # message -- passed through a scope that formats messages for clients. Naming the
    # types keeps that reach as small as the intent always was.
    except InboundAuthError as exc:
        return _error_json(client_safe_message(exc), "auth_error", exc.detail, exc.status_code)
    except ManifestDriftError as exc:
        return _error_json(client_safe_message(exc), "manifest_drift", "conflict", 409)

    messages = [
        ChatMessage.model_validate({"role": m.role, "content": m.content or ""}) for m in body.messages
    ]
    # Imported here rather than at module scope to keep the governance package off the
    # import path of a lean install, but *before* the try so the handler can name the
    # type it means. The old shape caught everything, tested with `isinstance`, and
    # re-raised the rest -- which put every exception in this call through a scope whose
    # job is formatting messages for clients.
    from felix.governance.inbound import (
        INBOUND_SCREENED_EXTRA,
        InboundScreeningError,
        apply_inbound_screening,
    )

    try:
        messages = await apply_inbound_screening(resolved.manifest, messages, settings)
    except InboundScreeningError as exc:
        return _error_json(client_safe_message(exc), "content_filter", exc.detail, exc.status_code)
    req_ctx = RequestContext(
        settings=settings,
        auth=auth,
        manifest_id=body.model,
        thread_id=thread,
        # Screened above, so the 422 could be answered before a stream opened.
        extras={INBOUND_SCREENED_EXTRA: True},
    )
    completion = _Completion.new(body.model)
    options = (
        ModelChatOptions(temperature=body.temperature, max_tokens=body.max_tokens)
        if body.temperature is not None or body.max_tokens is not None
        else None
    )
    invoke_input = InvokeInput(messages=messages, thread_id=thread, model_options=options)

    if body.stream:
        return sse_response(_stream_completion(settings, tools, resolved, req_ctx, invoke_input, completion))

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
            log_gateway_error(logger, exc)
            return _error_json(client_safe_message(exc), "model_gateway_error", "model_unavailable", 502)

    content = result.final.content if result.final else ""
    return completion.response(content, finish_reason_for(result.stop_reason), _usage_payload(req_ctx))
