"""Durable chat runs — fibers by default, Temporal when configured."""

from __future__ import annotations

import logging
from typing import Any

from felix.config import Settings
from felix.context import try_get_context
from felix.durability.fibers import create_fiber, get_fiber, now_ms, save_fiber
from felix.manifests.schema import ABSOLUTE_LIMITS, ExecutionSpec
from felix.patterns.types import ChatMessage

logger = logging.getLogger("felix.durability.runs")


def _ttl_seconds(settings: Settings, execution: ExecutionSpec) -> int:
    """How long the run — and so the authority it records — stays usable.

    Clamped here as well as in the schema. The schema bound only applies at parse; a manifest
    row stored before the cap existed still resolves, and this is the value that becomes
    `expires_at`.
    """
    ceiling = ABSOLUTE_LIMITS["resume_token_ttl_seconds"]
    if execution.resume_token_ttl_seconds is not None:
        return max(1, min(int(execution.resume_token_ttl_seconds), ceiling))
    return max(1, min(int(getattr(settings, "hibernate_after_seconds", 300) or 300), ceiling))


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
    if caller is not None and caller.auth.tenant_id == tenant_id:
        # The tenant guard is not decoration. This function takes `tenant_id` as a parameter
        # *and* reads the principal from ambient context, and reconciles them nowhere else.
        # Both callers today derive both from the same request, but an admin route or a
        # per-tenant fan-out job would write tenant A's scopes into tenant B's fiber, which
        # `_run_fiber_step` would then apply inside `rls_tenant(B)`.
        state["auth"] = {
            "principal_sub": caller.auth.principal_sub,
            "scopes": sorted(caller.auth.scopes),
            "anonymous": bool(caller.auth.anonymous),
            "scheme": caller.auth.scheme,
        }
        # The token's own expiry, as a single integer — not the claims. Without it this would
        # be the first path in Felix where authority survives `exp`: there is no revocation
        # anywhere in `felix/auth/`, so `exp` is the sole and complete bound on a compromised
        # credential, and a 60-second JWT starting a 300-second run would confer its scopes for
        # four minutes past its own death. Clamping here makes "a fiber cannot outlive the
        # token that started it" true rather than nearly true.
        token_exp = caller.auth.raw_claims.get("exp")
        if isinstance(token_exp, (int, float)):
            expires_at = min(expires_at, int(token_exp) * 1000)
            state["expires_at"] = expires_at
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

            # Mark and persist BEFORE starting the workflow, not after. Starting first and
            # saving second handed Temporal a snapshot of the row and then bumped the
            # stored `version` behind it, so every write the activity made compared
            # against a version that was already stale and was discarded — the workflow
            # ran to completion, reported "completed", and the fiber row stayed `pending`
            # forever. A durable chat that finishes invisibly is worse than one that fails.
            state = dict(fiber.get("state_json") or state)
            state["backend"] = "temporal"
            fiber["state_json"] = state
            await save_fiber(settings, fiber)
            await start_fiber_workflow(settings, fiber)
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
