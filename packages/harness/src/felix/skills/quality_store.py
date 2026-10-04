"""The skill library's quality loop, at the storage layer: what the feedback and evaluation stores
share, the `skill_policy` store, and the sweep lease. The feedback rows are `feedback_store.py`;
the evaluation rows are `eval_store.py`.

Data access only. What feedback may become and who may decide it is `skills/feedback.py`; what an
improvement and an evaluation do is `skills/improve.py` and `skills/evaluate.py`. This module
enforces the things only storage can:

- a decision lands only on feedback still `pending`, so an accept racing a reject cannot both win;
- an agent's pending-feedback cap is exact: the count and the insert share one transaction,
  under an advisory lock per `(tenant, author)` (`feedback_lock_key`);
- a version has at most one evaluation queued or running (a partial unique index on Postgres);
- a job is run by one worker at a time. `claim_next` takes one row under `FOR UPDATE SKIP
  LOCKED` on Postgres, and in one step with no await in the twin, and hands the claimer a
  `claim_token`. `heartbeat` and `finish` land only while the row still carries that token, so a
  worker whose lease lapsed and was taken over finds out at its next heartbeat and cannot write
  over the worker that took it;
- a job is claimed at most `MAX_ATTEMPTS` times. The claim after that fails it
  (`attempts_exhausted`), so a job that kills its worker every time stops being retried;
- claims are fair across tenants: `claim_next` takes the oldest due job of the tenant whose last
  claim is oldest, so a tenant with a deep queue cannot starve one with a single job. The
  candidates are cut to `_TENANTS_SCANNED` only after that ordering, so a tenant whose id sorts
  late is not left out of every scan;
- one sweep runs at a time: a `skill_job_lease` row taken, renewed and released by token, one
  statement per transaction, so it holds behind a transaction-mode pooler;
- a tenant's job caps are exact (`JobCaps`): queueing an evaluation and accepting feedback with
  `improve` each count both tables and write their row in one transaction, under one
  transaction-scoped advisory lock per tenant (`hold_job_caps`), so two requests at the cap
  cannot both see room. `pg_advisory_xact_lock` is released at commit or rollback, so like the
  lease it holds behind a transaction-mode pooler. The twins count and write with no await
  between (`memory_job_caps`), which is the same thing in one event loop.

A claim whose heartbeat is older than `CLAIM_LEASE_MS` belonged to a dead worker and is taken
again. Every listing ends on the primary key (`id`), so the two arms agree on where a page ends.
"""

from __future__ import annotations

import copy
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast, runtime_checkable

from felix.config import Settings

logger = logging.getLogger("felix.skills.quality_store")

FeedbackStatus = Literal["pending", "accepted", "rejected", "applied", "failed"]
FeedbackSource = Literal["human", "agent"]
EvalStatus = Literal["queued", "running", "succeeded", "failed"]
ScenarioSource = Literal["bundle", "generated", "default"]

# How long a claim stays the claimer's without a heartbeat. A running job heartbeats between
# model calls, so this only has to outlast the slowest single step.
CLAIM_LEASE_MS = 10 * 60 * 1000
# Claims one job gets. The claim after the last fails it with `attempts_exhausted`.
MAX_ATTEMPTS = 3
EXHAUSTED = "attempts_exhausted"
MAX_LISTED = 100
# Tenants one fair claim compares at most -- each tenant's oldest due job is a candidate, and
# the cut is taken after ordering them by last claim.
_TENANTS_SCANNED = 500

# A claim that failed an exhausted job scans again, at most this many times in one call.
MAX_RESCANS = 16
RESCAN = object()

# Where a listing resumes: the `(created_at, id)` of the last row a page held.
Cursor = tuple[int, str]


class SkillFeedbackConflict(Exception):
    """The feedback is not in the state the change was decided against, or does not exist."""


