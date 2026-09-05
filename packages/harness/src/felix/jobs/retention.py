"""Prune every control-plane table that has no bound of its own.

The sweep used to cover `audit_events`, `plans` and `memory_vectors` and nothing else, so
four tables grew for the life of the deployment: `fibers` (which carry the caller's
principal and scopes in `state_json`), `usage_events`, `a2a_tasks` and `session_events`.
Each TTL was a module constant. They are settings now — `FELIX_*_RETENTION_DAYS`, ``0``
meaning keep forever — and the seven tables in `TABLES` are swept here, on both backends,
under one rule per table. (Not swept, because each is bounded by something else or is the
record itself: `approvals` and `job_runs` go with their run or job, eval tables with their
dataset, *active* `memory_vectors` are the memory, and session retention does not reach the
facts memory capture extracted from a thread.)

* `audit_events` — older than the audit TTL. A manifest's `governance.retention_days`
  shortens that for its own rows (never lengthens: the operator's setting is the ceiling),
  which is what makes the field in `governed.yaml` a policy rather than a comment.
* `usage_events` — older than the usage TTL. The billing record, so the default is long.
* `fibers` — in a terminal status and untouched for the fiber TTL. A sleeping or pending
  fiber is never swept; a stuck one is the retry ceiling's job, not retention's.
* `a2a_tasks` — not in a live A2A state and untouched for the fiber TTL.
* `session_events` + `thread_state` — whole threads whose last event is older than the
  session TTL. Whole threads only: `seq` is dense and `head()` assumes nothing deletes an
  individual event. Off by default, because the event log is the chat record.
* `plans` — past their own `expires_at`; `memory_vectors` — superseded past a grace period.

The memory:// path is the CI twin, not a mock: it deletes the same rows by the same rules
from the module-level stores, so a rule that regresses fails here before it reaches Postgres.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, func, select, tuple_

from felix.config import Settings
from felix.db.models import (
    A2ATask,
    AuditEvent,
    Fiber,
    MemoryVector,
    Plan,
    SessionEventRow,
    ThreadState,
    UsageEvent,
)
from felix.db.session import _use_memory, get_session_factory

logger = logging.getLogger("felix.jobs.retention")

DAY_MS = 24 * 60 * 60 * 1000
MEMORY_SUPERSEDED_GRACE_MS = 7 * DAY_MS

# A fiber in one of these will never be stepped again; anything else is retention's to keep.
TERMINAL_FIBER_STATUSES = frozenset({"completed", "failed", "expired"})
# A2A task states that still expect activity. Trust is an allowlist: a state the harness
# does not know is treated as finished rather than kept forever.
LIVE_A2A_STATES = frozenset({"submitted", "working", "input-required", "auth-required"})

TABLES = ("audit_events", "plans", "memory_vectors", "fibers", "usage_events", "a2a_tasks", "session_events")

now_ms = lambda: int(time.time() * 1000)


@dataclass(frozen=True)
class Cutoffs:
    """Epoch-ms thresholds for one sweep. ``None`` means the table is kept."""

    now: int
    audit: int | None
    usage: int | None
    fiber: int | None  # terminal fibers and finished A2A tasks share it
    # Epoch ms like the rest. `session_events.ts` is epoch *seconds* as a float on both
    # arms, so both divide by 1000 at that comparison; `thread_state.updated_at` is seconds
    # on the Postgres row and milliseconds on the memory twin, and each arm compares in its
    # own store's unit.
    session: int | None
    memory: int

    @classmethod
    def from_settings(cls, settings: Settings, now: int) -> Cutoffs:
        def days(value: int) -> int | None:
            return now - value * DAY_MS if value > 0 else None

        return cls(
            now=now,
            audit=days(settings.audit_retention_days),
            usage=days(settings.usage_retention_days),
            fiber=days(settings.fiber_retention_days),
            session=days(settings.session_retention_days),
            memory=now - MEMORY_SUPERSEDED_GRACE_MS,
        )


# Epoch-ms audit cutoff per ``(tenant_id, manifest_id)`` pair whose governing manifest sets
# `governance.retention_days`.
ManifestCutoffs = dict[tuple[str, str], int]


async def manifest_audit_cutoffs(settings: Settings, now: int) -> ManifestCutoffs:
    """`governance.retention_days` for every manifest that has audit rows.

    Pairs come from `audit_events` itself and each is resolved the way a request would be
    (`resolve_tenant_manifest`: tenant Postgres → object store → bundled, honouring
    `bundled_only`), so the sweep applies the document that actually governs the rows
    rather than re-deriving that precedence here. A pair whose manifest no longer resolves
    keeps the operator's TTL: rows for a deleted manifest are legitimate history.

    These only ever *add* deletions — the operator's `FELIX_AUDIT_RETENTION_DAYS` applies
    to every row regardless, so a manifest keeps less than the deployment, never more.
    """
    from felix.audit.store import list_manifests_with_events
    from felix.runtime import resolve_tenant_manifest

    out: ManifestCutoffs = {}
    unresolved = 0
    for tenant_id, manifest_id in await list_manifests_with_events(settings):
        if not manifest_id:
            continue
        try:
            resolved = await resolve_tenant_manifest(settings, tenant_id, manifest_id)
        except Exception:
            logger.debug("retention: %s/%s does not resolve; operator TTL applies", tenant_id, manifest_id)
            unresolved += 1
            continue
        days = resolved.manifest.spec.governance.retention_days
        if days:
            out[(tenant_id, manifest_id)] = now - days * DAY_MS
    if unresolved:
        # A deleted manifest is normal; every pair failing means a store is down and
        # every manifest-level policy silently lapsed to the operator TTL tonight.
        logger.info("retention: %d manifest(s) with audit rows did not resolve", unresolved)
    return out


async def run_retention_sweep(settings: Settings) -> dict[str, int]:
    """Delete expired control-plane rows. Returns rows deleted per table."""
    from felix.db.session import rls_bypass

    cutoffs = Cutoffs.from_settings(settings, now_ms())
    # Cross-tenant maintenance, like the fiber claim: without the bypass there is no
    # app.tenant_id GUC and RLS makes every read empty and every DELETE a silent no-op.
    with rls_bypass():
        per_manifest = await manifest_audit_cutoffs(settings, cutoffs.now)
    if _use_memory(settings):
        counts = _sweep_memory(cutoffs, per_manifest)
    else:
        counts = await _sweep_postgres(settings, cutoffs, per_manifest)
    logger.info("retention_sweep %s", counts)
    return counts


# --- memory:// ---------------------------------------------------------------------------


def _audit_expired(event: dict[str, Any], cutoffs: Cutoffs, per_manifest: ManifestCutoffs) -> bool:
    ts = int(event.get("ts") or 0)
    if cutoffs.audit is not None and ts < cutoffs.audit:
        return True
    own = per_manifest.get((str(event.get("tenant_id") or ""), str(event.get("manifest_id") or "")))
    return own is not None and ts < own


def _pop_where(rows: dict[Any, dict[str, Any]], predicate: Callable[[dict[str, Any]], bool]) -> int:
    doomed = [k for k, r in rows.items() if predicate(r)]
    for k in doomed:
        rows.pop(k, None)
    return len(doomed)


def _sweep_memory(cutoffs: Cutoffs, per_manifest: ManifestCutoffs) -> dict[str, int]:
    from felix.a2a import tasks as a2a_store
    from felix.audit import store as audit_store
    from felix.durability import fibers as fiber_store
    from felix.memory import store as memory_store
    from felix.plans import store as plans_store
    from felix.usage import store as usage_store

    counts = dict.fromkeys(TABLES, 0)

    # Flushed events live in `_memory_events`; `_pending` is the DurableBuffer awaiting
    # flush and was what the sweep used to filter — it is not a list, so nothing was pruned.
    events = audit_store._memory_events
    keep = [e for e in events if not _audit_expired(e, cutoffs, per_manifest)]
    counts["audit_events"] = len(events) - len(keep)
    events[:] = keep

    counts["plans"] = _pop_where(
        plans_store._memory_plans, lambda p: p.get("expires_at") is not None and p["expires_at"] < cutoffs.now
    )
    counts["memory_vectors"] = _pop_where(
        memory_store._memory_rows,
        lambda r: r.get("superseded_seq") is not None and int(r.get("created_at") or 0) < cutoffs.memory,
    )
    if cutoffs.fiber is not None:
        fiber_cutoff = cutoffs.fiber
        counts["fibers"] = _pop_where(
            fiber_store._memory_fibers,
            lambda f: (
                f.get("status") in TERMINAL_FIBER_STATUSES and int(f.get("updated_at") or 0) < fiber_cutoff
            ),
        )
        counts["a2a_tasks"] = _pop_where(
            a2a_store._memory_tasks,
            lambda t: (
                str((t.get("status_json") or {}).get("state") or "") not in LIVE_A2A_STATES
                and int(t.get("updated_at") or 0) < fiber_cutoff
            ),
        )
    if cutoffs.usage is not None:
        usage = usage_store._memory_events
        fresh = [e for e in usage if int(e.get("ts") or 0) >= cutoffs.usage]
        counts["usage_events"] = len(usage) - len(fresh)
        usage[:] = fresh
    if cutoffs.session is not None:
        counts["session_events"] = _sweep_memory_sessions(cutoffs.session)
    return counts


def _sweep_memory_sessions(cutoff_ms: int) -> int:
    """Drop every thread whose last event *and* last metadata write predate the cutoff.

    Event timestamps alone are not enough: a fork copies the source thread's events with
    their timestamps, so a thread forked today from an old conversation looks idle on its
    first night. `thread_state.updated_at` moves on every write to the thread, so a thread
    is idle only when both say so.
    """
    from felix.session import store as session_store
    from felix.session import thread_state, tree

    cutoff_s = cutoff_ms / 1000
    dropped = 0
    for store in session_store._memory_session_stores.values():
        sessions = store._sessions
        idle = [
            tid
            for tid, s in sessions.items()
            if s._events
            and max(float(ev.ts) for ev in s._events) < cutoff_s
            # The memory twin stamps `updated_at` in ms (the Postgres row uses seconds).
            and int(thread_state._meta_by_thread.get(tid, {}).get("updated_at") or 0) < cutoff_ms
        ]
        for thread_id in idle:
            dropped += len(sessions[thread_id]._events)
            sessions.pop(thread_id, None)
            thread_state._meta_by_thread.pop(thread_id, None)
            tree._leaf_by_thread.pop(thread_id, None)
    return dropped


# --- Postgres ----------------------------------------------------------------------------


async def _sweep_postgres(
    settings: Settings, cutoffs: Cutoffs, per_manifest: ManifestCutoffs
) -> dict[str, int]:
    from felix.db.session import rls_bypass

    counts = dict.fromkeys(TABLES, 0)
    factory = get_session_factory(settings=settings)
    with rls_bypass():
        async with factory() as db:
            await _table(db, counts, "audit_events", lambda: _delete_audit(db, cutoffs, per_manifest))
            await _table(
                db,
                counts,
                "plans",
                lambda: _rowcount(
                    db, delete(Plan).where(Plan.expires_at.is_not(None), Plan.expires_at < cutoffs.now)
                ),
            )
            await _table(
                db,
                counts,
                "memory_vectors",
                lambda: _rowcount(
                    db,
                    delete(MemoryVector).where(
                        MemoryVector.superseded_seq.is_not(None), MemoryVector.created_at < cutoffs.memory
                    ),
                ),
            )
            if (fiber_cutoff := cutoffs.fiber) is not None:
                await _table(
                    db,
                    counts,
                    "fibers",
                    lambda: _rowcount(
                        db,
                        delete(Fiber).where(
                            Fiber.status.in_(sorted(TERMINAL_FIBER_STATUSES)), Fiber.updated_at < fiber_cutoff
                        ),
                    ),
                )
                state = func.coalesce(A2ATask.status_json["state"].astext, "")
                await _table(
                    db,
                    counts,
                    "a2a_tasks",
                    lambda: _rowcount(
                        db,
                        delete(A2ATask).where(
                            state.not_in(sorted(LIVE_A2A_STATES)), A2ATask.updated_at < fiber_cutoff
                        ),
                    ),
                )
            if (usage_cutoff := cutoffs.usage) is not None:
                await _table(
                    db,
                    counts,
                    "usage_events",
                    lambda: _rowcount(db, delete(UsageEvent).where(UsageEvent.ts < usage_cutoff)),
                )
            if (session_cutoff := cutoffs.session) is not None:
                await _table(db, counts, "session_events", lambda: _delete_idle_threads(db, session_cutoff))
    return counts


async def _table(db: Any, counts: dict[str, int], table: str, run: Callable[[], Awaitable[int]]) -> None:
    """One table, one transaction. A failure (a lock, a statement timeout) is logged and
    rolled back, and the sweep moves on: retention must not stop everywhere because one
    table's delete could not complete, and the counts line must still say what happened."""
    try:
        counts[table] = await run()
        await db.commit()
    except Exception:
        await db.rollback()
        logger.warning("retention: %s sweep failed; other tables continue", table, exc_info=True)


