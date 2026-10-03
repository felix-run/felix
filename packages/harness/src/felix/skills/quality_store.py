"""Rows for the skill library's quality loop: `skill_feedback`, `skill_eval`, `skill_policy`.

Data access only. What feedback may become and who may decide it is `skills/feedback.py`; what an
improvement and an evaluation do is `skills/improve.py` and `skills/evaluate.py`. This module
enforces the things only storage can:

- a decision lands only on feedback still `pending`, so an accept racing a reject cannot both win;
- a version has at most one evaluation queued or running (a partial unique index on Postgres);
- a job is run by one worker at a time. `claim_*` takes a row and stamps it (`claimed_at` for an
  improvement, `started_at` for an evaluation) under `FOR UPDATE SKIP LOCKED` on Postgres, and in
  one step with no await in the twin. The stamp is also the claim's token: `finish_*` lands only
  while the row still carries it, so a worker whose lease lapsed and was taken over cannot write
  over the worker that took it.

A claim older than `CLAIM_LEASE_MS` is taken again, so a worker that died mid-job does not strand
it. Every listing ends on the primary key (`id`), so the two arms agree on where a page ends.
"""

from __future__ import annotations

import copy
from typing import Any, Literal, Protocol, runtime_checkable

from felix.config import Settings

FeedbackStatus = Literal["pending", "accepted", "rejected", "applied", "failed"]
FeedbackSource = Literal["human", "agent"]
EvalStatus = Literal["queued", "running", "succeeded", "failed"]

# How long a claimed job is the claimer's. An evaluation is a few dozen model calls, so this is
# well past the longest a healthy one takes; a claim older than this belonged to a dead worker.
CLAIM_LEASE_MS = 30 * 60 * 1000
MAX_LISTED = 100

# Where a listing resumes: the `(created_at, id)` of the last row a page held.
Cursor = tuple[int, str]


class SkillFeedbackConflict(Exception):
    """The feedback is not in the state the change was decided against, or does not exist."""


class SkillEvalInFlight(Exception):
    """The version already has an evaluation queued or running."""


@runtime_checkable
class SkillFeedbackStore(Protocol):
    async def insert(self, tenant_id: str, row: dict[str, Any]) -> dict[str, Any]: ...

    async def get(self, tenant_id: str, feedback_id: str) -> dict[str, Any] | None: ...

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
        feedback_id: str,
        *,
        status: Literal["accepted", "rejected"],
        improve: bool,
        by: str,
        note: str | None,
        at: int,
    ) -> dict[str, Any]: ...

    async def claim_improvements(self, *, limit: int, now: int) -> list[dict[str, Any]]: ...

    async def claim_improvement(
        self, tenant_id: str, feedback_id: str, *, now: int
    ) -> dict[str, Any] | None: ...

    async def finish_improvement(
        self,
        tenant_id: str,
        feedback_id: str,
        *,
        claimed_at: int,
        status: Literal["applied", "failed"],
        result_version: str | None,
        model: str | None,
        error: str | None,
    ) -> bool: ...


@runtime_checkable
class SkillEvalStore(Protocol):
    async def insert(self, tenant_id: str, row: dict[str, Any]) -> dict[str, Any]: ...

    async def get(self, tenant_id: str, eval_id: str) -> dict[str, Any] | None: ...

    async def list_for_skill(
        self,
        tenant_id: str,
        name: str,
        *,
        version: str | None = None,
        limit: int = MAX_LISTED,
        before: Cursor | None = None,
    ) -> list[dict[str, Any]]: ...

    async def latest_succeeded(self, tenant_id: str, name: str, version: str) -> dict[str, Any] | None: ...

    async def claim_queued(self, *, limit: int, now: int) -> list[dict[str, Any]]: ...

    async def claim(self, tenant_id: str, eval_id: str, *, now: int) -> dict[str, Any] | None: ...

    async def finish(
        self, tenant_id: str, eval_id: str, *, started_at: int, fields: dict[str, Any]
    ) -> bool: ...


