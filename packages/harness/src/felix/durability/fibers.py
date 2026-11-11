"""Durable fiber scheduler — sleep / step / stash / complete."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import logging
import time
import uuid
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import select, text

from felix.config import Settings, process_identity
from felix.db.models import Fiber
from felix.db.session import _use_memory, get_session_factory

if TYPE_CHECKING:
    from felix.context import AuthContext

logger = logging.getLogger("felix.durability.fibers")

now_ms = lambda: int(time.time() * 1000)

_memory_fibers: dict[tuple[str, str], dict[str, Any]] = {}


def reset_memory_fibers() -> None:
    """Test helper — clear the in-memory fiber store.

    Every other `memory://` twin has one and `tests/conftest.py` calls it. This did not, so
    fibers leaked between tests: `_claim_due_memory` returns *every* due row, so a test that
    asserted on how many fibers came back passed alone and failed in the suite.

    The slots a worker's fibers hold are process state of the same kind: a test that leaves a
    fiber mid-step would otherwise leave the next one a worker with no room.
    """
    global _drain
    _memory_fibers.clear()
    _slots_held.clear()
    _advancing.clear()
    _drain = None
    resume_claiming()


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
# Where a fiber's `state_json` counts the claims that found it `running` under a lease nobody
# renewed -- its holder died mid-step -- and what its thread's log held at the last of them.
# `lease_lapses` is the streak with no progress between them, `lease_lapses_total` every one
# since a holder last let go cleanly. See `_count_lapsed_claim` and `_settle_lapsed_claim`.
FIBER_LAPSES_KEY = "lease_lapses"
FIBER_LAPSES_TOTAL_KEY = "lease_lapses_total"
FIBER_LAPSES_SEQ_KEY = "lease_lapses_seq"
# A fiber whose log moves between lost workers is not buried at `fiber_max_attempts` lapses,
# but it is at this many times that: a step that kills its worker from inside a tool call logs
# the call again on every attempt, which is movement and not progress.
FIBER_LAPSE_BACKSTOP = 4
# How long a claim cut short by a cancel may spend giving its lease back before it goes.
FIBER_CANCEL_RELEASE_S = 5.0
# How long the claim waits on a fiber's thread log before deciding without it. The claimed
# fibers' leases are running while it waits, and none of them has a heartbeat yet.
FIBER_LOG_HEAD_TIMEOUT_S = 5.0


def _claim_owner(settings: Any) -> str:
    """Who this process is, for the predicates that ask whose claim a fiber's is.

    `Settings.replica_id` is process-unique and refused when empty, so the fallback is only
    for a settings-like object with no such field. It must not be a shared constant: the
    fallback here *was* the literal "local", which is the very name two workers used to claim
    under -- a predicate that cannot tell two callers apart is not a predicate.
    """
    return str(getattr(settings, "replica_id", "") or "") or process_identity()


def _mint_claim(settings: Any) -> str:
    """What a claim writes to `lease_owner`: this process, and a name for the claim itself.

    The process alone does not name a claim. A worker claims one fiber many times -- its loop
    and the cron sweep, one tick and the next -- and a holder whose claim had lapsed matched
    the claim taken after it: its release cleared a lease someone was working under. Telling
    them apart by `lease_until` held until two claims wrote the same one, which a renewal and a
    claim in the same millisecond do. So each claim has a name nothing else will ever write.
    """
    return f"{_claim_owner(settings)}#{uuid.uuid4().hex}"


def _claim_of(settings: Any, row: dict[str, Any]) -> str:
    """The claim `row` was handed, or "" when it carries none of this process's."""
    claim = str(row.get("lease_owner") or "")
    return claim if claim.startswith(f"{_claim_owner(settings)}#") else ""


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


