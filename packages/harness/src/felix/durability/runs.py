"""Durable chat runs, enqueued as fibers the worker drives to completion."""

from __future__ import annotations

import logging
from typing import Any

from felix.config import Settings
from felix.context import try_get_context
from felix.durability.fibers import active_fiber_for_thread, create_fiber, get_fiber, now_ms
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
    """Enqueue an invoke fiber; the worker's fiber scheduler runs it.

    Raises `RunInProgress` when `thread_id` already has a durable run in flight: one run per
    thread, checked atomically with the enqueue (felix-run/felix#529).
    """
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
            # The starter's personal skill library, so the resumed compile loads the catalog the
            # request would have. Without it the resume silently ran on the org's skills alone.
            "skill_owner": caller.auth.skill_owner,
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
    from felix.durability.webhooks import endpoints_for_run

    fiber = await create_fiber(
        settings,
        tenant_id,
        kind="durable_chat",
        status="pending",
        state=state,
        # Validated against the registry for this tenant before anything is written, so a
        # manifest naming an endpoint it may not use is refused at enqueue, not after the run.
        webhooks=endpoints_for_run(settings, tenant_id, list(execution.webhooks)),
        thread_id=thread_id,
        exclusive_on_thread=True,
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
    return run_view(row)


async def active_durable_run(settings: Settings, tenant_id: str, thread_id: str) -> dict[str, Any] | None:
    """The durable run in flight on `thread_id`, as a client needs it to watch -- or None.

    The only way to learn a run's `resume_token` used to be the response that started it, so a
    client that reloaded had no handle on a run still writing to the thread it was showing.
    """
    row = await active_fiber_for_thread(settings, tenant_id, thread_id)
    if row is None:
        return None
    state = dict(row.get("state_json") or {})
    return {
        "resume_token": row.get("id"),
        "status": row.get("status"),
        "expires_at": state.get("expires_at"),
    }


def run_view(row: dict[str, Any]) -> dict[str, Any]:
    """What a caller is told about a durable run — the poll and the completion webhook both.

    One function so the two cannot drift: a webhook that said less than the poll would send a
    receiver back to poll anyway, and one that said more would be a second, unreviewed view.
    """
    state = dict(row.get("state_json") or {})
    last = dict((state.get("stash") or {}).get("last") or {})
    return {
        "status": row.get("status"),
        "fiber_id": row.get("id"),
        "resume_token": row.get("id"),
        "expires_at": state.get("expires_at"),
        "final": last.get("final") or ({"role": "assistant", "content": last.get("answer") or ""}),
        # A fiber buried because its *save* kept failing could not record the text.
        "error": last.get("error")
        or (f"step failed {int(row.get('attempts') or 0)} times" if row.get("status") == "dead" else ""),
        "manifest_id": last.get("manifest_id") or "",
        **_webhook_view(row),
    }


def _webhook_view(row: dict[str, Any]) -> dict[str, Any]:
    """Each endpoint's delivery status, when the run named any: `pending`, `delivered`, `dead`."""
    endpoints = dict((row.get("webhook_state") or {}).get("endpoints") or {})
    if not endpoints:
        return {}
    return {"webhooks": {name: str((ep or {}).get("status") or "pending") for name, ep in endpoints.items()}}


__all__ = ["active_durable_run", "get_durable_run", "run_view", "start_durable_chat"]
