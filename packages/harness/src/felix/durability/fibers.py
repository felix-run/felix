"""Durable fiber scheduler — sleep / step / stash / complete."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from typing import Any

from sqlalchemy import select

from felix.config import Settings
from felix.db.models import Fiber
from felix.db.session import _use_memory, get_session_factory

logger = logging.getLogger("felix.durability.fibers")

now_ms = lambda: int(time.time() * 1000)

_memory_fibers: dict[tuple[str, str], dict[str, Any]] = {}


def reset_memory_fibers() -> None:
    """Test helper — clear the in-memory fiber store.

    Every other `memory://` twin has one and `tests/conftest.py` calls it. This did not, so
    fibers leaked between tests: `_claim_due_memory` returns *every* due row, so a test that
    asserted on how many fibers came back passed alone and failed in the suite.
    """
    _memory_fibers.clear()


# How long a claim is held. Longer than any realistic single step, short enough that a
# worker killed mid-step frees the fiber within a few scheduler ticks.
FIBER_LEASE_MS = 5 * 60 * 1000
# The lease is renewed while a step is in flight, so it bounds "how long after a worker dies
# is its fiber stranded", not "how long may a step take". Those were the same number, and it
# was the wrong one: `FIBER_LEASE_MS` was exactly `approvals/interrupt.py`'s 300s default
# wait, and an approval rule may set `ttl_seconds` up to 3600. A run parked on an approval
# therefore outlived its own claim and was re-claimed by the next sweep — up to twelve times —
# re-running an invoke whose tool side effects had already happened, and then losing the write
# to the CAS check on `version`. Renewing at a third of the lease leaves two missed renewals
# of slack before another worker may take over.
FIBER_LEASE_RENEW_MS = FIBER_LEASE_MS // 3
# Bound the sweep: an unbounded SELECT loads a whole backlog into memory every minute.
FIBER_BATCH = 50


def _fiber_dict(row: Fiber | dict[str, Any]) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    return {
        "tenant_id": row.tenant_id,
        "id": row.id,
        "kind": row.kind,
        "status": row.status,
        "lease_owner": row.lease_owner,
        "lease_until": row.lease_until,
        "version": row.version,
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
    # The claim covers the duration of one step, not the life of the fiber. Every
    # _save_fiber call ends a step transition, so release it here: a still-runnable
    # fiber must be claimable again on the next tick, and a sleeping one when it wakes.
    row["lease_owner"] = ""
    row["lease_until"] = None

    if _use_memory(settings):
        stored = _memory_fibers.get((row["tenant_id"], row["id"]))
        if stored is not None and int(stored.get("version") or 0) != int(row.get("version") or 0):
            logger.warning("fiber version conflict id=%s; discarding stale write", row.get("id"))
            return
        row["version"] = int(row.get("version") or 0) + 1
        _memory_fibers[(row["tenant_id"], row["id"])] = row
        return

    from sqlalchemy import update

    from felix.db.session import rls_bypass

    expected = int(row.get("version") or 0)
    with rls_bypass():
        factory = get_session_factory(settings=settings)
        async with factory() as db:
            # Compare-and-set on version: this is a read-modify-write, and a lost update
            # can rewind `cursor` and replay a step that already ran.
            result = await db.execute(
                update(Fiber)
                .where(
                    Fiber.tenant_id == row["tenant_id"],
                    Fiber.id == row["id"],
                    Fiber.version == expected,
                )
                .values(
                    status=row["status"],
                    state_json=row.get("state_json") or {},
                    wake_at=row.get("wake_at"),
                    updated_at=row["updated_at"],
                    lease_owner=row.get("lease_owner", ""),
                    lease_until=row.get("lease_until"),
                    version=expected + 1,
                )
            )
            await db.commit()
            if not getattr(result, "rowcount", 0):
                logger.warning(
                    "fiber version conflict id=%s expected=%s; discarding stale write",
                    row.get("id"),
                    expected,
                )
                return
    row["version"] = expected + 1


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
                # Resume as the caller who started the run, bounded by the run's own TTL
                # (checked above). Before this, a fiber resumed with an empty scope set, so
                # `spec.policies` denied every policied tool and inbound `required_scopes`
                # refused the resume — making `execution.mode: durable` and `spec.policies`
                # mutually exclusive without either saying so.
                #
                # A fiber with no recorded caller — enqueued before this, or with no request
                # context — keeps the old behaviour: principal "fiber", no scopes, everything
                # policied denies. That is the fail-closed direction.
                stored_auth = state.get("auth") if isinstance(state.get("auth"), dict) else {}
                auth = AuthContext(
                    tenant_id=tenant_id,
                    principal_sub=str(stored_auth.get("principal_sub") or "fiber"),
                    scopes=frozenset(str(x) for x in (stored_auth.get("scopes") or ())),
                    anonymous=bool(stored_auth.get("anonymous", False)),
                    scheme=str(stored_auth.get("scheme") or "anonymous"),
                )
                thread = thread_id or f"{tenant_id}:fiber:{row['id']}"
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
                # Resolution happens *inside* the context, because that is what sets the
                # `app.tenant_id` GUC. The worker installs no ambient RequestContext, so with
                # these three calls above the `async with` they ran with no tenant: under
                # `FELIX_DATABASE_RLS=true` the FORCE'd policy filtered every row,
                # `get_active` returned None, and `_read_tenant_postgres` fell through to the
                # *bundled* manifest of the same name. Operators are told to fork `governed`,
                # so a durable run could silently execute a different, ungoverned manifest —
                # and `ensure_thread_pin` was equally blind, so the drift check that exists to
                # catch exactly that could not see the stored pin either.
                async with async_run_with_context(req_ctx):
                    resolved = await resolve_tenant_manifest(
                        settings, tenant_id, manifest_id, thread_id=thread
                    )
                    pinned = state.get("pin") if isinstance(state.get("pin"), dict) else None
                    if pinned:
                        assert_pin_matches(pinned, resolved.manifest, version=resolved.version)
                    await prepare_tenant_invoke(settings, resolved=resolved, auth=auth, thread_id=thread)
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
                final = result.final.model_dump() if result.final else {"role": "assistant", "content": ""}
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


async def _claim_due_memory(settings: Settings, ts: int) -> list[dict[str, Any]]:
    claimed: list[dict[str, Any]] = []
    for row in _memory_fibers.values():
        if (row.get("state_json") or {}).get("backend") == "temporal":
            continue
        lease_until = row.get("lease_until")
        if lease_until is not None and lease_until > ts:
            continue  # someone else holds the claim
        due_sleep = row["status"] == "sleeping" and row.get("wake_at") is not None and row["wake_at"] <= ts
        if not (due_sleep or row["status"] in {"running", "pending"}):
            continue
        row["status"] = "running"
        row["wake_at"] = None
        row["lease_owner"] = str(getattr(settings, "replica_id", "local") or "local")
        row["lease_until"] = ts + FIBER_LEASE_MS
        claimed.append(dict(row))
        if len(claimed) >= FIBER_BATCH:
            break
    return claimed


async def _claim_due_postgres(settings: Settings, ts: int) -> list[dict[str, Any]]:
    """Claim a bounded batch of due fibers, skipping rows another worker holds.

    ``FOR UPDATE SKIP LOCKED`` plus a lease column is what stops the same step running
    twice: the row lock serializes concurrent claimers within the transaction, and the
    lease keeps the fiber claimed for the duration of the step, which outlives it.
    """
    from felix.db.session import rls_bypass

    owner = str(getattr(settings, "replica_id", "local") or "local")
    factory = get_session_factory(settings=settings)
    # The sweep is cross-tenant maintenance, like retention: without a bypass this runs
    # with no app.tenant_id GUC and RLS silently returns nothing, stalling durability.
    with rls_bypass():
        async with factory() as db:
            stmt = (
                select(Fiber)
                .where(
                    Fiber.status.in_(("running", "pending", "sleeping")),
                    # sleeping fibers are only due once their timer fires
                    (Fiber.status != "sleeping") | (Fiber.wake_at.is_not(None) & (Fiber.wake_at <= ts)),
                    # unclaimed, or the previous claim expired (crashed worker)
                    Fiber.lease_until.is_(None) | (Fiber.lease_until <= ts),
                )
                .order_by(Fiber.updated_at)
                .limit(FIBER_BATCH)
                .with_for_update(skip_locked=True)
            )
            rows = (await db.scalars(stmt)).all()
            claimed: list[dict[str, Any]] = []
            for row in rows:
                if (row.state_json or {}).get("backend") == "temporal":
                    continue
                row.status = "running"
                row.wake_at = None
                row.lease_owner = owner
                row.lease_until = ts + FIBER_LEASE_MS
                row.updated_at = ts
                row.version = int(row.version or 0) + 1
                claimed.append(_fiber_dict(row))
            await db.commit()
            return claimed


async def resume_due_fibers(settings: Settings) -> int:
    """Claim and advance due fibers. Returns steps run.

    Each fiber is claimed before it is stepped, so a step still running when the next
    scheduler tick fires is not picked up again.
    """
    ts = now_ms()
    if _use_memory(settings):
        due = await _claim_due_memory(settings, ts)
    else:
        due = await _claim_due_postgres(settings, ts)

    ran = 0
    for row in due:
        try:
            await _step_with_lease(settings, row)
        except Exception:
            logger.warning("fiber step failed id=%s", row.get("id"), exc_info=True)
            await _release_fiber(settings, row)
        ran += 1
    return ran


async def _renew_lease(settings: Settings, row: dict[str, Any]) -> None:
    """Push this worker's claim out by another lease window."""
    until = now_ms() + FIBER_LEASE_MS
    owner = str(getattr(settings, "replica_id", "local") or "local")
    if _use_memory(settings):
        stored = _memory_fibers.get((row["tenant_id"], row["id"]))
        # Only if we still hold it: a lease we already lost must not be stolen back mid-step.
        if stored is not None and stored.get("lease_owner") == owner:
            stored["lease_until"] = until
        return
    from sqlalchemy import update

    from felix.db.session import rls_bypass

    with rls_bypass():
        factory = get_session_factory(settings=settings)
        async with factory() as db:
            await db.execute(
                update(Fiber)
                .where(
                    Fiber.tenant_id == row["tenant_id"],
                    Fiber.id == row["id"],
                    Fiber.lease_owner == owner,
                )
                .values(lease_until=until)
            )
            await db.commit()


