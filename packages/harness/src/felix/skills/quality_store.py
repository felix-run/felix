"""The skill library's quality loop, at the storage layer: what the feedback and evaluation stores
share, the `skill_policy` store, and the sweep lock. The feedback rows are `feedback_store.py`;
the evaluation rows are `eval_store.py`.

Data access only. What feedback may become and who may decide it is `skills/feedback.py`; what an
improvement and an evaluation do is `skills/improve.py` and `skills/evaluate.py`. This module
enforces the things only storage can:

- a decision lands only on feedback still `pending`, so an accept racing a reject cannot both win;
- a version has at most one evaluation queued or running (a partial unique index on Postgres);
- a job is run by one worker at a time. `claim_next` takes one row under `FOR UPDATE SKIP
  LOCKED` on Postgres, and in one step with no await in the twin, and hands the claimer a
  `claim_token`. `heartbeat` and `finish` land only while the row still carries that token, so a
  worker whose lease lapsed and was taken over finds out at its next heartbeat and cannot write
  over the worker that took it;
- a job is claimed at most `MAX_ATTEMPTS` times. The claim after that fails it
  (`attempts_exhausted`), so a job that kills its worker every time stops being retried;
- claims are fair across tenants: `claim_next` takes the oldest due job of the tenant whose last
  claim is oldest, so a tenant with a deep queue cannot starve one with a single job.

A claim whose heartbeat is older than `CLAIM_LEASE_MS` belonged to a dead worker and is taken
again. Every listing ends on the primary key (`id`), so the two arms agree on where a page ends.
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from typing import Any, Literal, Protocol, cast, runtime_checkable

from felix.config import Settings

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
# Tenants one fair claim compares at most -- each tenant's oldest due job is a candidate.
_TENANTS_SCANNED = 500

# A claim that failed an exhausted job scans again, at most this many times in one call.
MAX_RESCANS = 16
RESCAN = object()

# Where a listing resumes: the `(created_at, id)` of the last row a page held.
Cursor = tuple[int, str]


class SkillFeedbackConflict(Exception):
    """The feedback is not in the state the change was decided against, or does not exist."""


class SkillEvalInFlight(Exception):
    """The version already has an evaluation queued or running."""


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
        """`fair_order` in SQL: each tenant's oldest due row (`DISTINCT ON`), then each of
        those tenants' last claim, then lock the first candidate no other worker holds. A
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

        heads = (
            await db.execute(
                select(model.tenant_id, model.id, model.created_at)
                .where(due)
                .distinct(model.tenant_id)
                .order_by(model.tenant_id, model.created_at, collate(model.id, "C"))
                .limit(_TENANTS_SCANNED)
            )
        ).all()
        if not heads:
            await db.commit()
            return None
        tenants = [h[0] for h in heads]
        last = {
            t: at
            for t, at in (
                await db.execute(
                    select(model.tenant_id, func.max(claimed))
                    .where(model.tenant_id.in_(tenants))
                    .group_by(model.tenant_id)
                )
            ).all()
        }
        for tenant_id, row_id, _ in sorted(heads, key=lambda h: (last.get(h[0]) or -1, h[2], h[1])):
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
# The twin's sweep lock: one process, so one flag is the whole lock.
_memory_sweep = {"held": False}
# The Postgres advisory lock key every `skill_jobs` sweep contends for.
_SWEEP_LOCK_KEY = 7_046_211_901


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


@asynccontextmanager
async def sweep_lock(settings: Settings | None) -> AsyncIterator[bool]:
    """Whether this caller holds the one `skill_jobs` sweep slot, for the duration.

    Yields False, without waiting, when another sweep holds it -- across every worker process on
    Postgres (a session advisory lock, held on a connection of its own for the sweep and released
    with it), within this process under `memory://`. The cron fires every minute whether or not
    the last sweep finished; without this, overlapping sweeps multiply the model calls in flight.
    """
    pg = postgres_settings(settings)
    if pg is None:
        if _memory_sweep["held"]:
            yield False
            return
        _memory_sweep["held"] = True
        try:
            yield True
        finally:
            _memory_sweep["held"] = False
        return
    from sqlalchemy import text

    from felix.db.session import get_engine

    async with get_engine(pg.database_url).connect() as conn:
        held = bool(await conn.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": _SWEEP_LOCK_KEY}))
        await conn.commit()
        try:
            yield held
        finally:
            if held:
                await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _SWEEP_LOCK_KEY})
                await conn.commit()


def clear_memory() -> None:
    """Drop every in-memory row of the quality loop. Test seam, matching the other `memory://`
    stores; the feedback and evaluation twins live in their own modules."""
    from felix.skills import eval_store, feedback_store

    feedback_store.clear_memory()
    eval_store.clear_memory()
    _memory_policy.clear()
    _memory_sweep["held"] = False


__all__ = [
    "CLAIM_DEFAULTS",
    "CLAIM_LEASE_MS",
    "EXHAUSTED",
    "MAX_ATTEMPTS",
    "MAX_LISTED",
    "MAX_RESCANS",
    "RESCAN",
    "Cursor",
    "EvalStatus",
    "FeedbackSource",
    "FeedbackStatus",
    "InMemorySkillPolicyStore",
    "Postgres",
    "PostgresSkillPolicyStore",
    "ScenarioSource",
    "SkillEvalInFlight",
    "SkillFeedbackConflict",
    "SkillPolicyStore",
    "clear_memory",
    "fair_order",
    "get_skill_policy_store",
    "lapsed",
    "new_token",
    "postgres_settings",
    "row_key",
    "sweep_lock",
]
