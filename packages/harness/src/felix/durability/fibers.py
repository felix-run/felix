"""Durable fiber scheduler — sleep / step / stash / complete."""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from sqlalchemy import select

from felix.config import Settings
from felix.db.models import Fiber
from felix.db.session import _use_memory, get_session_factory

logger = logging.getLogger("felix.durability.fibers")

now_ms = lambda: int(time.time() * 1000)  # noqa: E731

_memory_fibers: dict[tuple[str, str], dict[str, Any]] = {}


def _fiber_dict(row: Fiber | dict[str, Any]) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    return {
        "tenant_id": row.tenant_id,
        "id": row.id,
        "kind": row.kind,
        "status": row.status,
        "state_json": row.state_json,
        "wake_at": row.wake_at,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


async def create_fiber(
    settings: Settings,
    tenant_id: str,
    *,
    kind: str = "step",
    state: dict[str, Any] | None = None,
    wake_at: int | None = None,
    status: str = "pending",
) -> dict[str, Any]:
    from felix.secrets import redact_json

    fiber_id = uuid.uuid4().hex
    ts = now_ms()
    safe_state = redact_json(state or {})
    row = {
        "tenant_id": tenant_id,
        "id": fiber_id,
        "kind": kind,
        "status": "sleeping" if wake_at else status,
        "state_json": safe_state if isinstance(safe_state, dict) else {},
        "wake_at": wake_at,
        "created_at": ts,
        "updated_at": ts,
    }
    if _use_memory(settings):
        _memory_fibers[(tenant_id, fiber_id)] = row
        return _fiber_dict(row)

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        db.add(Fiber(**row))
        await db.commit()
        return row


async def _save_fiber(settings: Settings, row: dict[str, Any]) -> None:
    from felix.secrets import redact_json

    row["updated_at"] = now_ms()
    state = row.get("state_json") or {}
    safe = redact_json(state)
    row["state_json"] = safe if isinstance(safe, dict) else {}
    if _use_memory(settings):
        _memory_fibers[(row["tenant_id"], row["id"])] = row
        return
    factory = get_session_factory(settings=settings)
    async with factory() as db:
        fiber = await db.get(Fiber, (row["tenant_id"], row["id"]))
        if fiber is None:
            return
        fiber.status = row["status"]
        fiber.state_json = row.get("state_json") or {}
        fiber.wake_at = row.get("wake_at")
        fiber.updated_at = row["updated_at"]
        await db.commit()


async def _run_fiber_step(settings: Settings, row: dict[str, Any]) -> dict[str, Any]:
    """Advance one fiber step.

    ``state_json`` schema:
      {
        "steps": [{"op": "sleep"|"invoke"|"stash"|"complete", ...}, ...],
        "cursor": 0,
        "stash": {},
        "result": null
      }
    """
    state = dict(row.get("state_json") or {})
    steps = list(state.get("steps") or [])
    cursor = int(state.get("cursor") or 0)
    stash = dict(state.get("stash") or {})

    if cursor >= len(steps):
        row["status"] = "completed"
        state["result"] = stash.get("last") or state.get("result")
        row["state_json"] = state
        row["wake_at"] = None
        await _save_fiber(settings, row)
        return row

    expires_at = state.get("expires_at")
    if expires_at is not None and now_ms() > int(expires_at):
        row["status"] = "expired"
        row["wake_at"] = None
        await _save_fiber(settings, row)
        return row

    step = steps[cursor]
    op = str(step.get("op") or "complete")

    if op == "sleep":
        delay_ms = int(step.get("delay_ms") or 0)
        row["status"] = "sleeping"
        row["wake_at"] = now_ms() + max(delay_ms, 0)
        state["cursor"] = cursor + 1
        row["state_json"] = state
        await _save_fiber(settings, row)
        return row

    if op == "stash":
        stash.update(dict(step.get("data") or {}))
        state["stash"] = stash
        state["cursor"] = cursor + 1
        row["state_json"] = state
        row["status"] = "running"
        await _save_fiber(settings, row)
        return row

    if op == "invoke":
        manifest_id = str(step.get("manifest_id") or "")
        prompt = str(step.get("prompt") or stash.get("prompt") or "continue")
        raw_messages = step.get("messages") or stash.get("messages")
        model_id = step.get("model_id") or stash.get("model_id")
        thread_id = str(step.get("thread_id") or stash.get("thread_id") or "")
        answer = ""
        final: dict[str, Any] | str = ""
        error = ""
        if manifest_id:
            try:
                from felix.context import AuthContext, RequestContext, async_run_with_context
                from felix.manifests.pin import assert_pin_matches
                from felix.patterns.types import ChatMessage, InvokeInput
                from felix.runtime import (
                    build_tenant_agent,
                    prepare_tenant_invoke,
                    resolve_tenant_manifest,
                )
                from felix.tools.builtins import default_tool_provider

                provider = default_tool_provider()
                tenant_id = row["tenant_id"]
                auth = AuthContext(tenant_id=tenant_id, principal_sub="fiber", anonymous=False)
                thread = thread_id or f"{tenant_id}:fiber:{row['id']}"
                resolved = await resolve_tenant_manifest(
                    settings, tenant_id, manifest_id, thread_id=thread
                )
                pinned = state.get("pin") if isinstance(state.get("pin"), dict) else None
                if pinned:
                    assert_pin_matches(
                        pinned, resolved.manifest, version=resolved.version
                    )
                await prepare_tenant_invoke(
                    settings, resolved=resolved, auth=auth, thread_id=thread
                )
                req_ctx = RequestContext(
                    settings=settings,
                    auth=auth,
                    manifest_id=manifest_id,
                    thread_id=thread,
                )
                if isinstance(raw_messages, list) and raw_messages:
                    messages = [
                        m if isinstance(m, ChatMessage) else ChatMessage.model_validate(m)
                        for m in raw_messages
                    ]
                else:
                    messages = [ChatMessage(role="user", content=prompt)]
                async with async_run_with_context(req_ctx):
                    agent = await build_tenant_agent(
                        settings,
                        manifest=resolved.manifest,
                        tools=provider,
                        tenant_id=tenant_id,
                    )
                    result = await agent.invoke(
                        InvokeInput(
                            messages=messages,
                            thread_id=thread,
                            model_id=str(model_id) if model_id else None,
                            tenant_id=tenant_id,
                        )
                    )
                answer = result.final.content if result.final else ""
                final = (
                    result.final.model_dump()
                    if result.final
                    else {"role": "assistant", "content": ""}
                )
            except Exception as exc:
                logger.exception("fiber_invoke_failed id=%s", row["id"])
                error = str(exc)
        stash["last"] = {
            "answer": answer,
            "final": final,
            "error": error,
            "manifest_id": manifest_id,
        }
        state["stash"] = stash
        state["cursor"] = cursor + 1
        row["state_json"] = state
        row["status"] = "failed" if error else "running"
        row["wake_at"] = None
        await _save_fiber(settings, row)
        return row

    # complete / unknown
    row["status"] = "completed"
    state["cursor"] = cursor + 1
    state["result"] = stash.get("last") or step.get("result")
    row["state_json"] = state
    row["wake_at"] = None
    await _save_fiber(settings, row)
    return row


async def resume_due_fibers(settings: Settings) -> int:
    """Wake due sleeping fibers and advance runnable ones. Returns steps run."""
    ts = now_ms()
    due: list[dict[str, Any]] = []

    if _use_memory(settings):
        for row in _memory_fibers.values():
            if (row.get("state_json") or {}).get("backend") == "temporal":
                continue
            due_sleep = (
                row["status"] == "sleeping"
                and row.get("wake_at") is not None
                and row["wake_at"] <= ts
            )
            if due_sleep:
                row["status"] = "running"
                row["wake_at"] = None
                due.append(dict(row))
            elif row["status"] in {"running", "pending"}:
                due.append(dict(row))
    else:
        factory = get_session_factory(settings=settings)
        async with factory() as db:
            sleeping = (
                await db.scalars(
                    select(Fiber).where(
                        Fiber.status == "sleeping",
                        Fiber.wake_at.is_not(None),
                        Fiber.wake_at <= ts,
                    )
                )
            ).all()
            for row in sleeping:
                if (row.state_json or {}).get("backend") == "temporal":
                    continue
                row.status = "running"
                row.wake_at = None
                row.updated_at = ts
            runnable = (
                await db.scalars(select(Fiber).where(Fiber.status.in_(("running", "pending"))))
            ).all()
            await db.commit()
            due = [
                _fiber_dict(r)
                for r in runnable
                if (r.state_json or {}).get("backend") != "temporal"
            ]

    ran = 0
    for row in due:
        await _run_fiber_step(settings, row)
        ran += 1
    return ran


async def get_fiber(settings: Settings, tenant_id: str, fiber_id: str) -> dict[str, Any] | None:
    if _use_memory(settings):
        row = _memory_fibers.get((tenant_id, fiber_id))
        return _fiber_dict(row) if row else None
    factory = get_session_factory(settings=settings)
    async with factory() as db:
        fiber = await db.get(Fiber, (tenant_id, fiber_id))
        return _fiber_dict(fiber) if fiber else None


advance_fiber = _run_fiber_step
save_fiber = _save_fiber


__all__ = [
    "advance_fiber",
    "create_fiber",
    "get_fiber",
    "now_ms",
    "resume_due_fibers",
    "save_fiber",
]