class SkillFeedbackAtCap(Exception):
    """An agent's manifest already holds ``held`` pending feedback, and ``limit`` is its cap."""

    def __init__(self, held: int, limit: int) -> None:
        super().__init__(f"{held} of {limit}")
        self.held, self.limit = held, limit


class SkillEvalInFlight(Exception):
    """The version already has an evaluation queued or running."""


class SkillJobsAtCap(Exception):
    """The tenant already holds as many skill jobs as ``what`` allows: ``queued`` (queued or
    running now) or ``daily`` (created since the start of the UTC day)."""

    def __init__(self, what: Literal["queued", "daily"], count: int, limit: int) -> None:
        super().__init__(f"{what}: {count} of {limit}")
        self.what, self.count, self.limit = what, count, limit


@dataclass(slots=True, frozen=True)
class JobCaps:
    """A tenant's job caps, handed to the write that adds a job so the store can check them in
    the same transaction as the insert. Limits rather than a check callable: the count has to
    run on the store's own session, under its lock, and a callable would need that session
    handed out to code that does not own it. ``since`` is the start of the UTC day."""

    max_queued: int
    daily_limit: int
    since: int

    def refuse_past(self, waiting: int, today: int) -> None:
        if waiting >= self.max_queued:
            raise SkillJobsAtCap("queued", waiting, self.max_queued)
        if today >= self.daily_limit:
            raise SkillJobsAtCap("daily", today, self.daily_limit)


def feedback_lock_key(tenant_id: str, author: str) -> str:
    """The advisory lock an agent's capped feedback insert takes: one per manifest."""
    return f"skill_feedback:{tenant_id}:{author}"


async def xact_lock(db: Any, key: str) -> None:
    """A transaction-scoped advisory lock on ``key``, released at commit or rollback -- never a
    session lock, which a transaction-mode pooler would leave on whichever server session it
    landed on."""
    from sqlalchemy import text

    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": key})


def job_lock_key(tenant_id: str) -> str:
    """The advisory lock every write that adds a tenant's skill job takes, whichever table."""
    return f"skill_jobs:{tenant_id}"


async def hold_job_caps(db: Any, tenant_id: str, caps: JobCaps) -> None:
    """Take the tenant's job lock in ``db``'s transaction, then count its jobs in both tables.

    Raises `SkillJobsAtCap` past either cap. The lock lasts until the caller commits or rolls
    back, so the row the caller then writes is counted by the next writer; the evaluation and
    the feedback stores both come through here, which is what makes the cap one cap across
    both tables.
    """
    from sqlalchemy import func, select

    from felix.db.models import SkillEvalRow as E
    from felix.db.models import SkillFeedbackRow as F

    await xact_lock(db, job_lock_key(tenant_id))
    f_waiting, f_today = (
        await db.execute(
            select(
                func.count().filter(F.status == "accepted", F.improve.is_(True)),
                func.count().filter(F.improve.is_(True), F.decided_at >= caps.since),
            ).where(F.tenant_id == tenant_id)
        )
    ).one()
    e_waiting, e_today = (
        await db.execute(
            select(
                func.count().filter(E.status.in_(("queued", "running"))),
                func.count().filter(E.created_at >= caps.since),
            ).where(E.tenant_id == tenant_id)
        )
    ).one()
    caps.refuse_past(int(f_waiting) + int(e_waiting), int(f_today) + int(e_today))


def memory_job_caps(tenant_id: str, caps: JobCaps, *, feedback: Any = None, evals: Any = None) -> None:
    """`hold_job_caps` for the twins: synchronous, so a caller that writes its row straight
    after, with no await between, holds the event loop from the count to the write. A store
    passes itself for its own table; the other is the module's twin."""
    from felix.skills import eval_store, feedback_store

    feedback = feedback if feedback is not None else feedback_store.memory_store()
    evals = evals if evals is not None else eval_store.memory_store()
    caps.refuse_past(
        feedback.jobs_in_flight(tenant_id) + evals.jobs_in_flight(tenant_id),
        feedback.jobs_since(tenant_id, caps.since) + evals.jobs_since(tenant_id, caps.since),
    )


