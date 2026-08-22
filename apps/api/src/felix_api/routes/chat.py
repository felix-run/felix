"""POST /chat, /chat/stream, steer/follow-up, fork/rewind — REST + SSE agent surface."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from felix.context import AuthContext, RequestContext, async_run_with_context, try_get_context
from felix.patterns.model import ModelGatewayError
from felix.patterns.types import ChatMessage, InvokeInput
from felix.runtime import build_tenant_agent, resolve_tenant_manifest
from felix.session.store import get_session_store
from felix.session.tree import fork_thread, get_leaf, rewind_to
from felix.steer import enqueue
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
    model: str | None = Field(
        default=None,
        description="Optional mid-session model override (allowlisted against manifest fallbacks).",
    )


class SteerRequest(BaseModel):
    model_config = {"extra": "forbid"}

    thread_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    kind: Literal["steer", "follow_up"] = "steer"


class ToolResultRequest(BaseModel):
    model_config = {"extra": "forbid"}

    thread_id: str = Field(min_length=1)
    tool_call_id: str = Field(min_length=1)
    content: str | dict[str, Any] | list[Any] = ""
    error: bool = False


class ForkRequest(BaseModel):
    model_config = {"extra": "forbid"}

    thread_id: str = Field(min_length=1, description="Source thread suffix.")
    new_thread_id: str = Field(min_length=1, description="Destination thread suffix.")
    from_event_id: str | None = None


class RewindRequest(BaseModel):
    model_config = {"extra": "forbid"}

    thread_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)


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


def _allowlisted_model(manifest: Any, model_id: str | None, settings: Any = None) -> str | None:
    if not model_id:
        return None
    from felix.config import DEFAULT_MODEL_ROUTES, get_settings

    spec = getattr(getattr(manifest, "spec", None), "model", None)
    primary = getattr(spec, "id", None)
    fallbacks = list(getattr(spec, "fallbacks", None) or [])
    allowed: set[str] = set(fallbacks)
    if primary:
        allowed.add(primary)
    cfg = settings or get_settings()
    allowed.add(cfg.default_model_id)
    allowed.update(DEFAULT_MODEL_ROUTES.keys())
    # Also accept provider/model keys present in FELIX_MODEL_ROUTES overrides via parse.
    try:
        from felix.patterns.model import parse_model_routes

        allowed.update(parse_model_routes(cfg).keys())
    except Exception:
        pass
    if model_id not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"model_not_allowlisted:{model_id}",
        )
    return model_id


@router.post("")
@router.post("/")
async def chat(body: ChatRequest, request: Request) -> Any:
    settings = request.app.state.settings
    tools = request.app.state.tools
    auth = _auth_from_request(request)
    thread = effective_thread_id(auth.tenant_id, body.thread_id)
    if body.thread_id and thread is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")
    if not (body.manifest or "").strip():
        raise HTTPException(status_code=400, detail="manifest_required")

    try:
        resolved = await resolve_tenant_manifest(
            settings, auth.tenant_id, body.manifest, thread_id=thread
        )
    except (LookupError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=f"unknown_manifest:{body.manifest}") from exc
    model_id = _allowlisted_model(resolved.manifest, body.model, settings)
    messages = [ChatMessage.model_validate(m) for m in body.messages]
    execution = getattr(getattr(resolved.manifest, "spec", None), "execution", None)
    if getattr(execution, "mode", "transient") == "durable":
        from felix.durability.runs import start_durable_chat

        payload = await start_durable_chat(
            settings,
            auth.tenant_id,
            manifest_id=body.manifest,
            messages=messages,
            thread_id=thread,
            model_id=model_id,
            execution=execution,
        )
        return JSONResponse(payload, status_code=202)

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
            result = await agent.invoke(
                InvokeInput(
                    messages=messages,
                    thread_id=thread,
                    model_id=model_id,
                    tenant_id=auth.tenant_id,
                )
            )
        except ModelGatewayError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    final = result.final
    return {
        "messages": [m.model_dump() for m in result.messages],
        "final": final.model_dump() if hasattr(final, "model_dump") else final,
        "thread_id": thread,
        "model": model_id,
        "leaf_id": get_leaf(thread) if thread else None,
    }


@router.get("/runs/{resume_token}")
async def chat_run(resume_token: str, request: Request) -> dict[str, Any]:
    """Poll a durable chat fiber started with ``spec.execution.mode: durable``."""
    from felix.durability.runs import get_durable_run

    auth = _auth_from_request(request)
    row = await get_durable_run(request.app.state.settings, auth.tenant_id, resume_token)
    if row is None:
        raise HTTPException(status_code=404, detail="run_not_found")
    return row


@router.post("/stream")
async def chat_stream(body: ChatRequest, request: Request) -> StreamingResponse:
    settings = request.app.state.settings
    tools = request.app.state.tools
    auth = _auth_from_request(request)
    thread = effective_thread_id(auth.tenant_id, body.thread_id)
    if body.thread_id and thread is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")
    if not (body.manifest or "").strip():
        raise HTTPException(status_code=400, detail="manifest_required")

    try:
        resolved = await resolve_tenant_manifest(
            settings, auth.tenant_id, body.manifest, thread_id=thread
        )
    except (LookupError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=f"unknown_manifest:{body.manifest}") from exc
    model_id = _allowlisted_model(resolved.manifest, body.model, settings)
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
                InvokeInput(
                    messages=messages,
                    thread_id=thread,
                    model_id=model_id,
                    tenant_id=auth.tenant_id,
                )
            ):
                payload = event.model_dump() if hasattr(event, "model_dump") else event
                yield f"data: {json.dumps(payload, default=str)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@router.post("/steer")
async def chat_steer(body: SteerRequest, request: Request) -> dict[str, Any]:
    """Queue a steer (interrupt remaining tools) or follow-up (after idle) message."""
    auth = _auth_from_request(request)
    thread = effective_thread_id(auth.tenant_id, body.thread_id)
    if thread is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")
    return await enqueue(auth.tenant_id, thread, kind=body.kind, text=body.text)


@router.post("/tool_result")
async def chat_tool_result(body: ToolResultRequest, request: Request) -> dict[str, Any]:
    """Complete a client-executed tool that paused the active agent run."""
    from felix.tools.client_bridge import client_tool_result_json, complete_result

    auth = _auth_from_request(request)
    thread = effective_thread_id(auth.tenant_id, body.thread_id)
    if thread is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")
    content = client_tool_result_json(body.content)
    signaled = await complete_result(
        thread,
        body.tool_call_id,
        content,
        error=body.error,
    )
    return {
        "ok": True,
        "signaled": signaled,
        "thread_id": thread,
        "tool_call_id": body.tool_call_id,
    }


@router.post("/fork")
async def chat_fork(body: ForkRequest, request: Request) -> dict[str, Any]:
    auth = _auth_from_request(request)
    settings = request.app.state.settings
    source_id = effective_thread_id(auth.tenant_id, body.thread_id)
    dest_id = effective_thread_id(auth.tenant_id, body.new_thread_id)
    if source_id is None or dest_id is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")
    store = get_session_store(settings, tenant_id=auth.tenant_id)
    result = await fork_thread(
        store.open(source_id),
        store.open(dest_id),
        from_event_id=body.from_event_id,
    )
    from felix.session.thread_state import persist_leaf

    await persist_leaf(
        settings=settings,
        tenant_id=auth.tenant_id,
        thread_id=dest_id,
        leaf_event_id=result.get("leaf_id"),
    )
    return result


@router.post("/rewind")
async def chat_rewind(body: RewindRequest, request: Request) -> dict[str, Any]:
    auth = _auth_from_request(request)
    settings = request.app.state.settings
    thread = effective_thread_id(auth.tenant_id, body.thread_id)
    if thread is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")
    store = get_session_store(settings, tenant_id=auth.tenant_id)
    result = await rewind_to(store.open(thread), body.event_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error", "rewind_failed"))
    from felix.session.thread_state import persist_leaf

    await persist_leaf(
        settings=settings,
        tenant_id=auth.tenant_id,
        thread_id=thread,
        leaf_event_id=result.get("leaf_id"),
    )
    return result


@router.get("/history/{thread_id}")
async def chat_history(thread_id: str, request: Request) -> dict[str, Any]:
    """Server-side transcript for a thread suffix (tenant-prefixed)."""
    auth = _auth_from_request(request)
    settings = request.app.state.settings
    thread = effective_thread_id(auth.tenant_id, thread_id)
    if thread is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")
    store = get_session_store(settings, tenant_id=auth.tenant_id)
    events = await store.open(thread).get_events()
    messages: list[dict[str, Any]] = []
    for ev in events:
        role = ev.role or ("assistant" if ev.kind == "assistant" else "user")
        if ev.kind in {"message", "user", "assistant", "system"} or ev.content:
            messages.append(
                {
                    "role": role,
                    "content": ev.content or "",
                    "seq": ev.seq,
                    "kind": ev.kind,
                }
            )
    return {"thread_id": thread, "messages": messages, "events": messages}


@router.delete("/history/{thread_id}")
async def chat_history_delete(thread_id: str, request: Request) -> dict[str, str]:
    auth = _auth_from_request(request)
    settings = request.app.state.settings
    thread = effective_thread_id(auth.tenant_id, thread_id)
    if thread is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")
    store = get_session_store(settings, tenant_id=auth.tenant_id)
    await store.open(thread).reset()
    return {"status": "deleted", "thread_id": thread}
