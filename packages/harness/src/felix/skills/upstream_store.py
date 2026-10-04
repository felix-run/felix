"""What an imported skill's origin held when Felix last looked: one row per library skill.

Written by every import (`importer.import_skill`, an import being a check too), every upstream
check of the stored ref (`upstream.check_upstream`, `upstream.outdated`), and the periodic sweep
(`upstream.run_upstream_checks`). Read by the upstream listing with `refresh=false` and by the
library detail, so neither has to ask GitHub to say whether an update is waiting.

Each row names the origin it was checked against (`origin_source`, `origin_ref`), the commit and
kept-file digest found there, when this tenant first saw that digest (the cooldown's clock, kept in
`sighting_store`; copied here so a listing can say when the update becomes eligible), when the check
ran, and the refusal code of the last one if it failed -- a failed check keeps the last good
upstream state. Whether an update is available is computed at read time against the skill's newest
version, so a row is never stale about what the library holds.

The sweep reads across tenants (`due`), oldest check first, under the RLS bypass, as the retention
sweep does; everything else is tenant-scoped.
"""

from __future__ import annotations

import copy
from collections.abc import Collection, Mapping
from typing import Any, Protocol, runtime_checkable

from felix.config import Settings

# Every column but the key, at the value Postgres gives an omitted one.
UPSTREAM_COLUMNS: tuple[str, ...] = (
    "origin_source",
    "origin_ref",
    "upstream_commit",
    "upstream_tree_hash",
    "first_seen_at",
    "checked_at",
    "error",
)


@runtime_checkable
class SkillUpstreamStore(Protocol):
    async def record(self, tenant_id: str, name: str, state: Mapping[str, Any]) -> None:
        """Insert the skill's row, or update the columns ``state`` names and leave the rest.
        ``state`` must name the origin (`origin_source`, `origin_ref`)."""
        ...

    async def get(self, tenant_id: str, names: Collection[str]) -> dict[str, dict[str, Any]]:
        """The rows of the named skills that have one, by name."""
        ...

    async def forget(self, tenant_id: str, name: str) -> None:
        """Drop the skill's row: it is no longer an import."""
        ...

    async def due(
        self, *, checked_by: int, limit: int, exclude: Collection[str] = ()
    ) -> list[dict[str, Any]]:
        """Up to ``limit`` rows of any tenant but those in ``exclude``, never checked or last
        checked at or before ``checked_by``: never-checked first, then oldest check, then by tenant
        and name. ``exclude`` is how a sweep leaves out a tenant whose budget is spent, so that
        tenant's backlog does not fill every batch."""
        ...


