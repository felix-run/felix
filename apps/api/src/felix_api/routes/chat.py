"""POST /chat, /chat/stream, steer/follow-up, fork/rewind — REST + SSE agent surface."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from felix.context import AuthContext, RequestContext, async_run_with_context, try_get_context
from felix.logging_setup import loggable
from felix.patterns.model import ModelGatewayError
from felix.patterns.types import ChatMessage, InvokeInput
from felix.runtime import build_tenant_agent, prepare_tenant_invoke, resolve_tenant_manifest
from felix.session.notify import Wake, thread_watch
from felix.session.store import get_session_store
from felix.session.tree import fork_thread, get_leaf, rewind_to
from felix.session.types import GetEventsOpts
from felix.steer import enqueue
from pydantic import BaseModel, Field

from felix_api.errors import client_safe_message
from felix_api.routes._sse import (
    DONE,
    HEARTBEAT,
    KEEP_ALIVE,
    error_frame,
    frame,
    is_resume_point,
    sse_response,
    with_heartbeat,
)
from felix_api.threads import effective_thread_id

logger = logging.getLogger("felix_api.routes.chat")

router = APIRouter(tags=["Threads"])


def _http_from_invoke_prep(exc: Exception) -> HTTPException | None:
    from felix.governance.inbound import InboundScreeningError
    from felix.manifests.inbound_auth import InboundAuthError
    from felix.manifests.loader import ManifestParseError
    from felix.manifests.pin import ManifestDriftError

    # This decides the status code; `client_safe_message` decides the wording. They
    # were the same decision here and in three other shapes across two modules, which
    # is how a message that was safe in one place got copied to one where it was not.
    if isinstance(exc, InboundAuthError | InboundScreeningError):
        return HTTPException(status_code=exc.status_code, detail=client_safe_message(exc))
    if isinstance(exc, ManifestDriftError):
        return HTTPException(status_code=409, detail=client_safe_message(exc))
    if isinstance(exc, ValueError) and str(exc).startswith("secret not found"):
        # Ours, and written to tell an operator which secret is missing.
        return HTTPException(status_code=503, detail=client_safe_message(exc, authored_for_clients=True))
    if isinstance(exc, ValueError) and str(exc).startswith(
        ("unknown checkpointer", "memory.checkpointer is")
    ):
        # A manifest stored before `memory.checkpointer` was implemented can name a
        # value that is now rejected — `agentcore`, `sqlite`, `do` were all inert.
        # `PUT /manifests` refuses new ones, but existing rows only fail here, and
        # unmapped that is a 500 with a traceback on every request for the manifest.
        return HTTPException(status_code=422, detail=client_safe_message(exc, authored_for_clients=True))
    if isinstance(exc, ManifestParseError):
        # Same shape one step earlier: a row stored before a schema tightening no longer
        # validates. `PUT /manifests` refuses new ones with a 400; without this the existing
        # rows answer 500 "internal error" on every request, which is indistinguishable from
        # an outage and sends the operator looking for one.
        return HTTPException(status_code=422, detail=client_safe_message(exc))
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
        # Names the template the caller asked for, which the caller supplied.
        raise HTTPException(
            status_code=404, detail=client_safe_message(exc, authored_for_clients=True)
        ) from exc
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
        resolved = await resolve_tenant_manifest(settings, auth.tenant_id, body.manifest, thread_id=thread)
        await prepare_tenant_invoke(settings, resolved=resolved, auth=auth, thread_id=thread)
    except Exception as exc:
        http = _http_from_invoke_prep(exc)
        if http is not None:
            raise http from exc
        if isinstance(exc, (LookupError, ValueError)):
            raise HTTPException(status_code=404, detail=f"unknown_manifest:{body.manifest}") from exc
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
    try:
        from felix.governance.inbound import apply_inbound_screening

        messages = await apply_inbound_screening(resolved.manifest, messages, settings)
    except Exception as exc:
        http = _http_from_invoke_prep(exc)
        if http is not None:
            raise http from exc
        raise
    execution = getattr(getattr(resolved.manifest, "spec", None), "execution", None)
    if execution is not None and getattr(execution, "mode", "transient") == "durable":
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
            # `body` is an upstream response: neither trusted nor single-line.
            # CodeQL does not flag it -- its taint source is the network rather than
            # the request -- but a gateway body is shaped by prompt content, which
            # makes it as forgeable as anything the client sends directly.
            logger.warning(
                "model gateway error label=%s status=%s body=%s",
                loggable(exc.label, limit=80),
                exc.status,
                loggable(exc.body),
            )
            raise HTTPException(status_code=502, detail=client_safe_message(exc)) from exc
        except Exception as exc:
            http = _http_from_invoke_prep(exc)
            if http is not None:
                raise http from exc
            raise

    final = result.final
    return {
        "messages": [m.model_dump() for m in result.messages],
        "final": final.model_dump() if hasattr(final, "model_dump") else final,
        "thread_id": thread,
        "model": model_id,
        "leaf_id": get_leaf(thread) if thread else None,
    }


def _safe_filename(thread_id: str) -> str:
    """A thread id reduced to characters that cannot escape a quoted header parameter.

    The id is interpolated into `filename="..."`. `effective_thread_id` rejects `:` and `#`,
    which makes header splitting look unreachable — but it permits `"`, and one quote ends
    the parameter early and starts attacker-controlled header text. Allowlist rather than
    escape: a filename has no need of anything outside this set.
    """
    return "".join(c if (c.isalnum() or c in "-_.") else "_" for c in thread_id)[:128] or "session"


@router.get("/runs/{resume_token}")
async def chat_run(resume_token: str, request: Request) -> dict[str, Any]:
    """Poll a durable chat fiber started with ``spec.execution.mode: durable``."""
    from felix.durability.runs import get_durable_run

    auth = _auth_from_request(request)
    row = await get_durable_run(request.app.state.settings, auth.tenant_id, resume_token)
    if row is None:
        raise HTTPException(status_code=404, detail="run_not_found")
    return row


async def _durable_run_gen(*, settings: Any, tenant_id: str, accepted: dict[str, Any]):
    """Stream a durable run's progress instead of pretending it is synchronous.

    `POST /chat` honours `spec.execution.mode: durable` and returns 202 with a
    `resume_token`; this endpoint did not mention it at all, so a manifest that asked
    for durable execution got it on one route and was silently ignored on the other.

    Streaming the run rather than returning 202 keeps the SSE contract a caller of this
    endpoint already has, and it delivers what durable is actually for: a disconnect
    here tears down the *poll*, not the run. That is the opposite of the transient
    path, where a hung-up client deliberately kills the run so it stops burning tokens.

    The first frame carries the `resume_token`, so a client that drops before the run
    finishes can come back to `GET /chat/runs/{token}` rather than starting over.
    """
    import json

    from felix.durability.runs import get_durable_run

    token = str(accepted.get("resume_token") or "")
    yield f"data: {json.dumps({'event': 'run_accepted', 'data': accepted}, default=str)}\n\n"

    poll = float(getattr(settings, "stream_resume_poll_seconds", 1.0) or 1.0)
    poll_max = max(poll, float(getattr(settings, "stream_resume_poll_max_seconds", 10.0) or 10.0))
    # The run's own TTL bounds this, not the resume stream's idle limit: a durable run
    # that outlives its expiry is finished either way, and holding the connection past
    # that point would keep a worker busy for a result that can no longer arrive.
    deadline = float(accepted.get("expires_at") or 0) or None

    waited = 0.0
    delay = poll
    last_status = ""
    while True:
        run = await get_durable_run(settings, tenant_id, token)
        if run is None:
            yield error_frame(f"run_not_found:{token}", kind="run_error")
            break
        status = str(run.get("status") or "")
        if status != last_status:
            last_status = status
            waited = 0.0
            delay = poll
            payload = {"event": "run_status", "data": {"status": status, "resume_token": token}}
            yield f"data: {json.dumps(payload, default=str)}\n\n"
        if status in _RUN_TERMINAL:
            if status == "completed":
                final = {"event": "final", "data": run.get("final") or {}}
                yield f"data: {json.dumps(final, default=str)}\n\n"
            else:
                yield error_frame(str(run.get("error") or status), kind="run_error")
            break
        if deadline and time.time() * 1000 >= deadline:
            # Says which it was. "expired" and "still running" look identical to a
            # client that only sees the stream close.
            yield error_frame(f"run_expired:{token}", kind="run_error")
            break
        yield ": keep-alive\n\n"
        await asyncio.sleep(delay)
        waited += delay
        delay = _next_poll_delay(waited, delay, floor=poll, ceiling=poll_max)
    yield DONE


@router.get("/stream/{thread_id}")
async def chat_stream_resume(request: Request, thread_id: str) -> StreamingResponse:
    """Reattach to a thread after a dropped connection or a page refresh.

    A client that loses `POST /chat/stream` mid-turn previously had nothing to come
    back to: no `id:` on the frames, no route to reconnect to, and the run itself torn
    down on disconnect — deliberately, so a hung-up client does not keep burning
    tokens. This does not change that. The old run is still gone; what a client gets
    back is the thread as it now stands, and then anything that lands afterwards.

    Cold reconnect (no `Last-Event-ID`) opens with a `snapshot` frame carrying the
    transcript. A warm one replays only the session events after that cursor. Both
    then tail the session log, which is shared state, so this works regardless of
    which replica served the original turn.
    """
    settings = request.app.state.settings
    auth = _auth_from_request(request)
    thread = effective_thread_id(auth.tenant_id, thread_id)
    if thread is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")

    header = request.headers.get("last-event-id") or request.query_params.get("last_event_id")
    try:
        after = int(header) if header not in (None, "") else None
    except ValueError:
        after = None

    poll = float(getattr(settings, "stream_resume_poll_seconds", 1.0) or 1.0)
    poll_max = max(poll, float(getattr(settings, "stream_resume_poll_max_seconds", 10.0) or 10.0))
    idle_limit = float(getattr(settings, "stream_resume_idle_seconds", 300.0) or 300.0)

    async def resume_gen():
        import json

        cursor = after
        try:
            if cursor is None:
                snapshot = await _build_thread_snapshot(
                    settings=settings, tenant_id=auth.tenant_id, thread=thread
                )
                cursor = int((await _stream_cursor(settings, auth.tenant_id, thread)) or 0)
                # The snapshot carries every event so far, so the cursor it hands back
                # is the next sequence the client should expect — not the last one it
                # has. Every `id:` on this stream means the same thing, which is what
                # lets a client hand it straight back as `Last-Event-ID`.
                yield (
                    f"id: {cursor}\n"
                    f"data: {json.dumps({'event': 'snapshot', 'data': snapshot}, default=str)}\n\n"
                )

            store = get_session_store(settings, tenant_id=auth.tenant_id)
            pacing = _ResumePacing(floor=poll, ceiling=poll_max, idle_limit=idle_limit)
            # One subscription for the life of the stream. Waiting through a watch
            # rather than a call per iteration is what keeps this to a single
            # SUBSCRIBE/UNSUBSCRIBE pair instead of one per poll interval.
            async with thread_watch(auth.tenant_id, thread) as watch:
                while True:
                    # `get_events(from_seq=...)` already applies `seq >= from_seq` in SQL
                    # on both backends, so filtering again in Python re-walks the page to
                    # discard nothing.
                    events = await store.open(thread).get_events(GetEventsOpts(from_seq=cursor))
                    for event in events:
                        cursor = event.seq + 1
                        yield _session_event_frame(event, cursor)
                    if events:
                        pacing.saw_events()
                    else:
                        pacing.went_quiet()
                    if pacing.exhausted:
                        break
                    yield ": keep-alive\n\n"
                    # Wait for the thread to move rather than sleeping through it. The
                    # query above runs either way, so a dropped notification costs
                    # latency and never correctness -- which is what lets the ceiling
                    # relax rather than disappear.
                    pacing.waited(await watch.wait(timeout=pacing.timeout))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("chat resume failed thread=%s", loggable(thread, limit=80))
            yield error_frame(client_safe_message(exc))
        yield DONE

    return sse_response(resume_gen())


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
        resolved = await resolve_tenant_manifest(settings, auth.tenant_id, body.manifest, thread_id=thread)
        await prepare_tenant_invoke(settings, resolved=resolved, auth=auth, thread_id=thread)
    except Exception as exc:
        http = _http_from_invoke_prep(exc)
        if http is not None:
            raise http from exc
        if isinstance(exc, (LookupError, ValueError)):
            raise HTTPException(status_code=404, detail=f"unknown_manifest:{body.manifest}") from exc
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
    try:
        from felix.governance.inbound import apply_inbound_screening

        messages = await apply_inbound_screening(resolved.manifest, messages, settings)
    except Exception as exc:
        http = _http_from_invoke_prep(exc)
        if http is not None:
            raise http from exc
        raise
    execution = getattr(getattr(resolved.manifest, "spec", None), "execution", None)
    if execution is not None and getattr(execution, "mode", "transient") == "durable":
        from felix.durability.runs import start_durable_chat
        from felix.manifests.pin import pin_fields

        accepted = await start_durable_chat(
            settings,
            auth.tenant_id,
            manifest_id=body.manifest,
            messages=messages,
            thread_id=thread,
            model_id=model_id,
            execution=execution,
            pin=pin_fields(resolved.manifest, version=resolved.version),
        )
        return sse_response(
            _durable_run_gen(
                settings=settings,
                tenant_id=auth.tenant_id,
                accepted=accepted,
            )
        )

    req_ctx = RequestContext(
        settings=settings,
        auth=auth,
        manifest_id=body.manifest,
        thread_id=thread,
    )

    async def event_gen():

        try:
            async with async_run_with_context(req_ctx):
                agent = await build_tenant_agent(
                    settings,
                    manifest=resolved.manifest,
                    tools=tools,
                    tenant_id=auth.tenant_id,
                )
                stream = agent.stream_events(
                    InvokeInput(
                        messages=messages,
                        thread_id=thread,
                        model_id=model_id,
                        tenant_id=auth.tenant_id,
                    )
                )
                cursor: int | None = None
                async for event in with_heartbeat(stream):
                    if event is HEARTBEAT:
                        # A comment frame keeps proxies and load balancers from closing
                        # an idle connection during a long tool call; clients ignore it.
                        yield KEEP_ALIVE
                        continue
                    payload = event.model_dump() if hasattr(event, "model_dump") else event
                    # `id:` is the session log's own cursor, so it still means
                    # something to the *next* connection — a per-connection counter
                    # would not. Only structural frames carry one: they are the points
                    # a reconnect can resume from, and they are rare, where deltas
                    # arrive per token and would cost a query each. Frames without an
                    # `id:` leave `lastEventId` untouched, which is exactly the
                    # semantics wanted here.
                    if thread and is_resume_point(str(payload.get("event") or "")):
                        # Re-read rather than trust the cached value: a structural
                        # frame is where an append may just have happened. Consecutive
                        # structural frames with nothing appended between them return
                        # the same number, which is correct and costs one small query.
                        fresh = await _stream_cursor(settings, auth.tenant_id, thread)
                        if fresh is not None:
                            cursor = fresh
                        yield frame(payload, cursor=cursor)
                    else:
                        yield frame(payload)
        except asyncio.CancelledError:
            # The client hung up. Nothing to send; let the cancellation propagate so the
            # run is torn down instead of continuing to burn model tokens.
            raise
        except Exception as exc:
            # Without this the body simply stopped under an already-sent 200 OK, with no
            # error event and no [DONE] — the client could not tell success from failure.
            logger.exception("chat stream failed thread=%s", loggable(thread, limit=80))
            yield error_frame(client_safe_message(exc))
        yield DONE

    return sse_response(event_gen())


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

    old_leaf = await load_leaf(settings=settings, tenant_id=auth.tenant_id, thread_id=thread) or get_leaf(
        thread
    )
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
        except LookupError, ValueError:
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


# The most events one `GET /chat/history` response may carry.
#
# This endpoint loaded the whole thread and returned every message, so the response grew
# without bound for the life of a thread -- a long-running session eventually returns a
# payload nothing wants to hold, and the client has no way to ask for less.
#
# The cap is deliberately far above any thread that exists today. Lowering the *default*
# would be the bigger win and is a breaking change for a shipped client, so it is left
# as a decision: this makes paging possible and makes the response bounded, without
# changing what an existing caller receives.
MAX_HISTORY_EVENTS = 5000


@router.get("/history/{thread_id}")
async def chat_history(
    thread_id: str,
    request: Request,
    limit: int | None = None,
    before_seq: int | None = None,
) -> dict[str, Any]:
    """Server-side transcript for a thread suffix (tenant-prefixed).

    `before_seq` pages backwards: it returns the events immediately preceding that
    sequence, so a client walks a long thread by handing back the `oldest_seq` it last
    received. `limit` counts *events read*, not messages returned, because the filter
    below drops some kinds -- a limit that counted messages could not be turned into a
    cursor without re-reading.
    """
    auth = _auth_from_request(request)
    settings = request.app.state.settings
    thread = effective_thread_id(auth.tenant_id, thread_id)
    if thread is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")
    if limit is not None and limit < 1:
        raise HTTPException(status_code=400, detail="limit_must_be_positive")

    window = min(limit or MAX_HISTORY_EVENTS, MAX_HISTORY_EVENTS)
    session = get_session_store(settings, tenant_id=auth.tenant_id).open(thread)

    # The newest window, not the oldest: `get_events(limit=n)` takes the first n, which
    # for a transcript is the wrong end. `head` is O(1) on both arms, so this costs one
    # cheap query rather than loading the thread to find its length.
    upper = before_seq if before_seq is not None else int((await session.head()).get("seq") or 0)
    lower = max(0, upper - window)
    events = await session.get_events(GetEventsOpts(from_seq=lower, to_seq=upper))

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
    return {
        "thread_id": thread,
        "messages": messages,
        "events": messages,
        # `lower` rather than the first message's seq: the filter above may have dropped
        # the oldest events in the window, and a cursor that skipped them would lose
        # them on the next page.
        "oldest_seq": lower,
        "has_more": lower > 0,
    }


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


# How long a stream stays at the floor before the poll starts decaying, and how
# sharply it decays after that.
#
# A plain exponential from the first empty round would be wrong here. Backing off costs
# first-event latency -- a thread that goes quiet and then produces makes the client
# wait up to the current delay -- and the moment a user is most likely to act is right
# after they reattach. So the first half-minute stays at the floor, and only a stream
# that has been silent past that decays. The load this finding is about comes from tabs
# left open for minutes, not from the first few seconds of one.
# Fiber statuses that mean the run will not change again.
_RUN_TERMINAL = frozenset({"completed", "failed", "expired", "cancelled"})

# The ceiling a stream may decay to once notifications are actually being delivered.
# Far above the un-notified ceiling because the poll is then a safety net against a
# dropped pub/sub message rather than the mechanism itself: one query per minute per
# idle client instead of one every ten seconds.
NOTIFIED_POLL_CEILING_SECONDS = 60.0

POLL_BACKOFF_GRACE_SECONDS = 30.0
POLL_BACKOFF_FACTOR = 1.5


def _next_poll_delay(idle: float, delay: float, *, floor: float, ceiling: float) -> float:
    """The wait before the next poll of a quiet stream."""
    if idle < POLL_BACKOFF_GRACE_SECONDS:
        return floor
    return min(delay * POLL_BACKOFF_FACTOR, ceiling)


def _session_event_frame(event: Any, cursor: int) -> str:
    """One `session_event` SSE frame.

    `id:` is the *next* sequence the client should expect, not the one it just got, so a
    client can hand it straight back as `Last-Event-ID`. Every `id:` on this stream means
    the same thing, including the snapshot's.
    """
    import json

    payload = {
        "event": "session_event",
        "data": {
            "seq": event.seq,
            "kind": event.kind,
            "role": event.role,
            "content": event.content,
            "name": event.name,
        },
    }
    return f"id: {cursor}\ndata: {json.dumps(payload, default=str)}\n\n"


@dataclass(slots=True)
class _ResumePacing:
    """How long a resume stream waits before asking again, and when it gives up.

    Split out of `chat_stream_resume` because the rules were interleaved line by line
    with SSE framing -- `yield f"id: {cursor}\ndata: ..."` two lines from the decay
    ceiling. Those are protocol and policy at different levels, and the comment density
    around them was what a missing seam looks like. Here the rules sit together and can
    be read as rules; the loop reads as protocol.

    Nothing about the behaviour changed: `saw_events`, `went_quiet`, `exhausted` and
    `waited` are the four things the loop body did, in the order it did them.
    """

    floor: float
    ceiling: float
    idle_limit: float
    #: How long to wait for the next append. The loop reads this, never computes it.
    timeout: float = 0.0
    _idle: float = 0.0
    _notified: bool = False

    def __post_init__(self) -> None:
        self.timeout = self.floor

    @property
    def exhausted(self) -> bool:
        """Time to close rather than hold an idle connection open forever. The client
        reconnects with its `Last-Event-ID` and loses nothing."""
        return self._idle >= self.idle_limit

    def saw_events(self) -> None:
        """Backoff is a measure of idleness, so activity resets it -- otherwise a thread
        that goes quiet and then busy answers the next message at the decayed interval."""
        self._idle = 0.0
        self.timeout = self.floor

    def went_quiet(self) -> None:
        # `self.timeout`, not `self.floor`: the accounting has to follow the actual wait
        # or the idle limit stops meaning the number of seconds it says.
        self._idle += self.timeout
        # A notified stream polls only as a safety net, so it can afford a far longer
        # interval. When Redis drops, `by_notification` goes False on the next wait and
        # this tightens back on its own, without anything having to notice.
        ceiling = NOTIFIED_POLL_CEILING_SECONDS if self._notified else self.ceiling
        self.timeout = _next_poll_delay(self._idle, self.timeout, floor=self.floor, ceiling=ceiling)

    def waited(self, wake: Wake) -> None:
        self._notified = wake.by_notification
        if wake.woken:
            self.saw_events()


async def _stream_cursor(settings: Any, tenant_id: str, thread: str | None) -> int | None:
    """The next session sequence this thread will write.

    A cursor, not a last-seen id: a client hands it back as `Last-Event-ID` and gets
    everything from there on. Using the session log rather than a per-connection
    counter is what makes it mean anything to the *next* connection.
    """
    if not thread:
        return None
    try:
        head = await get_session_store(settings, tenant_id=tenant_id).open(thread).head()
        return int(head.get("seq") or 0)
    except Exception:
        logger.debug("stream cursor unavailable for %s", loggable(thread, limit=80), exc_info=True)
        return None


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
    # Five reads against four different stores, none of which depends on another. They
    # ran in series on `GET /chat/sessions/{id}`, on both lease endpoints and on every
    # cold SSE reconnect -- the reattach path, where latency is the most visible thing
    # in the product.
    #
    # `gather` holds more pool connections at once, which is why it waited for the pool
    # to become a setting rather than a hardcoded 5 + 10.
    events, meta, stored_leaf, steer_n, lease = await asyncio.gather(
        store.open(thread).get_events(),
        get_thread_meta(settings=settings, tenant_id=tenant_id, thread_id=thread),
        load_leaf(settings=settings, tenant_id=tenant_id, thread_id=thread),
        peek_steer_count(tenant_id, thread),
        lease_status(thread),
    )
    # `get_leaf` is synchronous and in-process, so it stays out of the fan-out.
    leaf = stored_leaf or get_leaf(thread)
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
        headers={"Content-Disposition": f'attachment; filename="{_safe_filename(thread_id)}.jsonl"'},
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
    items = await list_thread_metadata(settings=request.app.state.settings, tenant_id=auth.tenant_id)
    return {"sessions": items, "items": items}


@router.get("/sessions/search")
async def search_sessions_route(request: Request, q: str = "", limit: int = 20) -> dict[str, Any]:
    from felix.session.search import search_sessions

    auth = _auth_from_request(request)
    hits = await search_sessions(request.app.state.settings, auth.tenant_id, q, limit=limit)
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
    # Once. This read the whole thread, then read it again to look at one element.
    events = await session.get_events()
    wake = analyze_wake(events)
    if wake.fresh:
        raise HTTPException(status_code=400, detail="nothing_to_continue")
    # Last turn must be user or tool result for a clean continue.
    last = events[-1] if events else None
    if last and last.role == "assistant" and not last.tool_calls and not wake.pending_tool_calls:
        raise HTTPException(status_code=400, detail="already_complete")

    try:
        resolved = await resolve_tenant_manifest(settings, auth.tenant_id, body.manifest, thread_id=thread)
        await prepare_tenant_invoke(settings, resolved=resolved, auth=auth, thread_id=thread)
    except Exception as exc:
        http = _http_from_invoke_prep(exc)
        if http is not None:
            raise http from exc
        if isinstance(exc, (LookupError, ValueError)):
            raise HTTPException(status_code=404, detail=f"unknown_manifest:{body.manifest}") from exc
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

    await update_thread_meta(settings=settings, tenant_id=auth.tenant_id, thread_id=thread, phase="retry")
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
            # `body` is an upstream response: neither trusted nor single-line.
            # CodeQL does not flag it -- its taint source is the network rather than
            # the request -- but a gateway body is shaped by prompt content, which
            # makes it as forgeable as anything the client sends directly.
            logger.warning(
                "model gateway error label=%s status=%s body=%s",
                loggable(exc.label, limit=80),
                exc.status,
                loggable(exc.body),
            )
            raise HTTPException(status_code=502, detail=client_safe_message(exc)) from exc
        except Exception as exc:
            http = _http_from_invoke_prep(exc)
            if http is not None:
                raise http from exc
            raise
    await update_thread_meta(settings=settings, tenant_id=auth.tenant_id, thread_id=thread, phase="idle")
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
        # `unknown_thinking_level:<value>` -- the caller's own input, echoed back.
        raise HTTPException(
            status_code=400, detail=client_safe_message(exc, authored_for_clients=True)
        ) from exc
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
        resolved = await resolve_tenant_manifest(settings, auth.tenant_id, body.manifest, thread_id=thread)
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
        context_window_tokens=int(getattr(strategy_spec, "context_window_tokens", 128000) or 128000),
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
    await update_thread_meta(settings=settings, tenant_id=auth.tenant_id, thread_id=thread, phase="idle")
    snapshot = await _build_thread_snapshot(settings=settings, tenant_id=auth.tenant_id, thread=thread)
    return {**result, "thread_id": thread, "snapshot": snapshot}
