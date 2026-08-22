"""A2A server — JSON-RPC methods backed by Felix agents."""

from __future__ import annotations

import time
import uuid
from typing import Any

from felix.a2a import tasks as task_store
from felix.config import Settings
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.patterns.types import ChatMessage, InvokeInput
from felix.tools.provider import ToolProvider


def _extract_text(params: dict[str, Any]) -> str:
    message = params.get("message") or params
    if isinstance(message, str):
        return message
    parts = message.get("parts") if isinstance(message, dict) else None
    if isinstance(parts, list):
        texts = [
            str(p.get("text") or p.get("content") or "")
            for p in parts
            if isinstance(p, dict)
        ]
        joined = "\n".join(t for t in texts if t)
        if joined:
            return joined
    if isinstance(message, dict):
        return str(message.get("text") or message.get("content") or "")
    return ""


async def handle_rpc(
    *,
    settings: Settings,
    tools: ToolProvider,
    tenant_id: str,
    method: str,
    params: dict[str, Any],
    rpc_id: str | int | None,
) -> dict[str, Any]:
    from felix.runtime import build_tenant_agent, resolve_tenant_manifest

    if method == "agent/authenticatedExtendedCard":
        from felix.a2a.card import build_agent_card
        from felix.manifests.loader import load_bundled

        name = str(params.get("manifest") or settings.default_manifest)
        try:
            manifest = load_bundled(name)
        except FileNotFoundError:
            return {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "error": {"code": -32004, "message": f"Unknown manifest: {name}"},
            }
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": build_agent_card(manifest),
        }

    if method == "message/send":
        name = str(params.get("manifest") or settings.default_manifest)
        text = _extract_text(params)
        if not text:
            return {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "error": {"code": -32602, "message": "message text required"},
            }
        task_id = str(params.get("taskId") or uuid.uuid4())
        thread = f"{tenant_id}:a2a:{task_id}"
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
        auth = AuthContext(tenant_id=tenant_id, principal_sub="a2a", anonymous=False)
        try:
            resolved = await resolve_tenant_manifest(
                settings, tenant_id, name, thread_id=thread
            )
            req_ctx = RequestContext(
                settings=settings, auth=auth, manifest_id=name, thread_id=thread
            )
            async with async_run_with_context(req_ctx):
                agent = await build_tenant_agent(
                    settings,
                    manifest=resolved.manifest,
                    tools=tools,
                    tenant_id=tenant_id,
                )
                result = await agent.invoke(
                    InvokeInput(
                        messages=[ChatMessage(role="user", content=text)],
                        thread_id=thread,
                    )
                )
            task = {
                "id": task_id,
                "status": {"state": "completed", "timestamp": int(time.time() * 1000)},
                "manifest": name,
                "artifacts": [
                    {"parts": [{"type": "text", "text": result.final.content}]},
                ],
            }
        except Exception as exc:
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
            return {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "error": {"code": -32001, "message": f"Task not found: {task_id}"},
            }
        return {"jsonrpc": "2.0", "id": rpc_id, "result": task}

    if method == "tasks/cancel":
        task_id = str(params.get("id") or params.get("taskId") or "")
        task = (
            await task_store.cancel_task(settings, tenant_id, task_id) if task_id else None
        )
        if task is None:
            return {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "error": {"code": -32001, "message": f"Task not found: {task_id}"},
            }
        return {"jsonrpc": "2.0", "id": rpc_id, "result": task}

    if method == "tasks/resubscribe":
        task_id = str(params.get("id") or params.get("taskId") or "")
        task = await task_store.get_task(settings, tenant_id, task_id) if task_id else None
        if task is None:
            return {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "error": {"code": -32001, "message": f"Task not found: {task_id}"},
            }
        return {"jsonrpc": "2.0", "id": rpc_id, "result": task}

    return {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


__all__ = ["handle_rpc"]