@runtime_checkable
class SkillPolicyStore(Protocol):
    async def get(self, tenant_id: str) -> dict[str, Any] | None: ...

    async def put(self, tenant_id: str, row: dict[str, Any]) -> dict[str, Any]: ...

    async def delete(self, tenant_id: str) -> bool: ...


# Every column a caller may leave out, at the value Postgres would give it, so a row read back
# from the twin has the same keys as one read back from the table.
CLAIM_DEFAULTS: dict[str, Any] = {"claim_token": None, "heartbeat_at": None, "attempts": 0}


def lapsed(row: dict[str, Any], now: int) -> bool:
    beat = row.get("heartbeat_at")
    return beat is None or beat <= now - CLAIM_LEASE_MS


def row_key(row: dict[str, Any]) -> Cursor:
    return (int(row["created_at"]), str(row["id"]))


def fair_order(
    rows: Iterable[dict[str, Any]], due: Callable[[dict[str, Any]], bool], claimed: str
) -> list[dict[str, Any]]:
    """Each tenant's oldest due row, the tenant whose last claim (``claimed`` column) is oldest
    first; a tenant never claimed from goes before any that has been."""
    rows = list(rows)
    heads: dict[str, dict[str, Any]] = {}
    last: dict[str, int] = {}
    for r in rows:
        t = r["tenant_id"]
        if r.get(claimed) is not None:
            last[t] = max(last.get(t, -1), int(r[claimed]))
        if due(r) and (t not in heads or row_key(r) < row_key(heads[t])):
            heads[t] = r
    return sorted(heads.values(), key=lambda r: (last.get(r["tenant_id"], -1), r["created_at"], r["id"]))


def new_token() -> str:
    return uuid.uuid4().hex


class InMemorySkillPolicyStore:
    def __init__(self) -> None:
        self._rows: dict[str, dict[str, Any]] = {}

    def clear(self) -> None:
        self._rows.clear()

    async def get(self, tenant_id: str) -> dict[str, Any] | None:
        row = self._rows.get(tenant_id)
        return copy.deepcopy(row) if row is not None else None

    async def put(self, tenant_id: str, row: dict[str, Any]) -> dict[str, Any]:
        stored = {**copy.deepcopy(row), "tenant_id": tenant_id}
        self._rows[tenant_id] = stored
        return copy.deepcopy(stored)

    async def delete(self, tenant_id: str) -> bool:
        return self._rows.pop(tenant_id, None) is not None


# -- Postgres --------------------------------------------------------------------------------


