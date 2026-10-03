"""Rows for `skill_feedback`: feedback on a library skill, and the improvement job an accept starts.

Data access only; `skills/feedback.py` decides what feedback may become and `skills/improve.py`
runs the job. The claim, heartbeat and fairness rules are `quality_store`'s, shared with
`eval_store`.
"""

from __future__ import annotations

import copy
from typing import Any, Literal, Protocol, runtime_checkable

from felix.config import Settings
from felix.skills.quality_store import (
    CLAIM_DEFAULTS,
    CLAIM_LEASE_MS,
    EXHAUSTED,
    MAX_ATTEMPTS,
    MAX_LISTED,
    MAX_RESCANS,
    Cursor,
    JobCaps,
    Postgres,
    SkillFeedbackAtCap,
    SkillFeedbackConflict,
    fair_order,
    feedback_lock_key,
    hold_job_caps,
    lapsed,
    memory_job_caps,
    new_token,
    postgres_settings,
    row_key,
    xact_lock,
)

_FEEDBACK_DEFAULTS: dict[str, Any] = {
    "author": "",
    "principal": None,
    "suggested_patch": None,
    "improve": False,
    "result_version": None,
    "model": None,
    "error": None,
    "claimed_at": None,
    "decided_at": None,
    "decided_by": None,
    "decision_note": None,
    **CLAIM_DEFAULTS,
}


def _improvement_due(row: dict[str, Any], now: int) -> bool:
    if row["status"] != "accepted" or not row.get("improve"):
        return False
    return row.get("claim_token") is None or lapsed(row, now)


@runtime_checkable
class SkillFeedbackStore(Protocol):
    async def insert(
        self, tenant_id: str, row: dict[str, Any], *, max_pending: int | None = None
    ) -> dict[str, Any]:
        """File feedback. With ``max_pending`` it is refused (`SkillFeedbackAtCap`) when the
        row's author already holds that much pending agent feedback, counted in the same
        transaction as the insert."""
        ...

    async def get(self, tenant_id: str, row_id: str) -> dict[str, Any] | None: ...

    async def list_for_skill(
        self,
        tenant_id: str,
        name: str,
        *,
        status: str | None = None,
        limit: int = MAX_LISTED,
        before: Cursor | None = None,
    ) -> list[dict[str, Any]]: ...

    async def list_by_status(
        self, tenant_id: str, status: str, *, limit: int = MAX_LISTED, after: Cursor | None = None
    ) -> list[dict[str, Any]]: ...

    async def count_pending_agent(self, tenant_id: str, author: str) -> int: ...

    async def decide(
        self,
        tenant_id: str,
        row_id: str,
        *,
        status: Literal["accepted", "rejected"],
        improve: bool,
        by: str,
        note: str | None,
        at: int,
        caps: JobCaps | None = None,
    ) -> dict[str, Any]:
        """Decide pending feedback. An accept with ``improve`` starts a job, so with ``caps``
        it is refused (`SkillJobsAtCap`) unless the tenant has room, counted in the same
        transaction as the decision (`quality_store.hold_job_caps`)."""
        ...

    async def count_jobs_in_flight(self, tenant_id: str) -> int: ...

    async def count_jobs_since(self, tenant_id: str, since: int) -> int: ...

    async def claim_next(self, *, now: int) -> dict[str, Any] | None: ...

    async def heartbeat(self, tenant_id: str, row_id: str, *, token: str, now: int) -> bool: ...

    async def finish(
        self,
        tenant_id: str,
        row_id: str,
        *,
        token: str,
        status: Literal["applied", "failed"],
        result_version: str | None,
        model: str | None,
        error: str | None,
    ) -> bool: ...


