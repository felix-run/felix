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

The row also carries the skill's `skill.update_available` notification (`update_notify.py`,
migration 0030): the digest last queued, and its delivery. Only the three notification methods
write those columns, and `get` and `due` do not return them; the delivery sweep claims across
tenants (`claim_notifications`) as the check sweep reads.
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
# The `skill.update_available` notification's columns (`update_notify.py`, migration 0030), each at
# the value Postgres gives a new row. Never written by `record`: only by the three notification
# methods, so a check can never reset a delivery, nor a delivery a check.
NOTIFY_DEFAULTS: dict[str, Any] = {
    "notified_tree_hash": None,
    "notify_status": None,
    "notify_due_at": None,
    "notify_attempts": 0,
    "notify_claim_until": None,
    "notify_generation": 0,
    "notify_checked_at": None,
    "notify_state": {},
}
NOTIFY_COLUMNS: tuple[str, ...] = tuple(NOTIFY_DEFAULTS)


def state_of(
    *,
    origin_source: str,
    origin_ref: str,
    commit: str,
    tree_hash: str,
    first_seen_at: int,
    checked_at: int,
) -> dict[str, Any]:
    """The row a successful check records: the origin it read, what it found there, when this
    tenant first saw those files, and when -- with no error. The one place the shape is built."""
    return {
        "origin_source": origin_source,
        "origin_ref": origin_ref,
        "upstream_commit": commit,
        "upstream_tree_hash": tree_hash,
        "first_seen_at": first_seen_at,
        "checked_at": checked_at,
        "error": None,
    }


def _may_queue(row: Mapping[str, Any], tree_hash: str, checked_at: int) -> bool:
    """Whether a check at ``checked_at`` finding ``tree_hash`` may queue over what ``row`` holds:
    not the digest already queued (unless that one was superseded, so never sent), and not a
    check older than the one that queued what is there. Both stores decide by this."""
    if row["notified_tree_hash"] == tree_hash and row["notify_status"] != "superseded":
        return False
    return row["notify_checked_at"] is None or checked_at >= row["notify_checked_at"]


