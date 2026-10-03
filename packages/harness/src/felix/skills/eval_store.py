"""Rows for `skill_eval`: one baseline-versus-with-skill evaluation of a library skill version.

Data access only; `skills/evaluate.py` runs the job. The claim, heartbeat and fairness rules are
`quality_store`'s, shared with `feedback_store`; the one-in-flight rule is this table's own (a
partial unique index on Postgres, enforced by hand in the twin).
"""

from __future__ import annotations

import copy
from typing import Any, Protocol, runtime_checkable

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
    SkillEvalInFlight,
    fair_order,
    hold_job_caps,
    lapsed,
    memory_job_caps,
    new_token,
    postgres_settings,
    row_key,
)

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
    **CLAIM_DEFAULTS,
}
# What a running evaluation may write at a heartbeat (the scenarios it chose, so they are pinned
# even if the run then fails) and at its finish.
_EVAL_HEARTBEAT_COLUMNS = frozenset({"scenario_source", "scenarios"})
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


def _eval_due(row: dict[str, Any], now: int) -> bool:
    return row["status"] == "queued" or (row["status"] == "running" and lapsed(row, now))


@runtime_checkable
class SkillEvalStore(Protocol):
    async def insert(
        self, tenant_id: str, row: dict[str, Any], *, caps: JobCaps | None = None
    ) -> dict[str, Any]:
        """Queue an evaluation. With ``caps`` it is refused (`SkillJobsAtCap`) unless the
        tenant has room, counted in the same transaction as the insert
        (`quality_store.hold_job_caps`)."""
        ...

    async def get(self, tenant_id: str, row_id: str) -> dict[str, Any] | None: ...

    async def list_for_skill(
        self,
        tenant_id: str,
        name: str,
        *,
        version: str | None = None,
        limit: int = MAX_LISTED,
        before: Cursor | None = None,
    ) -> list[dict[str, Any]]: ...

    async def latest_succeeded(
        self, tenant_id: str, name: str, version: str, *, scenario_source: str | None = None
    ) -> dict[str, Any] | None: ...

    async def pinned_scenarios(self, tenant_id: str, name: str, version: str) -> dict[str, Any] | None: ...

    async def count_jobs_in_flight(self, tenant_id: str) -> int: ...

    async def count_jobs_since(self, tenant_id: str, since: int) -> int: ...

    async def claim_next(self, *, now: int) -> dict[str, Any] | None: ...

    async def heartbeat(
        self, tenant_id: str, row_id: str, *, token: str, now: int, fields: dict[str, Any] | None = None
    ) -> bool: ...

    async def finish(self, tenant_id: str, row_id: str, *, token: str, fields: dict[str, Any]) -> bool: ...


