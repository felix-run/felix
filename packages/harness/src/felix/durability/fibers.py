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
# How long a step sleeps when the inbound screener was unavailable under `on_flag: block`.
FIBER_SCREENER_RETRY_MS = 60_000
# After this many sleeps the step fails like any other error, rather than waking forever.
FIBER_SCREENER_MAX_RETRIES = 10
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
# A step that raises outside the invoke's own handler — a save, a lease write, a store that
# is down — is retried after a delay that doubles per consecutive failure, from this base
# to this cap, and after `Settings.fiber_max_attempts` failures the fiber is `dead`: never
# claimed again, its error on the run view. At the default of 5 the delays are 1m, 2m, 4m,
# 8m and the fiber is dead 15 minutes after its first failure; the cap is reached from the
# eighth attempt. Before this it was released and re-claimed on the next tick, once a
# minute, until `expires_at`.
FIBER_RETRY_BASE_MS = 60_000
FIBER_RETRY_MAX_MS = 60 * 60 * 1000
# Statuses a fiber never leaves: the claim never selects them and nothing advances them
# again. Every consumer that decides "is this run over" — the resume stream, the SDK poller,
# the Temporal workflow loop — is checked against this set in `tests/unit/test_invariants.py`.
FIBER_TERMINAL_STATUSES = frozenset({"completed", "failed", "expired", "dead"})
# A backstop on how many ops one claim may run, for a `steps` list long enough that running
# it whole would hold the worker off the other 49 fibers in the batch. It is deliberately far
# above any shape the harness itself builds — a durable chat has one step — because the real
# bound on wall-clock is the one below it: at most one `invoke` per claim.
FIBER_MAX_OPS_PER_CLAIM = 64


def fiber_thread_id(tenant_id: str, fiber_id: str) -> str:
    """The thread a fiber writes to when the request supplied none.

    Named rather than interpolated at the two places that need it, because the API now
    derives the same id to tail a durable run's session log. A durable run with no thread
    of its own is exactly the case where an f-string here and a different one there would
    silently tail an empty log forever.

    The other two namespaced thread ids go through `thread_ids.a2a_thread_id` /
    `eval_thread_id`, which validate a caller-supplied segment and may return None. This
    one stays a plain f-string and returns `str`: `fiber_id` is a `uuid4` the harness
    generates, so it can fail no check those apply, and routing it through would add an
    unreachable None branch at both call sites.
    """
    return f"{tenant_id}:fiber:{fiber_id}"


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
        "attempts": int(row.attempts or 0),
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
        # Explicit, and matching the column's own default. Omitting it made the returned
        # dict disagree with the row that was just written: `_save_fiber` reads
        # `int(row.get("version") or 0)` for its compare-and-set, so any caller that keeps
        # this dict rather than re-reading the row wrote against a version the database
        # never had. The Postgres sweeper always re-reads and so never saw it; the Temporal
        # backend uses this dict directly, and every one of its writes was discarded.
        "version": 0,
        "attempts": 0,
    }
    if _use_memory(settings):
        _memory_fibers[(tenant_id, fiber_id)] = row
        return _fiber_dict(row)

    # Bound to this tenant rather than bypassing: the caller named the tenant, so the policy
    # can enforce it instead of being switched off. The HTTP path already supplies one --
    # `AuthMiddleware` wraps every request in `async_run_with_context`, which binds it -- but
    # the worker has no request context, and `fiber_scheduler` reaches here.
    from felix.db.session import tenant_session

    async with tenant_session(settings, tenant_id) as db:
        db.add(Fiber(**row))
        await db.commit()
        return row