async def _rowcount(db: Any, stmt: Any) -> int:
    return int((await db.execute(stmt)).rowcount or 0)


async def _delete_audit(db: Any, cutoffs: Cutoffs, per_manifest: ManifestCutoffs) -> int:
    deleted = 0
    if cutoffs.audit is not None:
        deleted += await _rowcount(db, delete(AuditEvent).where(AuditEvent.ts < cutoffs.audit))
    for (tenant_id, manifest_id), cutoff in per_manifest.items():
        stmt = delete(AuditEvent).where(
            AuditEvent.tenant_id == tenant_id, AuditEvent.manifest_id == manifest_id, AuditEvent.ts < cutoff
        )
        deleted += await _rowcount(db, stmt)
    return deleted


async def _delete_idle_threads(db: Any, cutoff_ms: int) -> int:
    """Whole threads whose last event predates the cutoff, with their `thread_state` row.

    Set-wise, so a first sweep over years of threads is one statement rather than a bound
    parameter per thread (Postgres caps a statement at 65535). A thread whose metadata was
    written after the cutoff is kept whatever its events say — see `_sweep_memory_sessions`
    for why (forks copy timestamps). The Postgres `thread_state.updated_at` is epoch seconds.
    """
    cutoff_s = cutoff_ms / 1000
    idle = (
        select(SessionEventRow.tenant_id, SessionEventRow.thread_id)
        .group_by(SessionEventRow.tenant_id, SessionEventRow.thread_id)
        .having(func.max(SessionEventRow.ts) < cutoff_s)
    )
    touched = select(ThreadState.tenant_id, ThreadState.thread_id).where(ThreadState.updated_at >= cutoff_s)
    doomed = idle.except_(touched).subquery()
    doomed_keys = select(doomed.c.tenant_id, doomed.c.thread_id)
    # Metadata first: once the events are gone the thread no longer qualifies as idle.
    await db.execute(
        delete(ThreadState).where(tuple_(ThreadState.tenant_id, ThreadState.thread_id).in_(doomed_keys))
    )
    return await _rowcount(
        db,
        delete(SessionEventRow).where(
            tuple_(SessionEventRow.tenant_id, SessionEventRow.thread_id).in_(doomed_keys)
        ),
    )


__all__ = [
    "LIVE_A2A_STATES",
    "TERMINAL_FIBER_STATUSES",
    "Cutoffs",
    "ManifestCutoffs",
    "manifest_audit_cutoffs",
    "run_retention_sweep",
]