class TooManyChildren(Exception):
    """A parent thread already has `limit` background children in flight (`create_fiber`)."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"too_many_background_children:{limit}")
        self.limit = limit


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
    max_children: int | None = None,
    fiber_id: str | None = None,
) -> dict[str, Any]:
    """Create a fiber. `webhooks` are endpoint ids announced when it reaches a terminal status
    (see `felix.durability.webhooks`); already validated by the caller.

    `thread_id` is the thread the fiber writes to, recorded so a thread's run can be found
    (`active_fiber_for_thread`). With `exclusive_on_thread`, raises `RunInProgress` instead of
    creating a second fiber on a thread that already has one in flight -- checked and inserted
    under one per-thread lock, so two concurrent sends cannot both pass the check.

    `max_children` does the same for a background child (`state["parent_thread_id"]`): raises
    `TooManyChildren` when that parent thread already has that many in flight. Each child has a
    thread of its own, so the one-run-per-thread rule alone never bounded how many a parent
    could start.

    `fiber_id` lets a caller that may run twice name the fiber it means, so its second attempt
    finds the first one's fiber (`get_fiber`) instead of starting another: a workflow step whose
    run was saved after its child started, and lost that save, replays the start.
    """
    from felix.secrets import redact_json

    fiber_id = fiber_id or uuid.uuid4().hex
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
    parent = str(row["state_json"].get("parent_thread_id") or "") if max_children is not None else ""
    if _use_memory(settings):
        # No await between the check and the insert, so the event loop is the lock.
        if exclusive:
            active = _active_memory_fiber(tenant_id, str(thread_id), ts)
            if active is not None:
                raise RunInProgress(active["id"])
        if parent and max_children is not None:
            children = [
                r
                for (tenant, _), r in _memory_fibers.items()
                if tenant == tenant_id
                and (r.get("state_json") or {}).get("parent_thread_id") == parent
                and run_in_flight(r, ts)
            ]
            if len(children) >= max_children:
                raise TooManyChildren(max_children)
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
        if parent and max_children is not None:
            # The same race as the thread check, so the same remedy: one lock per parent thread.
            await db.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                {"key": f"fiber-children:{tenant_id}:{parent}"},
            )
            result = await db.execute(
                select(Fiber).where(
                    Fiber.tenant_id == tenant_id,
                    Fiber.status.not_in(sorted(FIBER_TERMINAL_STATUSES)),
                    Fiber.state_json["parent_thread_id"].astext == parent,
                )
            )
            if sum(1 for f in result.scalars() if run_in_flight(_fiber_dict(f), ts)) >= max_children:
                raise TooManyChildren(max_children)
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
    # The claim covers the duration of one step, not the life of the fiber. Every
    # _save_fiber call ends a step transition, so it releases the claim: a still-runnable
    # fiber must be claimable again on the next tick, and a sleeping one when it wakes.
    #
    # `hold_claim` is for the one caller that runs several steps under a single claim
    # (`_step_with_lease`) and releases once at the end. It cannot be the default: a
    # released claim is what lets the *next* tick pick the fiber up, and a save that
    # kept it would strand a runnable fiber for the whole `FIBER_LEASE_MS` window.
    #
    # `row` gives its lease up only once the write has landed. Cleared first, a save that
    # raised left the holder with no claim to name, and the fallbacks that run next
    # (`_record_attempt`, `_release_fiber`) are scoped to exactly that.

    if _use_memory(settings):
        stored = _memory_fibers.get((row["tenant_id"], row["id"]))
        if stored is not None and int(stored.get("version") or 0) != int(row.get("version") or 0):
            logger.warning("fiber version conflict id=%s; discarding stale write", row.get("id"))
            return
        if not hold_claim:
            row["lease_owner"] = ""
            row["lease_until"] = None
        elif stored is not None:
            # The lease is the heartbeat's to write, as on Postgres below.
            row["lease_owner"] = stored.get("lease_owner", "")
            row["lease_until"] = stored.get("lease_until")
        row["version"] = int(row.get("version") or 0) + 1
        # A copy, as a database row is one. Stored by reference, the holder's dict *was* the
        # row: a later claim wrote its version and lease straight into a stale holder's hands,
        # and every predicate that asks "is this still my claim" answered yes. Deep, for the
        # same reason one level down: a step edits its `state_json` in place before it saves,
        # and a save the store discards must leave the stored state as it was.
        _memory_fibers[(row["tenant_id"], row["id"])] = _stored_copy(row)
        await _announce_fiber(row)
        return

    from sqlalchemy import update

    from felix.db.session import rls_bypass

    expected = int(row.get("version") or 0)
    values: dict[str, Any] = {
        "status": row["status"],
        "state_json": row.get("state_json") or {},
        "wake_at": row.get("wake_at"),
        "updated_at": row["updated_at"],
        "version": expected + 1,
        "attempts": int(row.get("attempts") or 0),
    }
    if not hold_claim:
        # A save that keeps the claim leaves the lease columns alone: they are the heartbeat's
        # to write. This wrote `row["lease_until"]` back -- the claim-time value, which no
        # renewal had touched -- so the closing save of a long claim could leave a `running`
        # row whose lease had already lapsed, free for another worker mid-claim.
        values.update(lease_owner="", lease_until=None)
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
                .values(**values)
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
    if not hold_claim:
        row["lease_owner"] = ""
        row["lease_until"] = None
    await _announce_fiber(row)


def _stored_copy(row: dict[str, Any]) -> dict[str, Any]:
    """What the twin keeps of a holder's row: its columns, sharing nothing with the holder."""
    stored = dict(row)
    stored["state_json"] = copy.deepcopy(row.get("state_json") or {})
    stored["webhook_state"] = copy.deepcopy(row.get("webhook_state") or {})
    return stored


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
        # A copy: `redact_json` hands back its input when nothing is registered to mask, and
        # the step goes on editing that dict -- which then was the stored row's too.
        stored["state_json"] = copy.deepcopy(state)
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

    `lost` is asked on every path, by one write under the version this claim read. It was asked
    only where a marker was being recorded, so a pattern that cannot resume, a manifest with no
    log and a retry that found its marker all went on to invoke without knowing whether the row
    was still theirs -- and a holder whose claim had lapsed ran the whole turn beside the worker
    that now held it.
    """
    point, logged = await _resume_point_from_log(settings, row, state, cursor, manifest, thread, request)
    try:
        held = await _checkpoint_state(settings, row)
    except Exception as exc:
        # The store could not say. That is not the turn failing, and it must not end the run
        # `failed` with the driver's text as its error: the step is retried (`_retry_or_dead`).
        raise FiberClaimUnconfirmed(f"could not confirm the claim on fiber {row.get('id')}") from exc
    if not held:
        return "lost", None
    return point, logged


async def _resume_point_from_log(
    settings: Settings,
    row: dict[str, Any],
    state: dict[str, Any],
    cursor: int,
    manifest: Any,
    thread: str,
    request: str,
) -> tuple[str, dict[str, Any] | None]:
    """What the thread's log says about this `invoke`: `fresh`, `resume`, or `done` and the reply.

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


async def stop_fiber(settings: Settings, tenant_id: str, fiber_id: str, reason: str) -> bool:
    """End a fiber that has not finished: `failed`, with `reason` as its error. False if none.

    For a run that no longer wants a child — a cancelled or failed workflow. A fiber not yet
    claimed never runs; one a worker holds keeps its invoke, but the worker's save then loses the
    compare-and-set and the stopped status stands. Pair it with `steer.request_abort` to cut a
    turn already under way short.
    """
    for _ in range(3):
        row = await get_fiber(settings, tenant_id, fiber_id)
        if row is None or row.get("status") in FIBER_TERMINAL_STATUSES:
            return row is not None
        read_at = int(row.get("version") or 0)
        state = dict(row.get("state_json") or {})
        state["stash"] = {
            **(state.get("stash") or {}),
            "last": {"answer": "", "final": "", "error": reason, "manifest_id": ""},
        }
        row.update(status="failed", state_json=state, wake_at=None)
        await _save_fiber(settings, row)
        if int(row.get("version") or 0) == read_at + 1:
            return True
    return False


def auth_from_state(tenant_id: str, stored_auth: object) -> AuthContext:
    """The `AuthContext` a run resumes under, from the caller it recorded at enqueue.

    Shared by the invoke op and the workflow engine, which starts each agent step's child run as
    the person who started the workflow. The reading itself is `felix.auth.recorded.replay_caller`,
    the one every kind of later work uses: actor `fiber`, `acting_for` the person who started it,
    their scopes, scheme and manifest pin. The actor is the fiber, not the person -- every other
    machine actor does the same (`cron`, `eval`, `a2a`) -- so an audit row never claims alice did
    what a worker did against a manifest that may have changed underneath her. A fiber with no
    recorded caller gets principal "fiber" and no scopes: everything policied denies, the
    fail-closed direction.
    """
    from felix.auth.actors import FIBER
    from felix.auth.recorded import replay_caller

    return replay_caller(
        stored_auth if isinstance(stored_auth, dict) else None,
        tenant_id=tenant_id,
        actor=FIBER,
        skill_owner=_stored_skill_owner(stored_auth),
    )