@runtime_checkable
class SkillUpstreamStore(Protocol):
    async def record(self, tenant_id: str, name: str, state: Mapping[str, Any]) -> None:
        """Insert the skill's row, or update the columns ``state`` names and leave the rest.
        ``state`` must name the origin (`origin_source`, `origin_ref`)."""
        ...

    async def get(self, tenant_id: str, names: Collection[str]) -> dict[str, dict[str, Any]]:
        """The rows of the named skills that have one, by name."""
        ...

    async def due(
        self, *, checked_by: int, limit: int, exclude: Collection[str] = ()
    ) -> list[dict[str, Any]]:
        """Up to ``limit`` rows of any tenant but those in ``exclude``, never checked or last
        checked at or before ``checked_by``: never-checked first, then oldest check, then by tenant
        and name. ``exclude`` is how a sweep leaves out a tenant whose budget is spent, so that
        tenant's backlog does not fill every batch."""
        ...

    async def enqueue_notification(
        self,
        tenant_id: str,
        name: str,
        *,
        tree_hash: str,
        checked_at: int,
        state: Mapping[str, Any],
        due_at: int,
    ) -> tuple[bool, str | None]:
        """Queue the skill's notification of ``tree_hash``, found by the check at ``checked_at``:
        pending, due at ``due_at``, no tries yet, unclaimed, the next generation, with ``state``
        (its event and endpoints). Refused -- (False, None) -- for a skill with no row (a
        notification is about a check); for the digest already queued, unless that one was
        superseded (once per digest); and for a check older than the one that queued what is
        there (a slow check never replaces a newer digest with a staler one). Otherwise (True,
        the status of the notification it replaced: `pending` when an undelivered one is
        superseded)."""
        ...

    async def cancel_notification(self, tenant_id: str, name: str, *, checked_at: int) -> bool:
        """Mark a pending notification `superseded` -- never sent -- when the check at
        ``checked_at`` is no older than the one that queued it. Whether one was."""
        ...

    async def claim_notifications(
        self, *, now: int, claim_until: int, limit: int, per_tenant: int
    ) -> list[dict[str, Any]]:
        """Up to ``limit`` pending notifications of any tenant, no more than ``per_tenant`` of one
        tenant, due by ``now`` and not claimed (or their claim lapsed by ``now``), each claimed
        until ``claim_until``: earliest due first, ties by tenant and name. Each row carries the
        key, the upstream columns and the notification's."""
        ...

    async def save_notification(
        self,
        tenant_id: str,
        name: str,
        *,
        generation: int,
        status: str,
        due_at: int | None,
        attempts: int,
        state: Mapping[str, Any],
    ) -> bool:
        """Write a delivery's outcome and release its claim -- unless the row has queued again
        since (another generation, even of the same digest), whose notification this outcome must
        not overwrite. Whether it was written."""
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
            row = {
                "tenant_id": tenant_id,
                "name": name,
                **dict.fromkeys(UPSTREAM_COLUMNS),
                **copy.deepcopy(NOTIFY_DEFAULTS),
            }
            self._rows[(tenant_id, name)] = row
        row.update(copy.deepcopy(dict(state)))

    @staticmethod
    def _upstream(row: Mapping[str, Any]) -> dict[str, Any]:
        """The key and the upstream columns, as `PostgresSkillUpstreamStore._row` reads them."""
        return copy.deepcopy({k: row[k] for k in ("tenant_id", "name", *UPSTREAM_COLUMNS)})

    async def get(self, tenant_id: str, names: Collection[str]) -> dict[str, dict[str, Any]]:
        found = {n: self._rows.get((tenant_id, n)) for n in set(names)}
        return {n: self._upstream(r) for n, r in found.items() if r is not None}

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
        return [self._upstream(r) for r in rows[:limit]]

    async def enqueue_notification(
        self,
        tenant_id: str,
        name: str,
        *,
        tree_hash: str,
        checked_at: int,
        state: Mapping[str, Any],
        due_at: int,
    ) -> tuple[bool, str | None]:
        row = self._rows.get((tenant_id, name))
        if row is None or not _may_queue(row, tree_hash, checked_at):
            return False, None
        previous = row["notify_status"]
        row.update(
            notified_tree_hash=tree_hash,
            notify_status="pending",
            notify_due_at=due_at,
            notify_attempts=0,
            notify_claim_until=None,
            notify_generation=row["notify_generation"] + 1,
            notify_checked_at=checked_at,
            notify_state=copy.deepcopy(dict(state)),
        )
        return True, previous

    async def cancel_notification(self, tenant_id: str, name: str, *, checked_at: int) -> bool:
        row = self._rows.get((tenant_id, name))
        if row is None or row["notify_status"] != "pending" or (row["notify_checked_at"] or 0) > checked_at:
            return False
        row.update(notify_status="superseded", notify_due_at=None, notify_claim_until=None)
        return True

    async def claim_notifications(
        self, *, now: int, claim_until: int, limit: int, per_tenant: int
    ) -> list[dict[str, Any]]:
        due = sorted(
            (
                r
                for r in self._rows.values()
                if r["notify_status"] == "pending"
                and r["notify_due_at"] is not None
                and r["notify_due_at"] <= now
                and (r["notify_claim_until"] is None or r["notify_claim_until"] <= now)
            ),
            key=lambda r: (r["notify_due_at"], r["tenant_id"], r["name"]),
        )
        taken: dict[str, int] = {}
        rows: list[dict[str, Any]] = []
        for r in due:
            if len(rows) == limit:
                break
            if taken.get(r["tenant_id"], 0) >= per_tenant:
                continue
            taken[r["tenant_id"]] = taken.get(r["tenant_id"], 0) + 1
            r["notify_claim_until"] = claim_until
            rows.append(r)
        return copy.deepcopy(rows)

    async def save_notification(
        self,
        tenant_id: str,
        name: str,
        *,
        generation: int,
        status: str,
        due_at: int | None,
        attempts: int,
        state: Mapping[str, Any],
    ) -> bool:
        row = self._rows.get((tenant_id, name))
        if row is None or row["notify_generation"] != generation:
            return False
        row.update(
            notify_status=status,
            notify_due_at=due_at,
            notify_attempts=attempts,
            notify_claim_until=None,
            notify_state=copy.deepcopy(dict(state)),
        )
        return True


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

    @classmethod
    def _notify_row(cls, r: Any) -> dict[str, Any]:
        return {**cls._row(r), **{c: getattr(r, c) for c in NOTIFY_COLUMNS}}

    async def enqueue_notification(
        self,
        tenant_id: str,
        name: str,
        *,
        tree_hash: str,
        checked_at: int,
        state: Mapping[str, Any],
        due_at: int,
    ) -> tuple[bool, str | None]:
        from sqlalchemy import select

        from felix.db.models import SkillUpstreamRow
        from felix.db.session import tenant_session

        R = SkillUpstreamRow
        async with tenant_session(self._settings, tenant_id) as db:
            # Locked, so of two checks recording one new digest at once exactly one queues it.
            row = await db.scalar(select(R).where(R.tenant_id == tenant_id, R.name == name).with_for_update())
            if row is None or not _may_queue(self._notify_row(row), tree_hash, checked_at):
                return False, None
            previous = row.notify_status
            row.notified_tree_hash = tree_hash
            row.notify_status = "pending"
            row.notify_due_at = due_at
            row.notify_attempts = 0
            row.notify_claim_until = None
            row.notify_generation = row.notify_generation + 1
            row.notify_checked_at = checked_at
            row.notify_state = dict(state)
            await db.commit()
            return True, previous

    async def cancel_notification(self, tenant_id: str, name: str, *, checked_at: int) -> bool:
        from sqlalchemy import or_, update

        from felix.db.models import SkillUpstreamRow
        from felix.db.session import tenant_session

        R = SkillUpstreamRow
        stmt = (
            update(R)
            .where(
                R.tenant_id == tenant_id,
                R.name == name,
                R.notify_status == "pending",
                or_(R.notify_checked_at.is_(None), R.notify_checked_at <= checked_at),
            )
            .values(notify_status="superseded", notify_due_at=None, notify_claim_until=None)
            .returning(R.name)
        )
        async with tenant_session(self._settings, tenant_id) as db:
            cancelled = (await db.execute(stmt)).first() is not None
            await db.commit()
            return cancelled

    async def claim_notifications(
        self, *, now: int, claim_until: int, limit: int, per_tenant: int
    ) -> list[dict[str, Any]]:
        from sqlalchemy import and_, collate, func, or_, select

        from felix.db.models import SkillUpstreamRow
        from felix.db.session import get_session_factory, rls_bypass

        R = SkillUpstreamRow
        due = (
            R.notify_status == "pending",
            R.notify_due_at <= now,
            or_(R.notify_claim_until.is_(None), R.notify_claim_until <= now),
        )
        # Each tenant's earliest `per_tenant`, so one tenant's backlog cannot fill a tick. In a
        # subquery, since a window function and FOR UPDATE cannot share a SELECT.
        ranked = (
            select(
                R.tenant_id,
                R.name,
                func.row_number()
                .over(partition_by=R.tenant_id, order_by=(R.notify_due_at, collate(R.name, "C")))
                .label("rank"),
            )
            .where(*due)
            .subquery()
        )
        stmt = (
            select(R)
            .join(ranked, and_(R.tenant_id == ranked.c.tenant_id, R.name == ranked.c.name))
            .where(ranked.c.rank <= per_tenant, *due)
            .order_by(R.notify_due_at, collate(R.tenant_id, "C"), collate(SkillUpstreamRow.name, "C"))
            .limit(limit)
            .with_for_update(of=R, skip_locked=True)
        )
        # Cross-tenant maintenance, as the check sweep's read is.
        with rls_bypass():
            async with get_session_factory(settings=self._settings)() as db:
                rows = (await db.scalars(stmt)).all()
                for r in rows:
                    r.notify_claim_until = claim_until
                out = [self._notify_row(r) for r in rows]
                await db.commit()
                return out

    async def save_notification(
        self,
        tenant_id: str,
        name: str,
        *,
        generation: int,
        status: str,
        due_at: int | None,
        attempts: int,
        state: Mapping[str, Any],
    ) -> bool:
        from sqlalchemy import update

        from felix.db.models import SkillUpstreamRow
        from felix.db.session import tenant_session

        R = SkillUpstreamRow
        stmt = (
            update(R)
            .where(R.tenant_id == tenant_id, R.name == name, R.notify_generation == generation)
            .values(
                notify_status=status,
                notify_due_at=due_at,
                notify_attempts=attempts,
                notify_claim_until=None,
                notify_state=dict(state),
            )
            .returning(R.name)
        )
        async with tenant_session(self._settings, tenant_id) as db:
            written = (await db.execute(stmt)).first() is not None
            await db.commit()
            return written


_memory = InMemorySkillUpstreamStore()


def get_upstream_store(settings: Settings | None = None) -> SkillUpstreamStore:
    from felix.skills.quality_store import postgres_settings

    pg = postgres_settings(settings)
    return _memory if pg is None else PostgresSkillUpstreamStore(pg)


def clear_memory() -> None:
    _memory.clear()


__all__ = [
    "NOTIFY_COLUMNS",
    "NOTIFY_DEFAULTS",
    "UPSTREAM_COLUMNS",
    "InMemorySkillUpstreamStore",
    "PostgresSkillUpstreamStore",
    "SkillUpstreamStore",
    "clear_memory",
    "get_upstream_store",
    "state_of",
]