class InMemorySkillUpstreamStore:
    """The `memory://` twin. Copies on the way in and out, as a row read from Postgres is."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], dict[str, Any]] = {}

    def clear(self) -> None:
        self._rows.clear()

    async def record(self, tenant_id: str, name: str, state: Mapping[str, Any]) -> None:
        unknown = set(state) - set(UPSTREAM_COLUMNS)
        if unknown:
            raise ValueError(f"not upstream columns: {sorted(unknown)}")
        row = self._rows.get((tenant_id, name))
        if row is None:
            if not state.get("origin_source") or not state.get("origin_ref"):
                raise ValueError("a new upstream row names its origin")
            row = {"tenant_id": tenant_id, "name": name, **dict.fromkeys(UPSTREAM_COLUMNS)}
            self._rows[(tenant_id, name)] = row
        row.update(copy.deepcopy(dict(state)))

    async def get(self, tenant_id: str, names: Collection[str]) -> dict[str, dict[str, Any]]:
        found = {n: self._rows.get((tenant_id, n)) for n in set(names)}
        return {n: copy.deepcopy(r) for n, r in found.items() if r is not None}

    async def forget(self, tenant_id: str, name: str) -> None:
        self._rows.pop((tenant_id, name), None)

    async def due(
        self, *, checked_by: int, limit: int, exclude: Collection[str] = ()
    ) -> list[dict[str, Any]]:
        left_out = set(exclude)
        rows = sorted(
            (
                r
                for r in self._rows.values()
                if r["tenant_id"] not in left_out
                and (r["checked_at"] is None or r["checked_at"] <= checked_by)
            ),
            # Never-checked first, then the oldest check; ties by the key, as the table orders them.
            key=lambda r: (r["checked_at"] is not None, r["checked_at"] or 0, r["tenant_id"], r["name"]),
        )
        return copy.deepcopy(rows[:limit])


class PostgresSkillUpstreamStore:
    """The `skill_upstream` table (`0029`), tenant-scoped under the same RLS policy as every
    other skill table."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @staticmethod
    def _row(r: Any) -> dict[str, Any]:
        return {"tenant_id": r.tenant_id, "name": r.name, **{c: getattr(r, c) for c in UPSTREAM_COLUMNS}}

    async def record(self, tenant_id: str, name: str, state: Mapping[str, Any]) -> None:
        from typing import cast

        from sqlalchemy.dialects.postgresql import insert as pg_insert

        from felix.db.models import SkillUpstreamRow
        from felix.db.session import tenant_session

        unknown = set(state) - set(UPSTREAM_COLUMNS)
        if unknown:
            raise ValueError(f"not upstream columns: {sorted(unknown)}")
        values = {"tenant_id": tenant_id, "name": name, **dict(state)}
        stmt = pg_insert(cast(Any, SkillUpstreamRow.__table__)).values(values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["tenant_id", "name"], set_={c: stmt.excluded[c] for c in state}
        )
        async with tenant_session(self._settings, tenant_id) as db:
            await db.execute(stmt)
            await db.commit()

    async def get(self, tenant_id: str, names: Collection[str]) -> dict[str, dict[str, Any]]:
        from sqlalchemy import select

        from felix.db.models import SkillUpstreamRow
        from felix.db.session import tenant_session

        wanted = sorted(set(names))
        if not wanted:
            return {}
        R = SkillUpstreamRow
        async with tenant_session(self._settings, tenant_id) as db:
            rows = (await db.scalars(select(R).where(R.tenant_id == tenant_id, R.name.in_(wanted)))).all()
            return {r.name: self._row(r) for r in rows}

    async def forget(self, tenant_id: str, name: str) -> None:
        from sqlalchemy import delete

        from felix.db.models import SkillUpstreamRow
        from felix.db.session import tenant_session

        R = SkillUpstreamRow
        async with tenant_session(self._settings, tenant_id) as db:
            await db.execute(delete(R).where(R.tenant_id == tenant_id, R.name == name))
            await db.commit()

    async def due(
        self, *, checked_by: int, limit: int, exclude: Collection[str] = ()
    ) -> list[dict[str, Any]]:
        from sqlalchemy import collate, or_, select

        from felix.db.models import SkillUpstreamRow
        from felix.db.session import get_session_factory, rls_bypass

        R = SkillUpstreamRow
        stmt = (
            select(R)
            .where(or_(R.checked_at.is_(None), R.checked_at <= checked_by))
            .where(R.tenant_id.not_in(sorted(set(exclude))) if exclude else R.tenant_id.is_not(None))
            # "C" so ties order as the memory twin's codepoint order, whatever the collation; the
            # last key by the model's own name, where `test_ordering_rule` can see it is the key.
            .order_by(
                R.checked_at.asc().nulls_first(),
                collate(R.tenant_id, "C"),
                collate(SkillUpstreamRow.name, "C"),
            )
            .limit(limit)
        )
        # Cross-tenant maintenance: without the bypass RLS makes the read return nothing.
        with rls_bypass():
            async with get_session_factory(settings=self._settings)() as db:
                return [self._row(r) for r in (await db.scalars(stmt)).all()]


_memory = InMemorySkillUpstreamStore()


def get_upstream_store(settings: Settings | None = None) -> SkillUpstreamStore:
    from felix.skills.quality_store import postgres_settings

    pg = postgres_settings(settings)
    return _memory if pg is None else PostgresSkillUpstreamStore(pg)


def clear_memory() -> None:
    _memory.clear()


__all__ = [
    "UPSTREAM_COLUMNS",
    "InMemorySkillUpstreamStore",
    "PostgresSkillUpstreamStore",
    "SkillUpstreamStore",
    "clear_memory",
    "get_upstream_store",
]
