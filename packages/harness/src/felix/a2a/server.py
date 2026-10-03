"""A2A server — JSON-RPC methods backed by Felix agents."""

from __future__ import annotations

import time
import uuid
from typing import Any

from felix_ai.wire.base import data_url

from felix.a2a import tasks as task_store
from felix.config import Settings
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.patterns.types import ChatMessage, ImageAttachment, InvokeInput
from felix.thread_ids import a2a_thread_id
from felix.tools.provider import ToolProvider


def _extract_message(params: dict[str, Any]) -> tuple[str, list[tuple[bytes, str]]]:
    """The sender's text, and the bytes and type of each image its FileParts carry.

    Raises `PartError` for a file part that fails the upload rules: refusing says the image was
    not seen, where dropping it -- what this did before -- answered as if it had never been sent.
    """
    from felix.a2a.parts import inbound_images, text_of

    message = params.get("message") or params
    if isinstance(message, str):
        return message, []
    parts = message.get("parts") if isinstance(message, dict) else None
    text = text_of(parts) if isinstance(parts, list) else ""
    if not text and isinstance(message, dict):
        text = str(message.get("text") or message.get("content") or "")
    return text, inbound_images(parts) if isinstance(parts, list) else []


async def _stored(
    settings: Settings, tenant_id: str, images: list[tuple[bytes, str]]
) -> list[ImageAttachment]:
    """The sender's images stored like uploads -- under the tenant's quota, collected by retention --
    so the session log keeps a reference, not up to 800 KB of base64 a turn replays forever."""
    from felix_ai.types import file_ref_url

    from felix.attachments import put_attachment
    from felix.storage import get_object_store

    stored: list[ImageAttachment] = []
    for raw, media_type in images:
        att = await put_attachment(
            get_object_store(settings),
            tenant_id=tenant_id,
            data=raw,
            media_type=media_type,
            settings=settings,
        )
        stored.append(ImageAttachment(url=file_ref_url(att.file_id), media_type=media_type))
    return stored


def _rpc_error(rpc_id: str | int | None, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}