async def _run_fiber_step(
    settings: Settings, row: dict[str, Any], *, hold_claim: bool = False
) -> dict[str, Any]:
    """Advance one fiber step.

    ``state_json`` schema:
      {
        "steps": [{"op": "sleep"|"invoke"|"stash"|"tool"|"workflow"|"complete", ...}, ...],
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
                from felix.context import RequestContext, async_run_with_context
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
                auth = auth_from_state(tenant_id, stored_auth)
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
                # And the hosts every agent above it may reach (`spec.network.allow_hosts`).
                # Put on the context the compile below reads once, at its root (`run_bound`).
                from felix.security.host_allowlist import EGRESS_CEILINGS_EXTRA, EgressBound

                egress = EgressBound.from_state(state.get("egress_ceilings"))
                if egress.restricted:
                    req_ctx.extras[EGRESS_CEILINGS_EXTRA] = egress
                if isinstance(state.get("expires_at"), int):
                    req_ctx.extras[RUN_NOT_AFTER_EXTRA] = state["expires_at"]
                if state.get("parent_thread_id"):
                    req_ctx.extras[BACKGROUND_CHILD_EXTRA] = True
                if isinstance(state.get("trigger"), dict):
                    from felix.triggers import TRIGGER_EVENT_EXTRA

                    # The event this run was started for, which its `trigger_event` tool reads.
                    req_ctx.extras[TRIGGER_EVENT_EXTRA] = state["trigger"]
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
                    step_schema = step.get("output_schema")
                    if isinstance(step_schema, dict):
                        # A workflow step's own output contract, applied to this turn only and
                        # after the pin check, which judged the manifest as stored.
                        spec = resolved.manifest.spec.model_copy(update={"output_schema": step_schema})
                        resolved.manifest = resolved.manifest.model_copy(update={"spec": spec})
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
                            person=auth.person,
                        )
                        if model_id:
                            # Checked when the run was enqueued; again here, since a run can
                            # outlive the route table or the rules it was checked against.
                            from felix.manifests.governance import GovernanceError, request_model_tree_refusal

                            refusal = await request_model_tree_refusal(
                                resolved.manifest,
                                str(model_id),
                                settings,
                                tenant_id=tenant_id,
                                children=resolved.sub_agents,
                            )
                            if refusal is not None:
                                raise GovernanceError(f"{refusal}:{model_id}")
                        result = await agent.invoke(
                            InvokeInput(
                                messages=[] if point == "resume" else messages,
                                thread_id=thread,
                                model_id=str(model_id) if model_id else None,
                                tenant_id=tenant_id,
                                # The person's own turn, sent to /chat and run here durably --
                                # never a background child's prompt, which a model wrote.
                                person_turn=point != "resume" and step.get("person_turn") is True,
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

                if isinstance(exc, FiberClaimUnconfirmed):
                    raise  # nothing ran; the step's failure path retries it
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
                # Written, never forwarded: this text is returned on the run's poll and its
                # stream, posted to webhooks and channels, and handed to a parent agent. A
                # driver error names a host, a path or a statement; a refusal written for a
                # caller (`felix.errors`) still reads as itself.
                from felix.errors import client_safe_message

                error = client_safe_message(exc)
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

    if op == "tool":
        # One workflow `tool_call` step, run as the run's starter (`felix.workflows.tool_step`).
        from felix.workflows.tool_step import TOOL_BEGAN, run_tool_step

        result = await run_tool_step(settings, row, step, lambda: _checkpoint_state(settings, row))
        if result.get("lost"):
            logger.warning("fiber claim lost before tool call id=%s", row["id"])
            return row
        error = str(result.get("error") or "")
        stash["last"] = {
            "answer": "",
            "final": "",
            "output": None if error else result.get("output"),
            "error": error,
            "manifest_id": str(step.get("manifest_id") or ""),
        }
        state["stash"] = stash
        state.pop(TOOL_BEGAN, None)
        state["cursor"] = cursor + 1
        row["state_json"] = state
        row["status"] = "failed" if error else "running"
        row["wake_at"] = None
        await _save_fiber(settings, row, hold_claim=hold_claim)
        return row

    if op == "workflow":
        # A workflow run's orchestrator: advance the graph once, then park until the next look or
        # finish. Never moves the cursor until the run is over, so a replayed claim re-reads the
        # run row and decides again; the engine's own starts are idempotent (see `engine.py`).
        from felix.workflows.engine import advance

        outcome = await advance(settings, row["tenant_id"], str(step.get("run_id") or ""))
        if outcome.finished:
            state["cursor"] = cursor + 1
            stash["last"] = {"answer": "", "final": "", "error": outcome.error, "manifest_id": ""}
            state["stash"] = stash
            row["state_json"] = state
            row["status"] = "failed" if outcome.error else "completed"
            row["wake_at"] = None
        else:
            row["state_json"] = state
            _park(row, outcome.delay_ms)
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


#: How long one `register_run_finished` hook may take before the worker moves on. Short: the hook
#: runs in the step's concurrency slot, so a slow far end (Slack, say) delays every tenant's runs.
RUN_FINISHED_HOOK_TIMEOUT_S = 10.0


async def _after_terminal(settings: Settings, row: dict[str, Any]) -> None:
    """What follows a run reaching a terminal status, once that status is saved. Never raises."""
    if row.get("status") not in FIBER_TERMINAL_STATUSES:
        return
    await _end_background_child(settings, row)
    from felix.durability.runs import DURABLE_CHAT_KIND  # runs imports this module

    if row.get("kind") == DURABLE_CHAT_KIND:
        await _notify_run_finished(settings, row)
        # A durable run is live between its turns too, so what a scheduled task reported meanwhile
        # waited for this, not for a turn's end (`session/idle_append.py:append_or_queue`).
        from felix.session.idle_append import deliver_pending

        await deliver_pending(settings, str(row.get("tenant_id") or ""), str(row.get("thread_id") or ""))
    if row.get("kind") == "workflow_run":  # `felix.workflows.engine.ORCHESTRATOR_KIND`
        # A buried orchestrator would otherwise leave its run `running` for good.
        try:
            from felix.workflows.engine import orchestrator_ended

            await orchestrator_ended(settings, row)
        except Exception:
            logger.exception("workflow orchestrator %s: closing its run failed", row.get("id"))


async def _end_background_child(settings: Settings, row: dict[str, Any]) -> None:
    """A background `task` child's run is over: its worktree, if it left nothing there, goes, so
    finished tasks do not hold the thread's worktree slots for the checkout TTL."""
    if (row.get("state_json") or {}).get("parent_thread_id") and row.get("thread_id"):
        from felix.repos.worktrees import release_if_untouched

        try:
            await release_if_untouched(settings, str(row["tenant_id"]), str(row["thread_id"]))
        except Exception:
            logger.warning("task_worktree_background_cleanup_failed id=%s", row.get("id"), exc_info=True)


async def _notify_run_finished(settings: Settings, row: dict[str, Any]) -> None:
    """The `register_run_finished` hooks of the plugin that started this run, in turn, each bounded
    and none able to stop the next: a channel posting a run's answer back to where it was asked."""
    from felix.plugins import RunEnd, RunFinished, get_registry

    origin = (row.get("state_json") or {}).get("origin")
    owner = str(origin.get("plugin") or "") if isinstance(origin, dict) else ""
    hooks = get_registry().run_finished_hooks(owner) if owner else []
    if not hooks:
        return
    from felix.durability.runs import run_view

    view = run_view(row)
    finished = RunFinished(
        tenant_id=str(row.get("tenant_id") or ""),
        thread_id=str(row.get("thread_id") or ""),
        run_id=str(row.get("id") or ""),
        # Terminal: `_after_terminal` checked it against FIBER_TERMINAL_STATUSES.
        status=cast("RunEnd", str(view.get("status") or "")),
        answer=str((view.get("final") or {}).get("content") or ""),
        error=str(view.get("error") or ""),
        origin={str(k): str(v) for k, v in origin.items()} if isinstance(origin, dict) else {},
    )
    for hook in hooks:
        try:
            await asyncio.wait_for(hook(settings, finished), timeout=RUN_FINISHED_HOOK_TIMEOUT_S)
        except Exception:
            logger.warning("run_finished_hook_failed id=%s", row.get("id"), exc_info=True)


