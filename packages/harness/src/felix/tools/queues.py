"""Queue tools from ``spec.queues`` — Redis lists with an in-memory fallback."""

from __future__ import annotations

import json
import time
import uuid
from collections import deque
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from felix.config import Settings
from felix.context import try_get_context
from felix.db.session import _use_memory
from felix.manifests.schema import QueueRef
from felix.tools.types import Tool, ToolInvocationCtx, define_tool

_memory: dict[str, deque[str]] = {}


class QueueArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["enqueue", "dequeue"] = "enqueue"
    payload: dict[str, Any] = Field(
        default_factory=dict, description="JSON body to enqueue (ignored on dequeue)."
    )


def _now_ms() -> int:
    return int(time.time() * 1000)


def _key(tenant_id: str, binding: str) -> str:
    return f"felix:queue:{tenant_id}:{binding}"


def _tenant_id() -> str:
    ctx = try_get_context()
    if ctx is not None:
        return ctx.auth.tenant_id
    return "default"


def _expired(msg: dict[str, Any]) -> bool:
    expires = msg.get("expires_at")
    return isinstance(expires, int) and expires > 0 and _now_ms() > expires


def _encode(
    payload: dict[str, Any],
    *,
    deadline_ms: int | None,
    ctx: ToolInvocationCtx | None,
) -> str:
    expires_at = (_now_ms() + deadline_ms) if deadline_ms else None
    return json.dumps(
        {
            "id": uuid.uuid4().hex,
            "payload": payload,
            "enqueued_at": _now_ms(),
            "expires_at": expires_at,
            "manifest_id": getattr(ctx, "manifest_id", None) or "",
            "thread_id": getattr(ctx, "thread_id", None) or "",
        },
        separators=(",", ":"),
    )


async def enqueue_message(
    settings: Settings | None,
    tenant_id: str,
    binding: str,
    raw: str,
) -> None:
    if settings is None or _use_memory(settings):
        _memory.setdefault(_key(tenant_id, binding), deque()).append(raw)
        return
    import redis.asyncio as redis

    client = redis.from_url(settings.redis_url)
    try:
        await client.rpush(_key(tenant_id, binding), raw)
    finally:
        await client.aclose()


async def dequeue_message(
    settings: Settings | None,
    tenant_id: str,
    binding: str,
) -> str | None:
    key = _key(tenant_id, binding)
    if settings is None or _use_memory(settings):
        q = _memory.get(key)
        if not q:
            return None
        return q.popleft()
    import redis.asyncio as redis

    client = redis.from_url(settings.redis_url)
    try:
        item = await client.lpop(key)
    finally:
        await client.aclose()
    if item is None:
        return None
    if isinstance(item, bytes):
        return item.decode("utf-8", errors="replace")
    return str(item)


def tools_from_queues(
    refs: list[QueueRef],
    *,
    settings: Settings | None = None,
) -> list[Tool]:
    out: list[Tool] = []
    for ref in refs:
        deadline = ref.deadline_ms
        binding = ref.queue_binding
        schema = ref.args_schema if isinstance(ref.args_schema, dict) else QueueArgs

        async def handler(
            args: QueueArgs | dict[str, Any],
            ctx: ToolInvocationCtx | None = None,
            *,
            _binding: str = binding,
            _deadline: int | None = deadline,
        ) -> str:
            if isinstance(args, dict):
                action = str(args.get("action") or "enqueue")
                if "payload" in args and isinstance(args["payload"], dict):
                    payload = dict(args["payload"])
                else:
                    payload = {k: v for k, v in args.items() if k != "action"}
            else:
                action = args.action
                payload = dict(args.payload)
            tenant = _tenant_id()
            if action == "dequeue":
                while True:
                    raw = await dequeue_message(settings, tenant, _binding)
                    if raw is None:
                        return "(empty)"
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        return raw
                    if isinstance(msg, dict) and _expired(msg):
                        continue
                    return raw
            raw = _encode(payload, deadline_ms=_deadline, ctx=ctx)
            await enqueue_message(settings, tenant, _binding, raw)
            return f"enqueued:{json.loads(raw)['id']}"

        out.append(
            define_tool(
                name=ref.name,
                description=ref.description or f"Enqueue or dequeue JSON on queue '{binding}'.",
                handler=handler,
                args_schema=schema if isinstance(schema, dict) else None,
                args=schema if not isinstance(schema, dict) else None,
                source=f"queue:{binding}",
                fatal=ref.fatal,
                transport="queue",
            )
        )
    return out


__all__ = ["dequeue_message", "enqueue_message", "tools_from_queues"]