@runtime_checkable
class SkillPolicyStore(Protocol):
    async def get(self, tenant_id: str) -> dict[str, Any] | None: ...

    async def put(self, tenant_id: str, row: dict[str, Any]) -> dict[str, Any]: ...


# Every column a caller may leave out, at the value Postgres would give it, so a row read back
# from the twin has the same keys as one read back from the table.
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
}
_EVAL_DEFAULTS: dict[str, Any] = {
    "scenario_source": None,
    "scenarios": [],
    "baseline_score": None,
    "with_skill_score": None,
    "uplift": None,
    "results": [],
    "model": None,
    "judge_model": None,
    "error": None,
    "requested_by": "",
    "started_at": None,
    "finished_at": None,
}
_EVAL_FINISH_COLUMNS = frozenset(
    {
        "status",
        "scenario_source",
        "scenarios",
        "baseline_score",
        "with_skill_score",
        "uplift",
        "results",
        "model",
        "judge_model",
        "error",
        "finished_at",
    }
)


def _improvement_due(row: dict[str, Any], now: int) -> bool:
    if row["status"] != "accepted" or not row.get("improve"):
        return False
    claimed = row.get("claimed_at")
    return claimed is None or claimed <= now - CLAIM_LEASE_MS


def _eval_due(row: dict[str, Any], now: int) -> bool:
    if row["status"] == "queued":
        return True
    started = row.get("started_at")
    return row["status"] == "running" and started is not None and started <= now - CLAIM_LEASE_MS


def _key(row: dict[str, Any]) -> Cursor:
    return (int(row["created_at"]), str(row["id"]))