def _count_lapsed_claim(status: str, lease_until: int | None, state: dict[str, Any] | None) -> dict[str, Any]:
    """The `state_json` a claim writes: it counts a claim that found the last holder gone.

    For a row the claim has already found due. `running` with a lease still on it means the last
    holder never let go: it was killed mid-step. A step that kills its worker raises nothing, so
    `attempts` never moved and the fiber was claimed again every lease until its `expires_at`
    -- a day of five-minute reclaims at the longest `resume_token_ttl_seconds`. Each such claim
    is counted here, in the claim's own transaction; what the count means is decided after it
    (`_settle_lapsed_claim`). A pending fiber, one woken from a sleep, and one released after a
    step or by a worker that was stopping are not lapses, and clear the counts.
    """
    state = dict(state or {})
    if status != "running" or lease_until is None:
        for key in (FIBER_LAPSES_KEY, FIBER_LAPSES_TOTAL_KEY, FIBER_LAPSES_SEQ_KEY):
            state.pop(key, None)
        return state
    state[FIBER_LAPSES_KEY] = int(state.get(FIBER_LAPSES_KEY) or 0) + 1
    state[FIBER_LAPSES_TOTAL_KEY] = int(state.get(FIBER_LAPSES_TOTAL_KEY) or 0) + 1
    return state


def _log_thread(row: dict[str, Any]) -> str:
    """The thread this fiber's pending step writes to, the way the `invoke` op picks it."""
    state = row.get("state_json") or {}
    steps = list(state.get("steps") or [])
    cursor = int(state.get("cursor") or 0)
    step = steps[cursor] if cursor < len(steps) and isinstance(steps[cursor], dict) else {}
    named = step.get("thread_id") or (state.get("stash") or {}).get("thread_id") or row.get("thread_id")
    return str(named or "") or fiber_thread_id(str(row["tenant_id"]), str(row["id"]))


async def _log_head(settings: Settings, row: dict[str, Any]) -> int | None:
    """Where the fiber's thread log ends, or None when that cannot be read."""
    from felix.db.session import rls_tenant
    from felix.session.store import get_session_store

    tenant_id = str(row["tenant_id"])
    try:
        # The claim runs with no request context, so the tenant is bound here: unbound under an
        # enforcing policy the log reads as empty, and an empty log never shows progress.
        with rls_tenant(tenant_id):
            session = get_session_store(settings, tenant_id=tenant_id).open(_log_thread(row))
            head = await asyncio.wait_for(session.head(), timeout=FIBER_LOG_HEAD_TIMEOUT_S)
        return int(head.get("seq") or 0)
    except Exception:
        logger.warning("fiber log head unavailable id=%s", row.get("id"), exc_info=True)
        return None


async def _settle_lapsed_claim(settings: Settings, row: dict[str, Any]) -> str:
    """Decide what a claim that found its fiber's last holder gone means: `claimed`, `buried`, `lost`.

    Burying on the count alone buried healthy runs. A run parked on an approval, or in a long
    turn, that lived through `fiber_max_attempts` lost workers was written `dead` -- and so was
    every fiber that shared a worker with one that kept killing it. So the streak is only of
    lost workers *with nothing done between them*: the thread's log is where a run's work
    lands, and if its head has moved since the last lapse the streak starts again at this one.

    The log is read from the deployment's own session store. A fiber that writes to none -- a
    manifest with `checkpointer: none` or one of its own, a workflow's orchestrator -- shows
    no movement, so for it every lapse counts: it is bounded by `fiber_max_attempts` lost
    workers in a row, with only a landed step to start the count again. A log that cannot be
    read, or not within `FIBER_LOG_HEAD_TIMEOUT_S`, is treated the same way, as no evidence.

    Movement is not always progress: a step that kills its worker inside a tool call logs the
    call again each time. `FIBER_LAPSE_BACKSTOP` bounds that -- every lapse since a holder last
    let go cleanly, whatever the log did.
    """
    state = dict(row.get("state_json") or {})
    streak = int(state.get(FIBER_LAPSES_KEY) or 0)
    if not streak:
        return "claimed"
    head = await _log_head(settings, row)
    seen = state.get(FIBER_LAPSES_SEQ_KEY)
    if head is not None and isinstance(seen, int) and head > seen:
        streak = 1
    state[FIBER_LAPSES_KEY] = streak
    if head is not None:
        state[FIBER_LAPSES_SEQ_KEY] = head
    total = int(state.get(FIBER_LAPSES_TOTAL_KEY) or 0)
    limit = settings.fiber_max_attempts
    if streak < limit and total < limit * FIBER_LAPSE_BACKSTOP:
        row["state_json"] = state
        return "claimed" if await _checkpoint_state(settings, row) else "lost"
    stash = dict(state.get("stash") or {})
    last = dict(stash.get("last") or {})
    last["error"] = f"step lost its worker {streak if streak >= limit else total} times; not run again"
    stash["last"] = last
    state["stash"] = stash
    # On a copy: if this write raises the fiber is run after all, from the row as claimed.
    buried: dict[str, Any] = {**row, "state_json": state, "status": "dead", "wake_at": None}
    claimed_at = int(row.get("version") or 0)
    await _save_fiber(settings, buried)
    if int(buried.get("version") or 0) == claimed_at:
        return "lost"
    row.clear()
    row.update(buried)
    return "buried"


FiberKey = tuple[str, str]  # (tenant_id, id)


def _due_memory(row: dict[str, Any], ts: int) -> bool:
    lease_until = row.get("lease_until")
    if lease_until is not None and lease_until > ts:
        return False  # someone else holds the claim
    due_sleep = row["status"] == "sleeping" and row.get("wake_at") is not None and row["wake_at"] <= ts
    return due_sleep or row["status"] in {"running", "pending"}


def _due_postgres(ts: int) -> tuple[Any, ...]:
    """The claim's WHERE, shared with `_due_keys` so the two cannot mean different fibers."""
    return (
        Fiber.status.in_(("running", "pending", "sleeping")),
        # sleeping fibers are only due once their timer fires
        (Fiber.status != "sleeping") | (Fiber.wake_at.is_not(None) & (Fiber.wake_at <= ts)),
        # unclaimed, or the previous claim expired (crashed worker)
        Fiber.lease_until.is_(None) | (Fiber.lease_until <= ts),
        # No `backend` filter. Rows an earlier version handed to Temporal carry
        # `backend: temporal`; their state is all here, so this scheduler picks
        # them up rather than leaving them stranded with no worker to drive them.
    )


async def _due_keys(settings: Settings, limit: int = FIBER_BATCH) -> list[FiberKey]:
    """Which fibers are due now, oldest first, claiming none of them.

    What a sweep is *over*: it claims from this list as it has room to start them
    (`resume_due_fibers`), where it used to claim the list.
    """
    ts = now_ms()
    if _use_memory(settings):
        return [key for key, row in _memory_fibers.items() if _due_memory(row, ts)][:limit]
    from felix.db.session import rls_bypass

    with rls_bypass():
        factory = get_session_factory(settings=settings)
        async with factory() as db:
            stmt = (
                select(Fiber.tenant_id, Fiber.id)
                .where(*_due_postgres(ts))
                .order_by(Fiber.updated_at, Fiber.tenant_id, Fiber.id)
                .limit(limit)
            )
            return [(str(tenant), str(fiber)) for tenant, fiber in (await db.execute(stmt)).all()]