async def _save_fiber(settings: Settings, row: dict[str, Any], *, hold_claim: bool = False) -> None:
    from felix.secrets import redact_json

    row["updated_at"] = now_ms()
    state = row.get("state_json") or {}
    safe = redact_json(state)
    row["state_json"] = safe if isinstance(safe, dict) else {}
    if not hold_claim:
        # The claim covers the duration of one step, not the life of the fiber. Every
        # _save_fiber call ends a step transition, so release it here: a still-runnable
        # fiber must be claimable again on the next tick, and a sleeping one when it wakes.
        #
        # `hold_claim` is for the one caller that runs several steps under a single claim
        # (`_step_with_lease`) and releases once at the end. It cannot be the default: a
        # released claim is what lets the *next* tick pick the fiber up, and a save that
        # kept it would strand a runnable fiber for the whole `FIBER_LEASE_MS` window.
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
                    attempts=int(row.get("attempts") or 0),
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


async def _run_fiber_step(
    settings: Settings, row: dict[str, Any], *, hold_claim: bool = False
) -> dict[str, Any]:
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
        await _save_fiber(settings, row, hold_claim=hold_claim)
        return row

    expires_at = state.get("expires_at")
    if expires_at is not None and now_ms() > int(expires_at):
        row["status"] = "expired"
        row["wake_at"] = None
        await _save_fiber(settings, row, hold_claim=hold_claim)
        return row

    step = steps[cursor]
    op = str(step.get("op") or "complete")

    if op == "sleep":
        delay_ms = int(step.get("delay_ms") or 0)
        row["status"] = "sleeping"
        row["wake_at"] = now_ms() + max(delay_ms, 0)
        state["cursor"] = cursor + 1
        row["state_json"] = state
        await _save_fiber(settings, row, hold_claim=hold_claim)
        return row

    if op == "stash":
        stash.update(dict(step.get("data") or {}))
        state["stash"] = stash
        state["cursor"] = cursor + 1
        row["state_json"] = state
        row["status"] = "running"
        await _save_fiber(settings, row, hold_claim=hold_claim)
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
                stored_auth = state.get("auth") if isinstance(state.get("auth"), dict) else {}
                # Resume as the caller who started the run, bounded by the run's own TTL
                # (checked above). Before this, a fiber resumed with an empty scope set, so
                # `spec.policies` denied every policied tool and inbound `required_scopes`
                # refused the resume — making `execution.mode: durable` and `spec.policies`
                # mutually exclusive without either saying so.
                #
                # A fiber with no recorded caller — enqueued before this, or with no request
                # context — keeps the old behaviour: principal "fiber", no scopes, everything
                # policied denies. That is the fail-closed direction.
                auth = AuthContext(
                    tenant_id=tenant_id,
                    # The actor is the fiber, not the person. Every other machine actor in
                    # this codebase does the same — `cron`, `eval`, `a2a` — and making the
                    # resumed run claim to *be* the caller would put `principal_subj=alice,
                    # scheme=jwt` in an audit row for work a worker did against a manifest
                    # that may have changed underneath it.
                    principal_sub="fiber",
                    on_behalf_of=str(stored_auth.get("principal_sub") or ""),
                    # A list, or nothing. `frozenset("admin")` is {"a","d","m","i","n"} —
                    # the one spot where the surrounding defensiveness was decorative.
                    scopes=frozenset(
                        str(x)
                        for x in (stored_auth.get("scopes") or [])
                        if isinstance(stored_auth.get("scopes"), list)
                    ),
                    anonymous=bool(stored_auth.get("anonymous", False)),
                    scheme=str(stored_auth.get("scheme") or "anonymous"),
                )
                thread = thread_id or fiber_thread_id(tenant_id, str(row["id"]))
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
                        # `pin_compile` is forced when the run carries recorded authority. The
                        # manifest is *re-resolved* here, not carried, and `pin_compile`
                        # defaults to false — so a holder of `manifests:write` could publish a
                        # new active version between the 202 and the scheduler tick, and the
                        # fiber would run their manifest with the original caller's scopes.
                        # Carrying authority and re-resolving the code that authority runs are
                        # not separable decisions.
                        #
                        # A fiber with no recorded auth keeps the manifest's own setting: it
                        # has nothing to escalate with, and forcing drift refusal there would
                        # break runs that work today.
                        if stored_auth:
                            pinned = {**pinned, "pin_compile": True}
                        assert_pin_matches(pinned, resolved.manifest, version=resolved.version)
                    await prepare_tenant_invoke(settings, resolved=resolved, auth=auth, thread_id=thread)
                    # /chat screened these before enqueuing; the compiled agent screens
                    # again on resume, under the manifest the run resumes with.
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
                state.pop("screener_retries", None)  # the budget is per step, not per fiber
            except Exception as exc:
                from felix.governance.inbound import InboundScreeningError

                retries = int(state.get("screener_retries") or 0)
                if (
                    isinstance(exc, InboundScreeningError)
                    and exc.status_code == 503
                    and retries < FIBER_SCREENER_MAX_RETRIES
                ):
                    # The screener could not run, not the turn failing to clear it. Under
                    # `on_flag: block` that must not terminate every in-flight durable run
                    # for the length of a provider blip: sleep and try the same step again,
                    # a bounded number of times — a screener down for hours is a failure.
                    logger.warning("fiber_screening_unavailable id=%s: %s", row["id"], exc.detail)
                    state["screener_retries"] = retries + 1
                    row["state_json"] = state
                    _park(row, FIBER_SCREENER_RETRY_MS)
                    await _save_fiber(settings, row, hold_claim=hold_claim)
                    return row
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
        await _save_fiber(settings, row, hold_claim=hold_claim)
        return row

    # complete / unknown
    row["status"] = "completed"
    state["cursor"] = cursor + 1
    state["result"] = stash.get("last") or step.get("result")
    row["state_json"] = state
    row["wake_at"] = None
    await _save_fiber(settings, row, hold_claim=hold_claim)
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
        # Bumped the way the Postgres claim bumps them. `updated_at` is not bookkeeping here:
        # the claim orders by it, so a re-claimed fiber goes to the back of the queue. Leaving
        # it alone kept the twin re-picking the same fiber ahead of everything else, which is
        # round-robin on the system of record and starvation on the twin.
        row["updated_at"] = ts
        row["version"] = int(row.get("version") or 0) + 1
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
                    # Temporal drives its own workflows, so those rows are not ours to claim.
                    # Filtered in SQL rather than after the fetch: `LIMIT` applies to the rows
                    # the WHERE returns, so dropping them in Python meant a tenant holding a
                    # batch's worth of Temporal fibers filled the batch with rows that were
                    # then discarded and claimed nothing at all -- its ordinary fibers never
                    # ran. The twin skips them while scanning, so it never had that problem,
                    # and starvation on the system of record is the harder one to notice.
                    Fiber.state_json["backend"].astext.is_distinct_from("temporal"),
                )
                .order_by(Fiber.updated_at)
                .limit(FIBER_BATCH)
                .with_for_update(skip_locked=True)
            )
            rows = (await db.scalars(stmt)).all()
            claimed: list[dict[str, Any]] = []
            for row in rows:
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
    """Claim and advance due fibers. Returns how many were stepped.

    "Steps run" until now, which was the same number while a claim ran one op. It is not any
    more, and the callers were always counting fibers: `tasks.py` logs it as the sweep's size.

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
        # A step that completes writes 0 with its own save; a step that raises is charged
        # against the count it was claimed with.
        prior_failures = int(row.get("attempts") or 0)
        row["attempts"] = 0
        try:
            landed, failure = await _step_with_lease(settings, row)
        except Exception as exc:  # the lease bookkeeping itself, not a step
            landed, failure = 0, exc
        if failure is not None:
            # `attempts` counts *consecutive* failures, and a claim now runs several steps.
            # If any of them landed before this one failed, the streak was broken inside this
            # sweep and the charge is 1 — the same arithmetic as before, when a landed step
            # and a failed step could never share a claim. Charging `prior_failures + 1`
            # regardless would bury a fiber that had just made progress.
            attempt = 1 if landed else prior_failures + 1
            logger.warning("fiber step failed id=%s attempt=%d", row.get("id"), attempt, exc_info=failure)
            await _retry_or_dead(settings, row, attempt, failure)
        ran += 1
    return ran


def _park(row: dict[str, Any], delay_ms: int) -> None:
    """Put a fiber to sleep for `delay_ms`; the caller saves."""
    row["status"] = "sleeping"
    row["wake_at"] = now_ms() + max(int(delay_ms), 0)


def retry_delay_ms(attempts: int) -> int:
    """Delay before the next try after `attempts` consecutive failures: 1m, 2m, 4m … 1h."""
    return min(FIBER_RETRY_BASE_MS * 2 ** max(attempts - 1, 0), FIBER_RETRY_MAX_MS)


async def _retry_or_dead(settings: Settings, row: dict[str, Any], attempts: int, exc: Exception) -> None:
    """Park a failed fiber for a backoff, or bury it once it has failed enough times.

    A step that reached a terminal status and then failed only at its save is finished:
    that status is kept rather than coerced to `sleeping`, which would re-enter the step
    loop with `cursor` past the end and report the run `completed` over a failed invoke.
    """
    if row.get("status") in FIBER_TERMINAL_STATUSES:
        row["attempts"] = 0
    elif attempts >= settings.fiber_max_attempts:
        row["attempts"] = attempts
        state = dict(row.get("state_json") or {})
        stash = dict(state.get("stash") or {})
        last = dict(stash.get("last") or {})
        # The first line only: a driver error's later lines echo the statement and its
        # parameters, which is the run's own state — not something to hand back on a poll.
        detail = (str(exc).splitlines() or [""])[0][:200]
        last["error"] = f"step failed {attempts} times; last: {type(exc).__name__}: {detail}"
        stash["last"] = last
        state["stash"] = stash
        row["state_json"] = state
        row["status"] = "dead"
        row["wake_at"] = None
    else:
        row["attempts"] = attempts
        _park(row, retry_delay_ms(attempts))
    try:
        await _save_fiber(settings, row)
        return
    except Exception:
        # The save is the thing failing — which is the failure this exists to bound, so
        # the count cannot go through the same write. Record it on the columns alone.
        logger.warning(
            "fiber save failed id=%s; recording the attempt without state", row.get("id"), exc_info=True
        )
    try:
        await _record_attempt(settings, row)
        return
    except Exception:
        # The store itself is down. Nothing can be recorded; drop the claim so the next
        # tick can try again — the count is lost, which errs toward retrying, not burying.
        logger.warning("fiber retry bookkeeping failed id=%s; releasing", row.get("id"), exc_info=True)
    try:
        await _release_fiber(settings, row)
    except Exception:
        # Also the store. The lease lapses on its own; the rest of the batch still runs.
        logger.warning("fiber release failed id=%s; lease will lapse", row.get("id"), exc_info=True)


async def _record_attempt(settings: Settings, row: dict[str, Any]) -> None:
    """Write status, wake_at and attempts — and nothing else — releasing the claim.

    `_save_fiber` also writes `state_json`, through `redact_json`; when that is what raised,
    this is the write that still lands. The run view derives the error from `attempts`
    when `dead` carries none.

    Scoped to this worker's claim, like `_release_fiber` and `_renew_lease`. It is the last
    write in the module that was not, and it clears the lease *and* overwrites `status` --
    so on a worker that has lost its claim it would take the row out from under whoever now
    owns it. No compare-and-set, deliberately: this path exists because the versioned write
    is what failed.
    """
    owner = str(getattr(settings, "replica_id", "local") or "local")
    row["updated_at"] = now_ms()
    row["lease_owner"] = ""
    row["lease_until"] = None
    fields = {
        k: row.get(k) for k in ("status", "wake_at", "attempts", "updated_at", "lease_owner", "lease_until")
    }
    if _use_memory(settings):
        stored = _memory_fibers.get((row["tenant_id"], row["id"]))
        if stored is not None and stored.get("lease_owner") in (owner, ""):
            stored.update(fields)
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
                    Fiber.lease_owner.in_((owner, "")),
                )
                .values(**fields)
            )
            await db.commit()


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


def _pending_op(row: dict[str, Any]) -> str:
    """The op the next step will run, or "" when the fiber is out of steps."""
    state = row.get("state_json") or {}
    steps = list(state.get("steps") or [])
    cursor = int(state.get("cursor") or 0)
    if cursor >= len(steps):
        return ""
    return str((steps[cursor] or {}).get("op") or "complete")


async def _step_with_lease(settings: Settings, row: dict[str, Any]) -> tuple[int, Exception | None]:
    """Run this fiber to its next suspension, renewing the claim while it works.

    Returns how many steps landed and the failure that stopped the loop, if any. It reports
    the failure rather than raising it because the count is only useful *with* it:
    `attempts` counts consecutive failures, and a claim can now land a step and then fail
    one, which a raise would lose on its way out.

    One step per claim was a whole scheduler tick per op, and the last of them did no work
    at all: `_run_fiber_step` flips a fiber to `completed` on the tick *after* the one that
    ran its final step, when it notices `cursor >= len(steps)`. A durable chat's `steps` has
    length one, so the cheapest possible run took two `* * * * *` ticks — around two minutes,
    the second of which was pure latency. A *failure* terminates inside one sweep, so a failed
    run reached its terminal state a full minute before a successful one.

    So the loop runs until the fiber suspends. "Suspends" is exactly `status != "running"`:
    terminal, `sleeping` after a `sleep` op, or parked by the screener retry. Two bounds keep
    a long `steps` list from holding the worker off the rest of its batch:

    * **At most one `invoke` per claim.** That is the only op that can take seconds, so this
      keeps wall-clock per fiber per sweep exactly what it was before — one model turn — and
      removes only the ticks that were doing bookkeeping. A two-invoke fiber still takes two
      sweeps; it no longer takes three.
    * `FIBER_MAX_OPS_PER_CLAIM`, plus a cursor-advance check, as a backstop.

    The claim is held across the whole loop (`hold_claim=True`) and released once at the end.
    It cannot be re-taken in between: `_save_fiber` clears the lease by default, and
    `_renew_lease` only renews a lease this worker still owns — so releasing per step and
    re-acquiring would leave a window where a second worker claims the fiber and runs the
    next `invoke` concurrently, which is a duplicated side effect rather than a lost write.
    """

    async def _heartbeat() -> None:
        while True:
            await asyncio.sleep(FIBER_LEASE_RENEW_MS / 1000)
            try:
                await _renew_lease(settings, row)
            except Exception:  # pragma: no cover - a failed renewal just lets the lease lapse
                logger.warning("fiber lease renewal failed id=%s", row.get("id"), exc_info=True)
                return

    beat = asyncio.create_task(_heartbeat())
    landed = 0
    failure: Exception | None = None
    try:
        invokes = 0
        for _ in range(FIBER_MAX_OPS_PER_CLAIM):
            if _pending_op(row) == "invoke":
                if invokes:
                    break  # one model turn per fiber per sweep, unchanged from before
                invokes += 1
            # An empty pending op is the completion flip, and running it here is the whole
            # point: it is the step that used to cost a minute of pure latency.
            before = int((row.get("state_json") or {}).get("cursor") or 0)
            before_version = int(row.get("version") or 0)
            try:
                await _run_fiber_step(settings, row, hold_claim=True)
            except Exception as exc:
                failure = exc
                break
            landed += 1
            if int(row.get("version") or 0) == before_version:
                # The save lost its compare-and-set, which `_save_fiber` reports by logging
                # and returning — it bumps `row["version"]` only when the write actually
                # landed, and every branch of `_run_fiber_step` saves exactly once, so an
                # unchanged version here means this fiber's row now belongs to someone else.
                #
                # Stopping matters more than it used to. `row` was mutated in place before
                # the save, so `status` and `cursor` still look like progress and the checks
                # below would wave the loop on -- for up to `FIBER_MAX_OPS_PER_CLAIM` more
                # ops, including its one `invoke`, whose tool side effects would happen and
                # never be persisted. One step per claim made this self-limiting; a loop
                # does not. The next sweep re-reads the row and starts from the truth.
                logger.warning("fiber write was discarded id=%s; yielding the claim", row.get("id"))
                break
            if row.get("status") != "running":
                break  # terminal, or suspended on a sleep — either way this claim is done
            if int((row.get("state_json") or {}).get("cursor") or 0) <= before:
                # No op in the tree does this today. If one ever leaves the fiber runnable
                # without consuming a step, the next tick retries it rather than this loop
                # spinning on it inside a claim nobody else can take.
                logger.warning("fiber step did not advance id=%s; yielding the claim", row.get("id"))
                break
    finally:
        beat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beat
        # Every save above kept the claim, so exactly one release closes it -- but *only*
        # when the claim ended cleanly.
        #
        # Releasing after a failure too was a race I introduced. `_retry_or_dead` parks the
        # fiber in a transaction of its own, so between this release and that park the row
        # is `status="running"` with a null `lease_until`, which is precisely what
        # `_claim_due_postgres` selects: a concurrent sweep claims it and re-runs the step
        # that just failed -- for a durable chat, the `invoke`, side effects and all. Before
        # the loop existed the failure path never reached a release at all; `_retry_or_dead`
        # wrote status, `wake_at` and the lease clear in one `UPDATE`, atomically, and it
        # still does. So the failure path keeps its claim until that write releases it.
        if failure is None:
            with contextlib.suppress(Exception):
                await _release_fiber(settings, row)
    return landed, failure


async def _release_fiber(settings: Settings, row: dict[str, Any]) -> None:
    """Drop the claim so a failed step is retried rather than stranded until expiry.

    Scoped to this worker's own claim, matching `_renew_lease`. It used to clear the lease
    columns unconditionally, which was harmless while the only caller was the failure path of
    a claim it certainly held; it is not harmless now that a claim spans several steps.

    **This is a second line of defence, not the first, and on a default deployment it decides
    nothing** -- `Settings.replica_id` is `"local"` and neither the Helm chart nor any Compose
    overlay sets `FELIX_REPLICA_ID`, so every worker claims under the same name and this
    predicate matches every claim including other workers'. The same is true of
    `_renew_lease`'s guard. What actually keeps two workers off one fiber is the claim itself
    (`lease_until` plus `FOR UPDATE SKIP LOCKED`), and, inside a multi-step claim, the
    compare-and-set check in `_step_with_lease` -- which is why that check reads the version
    rather than the owner. Fixing the identity is tracked in `docs/ROADMAP.md`.
    """
    owner = str(getattr(settings, "replica_id", "local") or "local")
    if _use_memory(settings):
        stored = _memory_fibers.get((row["tenant_id"], row["id"]))
        if stored is not None and stored.get("lease_owner") == owner:
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
                .where(
                    Fiber.tenant_id == row["tenant_id"],
                    Fiber.id == row["id"],
                    Fiber.lease_owner == owner,
                )
                .values(lease_owner="", lease_until=None)
            )
            await db.commit()


async def get_fiber(settings: Settings, tenant_id: str, fiber_id: str) -> dict[str, Any] | None:
    if _use_memory(settings):
        row = _memory_fibers.get((tenant_id, fiber_id))
        return _fiber_dict(row) if row else None
    # Same reasoning as `create_fiber`. Unbound under an enforcing policy this returns None
    # for a fiber that exists, so a resume token reads as an unknown run rather than an error.
    from felix.db.session import tenant_session

    async with tenant_session(settings, tenant_id) as db:
        fiber = await db.get(Fiber, (tenant_id, fiber_id))
        return _fiber_dict(fiber) if fiber else None


advance_fiber = _run_fiber_step
save_fiber = _save_fiber


__all__ = [
    "advance_fiber",
    "create_fiber",
    "fiber_thread_id",
    "get_fiber",
    "now_ms",
    "resume_due_fibers",
    "save_fiber",
]