class Postgres:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _session(self, tenant_id: str) -> Any:
        from felix.db.session import tenant_session

        return tenant_session(self._settings, tenant_id)

    @asynccontextmanager
    async def _sweep(self) -> AsyncIterator[Any]:
        """A session for cross-tenant maintenance, like the fiber sweep: under `rls_bypass`, or
        RLS with no tenant bound would silently return nothing and no job would ever run."""
        from felix.db.session import get_session_factory, rls_bypass

        with rls_bypass():
            async with get_session_factory(settings=self._settings)() as db:
                yield db

    @staticmethod
    def _row(row: Any) -> dict[str, Any]:
        return {c.key: getattr(row, c.key) for c in row.__table__.columns}

    async def _claim_fairly(
        self,
        db: Any,
        model: Any,
        due: Any,
        claimed: Any,
        take: Callable[[Any], None],
        exhaust: Callable[[Any], None],
    ) -> dict[str, Any] | None:
        """`fair_order` in SQL: each tenant's oldest due row (`DISTINCT ON`), ordered by that
        tenant's last claim, then lock the first candidate no other worker holds. A
        candidate out of attempts is failed and the scan starts again, so that tenant's next job
        is a candidate in the same call."""
        for _ in range(MAX_RESCANS):
            out = await self._claim_once(db, model, due, claimed, take, exhaust)
            if out is not RESCAN:
                return cast(dict[str, Any] | None, out)
        return None

    async def _claim_once(
        self,
        db: Any,
        model: Any,
        due: Any,
        claimed: Any,
        take: Callable[[Any], None],
        exhaust: Callable[[Any], None],
    ) -> object:
        from sqlalchemy import collate, func, select
        from sqlalchemy.orm import aliased

        heads = (
            select(model.tenant_id, model.id, model.created_at)
            .where(due)
            .distinct(model.tenant_id)
            .order_by(model.tenant_id, model.created_at, collate(model.id, "C"))
            .subquery()
        )
        # Each candidate tenant's last claim, across all its rows. Ordered on *before* the cut:
        # cutting the `DISTINCT ON` (which sorts by tenant id) first would hand every scan to the
        # same `_TENANTS_SCANNED` tenants and never reach one whose id sorts after them.
        other = aliased(model)
        last = (
            select(func.max(getattr(other, claimed.key)))
            .where(other.tenant_id == heads.c.tenant_id)
            .scalar_subquery()
        )
        candidates = (
            await db.execute(
                select(heads.c.tenant_id, heads.c.id)
                .order_by(
                    func.coalesce(last, -1),
                    heads.c.created_at,
                    collate(heads.c.id, "C"),
                    collate(heads.c.tenant_id, "C"),
                )
                .limit(_TENANTS_SCANNED)
            )
        ).all()
        if not candidates:
            await db.commit()
            return None
        for tenant_id, row_id in candidates:
            row = await db.scalar(
                select(model)
                .where(model.tenant_id == tenant_id, model.id == row_id, due)
                .with_for_update(skip_locked=True)
            )
            if row is None:
                continue
            if row.attempts >= MAX_ATTEMPTS:
                exhaust(row)
                await db.commit()
                return RESCAN
            take(row)
            out = self._row(row)
            await db.commit()
            return out
        await db.commit()
        return None


class PostgresSkillPolicyStore(Postgres):
    async def get(self, tenant_id: str) -> dict[str, Any] | None:
        from felix.db.models import SkillPolicyRow

        async with self._session(tenant_id) as db:
            row = await db.get(SkillPolicyRow, tenant_id)
            return self._row(row) if row is not None else None

    async def put(self, tenant_id: str, row: dict[str, Any]) -> dict[str, Any]:
        from typing import cast

        from sqlalchemy.dialects.postgresql import insert as pg_insert

        from felix.db.models import SkillPolicyRow

        values = {**row, "tenant_id": tenant_id}
        stmt = pg_insert(cast(Any, SkillPolicyRow.__table__)).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["tenant_id"], set_={k: v for k, v in values.items() if k != "tenant_id"}
        )
        async with self._session(tenant_id) as db:
            await db.execute(stmt)
            await db.commit()
            stored = await db.get(SkillPolicyRow, tenant_id, populate_existing=True)
            return self._row(stored) if stored is not None else values

    async def delete(self, tenant_id: str) -> bool:
        from sqlalchemy import delete

        from felix.db.models import SkillPolicyRow

        async with self._session(tenant_id) as db:
            gone = await db.execute(delete(SkillPolicyRow).where(SkillPolicyRow.tenant_id == tenant_id))
            await db.commit()
            return bool(getattr(gone, "rowcount", 0))


_memory_policy = InMemorySkillPolicyStore()


def postgres_settings(settings: Settings | None) -> Settings | None:
    """The settings to reach Postgres with, or None under `memory://`. Selected exactly as
    `library_store.get_skill_library_store` selects."""
    if settings is None:
        return None
    url = settings.database_url
    return None if ":memory:" in url or "sqlite" in url or url.startswith("memory://") else settings


def get_skill_policy_store(settings: Settings | None = None) -> SkillPolicyStore:
    pg = postgres_settings(settings)
    return _memory_policy if pg is None else PostgresSkillPolicyStore(pg)