async def handle_rpc(
    *,
    settings: Settings,
    tools: ToolProvider,
    tenant_id: str,
    method: str,
    params: dict[str, Any],
    rpc_id: str | int | None,
    auth: AuthContext | None = None,
) -> dict[str, Any]:
    from felix.runtime import build_tenant_agent, prepare_tenant_invoke, resolve_tenant_manifest

    if method == "agent/authenticatedExtendedCard":
        from felix.a2a.card import build_agent_card
        from felix.manifests.loader import load_bundled

        name = str(params.get("manifest") or settings.default_manifest)
        try:
            manifest = load_bundled(name)
        except FileNotFoundError:
            return _rpc_error(rpc_id, -32004, f"Unknown manifest: {name}")
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": build_agent_card(manifest),
        }

    if method == "message/send":
        from felix.a2a.parts import PartError

        name = str(params.get("manifest") or settings.default_manifest)
        try:
            text, images = _extract_message(params)
        except PartError as exc:
            return _rpc_error(rpc_id, -32602, str(exc))
        if not text and not images:
            return _rpc_error(rpc_id, -32602, "message text or an image file part required")
        raw_task_id = params.get("taskId")
        if raw_task_id is not None and not isinstance(raw_task_id, str):
            # `params` is `dict[str, Any]`, so `str()` would turn a JSON object or array into
            # a thread id from its Python repr — validating the stringified value rather than
            # the caller's. Harmless in practice (same tenant, still injective), but the guard
            # below is worth applying to what was actually sent.
            return _rpc_error(rpc_id, -32602, "taskId must be a string")
        task_id = str(raw_task_id or uuid.uuid4())
        thread = a2a_thread_id(tenant_id, task_id)
        if thread is None:
            # Before `put_task`, deliberately: `task_id` is half of the `a2a_tasks`
            # primary key and the whole tail of the `session_events` one, both plain
            # btree indexes. An incompressible id past ~2700 bytes does not merely
            # bloat them, it fails the insert outright ("index row size N exceeds
            # btree version 4 maximum 2704"), so an unchecked id here is a 500 a
            # caller can repeat at the rate limit. `memory://` keys a dict and shows
            # none of this, which is why the cap has to be enforced rather than tested
            # for on the CI path.
            return _rpc_error(rpc_id, -32602, f"taskId is not usable as a thread id: {task_id[:80]!r}")
        ts = int(time.time() * 1000)
        await task_store.put_task(
            settings,
            tenant_id,
            {
                "id": task_id,
                "status": {"state": "working", "timestamp": ts},
                "manifest": name,
                "artifacts": [],
            },
        )
        call_auth = auth or AuthContext(tenant_id=tenant_id, principal_sub="a2a", anonymous=False)

        async def refused(code: int, message: str) -> dict[str, Any]:
            # The task is recorded as failed first: returned from here, it was left `working`
            # and `tasks/get` said so forever.
            await task_store.put_task(
                settings,
                tenant_id,
                {
                    "id": task_id,
                    "status": {"state": "failed", "timestamp": int(time.time() * 1000), "message": message},
                    "manifest": name,
                    "artifacts": [],
                },
            )
            return _rpc_error(rpc_id, code, message)

        try:
            resolved = await resolve_tenant_manifest(settings, tenant_id, name, thread_id=thread)
            await prepare_tenant_invoke(settings, resolved=resolved, auth=call_auth, thread_id=thread)
            from felix.attachments import AttachmentError
            from felix.governance.inbound import INBOUND_SCREENED_EXTRA, apply_inbound_screening
            from felix.patterns.model_vision import unseeable_image_problem

            # Before anything is stored or run, as on `/chat`: a model that cannot see the image
            # would answer as if there were none.
            probe = ChatMessage(
                role="user",
                content=text,
                attachments=[ImageAttachment(url=data_url(m, raw)) for raw, m in images],
            )
            if problem := unseeable_image_problem(resolved.manifest, [probe], settings):
                return await refused(-32602, problem)
            try:
                attachments = await _stored(settings, tenant_id, images)
            except AttachmentError:
                # Not the reason: a quota message carries the tenant's totals.
                return await refused(-32602, "the image could not be stored")
            incoming = ChatMessage(role="user", content=text, attachments=attachments or None)
            req_ctx = RequestContext(settings=settings, auth=call_auth, manifest_id=name, thread_id=thread)
            async with async_run_with_context(req_ctx):
                # In this request's own context: a stored image is screened by reading it back
                # under the tenant, and with no context it reads as nothing to screen. The
                # `/a2a` route already runs inside the middleware's, but `handle_rpc` is called
                # without one too. The whole message, so the agent is handed what screening
                # left, not the text alone.
                screened = await apply_inbound_screening(resolved.manifest, [incoming], settings)
                incoming = screened[0] if screened else incoming
                req_ctx.extras[INBOUND_SCREENED_EXTRA] = True  # screened just above
                agent = await build_tenant_agent(
                    settings,
                    manifest=resolved.manifest,
                    sub_agents=resolved.sub_agents,
                    tools=tools,
                    tenant_id=tenant_id,
                )
                result = await agent.invoke(InvokeInput(messages=[incoming], thread_id=thread))
            task = {
                "id": task_id,
                "status": {"state": "completed", "timestamp": int(time.time() * 1000)},
                "manifest": name,
                "artifacts": [
                    {"parts": [{"type": "text", "text": result.final.content}]},
                ],
            }
        except Exception as exc:
            from felix.governance.inbound import InboundScreeningError
            from felix.manifests.inbound_auth import InboundAuthError

            if isinstance(exc, InboundAuthError):
                return await refused(-32001, exc.detail)
            if isinstance(exc, InboundScreeningError):
                return await refused(-32002, exc.detail)
            task = {
                "id": task_id,
                "status": {
                    "state": "failed",
                    "timestamp": int(time.time() * 1000),
                    "message": str(exc),
                },
                "manifest": name,
                "artifacts": [],
            }
        await task_store.put_task(settings, tenant_id, task)
        return {"jsonrpc": "2.0", "id": rpc_id, "result": task}

    if method == "tasks/get":
        task_id = str(params.get("id") or params.get("taskId") or "")
        task = await task_store.get_task(settings, tenant_id, task_id) if task_id else None
        if task is None:
            return _rpc_error(rpc_id, -32001, f"Task not found: {task_id}")
        return {"jsonrpc": "2.0", "id": rpc_id, "result": task}

    if method == "tasks/cancel":
        task_id = str(params.get("id") or params.get("taskId") or "")
        task = await task_store.cancel_task(settings, tenant_id, task_id) if task_id else None
        if task is None:
            return _rpc_error(rpc_id, -32001, f"Task not found: {task_id}")
        return {"jsonrpc": "2.0", "id": rpc_id, "result": task}

    if method == "tasks/resubscribe":
        task_id = str(params.get("id") or params.get("taskId") or "")
        task = await task_store.get_task(settings, tenant_id, task_id) if task_id else None
        if task is None:
            return _rpc_error(rpc_id, -32001, f"Task not found: {task_id}")
        return {"jsonrpc": "2.0", "id": rpc_id, "result": task}

    return _rpc_error(rpc_id, -32601, f"Method not found: {method}")


__all__ = ["handle_rpc"]