async def _claim_due_memory(
    settings: Settings,
    ts: int,
    limit: int = FIBER_BATCH,
    only: frozenset[FiberKey] | None = None,
) -> list[dict[str, Any]]:
    claimed: list[dict[str, Any]] = []
    if limit <= 0:
        return claimed
    for key, row in _memory_fibers.items():
        if (only is not None and key not in only) or not _due_memory(row, ts):
            continue
        row["state_json"] = _count_lapsed_claim(row["status"], row.get("lease_until"), row.get("state_json"))
        row["status"] = "running"
        row["wake_at"] = None
        row["lease_owner"] = _mint_claim(settings)
        row["lease_until"] = ts + FIBER_LEASE_MS
        # Bumped the way the Postgres claim bumps them. `updated_at` is not bookkeeping here:
        # the claim orders by it, so a re-claimed fiber goes to the back of the queue. Leaving
        # it alone kept the twin re-picking the same fiber ahead of everything else, which is
        # round-robin on the system of record and starvation on the twin.
        row["updated_at"] = ts
        row["version"] = int(row.get("version") or 0) + 1
        claimed.append(_stored_copy(row))
        if len(claimed) >= limit:
            break
    return claimed


async def _claim_due_postgres(
    settings: Settings,
    ts: int,
    limit: int = FIBER_BATCH,
    only: frozenset[FiberKey] | None = None,
) -> list[dict[str, Any]]:
    """Claim a bounded batch of due fibers, skipping rows another worker holds.

    ``FOR UPDATE SKIP LOCKED`` plus a lease column is what stops the same step running
    twice: the row lock serializes concurrent claimers within the transaction, and the
    lease keeps the fiber claimed for the duration of the step, which outlives it.
    """
    from sqlalchemy import tuple_

    from felix.db.session import rls_bypass

    if limit <= 0 or (only is not None and not only):
        return []
    factory = get_session_factory(settings=settings)
    # The sweep is cross-tenant maintenance, like retention: without a bypass this runs
    # with no app.tenant_id GUC and RLS silently returns nothing, stalling durability.
    with rls_bypass():
        async with factory() as db:
            stmt = select(Fiber).where(*_due_postgres(ts))
            if only is not None:
                stmt = stmt.where(tuple_(Fiber.tenant_id, Fiber.id).in_(sorted(only)))
            stmt = stmt.order_by(Fiber.updated_at).limit(limit).with_for_update(skip_locked=True)
            rows = (await db.scalars(stmt)).all()
            claimed: list[dict[str, Any]] = []
            for row in rows:
                # Assigned, not edited in place: a JSON column only notices a new value.
                row.state_json = _count_lapsed_claim(row.status, row.lease_until, row.state_json)
                row.status = "running"
                row.wake_at = None
                row.lease_owner = _mint_claim(settings)
                row.lease_until = ts + FIBER_LEASE_MS
                row.updated_at = ts
                row.version = int(row.version or 0) + 1
                claimed.append(_fiber_dict(row))
            await db.commit()
            return claimed