async def _step_with_lease(settings: Settings, row: dict[str, Any]) -> None:
    """Run one step, renewing the claim for as long as it is actually running."""

    async def _heartbeat() -> None:
        while True:
            await asyncio.sleep(FIBER_LEASE_RENEW_MS / 1000)
            try:
                await _renew_lease(settings, row)
            except Exception:  # pragma: no cover - a failed renewal just lets the lease lapse
                logger.warning("fiber lease renewal failed id=%s", row.get("id"), exc_info=True)
                return

    beat = asyncio.create_task(_heartbeat())
    try:
        await _run_fiber_step(settings, row)
    finally:
        beat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beat


async def _release_fiber(settings: Settings, row: dict[str, Any]) -> None:
    """Drop the claim so a failed step is retried rather than stranded until expiry."""
    if _use_memory(settings):
        stored = _memory_fibers.get((row["tenant_id"], row["id"]))
        if stored is not None:
            stored["lease_owner"] = ""
            stored["lease_until"] = None
        return
    from sqlalchemy import update

    from felix.db.session import rls_bypass

    with rls_bypass():
        factory = get_session_factory(settings=settings)
        async with factory() as db:
            await db.execute(
                update(Fiber)
                .where(Fiber.tenant_id == row["tenant_id"], Fiber.id == row["id"])
                .values(lease_owner="", lease_until=None)
            )
            await db.commit()


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