class InMemorySkillEvalStore:
    """The `memory://` twin, enforcing the one-in-flight rule the partial index enforces."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], dict[str, Any]] = {}

    def clear(self) -> None:
        self._rows.clear()

    async def insert(
        self, tenant_id: str, row: dict[str, Any], *, caps: JobCaps | None = None
    ) -> dict[str, Any]:
        # No await from the count to the write: one event loop cannot interleave another
        # insert between them.
        if caps is not None:
            memory_job_caps(tenant_id, caps, evals=self)
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

    async def get(self, tenant_id: str, row_id: str) -> dict[str, Any] | None:
        row = self._rows.get((tenant_id, row_id))
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
            and (before is None or row_key(r) < before)
        ]
        rows.sort(key=lambda r: (r["created_at"], r["id"]), reverse=True)
        return copy.deepcopy(rows[:limit])

    def _of_version(self, tenant_id: str, name: str, version: str) -> list[dict[str, Any]]:
        return [
            r
            for (t, _), r in self._rows.items()
            if t == tenant_id and r["name"] == name and r["version"] == version
        ]

    async def latest_succeeded(
        self, tenant_id: str, name: str, version: str, *, scenario_source: str | None = None
    ) -> dict[str, Any] | None:
        rows = [
            r
            for r in self._of_version(tenant_id, name, version)
            if r["status"] == "succeeded"
            and (scenario_source is None or r["scenario_source"] == scenario_source)
        ]
        rows.sort(key=lambda r: (int(r.get("finished_at") or 0), str(r["id"])))
        return copy.deepcopy(rows[-1]) if rows else None

    async def pinned_scenarios(self, tenant_id: str, name: str, version: str) -> dict[str, Any] | None:
        rows = [
            r for r in self._of_version(tenant_id, name, version) if r["scenario_source"] and r["scenarios"]
        ]
        rows.sort(key=lambda r: (r["created_at"], r["id"]))
        if not rows:
            return None
        return copy.deepcopy(
            {"scenario_source": rows[0]["scenario_source"], "scenarios": rows[0]["scenarios"]}
        )

    def jobs_in_flight(self, tenant_id: str) -> int:
        return sum(
            1 for (t, _), r in self._rows.items() if t == tenant_id and r["status"] in {"queued", "running"}
        )

    def jobs_since(self, tenant_id: str, since: int) -> int:
        return sum(1 for (t, _), r in self._rows.items() if t == tenant_id and r["created_at"] >= since)

    async def count_jobs_in_flight(self, tenant_id: str) -> int:
        return self.jobs_in_flight(tenant_id)

    async def count_jobs_since(self, tenant_id: str, since: int) -> int:
        return self.jobs_since(tenant_id, since)

    async def claim_next(self, *, now: int) -> dict[str, Any] | None:
        for _ in range(MAX_RESCANS):
            order = fair_order(self._rows.values(), lambda r: _eval_due(r, now), "started_at")
            if not order:
                return None
            row = order[0]
            if row["attempts"] >= MAX_ATTEMPTS:
                # Failed, then scanned again: this tenant's next job is a candidate now.
                row.update(status="failed", error=EXHAUSTED, finished_at=now, claim_token=None)
                continue
            row.update(
                status="running",
                started_at=now,
                heartbeat_at=now,
                claim_token=new_token(),
                attempts=row["attempts"] + 1,
            )
            return copy.deepcopy(row)
        return None

    async def heartbeat(
        self, tenant_id: str, row_id: str, *, token: str, now: int, fields: dict[str, Any] | None = None
    ) -> bool:
        row = self._rows.get((tenant_id, row_id))
        if row is None or row["status"] != "running" or row.get("claim_token") != token:
            return False
        row.update(copy.deepcopy({k: v for k, v in (fields or {}).items() if k in _EVAL_HEARTBEAT_COLUMNS}))
        row["heartbeat_at"] = now
        return True

    async def finish(self, tenant_id: str, row_id: str, *, token: str, fields: dict[str, Any]) -> bool:
        row = self._rows.get((tenant_id, row_id))
        if row is None or row["status"] != "running" or row.get("claim_token") != token:
            return False
        row.update(copy.deepcopy({k: v for k, v in fields.items() if k in _EVAL_FINISH_COLUMNS}))
        row["claim_token"] = None
        return True


class PostgresSkillEvalStore(Postgres):
    async def insert(
        self, tenant_id: str, row: dict[str, Any], *, caps: JobCaps | None = None
    ) -> dict[str, Any]:
        from sqlalchemy.exc import IntegrityError

        from felix.db.models import SkillEvalRow

        async with self._session(tenant_id) as db:
            if caps is not None:
                await hold_job_caps(db, tenant_id, caps)
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

    async def get(self, tenant_id: str, row_id: str) -> dict[str, Any] | None:
        from felix.db.models import SkillEvalRow

        async with self._session(tenant_id) as db:
            row = await db.get(SkillEvalRow, (tenant_id, row_id))
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

    async def latest_succeeded(
        self, tenant_id: str, name: str, version: str, *, scenario_source: str | None = None
    ) -> dict[str, Any] | None:
        from sqlalchemy import collate, select

        from felix.db.models import SkillEvalRow

        stmt = select(SkillEvalRow).where(
            SkillEvalRow.tenant_id == tenant_id,
            SkillEvalRow.name == name,
            SkillEvalRow.version == version,
            SkillEvalRow.status == "succeeded",
        )
        if scenario_source is not None:
            stmt = stmt.where(SkillEvalRow.scenario_source == scenario_source)
        async with self._session(tenant_id) as db:
            row = await db.scalar(
                stmt.order_by(SkillEvalRow.finished_at.desc(), collate(SkillEvalRow.id, "C").desc()).limit(1)
            )
            return self._row(row) if row is not None else None

    async def pinned_scenarios(self, tenant_id: str, name: str, version: str) -> dict[str, Any] | None:
        from sqlalchemy import collate, func, select

        from felix.db.models import SkillEvalRow

        async with self._session(tenant_id) as db:
            row = await db.scalar(
                select(SkillEvalRow)
                .where(
                    SkillEvalRow.tenant_id == tenant_id,
                    SkillEvalRow.name == name,
                    SkillEvalRow.version == version,
                    SkillEvalRow.scenario_source.is_not(None),
                    func.jsonb_array_length(SkillEvalRow.scenarios) > 0,
                )
                .order_by(SkillEvalRow.created_at, collate(SkillEvalRow.id, "C"))
                .limit(1)
            )
            if row is None:
                return None
            return {"scenario_source": row.scenario_source, "scenarios": row.scenarios}

    async def _count(self, tenant_id: str, *where: Any) -> int:
        from sqlalchemy import func, select

        from felix.db.models import SkillEvalRow

        async with self._session(tenant_id) as db:
            count = await db.scalar(
                select(func.count())
                .select_from(SkillEvalRow)
                .where(SkillEvalRow.tenant_id == tenant_id, *where)
            )
            return int(count or 0)

    async def count_jobs_in_flight(self, tenant_id: str) -> int:
        from felix.db.models import SkillEvalRow as R

        return await self._count(tenant_id, R.status.in_(("queued", "running")))

    async def count_jobs_since(self, tenant_id: str, since: int) -> int:
        from felix.db.models import SkillEvalRow as R

        return await self._count(tenant_id, R.created_at >= since)

    async def claim_next(self, *, now: int) -> dict[str, Any] | None:
        from felix.db.models import SkillEvalRow

        due = (SkillEvalRow.status == "queued") | (
            (SkillEvalRow.status == "running")
            & (SkillEvalRow.heartbeat_at.is_(None) | (SkillEvalRow.heartbeat_at <= now - CLAIM_LEASE_MS))
        )

        def take(row: Any) -> None:
            row.status, row.started_at, row.heartbeat_at, row.claim_token = "running", now, now, new_token()
            row.attempts = int(row.attempts or 0) + 1

        def exhaust(row: Any) -> None:
            row.status, row.error, row.finished_at, row.claim_token = "failed", EXHAUSTED, now, None

        async with self._sweep() as db:
            return await self._claim_fairly(db, SkillEvalRow, due, SkillEvalRow.started_at, take, exhaust)

    async def heartbeat(
        self, tenant_id: str, row_id: str, *, token: str, now: int, fields: dict[str, Any] | None = None
    ) -> bool:
        values = {k: v for k, v in (fields or {}).items() if k in _EVAL_HEARTBEAT_COLUMNS}
        return await self._update(tenant_id, row_id, token, {**values, "heartbeat_at": now})

    async def finish(self, tenant_id: str, row_id: str, *, token: str, fields: dict[str, Any]) -> bool:
        values = {k: v for k, v in fields.items() if k in _EVAL_FINISH_COLUMNS}
        return await self._update(tenant_id, row_id, token, {**values, "claim_token": None})

    async def _update(self, tenant_id: str, row_id: str, token: str, values: dict[str, Any]) -> bool:
        from sqlalchemy import update

        from felix.db.models import SkillEvalRow as R

        async with self._session(tenant_id) as db:
            done = await db.execute(
                update(R)
                .where(
                    R.tenant_id == tenant_id, R.id == row_id, R.status == "running", R.claim_token == token
                )
                .values(**values)
            )
            await db.commit()
            return bool(getattr(done, "rowcount", 0))


_memory = InMemorySkillEvalStore()


def get_skill_eval_store(settings: Settings | None = None) -> SkillEvalStore:
    pg = postgres_settings(settings)
    return _memory if pg is None else PostgresSkillEvalStore(pg)


def memory_store() -> InMemorySkillEvalStore:
    """The twin `get_skill_eval_store` hands out under `memory://`."""
    return _memory


def clear_memory() -> None:
    _memory.clear()


__all__ = [
    "InMemorySkillEvalStore",
    "PostgresSkillEvalStore",
    "SkillEvalStore",
    "clear_memory",
    "get_skill_eval_store",
    "memory_store",
]