async def _claim_and_settle(
    settings: Settings, limit: int = FIBER_BATCH, only: frozenset[FiberKey] | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Claim up to `limit` due fibers: those to run, and those the claim buried instead.

    `only` narrows the claim to those fibers, for a sweep (`_due_keys`). A buried fiber
    (`_settle_lapsed_claim`) is `dead` in the store; what follows a run ending is the caller's
    to do (`_follow_burials`), once the fibers claimed beside it are running.
    """
    ts = now_ms()
    if _use_memory(settings):
        written = await _claim_due_memory(settings, ts, limit, only)
    else:
        written = await _claim_due_postgres(settings, ts, limit, only)
    claimed: list[dict[str, Any]] = []
    buried: list[dict[str, Any]] = []
    for row in written:
        if _stopping:
            # The worker began to stop while this claim was being made. These rows are about
            # to be handed back unrun (`_start_due`), so there is nothing to decide about them,
            # and a log read each is time the shutdown does not have.
            claimed.append(row)
            continue
        try:
            outcome = await _settle_lapsed_claim(settings, row)
        except Exception:
            # The count could not be settled. The fiber is claimed all the same, and running it
            # is what the claim is for; the count is in the row for the next claim to settle.
            logger.warning("fiber lapse count unsettled id=%s; running it", row.get("id"), exc_info=True)
            outcome = "claimed"
        if outcome == "buried":
            logger.warning("fiber buried id=%s: %s", row.get("id"), _last_error(row))
            buried.append(row)  # its save announced it
        elif outcome == "claimed":
            claimed.append(row)
    # After the claim commits: a stream reports `running` when the run starts, not when it next
    # polls, which on a backed-up queue could be most of a minute later.
    for row in claimed:
        with contextlib.suppress(Exception):
            await _announce_fiber(row)
    return claimed, buried


async def _follow_burials(settings: Settings, buried: list[dict[str, Any]]) -> None:
    """What follows a run ending, for fibers a claim buried. Never raises."""
    for row in buried:
        try:
            await _after_terminal(settings, row)
        except Exception:
            logger.warning("fiber burial follow-up failed id=%s", row.get("id"), exc_info=True)


async def _claim_due(
    settings: Settings, limit: int = FIBER_BATCH, only: frozenset[FiberKey] | None = None
) -> list[dict[str, Any]]:
    """Claim up to `limit` due fibers from whichever store this process uses, for a caller that
    steps them itself. A fiber the claim buried is finished here and not returned."""
    claimed, buried = await _claim_and_settle(settings, limit, only)
    await _follow_burials(settings, buried)
    return claimed


def _last_error(row: dict[str, Any]) -> str:
    return str((((row.get("state_json") or {}).get("stash") or {}).get("last") or {}).get("error") or "")


# The fibers this process is advancing, and the slots each task holds: one per fiber in its
# step, and for a task that is claiming, as many as it asked the store for. One table for the
# whole process because `fiber_concurrency` is a promise about the worker, and a worker runs two
# claimers -- `run_fiber_loop` and the cron sweep. Each kept a count of its own, so together
# they ran twice the setting.
_slots_held: dict[asyncio.Task[Any], int] = {}
# The fiber tasks among them, whoever started each: what a stopping worker has to drain.
_advancing: set[asyncio.Task[None]] = set()
# Whether this process has begun to stop. From then on it starts no fiber, and it stays so:
# cleared when the drain ended, a sweep still running claimed a fresh fiber the moment the drain
# returned, to be cut off mid-turn by the exit with its lease held.
_stopping = False
# The drain in progress, so that two callers -- the worker's shutdown and the loop's own exit --
# share one: a second drain would cancel fibers a second time, on top of their release.
_drain: asyncio.Task[None] | None = None


def stop_claiming() -> None:
    """This process is stopping: start no more fibers. Stays set until `resume_claiming`."""
    global _stopping
    _stopping = True


def resume_claiming() -> None:
    """A worker starting up (and a test starting over): fibers may be started again."""
    global _stopping
    _stopping = False


def _free_slots(settings: Settings) -> int:
    """How many more fibers this process may start now."""
    loop = asyncio.get_running_loop()
    # A task that ended frees its slots itself; one stranded on a loop that is gone cannot.
    for task in [t for t in _slots_held if t.done() or t.get_loop() is not loop]:
        del _slots_held[task]
    return max(settings.fiber_concurrency - sum(_slots_held.values()), 0)


async def _start_due(
    settings: Settings, limit: int = FIBER_BATCH, only: frozenset[FiberKey] | None = None
) -> tuple[dict[asyncio.Task[None], dict[str, Any]], list[dict[str, Any]]]:
    """Claim as many due fibers as this process can start now, and start them.

    Returns the tasks started, each with its row, and the fibers the claim buried instead.

    A claim is a lease, and only a fiber in its step renews one (`_step_with_lease`). So nothing
    is claimed to wait: the sweep took a batch of fifty and ran `fiber_concurrency` of them, and
    the rest held a claim that lapsed after five minutes with its holder still queued behind a
    slot -- to be claimed again and run by someone else, and then run by the first holder too.
    """
    me = asyncio.current_task()
    want = min(_free_slots(settings), limit)
    if want <= 0 or me is None or _stopping:
        return {}, []
    # Held across the claim's await, so two claimers cannot both count the same free slots.
    _slots_held[me] = _slots_held.get(me, 0) + want
    try:
        due, buried = await _claim_and_settle(settings, want, only)
        started: dict[asyncio.Task[None], dict[str, Any]] = {}
        if _stopping:
            # Claimed across the moment the worker began to stop. Started now, a fiber would
            # miss the drain, which has already counted what it has to wait for; so each is
            # handed straight back, claimable by a worker that is staying.
            for row in due:
                with contextlib.suppress(Exception):
                    await _release_fiber(settings, row)
            due = []
        for row in due:
            task = asyncio.create_task(_advance_claimed(settings, row))
            _slots_held[task] = 1
            task.add_done_callback(lambda done: _slots_held.pop(done, None))
            _advancing.add(task)
            task.add_done_callback(_advancing.discard)
            started[task] = row
        # Only now, with every claimed fiber in its step and renewing its lease: a burial's
        # hooks may take ten seconds each, and nothing claimed beside it waits on them.
        await _follow_burials(settings, buried)
        return started, buried
    finally:
        left = _slots_held.get(me, 0) - want
        if left > 0:
            _slots_held[me] = left
        else:
            _slots_held.pop(me, None)


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
        await _after_terminal(settings, row)
        return
    if isinstance(failure, FiberLeaseLost):
        # Someone else's fiber now: charging or parking it would write over the claim of the
        # worker that holds it. (`_step_with_lease` has let go of it, if it was still ours.)
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
        saved = await _retry_or_dead(settings, row, attempt, failure)
    except Exception:  # the store is down; the claim lapses and a later sweep retries it
        logger.warning("fiber retry bookkeeping failed id=%s", row.get("id"), exc_info=True)
        saved = False
    if saved:
        # Only a status the store holds: an unsaved "dead" is claimed and run again.
        await _after_terminal(settings, row)


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

    A fiber is claimed when there is a slot to start it in, never before (`_start_due`), and the
    slots are the worker's: what `run_fiber_loop` is running in this process counts against the
    sweep too. The sweep is over the fibers due when it began, up to `FIBER_BATCH` (`_due_keys`),
    and claims from them as slots free. So it is the same sweep it was: each fiber gets one claim
    -- a second `invoke` still waits for the next -- and one that arrives meanwhile is the next
    sweep's, or the loop's.

    This is the backstop. `run_fiber_loop` is what picks a new run up within a second.
    """
    todo = set(await _due_keys(settings))
    stepped = 0
    running: set[asyncio.Task[None]] = set()
    try:
        while True:
            started: dict[asyncio.Task[None], dict[str, Any]] = {}
            buried: list[dict[str, Any]] = []
            if _stopping:
                todo.clear()  # what is left is for a worker that is staying
            if todo:
                try:
                    started, buried = await _start_due(settings, len(todo), frozenset(todo))
                except Exception:
                    if not running:
                        raise  # nothing of this sweep's is in flight: the caller hears it, as before
                    # Fibers already started are not abandoned over a claim that failed.
                    logger.warning("fiber sweep claim failed; finishing what it started", exc_info=True)
                    todo.clear()
            todo -= {(str(row["tenant_id"]), str(row["id"])) for row in (*started.values(), *buried)}
            stepped += len(started)
            running.update(started)
            if started or buried:
                # There may be more due, and slots left to start them in. A claim that drew
                # only burials started nothing and used no slot, and is no reason to stop.
                continue
            if not running:
                break
            done, running = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if not task.cancelled():  # by a stopping worker's drain: that fiber is done with
                    task.result()  # `_advance_claimed` raises nothing; what does escape is not ours to hide
    except BaseException:
        # Cancelled, or a step died of something no handler is meant to catch. A fiber left
        # running would be a step with no sweep behind it; cancelled, it stops and lets go.
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)
        raise
    return stepped


