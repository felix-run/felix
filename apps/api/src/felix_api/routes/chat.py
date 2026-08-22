"""POST /chat, /chat/stream, steer/follow-up, fork/rewind — REST + SSE agent surface."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from felix.context import AuthContext, RequestContext, async_run_with_context, try_get_context
from felix.patterns.model import ModelGatewayError
from felix.patterns.types import ChatMessage, InvokeInput
from felix.runtime import build_tenant_agent, prepare_tenant_invoke, resolve_tenant_manifest
from felix.session.store import get_session_store
from felix.session.tree import fork_thread, get_leaf, rewind_to
from felix.steer import enqueue
from pydantic import BaseModel, Field

router = APIRouter(tags=["Threads"])

_SUFFIX_DELIMS = frozenset(":#")


def _http_from_invoke_prep(exc: Exception) -> HTTPException | None:
    from felix.manifests.inbound_auth import InboundAuthError
    from felix.manifests.pin import ManifestDriftError

    if isinstance(exc, InboundAuthError):
        return HTTPException(status_code=exc.status_code, detail=exc.detail)
    if isinstance(exc, ManifestDriftError):
        return HTTPException(status_code=409, detail=str(exc))
    return None


class ChatRequest(BaseModel):
    model_config = {"extra": "forbid"}

    manifest: str = Field(description="Manifest name to invoke.")
    messages: list[dict[str, Any]] = Field(default_factory=list)
    thread_id: str | None = Field(
        default=None,
        description="Optional thread-id suffix; server prefixes the tenant id.",
    )
    model: str | None = Field(
        default=None,
        description="Optional mid-session model override (allowlisted against manifest fallbacks).",
    )
    # Named prompt from manifest ``spec.prompts``; expands into a user message.
    template: str | None = None
    template_args: list[str] = Field(default_factory=list)


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
    summarize: bool | None = None
    instructions: str | None = None
    manifest: str | None = None


class SessionNameRequest(BaseModel):
    model_config = {"extra": "forbid"}

    thread_id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=256)


class ThinkingRequest(BaseModel):
    model_config = {"extra": "forbid"}

    thread_id: str = Field(min_length=1)
    thinking_level: Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"]


class AbortRequest(BaseModel):
    model_config = {"extra": "forbid"}

    thread_id: str = Field(min_length=1)


class ContinueRequest(BaseModel):
    model_config = {"extra": "forbid"}

    thread_id: str = Field(min_length=1)
    manifest: str = Field(min_length=1)
    model: str | None = None


class CompactRequest(BaseModel):
    model_config = {"extra": "forbid"}

    thread_id: str = Field(min_length=1)
    manifest: str = Field(min_length=1)
    instructions: str | None = None


class LabelRequest(BaseModel):
    model_config = {"extra": "forbid"}

    thread_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    label: str | None = None


class CustomEntryRequest(BaseModel):
    model_config = {"extra": "forbid"}

    thread_id: str = Field(min_length=1)
    content: str = ""
    # When true, the entry is included in model context (custom_message semantics).
    in_context: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
    role: Literal["user", "assistant", "system"] = "system"


class LeaseRequest(BaseModel):
    model_config = {"extra": "forbid"}

    thread_id: str = Field(min_length=1)
    holder_id: str = Field(min_length=1, max_length=128)
    mode: Literal["exclusive", "shared"] = "exclusive"
    ttl_seconds: float = Field(default=300.0, ge=5, le=86400)
    token: str | None = None


class LeaseReleaseRequest(BaseModel):
    model_config = {"extra": "forbid"}

    thread_id: str = Field(min_length=1)
    holder_id: str | None = None
    token: str | None = None


class UiResponseRequest(BaseModel):
    model_config = {"extra": "forbid"}

    request_id: str = Field(min_length=1)
    value: Any = None
    cancelled: bool = False
    note: str = ""


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


async def _apply_template(
    messages: list[ChatMessage],
    *,
    manifest: Any,
    template: str | None,
    template_args: list[str],
    settings: Any,
    tenant_id: str,
    object_store: Any | None = None,
) -> list[ChatMessage]:
    if not template:
        return messages
    from felix.prompts import expand_named_prompt

    try:
        text = await expand_named_prompt(
            manifest,
            template,
            template_args,
            object_store=object_store,
            workspace_root=getattr(settings, "workspace_root", None),
            tenant_id=tenant_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return [*messages, ChatMessage(role="user", content=text)]


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
        await prepare_tenant_invoke(
            settings, resolved=resolved, auth=auth, thread_id=thread
        )
    except Exception as exc:
        http = _http_from_invoke_prep(exc)
        if http is not None:
            raise http from exc
        if isinstance(exc, (LookupError, ValueError)):
            raise HTTPException(
                status_code=404, detail=f"unknown_manifest:{body.manifest}"
            ) from exc
        raise
    model_id = _allowlisted_model(resolved.manifest, body.model, settings)
    messages = [ChatMessage.model_validate(m) for m in body.messages]
    messages = await _apply_template(
        messages,
        manifest=resolved.manifest,
        template=body.template,
        template_args=body.template_args,
        settings=settings,
        tenant_id=auth.tenant_id,
        object_store=getattr(request.app.state, "object_store", None),
    )
    if not messages:
        raise HTTPException(status_code=400, detail="messages_or_template_required")
    execution = getattr(getattr(resolved.manifest, "spec", None), "execution", None)
    if getattr(execution, "mode", "transient") == "durable":
        from felix.durability.runs import start_durable_chat
        from felix.manifests.pin import pin_fields

        payload = await start_durable_chat(
            settings,
            auth.tenant_id,
            manifest_id=body.manifest,
            messages=messages,
            thread_id=thread,
            model_id=model_id,
            execution=execution,
            pin=pin_fields(resolved.manifest, version=resolved.version),
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
        await prepare_tenant_invoke(
            settings, resolved=resolved, auth=auth, thread_id=thread
        )
    except Exception as exc:
        http = _http_from_invoke_prep(exc)
        if http is not None:
            raise http from exc
        if isinstance(exc, (LookupError, ValueError)):
            raise HTTPException(
                status_code=404, detail=f"unknown_manifest:{body.manifest}"
            ) from exc
        raise
    model_id = _allowlisted_model(resolved.manifest, body.model, settings)
    messages = [ChatMessage.model_validate(m) for m in body.messages]
    messages = await _apply_template(
        messages,
        manifest=resolved.manifest,
        template=body.template,
        template_args=body.template_args,
        settings=settings,
        tenant_id=auth.tenant_id,
        object_store=getattr(request.app.state, "object_store", None),
    )
    if not messages:
        raise HTTPException(status_code=400, detail="messages_or_template_required")
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
    from felix.session.thread_state import persist_leaf, update_thread_meta

    await persist_leaf(
        settings=settings,
        tenant_id=auth.tenant_id,
        thread_id=dest_id,
        leaf_event_id=result.get("leaf_id"),
    )
    await update_thread_meta(
        settings=settings,
        tenant_id=auth.tenant_id,
        thread_id=dest_id,
        parent_session_id=source_id,
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
    session = store.open(thread)
    from felix.session.thread_state import load_leaf, persist_leaf, update_thread_meta
    from felix.session.tree import get_leaf

    old_leaf = await load_leaf(
        settings=settings, tenant_id=auth.tenant_id, thread_id=thread
    ) or get_leaf(thread)
    result = await rewind_to(session, body.event_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error", "rewind_failed"))
    await persist_leaf(
        settings=settings,
        tenant_id=auth.tenant_id,
        thread_id=thread,
        leaf_event_id=result.get("leaf_id"),
    )
    await update_thread_meta(
        settings=settings,
        tenant_id=auth.tenant_id,
        thread_id=thread,
        phase="idle",
    )

    summarize = body.summarize
    model = None
    if body.manifest:
        try:
            resolved = await resolve_tenant_manifest(
                settings, auth.tenant_id, body.manifest, thread_id=thread
            )
            if summarize is None:
                summarize = bool(
                    getattr(getattr(resolved.manifest.spec, "session", None), "branch_summary", True)
                )
            from felix.patterns.model import build_model

            model = build_model(settings, resolved.manifest.spec.model)
        except (LookupError, ValueError):
            model = None
    if summarize is None:
        summarize = True
    branch_summary = None
    if summarize and old_leaf and old_leaf != body.event_id:
        try:
            from felix.session.branch import summarize_abandoned_branch
            from felix.session.thread_state import update_thread_meta as _utm

            await _utm(
                settings=settings,
                tenant_id=auth.tenant_id,
                thread_id=thread,
                phase="branch_summary",
            )
            branch_summary = await summarize_abandoned_branch(
                session,
                old_leaf_id=old_leaf,
                new_leaf_id=body.event_id,
                model=model,
                instructions=body.instructions,
            )
            if branch_summary:
                await persist_leaf(
                    settings=settings,
                    tenant_id=auth.tenant_id,
                    thread_id=thread,
                    leaf_event_id=branch_summary.get("event_id") or result.get("leaf_id"),
                )
            await update_thread_meta(
                settings=settings,
                tenant_id=auth.tenant_id,
                thread_id=thread,
                phase="idle",
            )
        except Exception:
            branch_summary = None
            await update_thread_meta(
                settings=settings,
                tenant_id=auth.tenant_id,
                thread_id=thread,
                phase="idle",
            )
    out = dict(result)
    if branch_summary:
        out["branch_summary"] = branch_summary
    return out


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


async def _build_thread_snapshot(
    *,
    settings: Any,
    tenant_id: str,
    thread: str,
) -> dict[str, Any]:
    from felix.session.lease import lease_status
    from felix.session.snapshot import build_snapshot
    from felix.session.thread_state import get_thread_meta, load_leaf
    from felix.session.tree import get_leaf
    from felix.steer import peek_steer_count

    store = get_session_store(settings, tenant_id=tenant_id)
    events = await store.open(thread).get_events()
    meta = await get_thread_meta(settings=settings, tenant_id=tenant_id, thread_id=thread)
    leaf = await load_leaf(settings=settings, tenant_id=tenant_id, thread_id=thread) or get_leaf(
        thread
    )
    steer_n = await peek_steer_count(tenant_id, thread)
    lease = await lease_status(thread)
    return build_snapshot(
        thread_id=thread,
        events=events,
        leaf_id=leaf,
        session_name=meta.get("session_name"),
        phase=str(meta.get("phase") or "idle"),
        model_id=meta.get("model_id"),
        thinking_level=meta.get("thinking_level"),
        parent_session_id=meta.get("parent_session_id"),
        labels=dict(meta.get("labels") or {}),
        queued_steer=[{"placeholder": True}] * steer_n if steer_n else [],
        revision=int(meta.get("revision") or 0),
        attached=bool(lease.get("attached")),
        locked=bool(lease.get("locked")),
    )


@router.post("/sessions/lease")
async def acquire_session_lease(body: LeaseRequest, request: Request) -> dict[str, Any]:
    """Acquire an exclusive or shared lease (maps to snapshot locked/attached)."""
    from felix.session.lease import acquire_lease

    auth = _auth_from_request(request)
    thread = effective_thread_id(auth.tenant_id, body.thread_id)
    if thread is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")
    result = await acquire_lease(
        thread,
        holder_id=body.holder_id,
        mode=body.mode,
        ttl_seconds=body.ttl_seconds,
        token=body.token,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error") or "lease_held")
    snapshot = await _build_thread_snapshot(
        settings=request.app.state.settings,
        tenant_id=auth.tenant_id,
        thread=thread,
    )
    return {**result, "snapshot": snapshot}


@router.post("/sessions/lease/release")
async def release_session_lease(body: LeaseReleaseRequest, request: Request) -> dict[str, Any]:
    from felix.session.lease import release_lease

    auth = _auth_from_request(request)
    thread = effective_thread_id(auth.tenant_id, body.thread_id)
    if thread is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")
    result = await release_lease(thread, holder_id=body.holder_id, token=body.token)
    if not result.get("ok"):
        raise HTTPException(status_code=403, detail=result.get("error") or "release_failed")
    snapshot = await _build_thread_snapshot(
        settings=request.app.state.settings,
        tenant_id=auth.tenant_id,
        thread=thread,
    )
    return {**result, "snapshot": snapshot}


@router.post("/ui")
async def chat_ui_response(body: UiResponseRequest, request: Request) -> dict[str, Any]:
    """Resolve a pending select/confirm/input prompt from the web client."""
    from felix.ui import resolve_ui_response

    _ = request
    return await resolve_ui_response(
        body.request_id,
        value=body.value,
        cancelled=body.cancelled,
        note=body.note,
    )


@router.get("/sessions/{thread_id}/export")
async def export_session(thread_id: str, request: Request) -> Any:
    """Export the active branch as JSONL (eval artifacts / sharing)."""
    from fastapi.responses import PlainTextResponse
    from felix.session.export import events_to_jsonl
    from felix.session.tree import active_branch_events, get_leaf

    auth = _auth_from_request(request)
    settings = request.app.state.settings
    thread = effective_thread_id(auth.tenant_id, thread_id)
    if thread is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")
    store = get_session_store(settings, tenant_id=auth.tenant_id)
    events = await store.open(thread).get_events()
    branch = active_branch_events(events, session_id=thread, leaf_id=get_leaf(thread))
    body = events_to_jsonl(branch)
    return PlainTextResponse(
        body,
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="{thread_id}.jsonl"'},
    )


@router.post("/sessions/custom")
async def append_custom_entry(body: CustomEntryRequest, request: Request) -> dict[str, Any]:
    """Persist a custom (UI/plugin) entry. Set ``in_context`` to include it in the LLM."""
    from felix.session.tree import annotate_and_append
    from felix.session.types import AppendableEvent

    auth = _auth_from_request(request)
    settings = request.app.state.settings
    thread = effective_thread_id(auth.tenant_id, body.thread_id)
    if thread is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")
    md = dict(body.metadata or {})
    md["in_context"] = bool(body.in_context)
    md["type"] = "custom"
    store = get_session_store(settings, tenant_id=auth.tenant_id)
    ids = await annotate_and_append(
        store.open(thread),
        [
            AppendableEvent(
                kind="custom",  # type: ignore[arg-type]
                role=body.role,
                content=body.content,
                metadata=md,
            )
        ],
    )
    return {
        "ok": True,
        "thread_id": thread,
        "event_id": ids[-1] if ids else None,
        "in_context": body.in_context,
    }


@router.get("/sessions")
async def list_sessions(request: Request) -> dict[str, Any]:
    from felix.session.thread_state import list_thread_metadata

    auth = _auth_from_request(request)
    items = await list_thread_metadata(
        settings=request.app.state.settings, tenant_id=auth.tenant_id
    )
    return {"sessions": items, "items": items}


@router.get("/sessions/search")
async def search_sessions_route(request: Request, q: str = "", limit: int = 20) -> dict[str, Any]:
    from felix.session.search import search_sessions

    auth = _auth_from_request(request)
    hits = await search_sessions(
        request.app.state.settings, auth.tenant_id, q, limit=limit
    )
    return {"query": q, "hits": hits}


@router.get("/sessions/{thread_id}")
async def get_session_snapshot(thread_id: str, request: Request) -> dict[str, Any]:
    auth = _auth_from_request(request)
    thread = effective_thread_id(auth.tenant_id, thread_id)
    if thread is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")
    return await _build_thread_snapshot(
        settings=request.app.state.settings,
        tenant_id=auth.tenant_id,
        thread=thread,
    )


@router.post("/sessions/name")
async def set_session_name(body: SessionNameRequest, request: Request) -> dict[str, Any]:
    from felix.session.thread_state import update_thread_meta
    from felix.session.tree import annotate_and_append
    from felix.session.types import AppendableEvent

    auth = _auth_from_request(request)
    settings = request.app.state.settings
    thread = effective_thread_id(auth.tenant_id, body.thread_id)
    if thread is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")
    meta = await update_thread_meta(
        settings=settings,
        tenant_id=auth.tenant_id,
        thread_id=thread,
        session_name=body.name,
    )
    store = get_session_store(settings, tenant_id=auth.tenant_id)
    await annotate_and_append(
        store.open(thread),
        [
            AppendableEvent(
                kind="session_info",  # type: ignore[arg-type]
                content=body.name,
                metadata={"type": "session_info", "name": body.name},
            )
        ],
    )
    return {"ok": True, "thread_id": thread, "name": body.name, "meta": meta}


@router.post("/sessions/label")
async def set_session_label(body: LabelRequest, request: Request) -> dict[str, Any]:
    from felix.session.thread_state import update_thread_meta
    from felix.session.tree import annotate_and_append, set_label
    from felix.session.types import AppendableEvent

    auth = _auth_from_request(request)
    settings = request.app.state.settings
    thread = effective_thread_id(auth.tenant_id, body.thread_id)
    if thread is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")
    set_label(body.event_id, body.label)
    await update_thread_meta(
        settings=settings,
        tenant_id=auth.tenant_id,
        thread_id=thread,
        labels={body.event_id: body.label},
    )
    store = get_session_store(settings, tenant_id=auth.tenant_id)
    await annotate_and_append(
        store.open(thread),
        [
            AppendableEvent(
                kind="label",  # type: ignore[arg-type]
                content=body.label,
                metadata={
                    "type": "label",
                    "targetId": body.event_id,
                    "label": body.label,
                },
            )
        ],
    )
    return {"ok": True, "thread_id": thread, "event_id": body.event_id, "label": body.label}


@router.post("/abort")
async def chat_abort(body: AbortRequest, request: Request) -> dict[str, Any]:
    from felix.session.thread_state import update_thread_meta
    from felix.steer import request_abort

    auth = _auth_from_request(request)
    thread = effective_thread_id(auth.tenant_id, body.thread_id)
    if thread is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")
    result = await request_abort(auth.tenant_id, thread)
    await update_thread_meta(
        settings=request.app.state.settings,
        tenant_id=auth.tenant_id,
        thread_id=thread,
        phase="aborted",
    )
    snapshot = await _build_thread_snapshot(
        settings=request.app.state.settings,
        tenant_id=auth.tenant_id,
        thread=thread,
    )
    return {**result, "snapshot": snapshot}


@router.post("/continue")
async def chat_continue(body: ContinueRequest, request: Request) -> Any:
    """Resume after abort/error without a new user message (wake-based)."""
    from felix.session.types import analyze_wake
    from felix.steer import clear_abort

    auth = _auth_from_request(request)
    settings = request.app.state.settings
    tools = request.app.state.tools
    thread = effective_thread_id(auth.tenant_id, body.thread_id)
    if thread is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")
    await clear_abort(auth.tenant_id, thread)
    store = get_session_store(settings, tenant_id=auth.tenant_id)
    session = store.open(thread)
    wake = analyze_wake(await session.get_events())
    if wake.fresh:
        raise HTTPException(status_code=400, detail="nothing_to_continue")
    # Last turn must be user or tool result for a clean continue.
    events = await session.get_events()
    last = events[-1] if events else None
    if last and last.role == "assistant" and not last.tool_calls and not wake.pending_tool_calls:
        raise HTTPException(status_code=400, detail="already_complete")

    try:
        resolved = await resolve_tenant_manifest(
            settings, auth.tenant_id, body.manifest, thread_id=thread
        )
        await prepare_tenant_invoke(
            settings, resolved=resolved, auth=auth, thread_id=thread
        )
    except Exception as exc:
        http = _http_from_invoke_prep(exc)
        if http is not None:
            raise http from exc
        if isinstance(exc, (LookupError, ValueError)):
            raise HTTPException(
                status_code=404, detail=f"unknown_manifest:{body.manifest}"
            ) from exc
        raise
    model_id = _allowlisted_model(resolved.manifest, body.model, settings)
    # Empty incoming — session strategy rebuilds context from leaf.
    messages = [ChatMessage(role="user", content="Continue.")]
    if wake.pending_tool_calls:
        # Nudge the model with a system-style continue after pending tools resolved client-side.
        messages = [ChatMessage(role="user", content="[continue]")]

    req_ctx = RequestContext(
        settings=settings,
        auth=auth,
        manifest_id=body.manifest,
        thread_id=thread,
    )
    from felix.session.thread_state import update_thread_meta

    await update_thread_meta(
        settings=settings, tenant_id=auth.tenant_id, thread_id=thread, phase="retry"
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
    await update_thread_meta(
        settings=settings, tenant_id=auth.tenant_id, thread_id=thread, phase="idle"
    )
    return {
        "messages": [m.model_dump() for m in result.messages],
        "final": result.final.model_dump() if hasattr(result.final, "model_dump") else result.final,
        "thread_id": thread,
        "continued": True,
    }


@router.post("/thinking")
async def chat_thinking(body: ThinkingRequest, request: Request) -> dict[str, Any]:
    from felix.session.thinking import parse_thinking_level
    from felix.session.thread_state import update_thread_meta
    from felix.session.tree import annotate_and_append
    from felix.session.types import AppendableEvent

    auth = _auth_from_request(request)
    settings = request.app.state.settings
    thread = effective_thread_id(auth.tenant_id, body.thread_id)
    if thread is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")
    try:
        level = parse_thinking_level(body.thinking_level)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await update_thread_meta(
        settings=settings,
        tenant_id=auth.tenant_id,
        thread_id=thread,
        thinking_level=level,
    )
    store = get_session_store(settings, tenant_id=auth.tenant_id)
    await annotate_and_append(
        store.open(thread),
        [
            AppendableEvent(
                kind="thinking_level_change",  # type: ignore[arg-type]
                content=level,
                metadata={"type": "thinking_level_change", "thinking_level": level},
            )
        ],
    )
    return {"ok": True, "thread_id": thread, "thinking_level": level}


@router.post("/compact")
async def chat_compact(body: CompactRequest, request: Request) -> dict[str, Any]:
    auth = _auth_from_request(request)
    settings = request.app.state.settings
    thread = effective_thread_id(auth.tenant_id, body.thread_id)
    if thread is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")
    try:
        resolved = await resolve_tenant_manifest(
            settings, auth.tenant_id, body.manifest, thread_id=thread
        )
    except (LookupError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=f"unknown_manifest:{body.manifest}") from exc

    from felix.patterns.model import build_model
    from felix.session.compaction import CompactingSessionStrategy
    from felix.session.thread_state import update_thread_meta

    store = get_session_store(settings, tenant_id=auth.tenant_id)
    session = store.open(thread)
    strategy_spec = getattr(resolved.manifest.spec, "session", None)
    strategy = CompactingSessionStrategy(
        reserve_tokens=int(getattr(strategy_spec, "reserve_tokens", 16384) or 16384),
        keep_recent_tokens=int(getattr(strategy_spec, "keep_recent_tokens", 20000) or 20000),
        context_window_tokens=int(
            getattr(strategy_spec, "context_window_tokens", 128000) or 128000
        ),
        enabled=True,
    )
    model = build_model(settings, resolved.manifest.spec.model)
    await update_thread_meta(
        settings=settings, tenant_id=auth.tenant_id, thread_id=thread, phase="compaction"
    )
    result = await strategy.compact_now(
        session,
        model=model,
        system_prompt="",
        instructions=body.instructions,
        reason="manual",
    )
    await update_thread_meta(
        settings=settings, tenant_id=auth.tenant_id, thread_id=thread, phase="idle"
    )
    snapshot = await _build_thread_snapshot(
        settings=settings, tenant_id=auth.tenant_id, thread=thread
    )
    return {**result, "thread_id": thread, "snapshot": snapshot}