# -- the sweep lease -------------------------------------------------------------------------

# The one `skill_job_lease` row every `skill_jobs` sweep contends for.
SWEEP_LEASE = "skill_jobs"
# What a sweep's lease outlasts one job's deadline by. The sweep renews before every claim, so
# the longest a live holder goes unrenewed is one job: its model calls are cut off at
# `FELIX_SKILL_JOB_DEADLINE_SECONDS`, and this margin covers the reads and writes around them. A
# worker that dies holding the lease frees it within one deadline plus this.
SWEEP_LEASE_MARGIN_MS = 5 * 60 * 1000

now_ms = lambda: int(time.time() * 1000)


def sweep_lease_ms(settings: Settings | None) -> int:
    deadline = (
        settings.skill_job_deadline_seconds
        if settings is not None
        else Settings.model_fields["skill_job_deadline_seconds"].default
    )
    return int(deadline) * 1000 + SWEEP_LEASE_MARGIN_MS


@runtime_checkable
class SweepLeaseStore(Protocol):
    """The sweep's lease. Each call is one statement in a transaction of its own, so it holds
    behind a transaction-mode pooler, where consecutive statements reach different server
    sessions and nothing tied to a session (an advisory lock) survives between them."""

    async def acquire(self, token: str, *, now: int, lease_ms: int) -> bool:
        """Take the lease for ``token`` until ``now + lease_ms``: when nobody holds it, its
        holder's lease lapsed, or ``token`` already holds it."""
        ...

    async def renew(self, token: str, *, now: int, lease_ms: int) -> bool:
        """Extend it to ``now + lease_ms``, only while ``token`` is still the holder."""
        ...

    async def release(self, token: str) -> bool:
        """Lapse it at once, only while ``token`` is still the holder."""
        ...


class InMemorySweepLease:
    """One process, no awaits between the check and the write: each call is atomic."""

    def __init__(self) -> None:
        self._rows: dict[str, tuple[str, int]] = {}

    def clear(self) -> None:
        self._rows.clear()

    async def acquire(self, token: str, *, now: int, lease_ms: int) -> bool:
        row = self._rows.get(SWEEP_LEASE)
        if row is not None and row[0] != token and row[1] >= now:
            return False
        self._rows[SWEEP_LEASE] = (token, now + lease_ms)
        return True

    async def renew(self, token: str, *, now: int, lease_ms: int) -> bool:
        return self._set(token, now + lease_ms)

    async def release(self, token: str) -> bool:
        return self._set(token, 0)

    def _set(self, token: str, until: int) -> bool:
        row = self._rows.get(SWEEP_LEASE)
        if row is None or row[0] != token:
            return False
        self._rows[SWEEP_LEASE] = (token, until)
        return True


class PostgresSweepLease:
    """The `skill_job_lease` row (`0024`). No tenant and no RLS: one row is the whole sweep."""

    def __init__(self, settings: Settings) -> None:
        self._url = settings.database_url

    async def acquire(self, token: str, *, now: int, lease_ms: int) -> bool:
        # Two acquirers racing on an absent row: one inserts, the other conflicts, waits on that
        # row's lock, and re-checks the WHERE against the winner's lease -- which refuses it.
        return await self._one(
            "INSERT INTO skill_job_lease (name, holder, until_ms) VALUES (:n, :t, :until) "
            "ON CONFLICT (name) DO UPDATE SET holder = EXCLUDED.holder, until_ms = EXCLUDED.until_ms "
            "WHERE skill_job_lease.until_ms < :now OR skill_job_lease.holder = :t "
            "RETURNING holder",
            {"n": SWEEP_LEASE, "t": token, "until": now + lease_ms, "now": now},
        )

    async def renew(self, token: str, *, now: int, lease_ms: int) -> bool:
        return await self._set(token, now + lease_ms)

    async def release(self, token: str) -> bool:
        return await self._set(token, 0)

    async def _set(self, token: str, until: int) -> bool:
        return await self._one(
            "UPDATE skill_job_lease SET until_ms = :until WHERE name = :n AND holder = :t RETURNING holder",
            {"n": SWEEP_LEASE, "t": token, "until": until},
        )

    async def _one(self, sql: str, params: dict[str, Any]) -> bool:
        from sqlalchemy import text

        from felix.db.session import get_engine

        async with get_engine(self._url).begin() as conn:
            return (await conn.execute(text(sql), params)).first() is not None