# How long a stopping worker waits for in-flight fibers before cancelling them. A cancelled
# fiber stops its step and gives its claim back, so another worker picks the run up from its
# log at once; only if that write fails does the lease lapse — the crashed-worker path.
FIBER_LOOP_DRAIN_S = 30.0
# How long a cancelled fiber then has to stop its step and give its claim back, before the
# worker goes on without it. The release alone may take `FIBER_CANCEL_RELEASE_S`; the rest is
# for the step to stop. A tool that ignores cancellation is left behind with its lease, rather
# than holding the worker's shutdown until it is killed.
FIBER_STEP_STOP_S = 10.0
FIBER_LOOP_STOP_S = FIBER_STEP_STOP_S + FIBER_CANCEL_RELEASE_S
# The whole of `drain_fibers`, for whoever has to allow time for it (`felix_worker.shutdown`).
FIBER_DRAIN_BUDGET_S = FIBER_LOOP_DRAIN_S + FIBER_LOOP_STOP_S
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
    own task, and the loop claims only as many as the worker has free slots for, out of
    `fiber_concurrency` (`_start_due`; the cron sweep draws on the same slots), so one fiber
    waiting minutes on an approval occupies one slot and delays nothing else.
    """
    interval = settings.fiber_poll_seconds
    delay = interval
    logger.info("fiber_loop started interval=%ss concurrency=%d", interval, settings.fiber_concurrency)
    try:
        while not stop.is_set():
            try:
                await _start_due(settings)
                delay = interval
            except Exception:
                delay = min(max(delay * 2, interval), FIBER_LOOP_MAX_BACKOFF_S)
                logger.warning("fiber_loop claim failed; next attempt in %ss", delay, exc_info=True)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)
    finally:
        if stop.is_set():
            await drain_fibers()
            logger.info("fiber_loop stopped")
        else:
            # Not a shutdown. Draining would stop this process claiming for good, the cron
            # sweep included, and cancel healthy runs, while the worker went on beating.
            logger.error("fiber_loop ended without being asked to; the cron sweep is now the only pickup")


async def drain_fibers() -> None:
    """Let the fibers this process is running finish, then stop the ones that have not.

    Every one of them, the cron sweep's as well as the loop's: a sweep still holding a fiber
    when the worker stops is cut off with the process, and its fiber was left under a lease.
    Nothing new is started from here on (`stop_claiming`), by this drain or after it. Each
    fiber gets `FIBER_LOOP_DRAIN_S` to reach its next suspension; one still in its step is then
    cancelled, which stops the step and gives the claim back (`_step_with_lease`), and gets
    `FIBER_LOOP_STOP_S` to do that. Bounded, and cancelled once only: a second cancel would land
    on the release itself.
    """
    global _drain
    stop_claiming()
    loop = asyncio.get_running_loop()
    if _drain is None or _drain.done() or _drain.get_loop() is not loop:
        _drain = asyncio.create_task(_drain_fibers())
    # Shielded: a caller cut off mid-drain (the handler's own timeout) must not become a
    # second cancel on the fibers, and the other caller is still waiting on the same drain.
    await asyncio.shield(_drain)


async def _drain_fibers() -> None:
    loop = asyncio.get_running_loop()
    running = {task for task in _advancing if not task.done() and task.get_loop() is loop}
    if not running:
        return
    _, pending = await asyncio.wait(running, timeout=FIBER_LOOP_DRAIN_S)
    for task in pending:
        task.cancel()
    if pending:
        # Until each has stopped its step and let go of its claim: returning sooner left
        # the step running with nothing behind it. What it was doing is in the log, for
        # whoever claims it next.
        _, stuck = await asyncio.wait(pending, timeout=FIBER_LOOP_STOP_S)
        if stuck:
            logger.warning("fiber_loop left %d fibers that would not stop; leases will lapse", len(stuck))


def _park(row: dict[str, Any], delay_ms: int) -> None:
    """Put a fiber to sleep for `delay_ms`; the caller saves."""
    row["status"] = "sleeping"
    row["wake_at"] = now_ms() + max(int(delay_ms), 0)


def retry_delay_ms(attempts: int) -> int:
    """Delay before the next try after `attempts` consecutive failures: 1m, 2m, 4m … 1h."""
    return min(FIBER_RETRY_BASE_MS * 2 ** max(attempts - 1, 0), FIBER_RETRY_MAX_MS)


async def _retry_or_dead(settings: Settings, row: dict[str, Any], attempts: int, exc: Exception) -> bool:
    """Park a failed fiber for a backoff, or bury it once it has failed enough times.

    A step that reached a terminal status and then failed only at its save is finished:
    that status is kept rather than coerced to `sleeping`, which would re-enter the step
    loop with `cursor` past the end and report the run `completed` over a failed invoke.
    Returns whether the row's new state was saved.
    """
    if row.get("status") in FIBER_TERMINAL_STATUSES:
        row["attempts"] = 0
    elif attempts >= settings.fiber_max_attempts:
        row["attempts"] = attempts
        state = dict(row.get("state_json") or {})
        stash = dict(state.get("stash") or {})
        last = dict(stash.get("last") or {})
        # Not the exception's text, not even its first line: that named the database host on
        # a connection error, and this is handed back on a poll. The exception is in the log.
        from felix.errors import client_safe_message

        last["error"] = f"step failed {attempts} times; last: {client_safe_message(exc)}"
        stash["last"] = last
        state["stash"] = stash
        row["state_json"] = state
        row["status"] = "dead"
        row["wake_at"] = None
    else:
        row["attempts"] = attempts
        _park(row, retry_delay_ms(attempts))
    read_at = int(row.get("version") or 0)
    try:
        await _save_fiber(settings, row)
    except Exception:
        # The save is the thing failing — which is the failure this exists to bound, so
        # the count cannot go through the same write. Record it on the columns alone.
        logger.warning(
            "fiber save failed id=%s; recording the attempt without state", row.get("id"), exc_info=True
        )
    else:
        if int(row.get("version") or 0) != read_at:
            return True
        # `_save_fiber` reports a lost compare-and-set by returning, and "it returned" was read
        # here as "it landed": an unsaved `dead` was announced as the run's end. The store's row
        # has moved on from the one this holder carries. Either it is someone else's now (a
        # later claim, `stop_fiber`), and the release below matches nothing; or it is this
        # claim's own step, saved, whose answer never came back -- and then nothing but this
        # release would let go of the lease, and the fiber would sit out the rest of it.
        # Nothing is recorded either way: `row` is not what the store holds.
        logger.warning("fiber retry write was discarded id=%s; giving the claim up", row.get("id"))
        try:
            if await _release_fiber(settings, row):
                # Still this claim's, so what the store holds is this holder's own step. If
                # that step finished the run, nobody else will say so: the status was never
                # in `row`'s hands to announce. Said here, once, from the stored row.
                stored = await get_fiber(settings, str(row["tenant_id"]), str(row["id"]))
                if stored is not None:
                    await _after_terminal(settings, stored)
        except Exception:
            logger.warning("fiber release failed id=%s; lease will lapse", row.get("id"), exc_info=True)
        return False
    try:
        await _record_attempt(settings, row)
        return False
    except Exception:
        # The store itself is down. Nothing can be recorded; drop the claim so the next
        # tick can try again — the count is lost, which errs toward retrying, not burying.
        logger.warning("fiber retry bookkeeping failed id=%s; releasing", row.get("id"), exc_info=True)
    try:
        await _release_fiber(settings, row)
    except Exception:
        # Also the store. The lease lapses on its own; the rest of the batch still runs.
        logger.warning("fiber release failed id=%s; lease will lapse", row.get("id"), exc_info=True)
    return False


def _holds_claim(stored: dict[str, Any] | None, claim: str) -> bool:
    """Whether the stored row is still under `claim` -- the twin's `_claim_is`."""
    return bool(claim) and stored is not None and stored.get("lease_owner") == claim


def _claim_is(claim: str) -> Any:
    """The predicate for "this row is still under `claim`" (`_claim_of`, `_mint_claim`).

    `lease_owner` names the claim, not only the process, so this one comparison is the whole
    question for all three writes scoped to a claim: `_renew_lease`, `_release_fiber`,
    `_record_attempt`. It does not move with the holder's own saves, as `version` does, and it
    needs no knowledge of what the store last heard: a save or a renewal that landed and whose
    answer was lost leaves the claim's name where it was.
    """
    from sqlalchemy import false

    if not claim:
        return false()  # not a row this process claimed: there is no claim here to match
    return Fiber.lease_owner == claim