class InMemorySkillFeedbackStore:
    """The `memory://` twin. Copies on the way in and out, as a row read from Postgres is."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], dict[str, Any]] = {}

    def clear(self) -> None:
        self._rows.clear()

    async def insert(self, tenant_id: str, row: dict[str, Any]) -> dict[str, Any]:
        stored = {**_FEEDBACK_DEFAULTS, **copy.deepcopy(row), "tenant_id": tenant_id}
        self._rows[(tenant_id, row["id"])] = stored
        return copy.deepcopy(stored)

    async def get(self, tenant_id: str, feedback_id: str) -> dict[str, Any] | None:
        row = self._rows.get((tenant_id, feedback_id))
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
            and (before is None or _key(r) < before)
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
            if t == tenant_id and r["status"] == status and (after is None or _key(r) > after)
        ]
        rows.sort(key=lambda r: (r["created_at"], r["id"]))
        return copy.deepcopy(rows[:limit])

    async def count_pending_agent(self, tenant_id: str, author: str) -> int:
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
        feedback_id: str,
        *,
        status: Literal["accepted", "rejected"],
        improve: bool,
        by: str,
        note: str | None,
        at: int,
    ) -> dict[str, Any]:
        row = self._rows.get((tenant_id, feedback_id))
        if row is None or row["status"] != "pending":
            raise SkillFeedbackConflict(feedback_id)
        row.update(status=status, improve=improve, decided_by=by, decision_note=note, decided_at=at)
        return copy.deepcopy(row)

    async def claim_improvements(self, *, limit: int, now: int) -> list[dict[str, Any]]:
        due = sorted(
            (r for r in self._rows.values() if _improvement_due(r, now)),
            key=lambda r: (r["created_at"], r["id"]),
        )[:limit]
        for row in due:
            row["claimed_at"] = now
        return copy.deepcopy(due)

    async def claim_improvement(self, tenant_id: str, feedback_id: str, *, now: int) -> dict[str, Any] | None:
        row = self._rows.get((tenant_id, feedback_id))
        if row is None or not _improvement_due(row, now):
            return None
        row["claimed_at"] = now
        return copy.deepcopy(row)

    async def finish_improvement(
        self,
        tenant_id: str,
        feedback_id: str,
        *,
        claimed_at: int,
        status: Literal["applied", "failed"],
        result_version: str | None,
        model: str | None,
        error: str | None,
    ) -> bool:
        row = self._rows.get((tenant_id, feedback_id))
        if row is None or row["status"] != "accepted" or row.get("claimed_at") != claimed_at:
            return False
        row.update(status=status, result_version=result_version, model=model, error=error)
        return True


class InMemorySkillEvalStore:
    """The `memory://` twin, enforcing the one-in-flight rule the partial index enforces."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], dict[str, Any]] = {}

    def clear(self) -> None:
        self._rows.clear()

    async def insert(self, tenant_id: str, row: dict[str, Any]) -> dict[str, Any]:
        for (t, _), other in self._rows.items():
            if (
                t == tenant_id
                and other["name"] == row["name"]
                and other["version"] == row["version"]
                and other["status"] in {"queued", "running"}
            ):
                raise SkillEvalInFlight(f"{row['name']}@{row['version']}")
        stored = {**_EVAL_DEFAULTS, **copy.deepcopy(row), "tenant_id": tenant_id}
        self._rows[(tenant_id, row["id"])] = stored
        return copy.deepcopy(stored)

    async def get(self, tenant_id: str, eval_id: str) -> dict[str, Any] | None:
        row = self._rows.get((tenant_id, eval_id))
        return copy.deepcopy(row) if row is not None else None

    async def list_for_skill(
        self,
        tenant_id: str,
        name: str,
        *,
        version: str | None = None,
        limit: int = MAX_LISTED,
        before: Cursor | None = None,
    ) -> list[dict[str, Any]]:
        rows = [
            r
            for (t, _), r in self._rows.items()
            if t == tenant_id
            and r["name"] == name
            and (version is None or r["version"] == version)
            and (before is None or _key(r) < before)
        ]
        rows.sort(key=lambda r: (r["created_at"], r["id"]), reverse=True)
        return copy.deepcopy(rows[:limit])

    async def latest_succeeded(self, tenant_id: str, name: str, version: str) -> dict[str, Any] | None:
        rows = [
            r
            for (t, _), r in self._rows.items()
            if t == tenant_id and r["name"] == name and r["version"] == version and r["status"] == "succeeded"
        ]
        rows.sort(key=lambda r: (int(r.get("finished_at") or 0), str(r["id"])))
        return copy.deepcopy(rows[-1]) if rows else None

    async def claim_queued(self, *, limit: int, now: int) -> list[dict[str, Any]]:
        due = sorted(
            (r for r in self._rows.values() if _eval_due(r, now)), key=lambda r: (r["created_at"], r["id"])
        )[:limit]
        for row in due:
            row.update(status="running", started_at=now)
        return copy.deepcopy(due)

    async def claim(self, tenant_id: str, eval_id: str, *, now: int) -> dict[str, Any] | None:
        row = self._rows.get((tenant_id, eval_id))
        if row is None or not _eval_due(row, now):
            return None
        row.update(status="running", started_at=now)
        return copy.deepcopy(row)

    async def finish(self, tenant_id: str, eval_id: str, *, started_at: int, fields: dict[str, Any]) -> bool:
        row = self._rows.get((tenant_id, eval_id))
        if row is None or row["status"] != "running" or row.get("started_at") != started_at:
            return False
        row.update(copy.deepcopy({k: v for k, v in fields.items() if k in _EVAL_FINISH_COLUMNS}))
        return True


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


# -- Postgres --------------------------------------------------------------------------------


class _Postgres:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _session(self, tenant_id: str) -> Any:
        from felix.db.session import tenant_session

        return tenant_session(self._settings, tenant_id)

    @staticmethod
    def _row(row: Any) -> dict[str, Any]:
        return {c.key: getattr(row, c.key) for c in row.__table__.columns}