class InMemorySkillFeedbackStore:
    """The `memory://` twin. Copies on the way in and out, as a row read from Postgres is."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], dict[str, Any]] = {}

    def clear(self) -> None:
        self._rows.clear()

    async def insert(
        self, tenant_id: str, row: dict[str, Any], *, max_pending: int | None = None
    ) -> dict[str, Any]:
        # No await from the count to the write, as `decide`.
        if max_pending is not None and (held := self.pending_agent(tenant_id, row["author"])) >= max_pending:
            raise SkillFeedbackAtCap(held, max_pending)
        stored = {**_FEEDBACK_DEFAULTS, **copy.deepcopy(row), "tenant_id": tenant_id}
        self._rows[(tenant_id, row["id"])] = stored
        return copy.deepcopy(stored)

    async def get(self, tenant_id: str, row_id: str) -> dict[str, Any] | None:
        row = self._rows.get((tenant_id, row_id))
        return copy.deepcopy(row) if row is not None else None

    async def list_for_skill(
        self,
        tenant_id: str,
        name: str,
        *,
        status: str | None = None,
        limit: int = MAX_LISTED,
        before: Cursor | None = None,
    ) -> list[dict[str, Any]]:
        rows = [
            r
            for (t, _), r in self._rows.items()
            if t == tenant_id
            and r["name"] == name
            and (status is None or r["status"] == status)
            and (before is None or row_key(r) < before)
        ]
        # Newest first, ending on the id so two filed in one millisecond keep one order.
        rows.sort(key=lambda r: (r["created_at"], r["id"]), reverse=True)
        return copy.deepcopy(rows[:limit])

    async def list_by_status(
        self, tenant_id: str, status: str, *, limit: int = MAX_LISTED, after: Cursor | None = None
    ) -> list[dict[str, Any]]:
        rows = [
            r
            for (t, _), r in self._rows.items()
            if t == tenant_id and r["status"] == status and (after is None or row_key(r) > after)
        ]
        rows.sort(key=lambda r: (r["created_at"], r["id"]))
        return copy.deepcopy(rows[:limit])

    async def count_pending_agent(self, tenant_id: str, author: str) -> int:
        return self.pending_agent(tenant_id, author)

    def pending_agent(self, tenant_id: str, author: str) -> int:
        return sum(
            1
            for (t, _), r in self._rows.items()
            if t == tenant_id
            and r["status"] == "pending"
            and r["source"] == "agent"
            and r["author"] == author
        )

    async def decide(
        self,
        tenant_id: str,
        row_id: str,
        *,
        status: Literal["accepted", "rejected"],
        improve: bool,
        by: str,
        note: str | None,
        at: int,
        caps: JobCaps | None = None,
    ) -> dict[str, Any]:
        # No await from the count to the write: one event loop cannot interleave another
        # decision between them.
        if improve and caps is not None:
            memory_job_caps(tenant_id, caps, feedback=self)
        row = self._rows.get((tenant_id, row_id))
        if row is None or row["status"] != "pending":
            raise SkillFeedbackConflict(row_id)
        row.update(status=status, improve=improve, decided_by=by, decision_note=note, decided_at=at)
        return copy.deepcopy(row)

    def jobs_in_flight(self, tenant_id: str) -> int:
        return sum(
            1
            for (t, _), r in self._rows.items()
            if t == tenant_id and r["status"] == "accepted" and r["improve"]
        )

    def jobs_since(self, tenant_id: str, since: int) -> int:
        return sum(
            1
            for (t, _), r in self._rows.items()
            if t == tenant_id and r["improve"] and (r.get("decided_at") or 0) >= since
        )

    async def count_jobs_in_flight(self, tenant_id: str) -> int:
        return self.jobs_in_flight(tenant_id)

    async def count_jobs_since(self, tenant_id: str, since: int) -> int:
        return self.jobs_since(tenant_id, since)

    async def claim_next(self, *, now: int) -> dict[str, Any] | None:
        for _ in range(MAX_RESCANS):
            order = fair_order(self._rows.values(), lambda r: _improvement_due(r, now), "claimed_at")
            if not order:
                return None
            row = order[0]
            if row["attempts"] >= MAX_ATTEMPTS:
                # Failed, then scanned again: this tenant's next job is a candidate now.
                row.update(status="failed", error=EXHAUSTED, claim_token=None)
                continue
            row.update(claimed_at=now, heartbeat_at=now, claim_token=new_token())
            row["attempts"] += 1
            return copy.deepcopy(row)
        return None

    async def heartbeat(self, tenant_id: str, row_id: str, *, token: str, now: int) -> bool:
        row = self._rows.get((tenant_id, row_id))
        if row is None or row["status"] != "accepted" or row.get("claim_token") != token:
            return False
        row["heartbeat_at"] = now
        return True

    async def finish(
        self,
        tenant_id: str,
        row_id: str,
        *,
        token: str,
        status: Literal["applied", "failed"],
        result_version: str | None,
        model: str | None,
        error: str | None,
    ) -> bool:
        row = self._rows.get((tenant_id, row_id))
        if row is None or row["status"] != "accepted" or row.get("claim_token") != token:
            return False
        row.update(status=status, result_version=result_version, model=model, error=error, claim_token=None)
        return True


class PostgresSkillFeedbackStore(Postgres):
    async def insert(
        self, tenant_id: str, row: dict[str, Any], *, max_pending: int | None = None
    ) -> dict[str, Any]:
        from felix.db.models import SkillFeedbackRow as R

        async with self._session(tenant_id) as db:
            if max_pending is not None:
                await xact_lock(db, feedback_lock_key(tenant_id, row["author"]))
                if (held := await self._pending_agent(db, tenant_id, row["author"])) >= max_pending:
                    raise SkillFeedbackAtCap(held, max_pending)
            obj = R(**{**_FEEDBACK_DEFAULTS, **row, "tenant_id": tenant_id})
            db.add(obj)
            await db.commit()
            return self._row(obj)

    async def get(self, tenant_id: str, row_id: str) -> dict[str, Any] | None:
        from felix.db.models import SkillFeedbackRow

        async with self._session(tenant_id) as db:
            row = await db.get(SkillFeedbackRow, (tenant_id, row_id))
            return self._row(row) if row is not None else None

    async def list_for_skill(
        self,
        tenant_id: str,
        name: str,
        *,
        status: str | None = None,
        limit: int = MAX_LISTED,
        before: Cursor | None = None,
    ) -> list[dict[str, Any]]:
        from sqlalchemy import collate, literal, select, tuple_

        from felix.db.models import SkillFeedbackRow

        stmt = select(SkillFeedbackRow).where(
            SkillFeedbackRow.tenant_id == tenant_id, SkillFeedbackRow.name == name
        )
        if status is not None:
            stmt = stmt.where(SkillFeedbackRow.status == status)
        if before is not None:
            stmt = stmt.where(
                tuple_(SkillFeedbackRow.created_at, collate(SkillFeedbackRow.id, "C"))
                < tuple_(literal(before[0]), literal(before[1]))
            )
        async with self._session(tenant_id) as db:
            rows = (
                await db.scalars(
                    stmt.order_by(
                        SkillFeedbackRow.created_at.desc(), collate(SkillFeedbackRow.id, "C").desc()
                    ).limit(limit)
                )
            ).all()
            return [self._row(r) for r in rows]

    async def list_by_status(
        self, tenant_id: str, status: str, *, limit: int = MAX_LISTED, after: Cursor | None = None
    ) -> list[dict[str, Any]]:
        from sqlalchemy import collate, literal, select, tuple_

        from felix.db.models import SkillFeedbackRow

        stmt = select(SkillFeedbackRow).where(
            SkillFeedbackRow.tenant_id == tenant_id, SkillFeedbackRow.status == status
        )
        if after is not None:
            stmt = stmt.where(
                tuple_(SkillFeedbackRow.created_at, collate(SkillFeedbackRow.id, "C"))
                > tuple_(literal(after[0]), literal(after[1]))
            )
        async with self._session(tenant_id) as db:
            rows = (
                await db.scalars(
                    stmt.order_by(SkillFeedbackRow.created_at, collate(SkillFeedbackRow.id, "C")).limit(limit)
                )
            ).all()
            return [self._row(r) for r in rows]

    async def _count(self, tenant_id: str, *where: Any) -> int:
        from sqlalchemy import func, select

        from felix.db.models import SkillFeedbackRow

        async with self._session(tenant_id) as db:
            count = await db.scalar(
                select(func.count())
                .select_from(SkillFeedbackRow)
                .where(SkillFeedbackRow.tenant_id == tenant_id, *where)
            )
            return int(count or 0)

    @staticmethod
    async def _pending_agent(db: Any, tenant_id: str, author: str) -> int:
        from sqlalchemy import func, select

        from felix.db.models import SkillFeedbackRow as R

        count = await db.scalar(
            select(func.count())
            .select_from(R)
            .where(R.tenant_id == tenant_id, R.author == author, R.status == "pending", R.source == "agent")
        )
        return int(count or 0)

    async def count_pending_agent(self, tenant_id: str, author: str) -> int:
        async with self._session(tenant_id) as db:
            return await self._pending_agent(db, tenant_id, author)

    async def count_jobs_in_flight(self, tenant_id: str) -> int:
        from felix.db.models import SkillFeedbackRow as R

        return await self._count(tenant_id, R.status == "accepted", R.improve.is_(True))

    async def count_jobs_since(self, tenant_id: str, since: int) -> int:
        from felix.db.models import SkillFeedbackRow as R

        return await self._count(tenant_id, R.improve.is_(True), R.decided_at >= since)

    async def decide(
        self,
        tenant_id: str,
        row_id: str,
        *,
        status: Literal["accepted", "rejected"],
        improve: bool,
        by: str,
        note: str | None,
        at: int,
        caps: JobCaps | None = None,
    ) -> dict[str, Any]:
        from sqlalchemy import select

        from felix.db.models import SkillFeedbackRow

        async with self._session(tenant_id) as db:
            if improve and caps is not None:
                # The job lock before the row lock, as every caller takes them, so two
                # decisions cannot each hold one and wait on the other.
                await hold_job_caps(db, tenant_id, caps)
            row = await db.scalar(
                select(SkillFeedbackRow)
                .where(SkillFeedbackRow.tenant_id == tenant_id, SkillFeedbackRow.id == row_id)
                .with_for_update()
            )
            if row is None or row.status != "pending":
                await db.rollback()
                raise SkillFeedbackConflict(row_id)
            row.status, row.improve, row.decided_by, row.decision_note, row.decided_at = (
                status,
                improve,
                by,
                note,
                at,
            )
            await db.commit()
            return self._row(row)

    async def claim_next(self, *, now: int) -> dict[str, Any] | None:
        from felix.db.models import SkillFeedbackRow

        due = (
            (SkillFeedbackRow.status == "accepted")
            & SkillFeedbackRow.improve.is_(True)
            & (
                SkillFeedbackRow.claim_token.is_(None)
                | SkillFeedbackRow.heartbeat_at.is_(None)
                | (SkillFeedbackRow.heartbeat_at <= now - CLAIM_LEASE_MS)
            )
        )

        def take(row: Any) -> None:
            row.claimed_at, row.heartbeat_at, row.claim_token = now, now, new_token()
            row.attempts = int(row.attempts or 0) + 1

        def exhaust(row: Any) -> None:
            row.status, row.error, row.claim_token = "failed", EXHAUSTED, None

        async with self._sweep() as db:
            return await self._claim_fairly(
                db, SkillFeedbackRow, due, SkillFeedbackRow.claimed_at, take, exhaust
            )

    async def heartbeat(self, tenant_id: str, row_id: str, *, token: str, now: int) -> bool:
        return await self._update(tenant_id, row_id, token, {"heartbeat_at": now})

    async def finish(
        self,
        tenant_id: str,
        row_id: str,
        *,
        token: str,
        status: Literal["applied", "failed"],
        result_version: str | None,
        model: str | None,
        error: str | None,
    ) -> bool:
        values = {
            "status": status,
            "result_version": result_version,
            "model": model,
            "error": error,
            "claim_token": None,
        }
        return await self._update(tenant_id, row_id, token, values)

    async def _update(self, tenant_id: str, row_id: str, token: str, values: dict[str, Any]) -> bool:
        from sqlalchemy import update

        from felix.db.models import SkillFeedbackRow as R

        async with self._session(tenant_id) as db:
            done = await db.execute(
                update(R)
                .where(
                    R.tenant_id == tenant_id, R.id == row_id, R.status == "accepted", R.claim_token == token
                )
                .values(**values)
            )
            await db.commit()
            return bool(getattr(done, "rowcount", 0))


_memory = InMemorySkillFeedbackStore()


def get_skill_feedback_store(settings: Settings | None = None) -> SkillFeedbackStore:
    pg = postgres_settings(settings)
    return _memory if pg is None else PostgresSkillFeedbackStore(pg)


def memory_store() -> InMemorySkillFeedbackStore:
    """The twin `get_skill_feedback_store` hands out under `memory://`."""
    return _memory


def clear_memory() -> None:
    _memory.clear()


__all__ = [
    "InMemorySkillFeedbackStore",
    "PostgresSkillFeedbackStore",
    "SkillFeedbackStore",
    "clear_memory",
    "get_skill_feedback_store",
    "memory_store",
]