_memory_lease = InMemorySweepLease()


def get_sweep_lease_store(settings: Settings | None = None) -> SweepLeaseStore:
    pg = postgres_settings(settings)
    return _memory_lease if pg is None else PostgresSweepLease(pg)


@dataclass(slots=True)
class SweepLease:
    """A held lease: the sweep renews it before every job and stops when a renewal is refused."""

    store: SweepLeaseStore
    token: str
    lease_ms: int

    async def renew(self) -> bool:
        return await self.store.renew(self.token, now=now_ms(), lease_ms=self.lease_ms)


@asynccontextmanager
async def sweep_lock(settings: Settings | None) -> AsyncIterator[SweepLease | None]:
    """The one `skill_jobs` sweep slot for the duration, or None, without waiting, when another
    sweep holds it -- across every worker process on Postgres, within this process under
    `memory://`. The cron fires every minute whether or not the last sweep finished; without
    this, overlapping sweeps multiply the model calls in flight.

    A lease rather than a lock because it bounds concurrency, not correctness: two sweeps that
    overlap after a lease lapsed still never run one job twice (`claim_next` hands each job to
    one claimer), they only run more model calls at once than one sweep would.
    """
    store = get_sweep_lease_store(settings)
    lease = SweepLease(store, new_token(), sweep_lease_ms(settings))
    if not await store.acquire(lease.token, now=now_ms(), lease_ms=lease.lease_ms):
        yield None
        return
    try:
        yield lease
    finally:
        try:
            await store.release(lease.token)
        except Exception:
            # Not raised over whatever ended the sweep: an unreleased lease lapses on its own.
            logger.warning("skill_jobs: releasing the sweep lease failed", exc_info=True)


def clear_memory() -> None:
    """Drop every in-memory row of the quality loop. Test seam, matching the other `memory://`
    stores; the feedback and evaluation twins live in their own modules."""
    from felix.skills import eval_store, feedback_store, sighting_store

    feedback_store.clear_memory()
    # Import sightings gate the cooldown: one test's sighting would be another's early eligibility.
    sighting_store.clear_memory()
    eval_store.clear_memory()
    _memory_policy.clear()
    _memory_lease.clear()


__all__ = [
    "CLAIM_DEFAULTS",
    "CLAIM_LEASE_MS",
    "EXHAUSTED",
    "MAX_ATTEMPTS",
    "MAX_LISTED",
    "MAX_RESCANS",
    "RESCAN",
    "SWEEP_LEASE",
    "SWEEP_LEASE_MARGIN_MS",
    "Cursor",
    "EvalStatus",
    "FeedbackSource",
    "FeedbackStatus",
    "InMemorySkillPolicyStore",
    "InMemorySweepLease",
    "JobCaps",
    "Postgres",
    "PostgresSkillPolicyStore",
    "PostgresSweepLease",
    "ScenarioSource",
    "SkillEvalInFlight",
    "SkillFeedbackAtCap",
    "SkillFeedbackConflict",
    "SkillJobsAtCap",
    "SkillPolicyStore",
    "SweepLease",
    "SweepLeaseStore",
    "clear_memory",
    "fair_order",
    "feedback_lock_key",
    "get_skill_policy_store",
    "get_sweep_lease_store",
    "hold_job_caps",
    "job_lock_key",
    "lapsed",
    "memory_job_caps",
    "new_token",
    "postgres_settings",
    "row_key",
    "sweep_lease_ms",
    "sweep_lock",
    "xact_lock",
]
