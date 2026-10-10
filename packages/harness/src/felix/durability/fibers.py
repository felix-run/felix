"""Durable fiber scheduler — sleep / step / stash / complete."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from typing import Any

from sqlalchemy import select, text

from felix.config import Settings, process_identity
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
# again. Every consumer that decides "is this run over" — the resume stream and the SDK
# poller — is checked against this set in `tests/unit/test_invariants.py`.
FIBER_TERMINAL_STATUSES = frozenset({"completed", "failed", "expired", "dead"})
# A backstop on how many ops one claim may run, for a `steps` list long enough that running
# it whole would hold the worker off the other 49 fibers in the batch. It is deliberately far
# above any shape the harness itself builds — a durable chat has one step — because the real
# bound on wall-clock is the one below it: at most one `invoke` per claim.
FIBER_MAX_OPS_PER_CLAIM = 64


def _claim_owner(settings: Any) -> str:
    """Who this process is, for the predicates that ask whose claim a fiber's is.

    `Settings.replica_id` is process-unique and refused when empty, so the fallback is only
    for a settings-like object with no such field. It must not be a shared constant: the
    fallback here *was* the literal "local", which is the very name two workers used to claim
    under -- a predicate that cannot tell two callers apart is not a predicate.
    """
    return str(getattr(settings, "replica_id", "") or "") or process_identity()


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


class RunInProgress(Exception):
    """A thread already has a durable run in flight, so another may not start on it.

    Two runs on one thread both append to its log, and neither sees the other's work until a
    whole tool batch lands (`patterns/react.py`), so each re-does what the other is doing:
    the same writes twice, turns landing between another run's tool calls, a pending approval
    per copy (felix-run/felix#529). `resume_token` is the run already there, for the caller
    to watch instead.
    """

    def __init__(self, resume_token: str) -> None:
        super().__init__(f"run_in_progress:{resume_token}")
        self.resume_token = resume_token


def run_in_flight(row: dict[str, Any], now: int) -> bool:
    """Whether a fiber still holds its thread.

    Not merely "not terminal". A fiber past its `expires_at` that no worker holds is over in
    all but name -- the next claim marks it `expired` without running anything -- and on a
    deployment with no worker it would never be claimed at all, so counting it would lock the
    thread for good. One a worker *does* hold past its expiry is still running its step (expiry
    is checked only between steps), so it counts until its lease lapses.
    """
    if row.get("status") in FIBER_TERMINAL_STATUSES:
        return False
    raw = (row.get("state_json") or {}).get("expires_at")
    try:
        expires_at = int(raw) if raw is not None else None
    except TypeError, ValueError:
        expires_at = None
    # No readable deadline: the run is bounded by nothing we can see, so it holds the thread.
    if expires_at is None:
        return True
    if now < expires_at:
        return True
    lease_until = row.get("lease_until")
    return lease_until is not None and now < int(lease_until)


def _fiber_dict(row: Fiber | dict[str, Any]) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    return {
        "tenant_id": row.tenant_id,
        "id": row.id,
        "kind": row.kind,
        "thread_id": row.thread_id,
        "status": row.status,
        "lease_owner": row.lease_owner,
        "lease_until": row.lease_until,
        "version": row.version,
        "attempts": int(row.attempts or 0),
        "state_json": row.state_json,
        "webhook_status": row.webhook_status,
        "webhook_due_at": row.webhook_due_at,
        "webhook_state": dict(row.webhook_state or {}),
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
    webhooks: list[str] | None = None,
    thread_id: str | None = None,
    exclusive_on_thread: bool = False,
) -> dict[str, Any]:
    """Create a fiber. `webhooks` are endpoint ids announced when it reaches a terminal status
    (see `felix.durability.webhooks`); already validated by the caller.

    `thread_id` is the thread the fiber writes to, recorded so a thread's run can be found
    (`active_fiber_for_thread`). With `exclusive_on_thread`, raises `RunInProgress` instead of
    creating a second fiber on a thread that already has one in flight -- checked and inserted
    under one per-thread lock, so two concurrent sends cannot both pass the check.
    """
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
        # never had. The Postgres sweeper always re-reads and so never saw it; a caller that
        # writes through the returned dict (the Temporal backend did) had every write discarded.
        "version": 0,
        "attempts": 0,
        "thread_id": thread_id,
        "webhook_status": "pending" if webhooks else None,
        "webhook_due_at": None,
        "webhook_state": {
            "endpoints": {name: {"status": "pending", "attempts": 0} for name in webhooks or []}
        },
    }
    exclusive = bool(thread_id) and exclusive_on_thread
    if _use_memory(settings):
        # No await between the check and the insert, so the event loop is the lock.
        if exclusive:
            active = _active_memory_fiber(tenant_id, str(thread_id), ts)
            if active is not None:
                raise RunInProgress(active["id"])
        _memory_fibers[(tenant_id, fiber_id)] = row
        return _fiber_dict(row)

    # Bound to this tenant rather than bypassing: the caller named the tenant, so the policy
    # can enforce it instead of being switched off. The HTTP path already supplies one --
    # `AuthMiddleware` wraps every request in `async_run_with_context`, which binds it -- but
    # the worker has no request context, and `fiber_scheduler` reaches here.
    from felix.db.session import tenant_session

    async with tenant_session(settings, tenant_id) as db:
        if exclusive:
            # Check-then-insert is a race under READ COMMITTED: two sends both see the thread
            # free. One lock per thread for the length of this transaction makes it a check.
            await db.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                {"key": f"fiber-thread:{tenant_id}:{thread_id}"},
            )
            active = await _active_postgres_fiber(db, tenant_id, str(thread_id), ts)
            if active is not None:
                raise RunInProgress(active["id"])
        db.add(Fiber(**row))
        await db.commit()
        return row


def _active_memory_fiber(tenant_id: str, thread_id: str, now: int) -> dict[str, Any] | None:
    rows = [
        row
        for (tenant, _), row in _memory_fibers.items()
        if tenant == tenant_id and row.get("thread_id") == thread_id and run_in_flight(row, now)
    ]
    # Newest first, then by id: the same tie-break the Postgres arm's ORDER BY uses.
    rows.sort(key=lambda r: (-int(r.get("created_at") or 0), str(r.get("id"))))
    return _fiber_dict(rows[0]) if rows else None


async def _active_postgres_fiber(db: Any, tenant_id: str, thread_id: str, now: int) -> dict[str, Any] | None:
    # Non-terminal rows on one thread: one in the normal case, a handful at worst, so the
    # time-dependent half of the predicate runs here rather than in SQL.
    result = await db.execute(
        select(Fiber)
        .where(
            Fiber.tenant_id == tenant_id,
            Fiber.thread_id == thread_id,
            Fiber.status.not_in(sorted(FIBER_TERMINAL_STATUSES)),
        )
        .order_by(Fiber.created_at.desc(), Fiber.id)
    )
    for fiber in result.scalars():
        row = _fiber_dict(fiber)
        if run_in_flight(row, now):
            return row
    return None


async def active_fiber_for_thread(
    settings: Settings, tenant_id: str, thread_id: str
) -> dict[str, Any] | None:
    """The fiber in flight on `thread_id`, newest first, or None. See `run_in_flight`."""
    ts = now_ms()
    if _use_memory(settings):
        return _active_memory_fiber(tenant_id, thread_id, ts)
    from felix.db.session import tenant_session

    async with tenant_session(settings, tenant_id) as db:
        return await _active_postgres_fiber(db, tenant_id, thread_id, ts)


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
        await _announce_fiber(row)
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
    await _announce_fiber(row)


async def _announce_fiber(row: dict[str, Any]) -> None:
    """Wake whatever is watching this fiber's thread: its status, and so its run's, just moved.

    Called after every write of a fiber's status: `_save_fiber`, `_record_attempt` (its fallback
    when the save itself failed) and the claim that sets `running` -- so a stream tailing a
    durable run hears it start, finish, fail or expire the moment it does, rather than on its
    next poll. The thread is the
    one the run writes to: its own, or the `fiber_thread_id` it minted. Best effort, like every
    notification: the poll underneath stays the safety net.
    """
    from felix.session.notify import notify_appended

    tenant_id = str(row.get("tenant_id") or "")
    thread = str(row.get("thread_id") or "") or fiber_thread_id(tenant_id, str(row.get("id") or ""))
    await notify_appended(tenant_id, thread)


async def _checkpoint_state(settings: Settings, row: dict[str, Any]) -> bool:
    """Persist `state_json` mid-step, under the version the claim read, without bumping it.

    For a note the step must leave *before* doing work it cannot take back — the invoke's
    resume marker. Leaving `version` alone keeps the step's closing `_save_fiber` the one
    write that advances the row, which is what `_step_with_lease` checks to tell a landed
    step from a discarded one. `False` means the row is no longer this claim's to write.
    """
    from felix.secrets import redact_json

    safe = redact_json(row.get("state_json") or {})
    state = safe if isinstance(safe, dict) else {}
    expected = int(row.get("version") or 0)
    if _use_memory(settings):
        stored = _memory_fibers.get((row["tenant_id"], row["id"]))
        if stored is None or int(stored.get("version") or 0) != expected:
            return False
        stored["state_json"] = state
        return True

    from sqlalchemy import update

    from felix.db.session import rls_bypass

    with rls_bypass():
        factory = get_session_factory(settings=settings)
        async with factory() as db:
            result = await db.execute(
                update(Fiber)
                .where(Fiber.tenant_id == row["tenant_id"], Fiber.id == row["id"], Fiber.version == expected)
                .values(state_json=state)
            )
            await db.commit()
            return bool(getattr(result, "rowcount", 0))


# Patterns that rebuild the turn from the thread's log, and so can be continued from it with
# no incoming messages. A composite reads the request from `input.messages` to route or to
# score, and resuming it with none would route to its first child or judge an empty request.
_RESUMABLE_PATTERNS = frozenset({"react", "deep"})


def _stored_skill_owner(stored_auth: object) -> str | None:
    """The personal skill library a fiber's starter had, as recorded at enqueue, or None.

    A row is read back from storage, so a value that is not a well-formed owner is dropped
    rather than trusted: the resume then loads the tenant's library only, which is also what a
    fiber enqueued before owners were recorded gets.
    """
    from felix.skills.library_keys import ORG_OWNER, InvalidSkillOwner, require_owner

    if not isinstance(stored_auth, dict):
        return None
    owner = stored_auth.get("skill_owner")
    if not isinstance(owner, str) or owner == ORG_OWNER:
        return None
    try:
        require_owner(owner)
    except InvalidSkillOwner:
        logger.warning("fiber recorded an unusable skill owner; resuming on the tenant's library")
        return None
    # The owner is `issuer|subject`, and the subject is recorded beside it: a pair that disagrees
    # was not written by the enqueue (a redacted subject, say), so it names no one's library.
    if owner.partition("|")[2] != stored_auth.get("principal_sub"):
        logger.warning("fiber's skill owner does not match its caller; resuming on the tenant's library")
        return None
    return owner


def _resumable(manifest: Any) -> bool:
    spec = getattr(manifest, "spec", None)
    if str(getattr(spec, "pattern", "react") or "react") not in _RESUMABLE_PATTERNS:
        return False
    # `semantic:N` ranks history by the incoming text; with none, the run's own request and
    # tool results can rank out of the prompt it resumes with.
    strategy = str(getattr(getattr(spec, "session", None), "strategy", "") or "")
    return not strategy.startswith("semantic")


async def _invoke_resume_point(
    settings: Settings,
    row: dict[str, Any],
    state: dict[str, Any],
    cursor: int,
    manifest: Any,
    thread: str,
    request: str,
) -> tuple[str, dict[str, Any] | None]:
    """Where this `invoke` starts: `fresh`, `resume`, `done` (with the final message), or `lost`.

    The thread's session log already journals the run as it goes — each model turn and each
    tool result is appended when it happens. So a step re-run after a crash does not need a
    journal of its own; it needs to know which part of the log is its own. Before the first
    attempt the step records the log's head as `invoke_began`, and a later attempt at the same
    cursor looks for *this request's* user turn among what was appended since:

    * not there — the crash came before the turn was logged (the loop writes a model change,
      a compaction, closed-out calls ahead of it): run it as new.
    * there, and a reply with no tool calls is last — the turn finished and only the fiber's
      save was lost: take that reply, and call nothing.
    * there, and anything else last — the run was mid-loop: continue from the log with no new
      user turn, so the model sees its own tool results rather than the request again, and a
      call in flight is closed by `_interrupted_tool_results` as before.

    Re-sending the turn — the old behaviour — remains the answer whenever the log cannot say:
    no log (`checkpointer: none`), a pattern or strategy that needs the request in hand, a
    request stored in a form it was not sent in (input redaction), or a log that could not be
    read. Re-sending made the model answer a duplicated request and could repeat tool calls
    whose side effects had already happened; it is the fallback, not the path.
    """
    if not _resumable(manifest):
        return "fresh", None
    try:
        from felix.session.store import build_checkpointer
        from felix.session.types import GetEventsOpts

        memory = getattr(getattr(manifest, "spec", None), "memory", None)
        checkpointer = str(getattr(memory, "checkpointer", "postgres") or "postgres")
        store = build_checkpointer(checkpointer, settings, tenant_id=row["tenant_id"])
        if store is None:
            return "fresh", None
        session = store.open(thread)
        # The claim loop passes the row it read at claim time and saves in place, so the marker
        # on it is current. (The Temporal backend retried with stale rows and re-read here.)
        marker = state.get("invoke_began")
        if isinstance(marker, dict) and marker.get("cursor") == cursor:
            events = await session.get_events(GetEventsOpts(from_seq=int(marker.get("seq") or 0)))
            mine = next(
                (i for i, e in enumerate(events) if e.role == "user" and (e.content or "") == request),
                None,
            )
            if mine is None:
                return "fresh", None
            since = events[mine + 1 :]
            if since and since[-1].role == "assistant" and not since[-1].tool_calls:
                return "done", {"role": "assistant", "content": since[-1].content or ""}
            return "resume", None
        state["invoke_began"] = {"cursor": cursor, "seq": int((await session.head()).get("seq") or 0)}
        row["state_json"] = state
    except Exception:
        # The journal is an optimisation over re-sending; failing to read it must not fail
        # the run, which would make a transient store error terminal.
        logger.warning("fiber resume point unavailable id=%s; re-sending", row.get("id"), exc_info=True)
        state.pop("invoke_began", None)
        return "fresh", None
    if not await _checkpoint_state(settings, row):
        return "lost", None
    return "fresh", None


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
                from felix.manifests.pin import assert_resume_pin
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
                    skill_owner=_stored_skill_owner(stored_auth),
                )
                thread = thread_id or fiber_thread_id(tenant_id, str(row["id"]))
                req_ctx = RequestContext(
                    settings=settings,
                    auth=auth,
                    manifest_id=manifest_id,
                    thread_id=thread,
                )
                # A background child's run carries the caps of every agent above it; the
                # parent may be long finished, and its limits still bound what it delegated.
                from felix.durability.runs import (
                    BACKGROUND_CHILD_EXTRA,
                    RUN_NOT_AFTER_EXTRA,
                    restore_ceilings,
                )

                req_ctx.limit_state.ceilings = restore_ceilings(state)
                if isinstance(state.get("expires_at"), int):
                    req_ctx.extras[RUN_NOT_AFTER_EXTRA] = state["expires_at"]
                if state.get("parent_thread_id"):
                    req_ctx.extras[BACKGROUND_CHILD_EXTRA] = True
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
                        await assert_resume_pin(
                            settings,
                            tenant_id,
                            pinned,
                            resolved.manifest,
                            version=resolved.version,
                            resolved_out=resolved.sub_agents,
                        )
                    await prepare_tenant_invoke(settings, resolved=resolved, auth=auth, thread_id=thread)
                    request = next((m.content or "" for m in reversed(messages) if m.role == "user"), "")
                    point, logged = await _invoke_resume_point(
                        settings, row, state, cursor, resolved.manifest, thread, request
                    )
                    if point == "lost":
                        # Another worker holds this row now; `_step_with_lease` sees the
                        # unchanged version and yields the claim without an invoke.
                        logger.warning("fiber claim lost before invoke id=%s", row["id"])
                        return row
                    if point != "fresh":
                        logger.info("fiber invoke %s from its session log id=%s", point, row["id"])
                    if logged is not None:
                        final = logged
                    else:
                        # /chat screened these before enqueuing; the compiled agent screens
                        # again on resume, under the manifest the run resumes with.
                        agent = await build_tenant_agent(
                            settings,
                            manifest=resolved.manifest,
                            sub_agents=resolved.sub_agents,
                            tools=provider,
                            tenant_id=tenant_id,
                            skill_owner=auth.skill_owner,
                        )
                        result = await agent.invoke(
                            InvokeInput(
                                messages=[] if point == "resume" else messages,
                                thread_id=thread,
                                model_id=str(model_id) if model_id else None,
                                tenant_id=tenant_id,
                            )
                        )
                        final = (
                            result.final.model_dump()
                            if result.final
                            else {"role": "assistant", "content": ""}
                        )
                answer = str(final.get("content") or "")
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
        state.pop("invoke_began", None)  # this cursor's; the next invoke records its own
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


async def _claim_due_memory(settings: Settings, ts: int, limit: int = FIBER_BATCH) -> list[dict[str, Any]]:
    claimed: list[dict[str, Any]] = []
    for row in _memory_fibers.values():
        lease_until = row.get("lease_until")
        if lease_until is not None and lease_until > ts:
            continue  # someone else holds the claim
        due_sleep = row["status"] == "sleeping" and row.get("wake_at") is not None and row["wake_at"] <= ts
        if not (due_sleep or row["status"] in {"running", "pending"}):
            continue
        row["status"] = "running"
        row["wake_at"] = None
        row["lease_owner"] = _claim_owner(settings)
        row["lease_until"] = ts + FIBER_LEASE_MS
        # Bumped the way the Postgres claim bumps them. `updated_at` is not bookkeeping here:
        # the claim orders by it, so a re-claimed fiber goes to the back of the queue. Leaving
        # it alone kept the twin re-picking the same fiber ahead of everything else, which is
        # round-robin on the system of record and starvation on the twin.
        row["updated_at"] = ts
        row["version"] = int(row.get("version") or 0) + 1
        claimed.append(dict(row))
        if len(claimed) >= limit:
            break
    return claimed


async def _claim_due_postgres(settings: Settings, ts: int, limit: int = FIBER_BATCH) -> list[dict[str, Any]]:
    """Claim a bounded batch of due fibers, skipping rows another worker holds.

    ``FOR UPDATE SKIP LOCKED`` plus a lease column is what stops the same step running
    twice: the row lock serializes concurrent claimers within the transaction, and the
    lease keeps the fiber claimed for the duration of the step, which outlives it.
    """
    from felix.db.session import rls_bypass

    owner = _claim_owner(settings)
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
                    # No `backend` filter. Rows an earlier version handed to Temporal carry
                    # `backend: temporal`; their state is all here, so this scheduler picks
                    # them up rather than leaving them stranded with no worker to drive them.
                )
                .order_by(Fiber.updated_at)
                .limit(limit)
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


async def _claim_due(settings: Settings, limit: int = FIBER_BATCH) -> list[dict[str, Any]]:
    """Claim up to `limit` due fibers from whichever store this process uses."""
    ts = now_ms()
    if _use_memory(settings):
        claimed = await _claim_due_memory(settings, ts, limit)
    else:
        claimed = await _claim_due_postgres(settings, ts, limit)
    # After the claim commits: a stream reports `running` when the run starts, not when it next
    # polls, which on a backed-up queue could be most of a minute later.
    for row in claimed:
        await _announce_fiber(row)
    return claimed


async def _advance_claimed(settings: Settings, row: dict[str, Any]) -> None:
    """Run one claimed fiber to its next suspension, and charge a failure against it.

    Never raises. Both callers run this as one of several concurrent tasks, and an exception
    escaping one would either be lost with its task or cancel its siblings mid-step.
    """
    # A step that completes writes 0 with its own save; a step that raises is charged
    # against the count it was claimed with.
    prior_failures = int(row.get("attempts") or 0)
    row["attempts"] = 0
    try:
        landed, failure = await _step_with_lease(settings, row)
    except Exception as exc:  # the lease bookkeeping itself, not a step
        landed, failure = 0, exc
    if failure is None:
        return
    if isinstance(failure, FiberLeaseLost):
        # Someone else's fiber now: charging, parking or releasing it would write over the
        # claim of the worker that holds it.
        logger.warning("%s; leaving it to its new owner", failure)
        return
    # `attempts` counts *consecutive* failures, and a claim now runs several steps. If any of
    # them landed before this one failed, the streak was broken inside this claim and the
    # charge is 1 — the same arithmetic as before, when a landed step and a failed step could
    # never share a claim. Charging `prior_failures + 1` regardless would bury a fiber that had
    # just made progress.
    attempt = 1 if landed else prior_failures + 1
    logger.warning("fiber step failed id=%s attempt=%d", row.get("id"), attempt, exc_info=failure)
    try:
        await _retry_or_dead(settings, row, attempt, failure)
    except Exception:  # the store is down; the claim lapses and a later sweep retries it
        logger.warning("fiber retry bookkeeping failed id=%s", row.get("id"), exc_info=True)


async def resume_due_fibers(settings: Settings) -> int:
    """Claim and advance due fibers. Returns how many were stepped.

    "Steps run" until now, which was the same number while a claim ran one op. It is not any
    more, and the callers were always counting fibers: `tasks.py` logs it as the sweep's size.

    Each fiber is claimed before it is stepped, so a step still running when the next
    scheduler tick fires is not picked up again.

    The claimed fibers advance **concurrently**, at most `fiber_concurrency` at a time. They
    ran one after another, so a fiber parked on an approval held every fiber claimed after it
    for as long as the person took to answer: measured on the reference deployment, one
    `write_file` approval made a sweep take 89 seconds. Each fiber holds its own lease and
    renews it itself, so nothing they share needs the order.

    This is the backstop. `run_fiber_loop` is what picks a new run up within a second.
    """
    due = await _claim_due(settings)
    gate = asyncio.Semaphore(settings.fiber_concurrency)

    async def _bounded(row: dict[str, Any]) -> None:
        async with gate:
            await _advance_claimed(settings, row)

    await asyncio.gather(*(_bounded(row) for row in due))
    return len(due)


# How long a stopping worker waits for in-flight fibers before cancelling them. A cancelled
# fiber keeps its lease and is taken over when the lease lapses — the crashed-worker path.
FIBER_LOOP_DRAIN_S = 30.0
# The poll backs off to this while claiming keeps failing, so a store that is down is
# asked once a minute rather than once a second.
FIBER_LOOP_MAX_BACKOFF_S = 60.0


async def run_fiber_loop(settings: Settings, stop: asyncio.Event) -> None:
    """Pick up due fibers as they arrive, until `stop` is set.

    Submitting a durable run writes a `pending` fiber and returns; nothing tells the worker.
    The only thing that ran it was `fiber_scheduler`, a `* * * * *` cron, so every durable run
    waited for the next minute boundary before its first model call — 0 to 60 seconds, 30 on
    average, with the client reading `pending` the whole time. On the reference deployment
    two runs started at 01:08:53 and 01:14:53, the second the cron fired, both about 25s
    after they were submitted. Cron cannot tick faster than a minute.

    This polls every `fiber_poll_seconds` instead. The claim is cheap — `FOR UPDATE SKIP
    LOCKED` over an ordered, limited select — and it is the same claim the cron sweep takes,
    so the two cannot run one fiber twice; whichever claims first owns it.

    Unlike the sweep it does not wait for a batch to finish. Each claimed fiber runs as its
    own task, and the loop claims only as many as it has free slots for, out of
    `fiber_concurrency`, so one fiber waiting minutes on an approval occupies one slot and
    delays nothing else.
    """
    interval = settings.fiber_poll_seconds
    capacity = settings.fiber_concurrency
    in_flight: set[asyncio.Task[None]] = set()
    delay = interval
    logger.info("fiber_loop started interval=%ss concurrency=%d", interval, capacity)
    try:
        while not stop.is_set():
            free = capacity - len(in_flight)
            if free > 0:
                try:
                    due = await _claim_due(settings, min(free, FIBER_BATCH))
                    delay = interval
                except Exception:
                    due = []
                    delay = min(max(delay * 2, interval), FIBER_LOOP_MAX_BACKOFF_S)
                    logger.warning("fiber_loop claim failed; next attempt in %ss", delay, exc_info=True)
                for row in due:
                    task = asyncio.create_task(_advance_claimed(settings, row))
                    in_flight.add(task)
                    task.add_done_callback(in_flight.discard)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)
    finally:
        if in_flight:
            _, pending = await asyncio.wait(in_flight, timeout=FIBER_LOOP_DRAIN_S)
            for task in pending:
                task.cancel()
        logger.info("fiber_loop stopped")


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
    owner = _claim_owner(settings)
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
            await _announce_fiber(row)
        return
    from sqlalchemy import update

    from felix.db.session import rls_bypass

    with rls_bypass():
        factory = get_session_factory(settings=settings)
        async with factory() as db:
            result = await db.execute(
                update(Fiber)
                .where(
                    Fiber.tenant_id == row["tenant_id"],
                    Fiber.id == row["id"],
                    Fiber.lease_owner.in_((owner, "")),
                )
                .values(**fields)
            )
            await db.commit()
    # The fallback when `_save_fiber` itself failed, and it writes `status` -- `dead` among
    # them -- so it announces like the save it stands in for.
    if getattr(result, "rowcount", 0):
        await _announce_fiber(row)


async def _renew_lease(settings: Settings, row: dict[str, Any]) -> bool:
    """Push this worker's claim out by another lease window. False when it no longer holds it.

    The answer used to be thrown away: an update matching no row -- the claim lapsed and another
    worker took it -- looked exactly like a renewal, and the step went on running beside the
    new owner's (felix-run/felix#531).
    """
    until = now_ms() + FIBER_LEASE_MS
    owner = _claim_owner(settings)
    if _use_memory(settings):
        stored = _memory_fibers.get((row["tenant_id"], row["id"]))
        # Only if we still hold it: a lease we already lost must not be stolen back mid-step.
        if stored is not None and stored.get("lease_owner") == owner:
            stored["lease_until"] = until
            return True
        return False
    from sqlalchemy import update

    from felix.db.session import rls_bypass

    with rls_bypass():
        factory = get_session_factory(settings=settings)
        async with factory() as db:
            result = await db.execute(
                update(Fiber)
                .where(
                    Fiber.tenant_id == row["tenant_id"],
                    Fiber.id == row["id"],
                    Fiber.lease_owner == owner,
                )
                .values(lease_until=until)
            )
            await db.commit()
            return int(getattr(result, "rowcount", 0) or 0) > 0


class FiberLeaseLost(Exception):
    """This worker's claim on a fiber lapsed or was taken while a step was running.

    Not a step failure: the fiber belongs to whoever holds it now, so nothing here may charge an
    attempt, park it or release it. The step is cancelled, because running on beside the new
    owner's is the duplicate side effect the lease exists to prevent.
    """


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

    lost = asyncio.Event()

    async def _heartbeat() -> None:
        """Keep the claim, and say when it is gone.

        One failed renewal used to end this for good: the lease then lapsed under a step still
        running, another worker claimed the fiber and ran the same `invoke`, and both sets of
        side effects happened (felix-run/felix#531). A renewal that raises is retried while the
        lease last written still stands, giving up one interval *before* it lapses so that no
        other worker can have claimed it yet; one that finds the claim gone ends it at once.
        Either way the step is told (`lost`), and stops.
        """
        # The claim that put this fiber in our hands set `lease_until` a moment ago.
        held_until = now_ms() + FIBER_LEASE_MS
        while True:
            await asyncio.sleep(FIBER_LEASE_RENEW_MS / 1000)
            try:
                if await _renew_lease(settings, row):
                    held_until = now_ms() + FIBER_LEASE_MS
                    continue
                logger.warning("fiber lease was taken mid-step id=%s; stopping the step", row.get("id"))
            except Exception:
                if now_ms() + FIBER_LEASE_RENEW_MS < held_until:
                    logger.warning("fiber lease renewal failed id=%s; retrying", row.get("id"), exc_info=True)
                    continue
                logger.warning(
                    "fiber lease could not be renewed before it lapses id=%s; stopping the step",
                    row.get("id"),
                    exc_info=True,
                )
            lost.set()
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
            step = asyncio.create_task(_run_fiber_step(settings, row, hold_claim=True))
            gone = asyncio.create_task(lost.wait())
            try:
                await asyncio.wait({step, gone}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                gone.cancel()
            if not step.done():
                step.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await step
                failure = FiberLeaseLost(f"lease lost mid-step on fiber {row.get('id')}")
                break
            try:
                await step
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

    **This is a second line of defence, not the first.** What keeps two workers off one fiber
    is the claim itself (`lease_until` plus `FOR UPDATE SKIP LOCKED`), and inside a multi-step
    claim the compare-and-set check in `_step_with_lease` -- which is why that check reads the
    version and not the owner.

    It decided nothing at all until `replica_id` stopped defaulting to the constant "local":
    every worker claimed under one name, so this predicate matched every claim including other
    workers'. The identity is `{hostname}:{pid}` now and the Helm chart sets it from the pod
    name, so the guard discriminates -- but it is only ever as good as that setting, which is
    why it is not the thing being relied on.
    """
    owner = _claim_owner(settings)
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
    "RunInProgress",
    "active_fiber_for_thread",
    "advance_fiber",
    "create_fiber",
    "fiber_thread_id",
    "get_fiber",
    "now_ms",
    "resume_due_fibers",
    "run_fiber_loop",
    "run_in_flight",
    "save_fiber",
]