class PostgresSkillFeedbackStore(_Postgres):
    async def insert(self, tenant_id: str, row: dict[str, Any]) -> dict[str, Any]:
        from felix.db.models import SkillFeedbackRow

        async with self._session(tenant_id) as db:
            obj = SkillFeedbackRow(**{**_FEEDBACK_DEFAULTS, **row, "tenant_id": tenant_id})
            db.add(obj)
            await db.commit()
            return self._row(obj)

    async def get(self, tenant_id: str, feedback_id: str) -> dict[str, Any] | None:
        from felix.db.models import SkillFeedbackRow

        async with self._session(tenant_id) as db:
            row = await db.get(SkillFeedbackRow, (tenant_id, feedback_id))
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

    async def count_pending_agent(self, tenant_id: str, author: str) -> int:
        from sqlalchemy import func, select

        from felix.db.models import SkillFeedbackRow

        async with self._session(tenant_id) as db:
            count = await db.scalar(
                select(func.count())
                .select_from(SkillFeedbackRow)
                .where(
                    SkillFeedbackRow.tenant_id == tenant_id,
                    SkillFeedbackRow.author == author,
                    SkillFeedbackRow.status == "pending",
                    SkillFeedbackRow.source == "agent",
                )
            )
            return int(count or 0)

    async def decide(
        self,
        tenant_id: str,
        feedback_id: str,
        *,
        status: Literal["accepted", "rejected"],
        improve: bool,
        by: str,
        note: str | None,
        at: int,
    ) -> dict[str, Any]:
        from sqlalchemy import select

        from felix.db.models import SkillFeedbackRow

        async with self._session(tenant_id) as db:
            row = await db.scalar(
                select(SkillFeedbackRow)
                .where(SkillFeedbackRow.tenant_id == tenant_id, SkillFeedbackRow.id == feedback_id)
                .with_for_update()
            )
            if row is None or row.status != "pending":
                await db.rollback()
                raise SkillFeedbackConflict(feedback_id)
            row.status, row.improve, row.decided_by, row.decision_note, row.decided_at = (
                status,
                improve,
                by,
                note,
                at,
            )
            await db.commit()
            return self._row(row)

    @staticmethod
    def _due(now: int) -> Any:
        from felix.db.models import SkillFeedbackRow

        return (
            (SkillFeedbackRow.status == "accepted")
            & SkillFeedbackRow.improve.is_(True)
            & (SkillFeedbackRow.claimed_at.is_(None) | (SkillFeedbackRow.claimed_at <= now - CLAIM_LEASE_MS))
        )

    async def claim_improvements(self, *, limit: int, now: int) -> list[dict[str, Any]]:
        """A bounded batch across every tenant, skipping rows another worker is claiming.

        Cross-tenant maintenance, like the fiber sweep: under `rls_bypass`, or RLS with no
        tenant GUC would silently return nothing and no improvement would ever run.
        """
        from sqlalchemy import collate, select

        from felix.db.models import SkillFeedbackRow
        from felix.db.session import get_session_factory, rls_bypass

        with rls_bypass():
            async with get_session_factory(settings=self._settings)() as db:
                rows = (
                    await db.scalars(
                        select(SkillFeedbackRow)
                        .where(self._due(now))
                        .order_by(SkillFeedbackRow.created_at, collate(SkillFeedbackRow.id, "C"))
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
                for row in rows:
                    row.claimed_at = now
                claimed = [self._row(r) for r in rows]
                await db.commit()
                return claimed

    async def claim_improvement(self, tenant_id: str, feedback_id: str, *, now: int) -> dict[str, Any] | None:
        from sqlalchemy import select

        from felix.db.models import SkillFeedbackRow

        async with self._session(tenant_id) as db:
            row = await db.scalar(
                select(SkillFeedbackRow)
                .where(
                    SkillFeedbackRow.tenant_id == tenant_id,
                    SkillFeedbackRow.id == feedback_id,
                    self._due(now),
                )
                .with_for_update(skip_locked=True)
            )
            if row is None:
                await db.rollback()
                return None
            row.claimed_at = now
            claimed = self._row(row)
            await db.commit()
            return claimed

    async def finish_improvement(
        self,
        tenant_id: str,
        feedback_id: str,
        *,
        claimed_at: int,
        status: Literal["applied", "failed"],
        result_version: str | None,
        model: str | None,
        error: str | None,
    ) -> bool:
        from sqlalchemy import update

        from felix.db.models import SkillFeedbackRow

        async with self._session(tenant_id) as db:
            done = await db.execute(
                update(SkillFeedbackRow)
                .where(
                    SkillFeedbackRow.tenant_id == tenant_id,
                    SkillFeedbackRow.id == feedback_id,
                    SkillFeedbackRow.status == "accepted",
                    SkillFeedbackRow.claimed_at == claimed_at,
                )
                .values(status=status, result_version=result_version, model=model, error=error)
            )
            await db.commit()
            return bool(getattr(done, "rowcount", 0))


class PostgresSkillEvalStore(_Postgres):
    async def insert(self, tenant_id: str, row: dict[str, Any]) -> dict[str, Any]:
        from sqlalchemy.exc import IntegrityError

        from felix.db.models import SkillEvalRow

        async with self._session(tenant_id) as db:
            obj = SkillEvalRow(**{**_EVAL_DEFAULTS, **row, "tenant_id": tenant_id})
            db.add(obj)
            try:
                await db.commit()
            except IntegrityError as exc:
                await db.rollback()
                if getattr(exc.orig, "sqlstate", None) == "23505":
                    raise SkillEvalInFlight(f"{row['name']}@{row['version']}") from exc
                raise
            return self._row(obj)

    async def get(self, tenant_id: str, eval_id: str) -> dict[str, Any] | None:
        from felix.db.models import SkillEvalRow

        async with self._session(tenant_id) as db:
            row = await db.get(SkillEvalRow, (tenant_id, eval_id))
            return self._row(row) if row is not None else None

    async def list_for_skill(
        self,
        tenant_id: str,
        name: str,
        *,
        version: str | None = None,
        limit: int = MAX_LISTED,
        before: Cursor | None = None,
    ) -> list[dict[str, Any]]:
        from sqlalchemy import collate, literal, select, tuple_

        from felix.db.models import SkillEvalRow

        stmt = select(SkillEvalRow).where(SkillEvalRow.tenant_id == tenant_id, SkillEvalRow.name == name)
        if version is not None:
            stmt = stmt.where(SkillEvalRow.version == version)
        if before is not None:
            stmt = stmt.where(
                tuple_(SkillEvalRow.created_at, collate(SkillEvalRow.id, "C"))
                < tuple_(literal(before[0]), literal(before[1]))
            )
        async with self._session(tenant_id) as db:
            rows = (
                await db.scalars(
                    stmt.order_by(SkillEvalRow.created_at.desc(), collate(SkillEvalRow.id, "C").desc()).limit(
                        limit
                    )
                )
            ).all()
            return [self._row(r) for r in rows]

    async def latest_succeeded(self, tenant_id: str, name: str, version: str) -> dict[str, Any] | None:
        from sqlalchemy import collate, select

        from felix.db.models import SkillEvalRow

        async with self._session(tenant_id) as db:
            row = await db.scalar(
                select(SkillEvalRow)
                .where(
                    SkillEvalRow.tenant_id == tenant_id,
                    SkillEvalRow.name == name,
                    SkillEvalRow.version == version,
                    SkillEvalRow.status == "succeeded",
                )
                .order_by(SkillEvalRow.finished_at.desc(), collate(SkillEvalRow.id, "C").desc())
                .limit(1)
            )
            return self._row(row) if row is not None else None

    @staticmethod
    def _due(now: int) -> Any:
        from felix.db.models import SkillEvalRow

        return (SkillEvalRow.status == "queued") | (
            (SkillEvalRow.status == "running")
            & SkillEvalRow.started_at.is_not(None)
            & (SkillEvalRow.started_at <= now - CLAIM_LEASE_MS)
        )

    async def claim_queued(self, *, limit: int, now: int) -> list[dict[str, Any]]:
        """A bounded batch across every tenant, under `rls_bypass` as `claim_improvements`."""
        from sqlalchemy import collate, select

        from felix.db.models import SkillEvalRow
        from felix.db.session import get_session_factory, rls_bypass

        with rls_bypass():
            async with get_session_factory(settings=self._settings)() as db:
                rows = (
                    await db.scalars(
                        select(SkillEvalRow)
                        .where(self._due(now))
                        .order_by(SkillEvalRow.created_at, collate(SkillEvalRow.id, "C"))
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
                for row in rows:
                    row.status, row.started_at = "running", now
                claimed = [self._row(r) for r in rows]
                await db.commit()
                return claimed

    async def claim(self, tenant_id: str, eval_id: str, *, now: int) -> dict[str, Any] | None:
        from sqlalchemy import select

        from felix.db.models import SkillEvalRow

        async with self._session(tenant_id) as db:
            row = await db.scalar(
                select(SkillEvalRow)
                .where(SkillEvalRow.tenant_id == tenant_id, SkillEvalRow.id == eval_id, self._due(now))
                .with_for_update(skip_locked=True)
            )
            if row is None:
                await db.rollback()
                return None
            row.status, row.started_at = "running", now
            claimed = self._row(row)
            await db.commit()
            return claimed

    async def finish(self, tenant_id: str, eval_id: str, *, started_at: int, fields: dict[str, Any]) -> bool:
        from sqlalchemy import update

        from felix.db.models import SkillEvalRow

        values = {k: v for k, v in fields.items() if k in _EVAL_FINISH_COLUMNS}
        async with self._session(tenant_id) as db:
            done = await db.execute(
                update(SkillEvalRow)
                .where(
                    SkillEvalRow.tenant_id == tenant_id,
                    SkillEvalRow.id == eval_id,
                    SkillEvalRow.status == "running",
                    SkillEvalRow.started_at == started_at,
                )
                .values(**values)
            )
            await db.commit()
            return bool(getattr(done, "rowcount", 0))


class PostgresSkillPolicyStore(_Postgres):
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


_memory_feedback = InMemorySkillFeedbackStore()
_memory_evals = InMemorySkillEvalStore()
_memory_policy = InMemorySkillPolicyStore()


def _postgres(settings: Settings | None) -> Settings | None:
    """The settings to reach Postgres with, or None under `memory://`. Selected exactly as
    `library_store.get_skill_library_store` selects."""
    if settings is None:
        return None
    url = settings.database_url
    return None if ":memory:" in url or "sqlite" in url or url.startswith("memory://") else settings


def get_skill_feedback_store(settings: Settings | None = None) -> SkillFeedbackStore:
    pg = _postgres(settings)
    return _memory_feedback if pg is None else PostgresSkillFeedbackStore(pg)


def get_skill_eval_store(settings: Settings | None = None) -> SkillEvalStore:
    pg = _postgres(settings)
    return _memory_evals if pg is None else PostgresSkillEvalStore(pg)


def get_skill_policy_store(settings: Settings | None = None) -> SkillPolicyStore:
    pg = _postgres(settings)
    return _memory_policy if pg is None else PostgresSkillPolicyStore(pg)


def clear_memory() -> None:
    """Drop the in-memory rows. Test seam, matching the other `memory://` stores."""
    _memory_feedback.clear()
    _memory_evals.clear()
    _memory_policy.clear()


__all__ = [
    "CLAIM_LEASE_MS",
    "MAX_LISTED",
    "Cursor",
    "EvalStatus",
    "FeedbackSource",
    "FeedbackStatus",
    "InMemorySkillEvalStore",
    "InMemorySkillFeedbackStore",
    "InMemorySkillPolicyStore",
    "PostgresSkillEvalStore",
    "PostgresSkillFeedbackStore",
    "PostgresSkillPolicyStore",
    "SkillEvalInFlight",
    "SkillEvalStore",
    "SkillFeedbackConflict",
    "SkillFeedbackStore",
    "SkillPolicyStore",
    "clear_memory",
    "get_skill_eval_store",
    "get_skill_feedback_store",
    "get_skill_policy_store",
]