async def _record_attempt(settings: Settings, row: dict[str, Any]) -> None:
    """Write status, wake_at and attempts — and nothing else — releasing the claim.

    `_save_fiber` also writes `state_json`, through `redact_json`; when that is what raised,
    this is the write that still lands. The run view derives the error from `attempts`
    when `dead` carries none.

    Scoped to this claim (`_claim_is`), like `_release_fiber` and `_renew_lease`, or to a row
    nobody holds. It clears the lease *and* overwrites `status` -- so on a worker that has lost
    its claim it would take the row out from under whoever now owns it. No compare-and-set,
    deliberately: this path exists because the versioned write is what failed.
    """
    claim = _claim_of(settings, row)
    row["updated_at"] = now_ms()
    fields = {k: row.get(k) for k in ("status", "wake_at", "attempts", "updated_at")}
    fields.update(lease_owner="", lease_until=None)
    if _use_memory(settings):
        stored = _memory_fibers.get((row["tenant_id"], row["id"]))
        if stored is not None and (_holds_claim(stored, claim) or stored.get("lease_owner") == ""):
            stored.update(fields)
            row.update(fields)
            await _announce_fiber(row)
        return
    from sqlalchemy import or_, update

    from felix.db.session import rls_bypass

    mine = _claim_is(claim)
    with rls_bypass():
        factory = get_session_factory(settings=settings)
        async with factory() as db:
            result = await db.execute(
                update(Fiber)
                .where(
                    Fiber.tenant_id == row["tenant_id"],
                    Fiber.id == row["id"],
                    or_(mine, Fiber.lease_owner == ""),
                )
                .values(**fields)
            )
            await db.commit()
    # The fallback when `_save_fiber` itself failed, and it writes `status` -- `dead` among
    # them -- so it announces like the save it stands in for.
    if getattr(result, "rowcount", 0):
        row.update(fields)
        await _announce_fiber(row)


async def _renew_lease(settings: Settings, row: dict[str, Any]) -> bool:
    """Push this worker's claim out by another lease window. False when it no longer holds it.

    The answer used to be thrown away: an update matching no row -- the claim lapsed and another
    worker took it -- looked exactly like a renewal, and the step went on running beside the
    new owner's (felix-run/felix#531).
    """
    claim = _claim_of(settings, row)
    if not claim:
        return False  # not a row this process claimed: there is no claim here to renew
    # Never backwards: a renewal is not allowed to shorten the lease the store holds.
    until = max(now_ms() + FIBER_LEASE_MS, int(row.get("lease_until") or 0))
    if _use_memory(settings):
        # Only if we still hold it: a lease we already lost must not be stolen back mid-step.
        stored = _memory_fibers.get((row["tenant_id"], row["id"]))
        if stored is None or not _holds_claim(stored, claim):
            return False
        stored["lease_until"] = row["lease_until"] = until
        return True
    from sqlalchemy import update

    from felix.db.session import rls_bypass

    with rls_bypass():
        factory = get_session_factory(settings=settings)
        async with factory() as db:
            result = await db.execute(
                update(Fiber)
                .where(Fiber.tenant_id == row["tenant_id"], Fiber.id == row["id"], _claim_is(claim))
                .values(lease_until=until)
            )
            await db.commit()
            if not int(getattr(result, "rowcount", 0) or 0):
                return False
    row["lease_until"] = until
    return True


class FiberClaimUnconfirmed(Exception):
    """The write an `invoke` makes to ask whether the row is still its claim's could not be made.

    Raised out of the step, past the invoke's own handler, so it is charged and retried like any
    other store failure rather than recorded as the run's answer.
    """


class FiberLeaseLost(Exception):
    """This worker's claim on a fiber lapsed or was taken while a step was running.

    Not a step failure: the fiber belongs to whoever holds it now, so nothing here may charge an
    attempt or park it, and the only release is one that matches this claim alone (`_claim_is`).
    The step is cancelled, because running on beside the new owner's is the duplicate side effect
    the lease exists to prevent.
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
        # The lease the claim wrote, not a lease from now: between the claim and this step
        # starting the claim may have read a thread log, and "a moment ago" was an assumption
        # that let a renewal be retried past the point another worker could take the fiber.
        held_until = int(row.get("lease_until") or (now_ms() + FIBER_LEASE_MS))
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
    discarded = False
    interrupted = False
    stopped = False
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
            except asyncio.CancelledError:
                # The worker is stopping and the drain ran out. `asyncio.wait` does not cancel
                # what it waits on, so the step ran on with nobody behind it -- and the release
                # below then freed the fiber for another worker to run beside it. Stop the step
                # first; the claim is given up below only once it has stopped.
                interrupted = True
                step.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await step
                stopped = step.done()
                raise
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
                discarded = True
                landed -= 1
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
        #
        # A claim the heartbeat gave up on is released too. The release matches only this
        # claim (`_claim_is`), so on a fiber someone else now holds it does nothing, and on
        # one whose renewals merely kept failing it saves the fiber the rest of a lease.
        #
        # A claim cut short by a cancel is released once its step has stopped -- the property
        # that matters is that nothing is running when the claim is given up. Keeping the lease
        # instead stalled every run in flight at a worker's stop for up to a lease, and the
        # next claim read each as a worker lost. Shielded and bounded: the task is on its way
        # out, and if this write does not land the lease lapses, as it always could.
        if failure is None or isinstance(failure, FiberLeaseLost):
            if not interrupted:
                with contextlib.suppress(Exception):
                    await _release_fiber(settings, row)
            elif stopped:
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await asyncio.wait_for(
                        asyncio.shield(_release_fiber(settings, row)), timeout=FIBER_CANCEL_RELEASE_S
                    )
    if discarded:
        # Released above, if it is still this claim's to release. But `row` is what the step
        # meant to write, not what the store holds, so nothing may act on its status: a
        # `completed` that was never saved announced a run its real holder would announce again.
        failure = FiberLeaseLost(f"write discarded on fiber {row.get('id')}")
    return landed, failure


async def _release_fiber(settings: Settings, row: dict[str, Any]) -> bool:
    """Drop the claim so a failed step is retried rather than stranded until expiry. Returns
    whether the row was still under it.

    Scoped to this claim (`_claim_is`), matching `_renew_lease`. It used to clear the lease
    columns unconditionally, which was harmless while the only caller was the failure path of
    a claim it certainly held; it is not harmless now that a claim spans several steps. Then
    it was scoped to the worker, which is not the claim: see `_claim_is`.

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
    claim = _claim_of(settings, row)
    if _use_memory(settings):
        stored = _memory_fibers.get((row["tenant_id"], row["id"]))
        if stored is None or not _holds_claim(stored, claim):
            return False
        stored["lease_owner"] = ""
        stored["lease_until"] = None
        return True
    from sqlalchemy import update

    from felix.db.session import rls_bypass

    with rls_bypass():
        factory = get_session_factory(settings=settings)
        async with factory() as db:
            result = await db.execute(
                update(Fiber)
                .where(Fiber.tenant_id == row["tenant_id"], Fiber.id == row["id"], _claim_is(claim))
                .values(lease_owner="", lease_until=None)
            )
            await db.commit()
            return int(getattr(result, "rowcount", 0) or 0) > 0


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
    "TooManyChildren",
    "active_fiber_for_thread",
    "advance_fiber",
    "auth_from_state",
    "create_fiber",
    "drain_fibers",
    "fiber_thread_id",
    "get_fiber",
    "now_ms",
    "resume_claiming",
    "resume_due_fibers",
    "run_fiber_loop",
    "run_in_flight",
    "save_fiber",
    "stop_claiming",
    "stop_fiber",
]
