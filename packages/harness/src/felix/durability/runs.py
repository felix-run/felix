"""Durable chat runs — fibers by default, Temporal when configured."""

from __future__ import annotations

import logging
from typing import Any

from felix.config import Settings
from felix.context import try_get_context
from felix.durability.fibers import create_fiber, get_fiber, now_ms, save_fiber
from felix.manifests.schema import ExecutionSpec
from felix.patterns.types import ChatMessage

logger = logging.getLogger("felix.durability.runs")


def _ttl_seconds(settings: Settings, execution: ExecutionSpec) -> int:
    if execution.resume_token_ttl_seconds is not None:
        return max(1, int(execution.resume_token_ttl_seconds))
    return max(1, int(getattr(settings, "hibernate_after_seconds", 300) or 300))


def _dump_messages(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if hasattr(m, "model_dump"):
            out.append(m.model_dump())
        elif isinstance(m, dict):
            out.append(dict(m))
        else:
            out.append(
                {
                    "role": getattr(m, "role", "user"),
                    "content": str(getattr(m, "content", "")),
                }
            )
    return out


async def start_durable_chat(
    settings: Settings,
    tenant_id: str,
    *,
    manifest_id: str,
    messages: list[ChatMessage],
    thread_id: str | None,
    model_id: str | None,
    execution: ExecutionSpec,
    pin: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Enqueue an invoke fiber and optionally start a Temporal workflow."""
    ttl = _ttl_seconds(settings, execution)
    expires_at = now_ms() + ttl * 1000
    state: dict[str, Any] = {
        "steps": [
            {
                "op": "invoke",
                "manifest_id": manifest_id,
                "messages": _dump_messages(messages),
                "model_id": model_id,
                "thread_id": thread_id,
            }
        ],
        "cursor": 0,
        "stash": {},
        "expires_at": expires_at,
    }
    if pin:
        state["pin"] = pin

    # Who asked for this run. Without it a resumed fiber runs with an empty scope set, so
    # `spec.policies` denies every policied tool and `auth.inbound.required_scopes` refuses the
    # resume — a manifest that works over HTTP stops working the moment it is made durable.
    #
    # This is authority in durable state, so the bound matters: it is exactly the caller's own
    # scopes, never widened, and it dies with the run. `state["expires_at"]` above is enforced
    # at resume (`fibers.py`), and its default is `hibernate_after_seconds` — five minutes, not
    # five weeks. A fiber cannot outlive the token that started it by more than its own TTL.
    #
    # Absent (enqueued with no request context), resume falls back to no scopes, which is what
    # it did before. Fail closed on the way in, not just on the way out.
    caller = try_get_context()
    if caller is not None:
        state["auth"] = {
            "principal_sub": caller.auth.principal_sub,
            "scopes": sorted(caller.auth.scopes),
            "anonymous": bool(caller.auth.anonymous),
            "scheme": caller.auth.scheme,
        }
    fiber = await create_fiber(
        settings,
        tenant_id,
        kind="durable_chat",
        status="pending",
        state=state,
    )
    if getattr(settings, "durability", "fibers") == "temporal":
        try:
            from felix.durability.temporal import start_fiber_workflow

            await start_fiber_workflow(settings, fiber)
            state = dict(fiber.get("state_json") or state)
            state["backend"] = "temporal"
            fiber["state_json"] = state
            await save_fiber(settings, fiber)
        except Exception:
            # Record the fallback, not just log it. `backend` was set inside the `try`,
            # so a failed start left the row indistinguishable from a run that never
            # asked for Temporal -- and the feature was broken for long enough that
            # nobody could tell from a fiber row which one they were looking at.
            state = dict(fiber.get("state_json") or state)
            state["backend"] = "fibers"
            state["backend_fallback"] = "temporal_start_failed"
            fiber["state_json"] = state
            await save_fiber(settings, fiber)
            logger.warning(
                "temporal start failed; fiber scheduler will run this chat",
                exc_info=True,
            )
    return {
        "status": "accepted",
        "resume_token": fiber["id"],
        "fiber_id": fiber["id"],
        "expires_at": expires_at,
        "thread_id": thread_id,
    }


async def get_durable_run(settings: Settings, tenant_id: str, resume_token: str) -> dict[str, Any] | None:
    row = await get_fiber(settings, tenant_id, resume_token)
    if row is None:
        return None
    state = dict(row.get("state_json") or {})
    last = dict((state.get("stash") or {}).get("last") or {})
    return {
        "status": row.get("status"),
        "fiber_id": row.get("id"),
        "resume_token": row.get("id"),
        "expires_at": state.get("expires_at"),
        "final": last.get("final") or ({"role": "assistant", "content": last.get("answer") or ""}),
        "error": last.get("error") or "",
        "manifest_id": last.get("manifest_id") or "",
    }


__all__ = ["get_durable_run", "start_durable_chat"]
