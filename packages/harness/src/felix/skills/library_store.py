"""Rows for the tenant skill library: `skill`, `skill_version`, `skill_file`.

Data access only. What a version may become, and who may make it so, is `skills/library.py`;
this module stores what it decides and enforces the two things only storage can — that a
version is written once (its primary key), and that a status change lands only on the state it
was decided against (`SkillStateConflict`), so a publish racing a reject cannot both win.

File bytes are not here. They live in the object store at `skills/{tenant}/{name}/{version}/
{path}`; a `skill_file` row records the digest and size of what was written there.
"""

from __future__ import annotations

import copy
from collections.abc import Collection
from typing import Any, Protocol, runtime_checkable

from felix.config import Settings

# What one catalog load reads from the library, at most. A catalog is offered to the model in
# the system prompt, so a tenant with more live skills than this has outgrown a flat catalog.
MAX_LIBRARY_SKILLS = 500
MAX_VERSIONS_LISTED = 500


class SkillVersionExists(Exception):
    """The `(tenant, name, version)` row is already there — two saves raced to one version."""


class SkillStateConflict(Exception):
    """The version is not in the state the change was decided against, or does not exist."""


@runtime_checkable
class SkillLibraryStore(Protocol):
    async def get_skill(self, tenant_id: str, name: str) -> dict[str, Any] | None: ...

    async def list_skills(
        self, tenant_id: str, *, limit: int = MAX_LIBRARY_SKILLS
    ) -> list[dict[str, Any]]: ...

    async def get_version(self, tenant_id: str, name: str, version: str) -> dict[str, Any] | None: ...

    async def list_versions(
        self, tenant_id: str, name: str, *, limit: int = MAX_VERSIONS_LISTED
    ) -> list[dict[str, Any]]: ...

    async def version_ids(self, tenant_id: str, name: str) -> list[str]: ...

    async def list_files(self, tenant_id: str, name: str, version: str) -> list[dict[str, Any]]: ...

    async def count_pending(self, tenant_id: str, origin_manifest_id: str) -> int: ...

    async def insert_version(
        self, tenant_id: str, row: dict[str, Any], files: list[dict[str, Any]], *, created_by: str, at: int
    ) -> None: ...

    async def delete_draft(self, tenant_id: str, name: str, version: str) -> None: ...

    async def reject(
        self, tenant_id: str, name: str, version: str, *, by: str, note: str, at: int
    ) -> None: ...

    async def publish(
        self, tenant_id: str, name: str, version: str, *, from_statuses: Collection[str], by: str, at: int
    ) -> str | None: ...

    async def archive_skill(self, tenant_id: str, name: str, *, by: str, at: int) -> str | None: ...


# Every `skill_version` column a caller may leave out, at the value Postgres would give it, so
# a row read back from the twin has the same keys as one read back from the table.
_VERSION_DEFAULTS: dict[str, Any] = {
    "parent_version": None,
    "author": "",
    "origin_manifest_id": None,
    "session_id": None,
    "reason": "",
    "description": "",
    "quality_score": 0,
    "security_issues": [],
    "review_checks": [],
    "decided_by": None,
    "decision_note": None,
    "decided_at": None,
    "published_at": None,
}


class InMemorySkillLibraryStore:
    """The `memory://` twin. Copies on the way in and out, as a row read from Postgres is."""

    def __init__(self) -> None:
        self._skills: dict[tuple[str, str], dict[str, Any]] = {}
        self._versions: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._files: dict[tuple[str, str, str], list[dict[str, Any]]] = {}

    def clear(self) -> None:
        self._skills.clear()
        self._versions.clear()
        self._files.clear()

    async def get_skill(self, tenant_id: str, name: str) -> dict[str, Any] | None:
        row = self._skills.get((tenant_id, name))
        return copy.deepcopy(row) if row is not None else None

    async def list_skills(self, tenant_id: str, *, limit: int = MAX_LIBRARY_SKILLS) -> list[dict[str, Any]]:
        rows = sorted((r for (t, _), r in self._skills.items() if t == tenant_id), key=lambda r: r["name"])
        return copy.deepcopy(rows[:limit])

    async def get_version(self, tenant_id: str, name: str, version: str) -> dict[str, Any] | None:
        row = self._versions.get((tenant_id, name, version))
        return copy.deepcopy(row) if row is not None else None

    async def list_versions(
        self, tenant_id: str, name: str, *, limit: int = MAX_VERSIONS_LISTED
    ) -> list[dict[str, Any]]:
        rows = [r for (t, n, _), r in self._versions.items() if t == tenant_id and n == name]
        # Newest first, ending on the version so two saved in one millisecond keep one order.
        rows.sort(key=lambda r: (r["created_at"], r["version"]), reverse=True)
        return copy.deepcopy(rows[:limit])

    async def version_ids(self, tenant_id: str, name: str) -> list[str]:
        return sorted(v for (t, n, v) in self._versions if t == tenant_id and n == name)

    async def list_files(self, tenant_id: str, name: str, version: str) -> list[dict[str, Any]]:
        files = self._files.get((tenant_id, name, version), [])
        return copy.deepcopy(sorted(files, key=lambda f: f["path"]))

    async def count_pending(self, tenant_id: str, origin_manifest_id: str) -> int:
        return sum(
            1
            for (t, _, _), r in self._versions.items()
            if t == tenant_id
            and r["status"] == "draft"
            and r["source"] == "agent"
            and r.get("origin_manifest_id") == origin_manifest_id
        )

    async def insert_version(
        self, tenant_id: str, row: dict[str, Any], files: list[dict[str, Any]], *, created_by: str, at: int
    ) -> None:
        name, version = row["name"], row["version"]
        if (tenant_id, name, version) in self._versions:
            raise SkillVersionExists(f"{name}@{version}")
        skill = self._skills.get((tenant_id, name))
        if skill is None:
            self._skills[(tenant_id, name)] = {
                "tenant_id": tenant_id,
                "name": name,
                "live_version": None,
                "created_by": created_by,
                "created_at": at,
                "updated_at": at,
            }
        else:
            skill["updated_at"] = at
        self._versions[(tenant_id, name, version)] = copy.deepcopy(
            {**_VERSION_DEFAULTS, **row, "tenant_id": tenant_id}
        )
        self._files[(tenant_id, name, version)] = [
            {**f, "tenant_id": tenant_id, "name": name, "version": version} for f in copy.deepcopy(files)
        ]

    async def delete_draft(self, tenant_id: str, name: str, version: str) -> None:
        row = self._versions.get((tenant_id, name, version))
        if row is None or row["status"] != "draft":
            return
        del self._versions[(tenant_id, name, version)]
        self._files.pop((tenant_id, name, version), None)
        if not any(t == tenant_id and n == name for (t, n, _) in self._versions):
            self._skills.pop((tenant_id, name), None)

    async def reject(self, tenant_id: str, name: str, version: str, *, by: str, note: str, at: int) -> None:
        row = self._versions.get((tenant_id, name, version))
        if row is None or row["status"] != "draft":
            raise SkillStateConflict(f"{name}@{version}")
        row.update(status="archived", decided_by=by, decision_note=note, decided_at=at)

    async def publish(
        self, tenant_id: str, name: str, version: str, *, from_statuses: Collection[str], by: str, at: int
    ) -> str | None:
        row = self._versions.get((tenant_id, name, version))
        skill = self._skills.get((tenant_id, name))
        if row is None or skill is None or row["status"] not in from_statuses:
            raise SkillStateConflict(f"{name}@{version}")
        previous = skill["live_version"]
        for (t, n, v), other in self._versions.items():
            if t == tenant_id and n == name and v != version and other["status"] == "published":
                other["status"] = "archived"
        row.update(status="published", decided_by=by, decided_at=at)
        if row.get("published_at") is None:
            row["published_at"] = at
        skill.update(live_version=version, updated_at=at)
        return previous

    async def archive_skill(self, tenant_id: str, name: str, *, by: str, at: int) -> str | None:
        skill = self._skills.get((tenant_id, name))
        if skill is None:
            raise SkillStateConflict(name)
        previous = skill["live_version"]
        for (t, n, _), row in self._versions.items():
            if t == tenant_id and n == name and row["status"] == "published":
                row.update(status="archived", decided_by=by, decided_at=at)
        skill.update(live_version=None, updated_at=at)
        return previous


class PostgresSkillLibraryStore:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _session(self, tenant_id: str) -> Any:
        from felix.db.session import tenant_session

        return tenant_session(self._settings, tenant_id)

    @staticmethod
    def _row(row: Any) -> dict[str, Any]:
        return {c.key: getattr(row, c.key) for c in row.__table__.columns}

    async def get_skill(self, tenant_id: str, name: str) -> dict[str, Any] | None:
        from felix.db.models import SkillRow

        async with self._session(tenant_id) as db:
            row = await db.get(SkillRow, (tenant_id, name))
            return self._row(row) if row is not None else None

    async def list_skills(self, tenant_id: str, *, limit: int = MAX_LIBRARY_SKILLS) -> list[dict[str, Any]]:
        from sqlalchemy import collate, select

        from felix.db.models import SkillRow

        async with self._session(tenant_id) as db:
            rows = (
                await db.scalars(
                    select(SkillRow)
                    .where(SkillRow.tenant_id == tenant_id)
                    # "C" so the order is the memory twin's codepoint order, whatever the
                    # database's default collation.
                    .order_by(collate(SkillRow.name, "C"))
                    .limit(limit)
                )
            ).all()
            return [self._row(r) for r in rows]

    async def get_version(self, tenant_id: str, name: str, version: str) -> dict[str, Any] | None:
        from felix.db.models import SkillVersionRow

        async with self._session(tenant_id) as db:
            row = await db.get(SkillVersionRow, (tenant_id, name, version))
            return self._row(row) if row is not None else None

    async def list_versions(
        self, tenant_id: str, name: str, *, limit: int = MAX_VERSIONS_LISTED
    ) -> list[dict[str, Any]]:
        from sqlalchemy import collate, select

        from felix.db.models import SkillVersionRow

        async with self._session(tenant_id) as db:
            rows = (
                await db.scalars(
                    select(SkillVersionRow)
                    .where(SkillVersionRow.tenant_id == tenant_id, SkillVersionRow.name == name)
                    .order_by(SkillVersionRow.created_at.desc(), collate(SkillVersionRow.version, "C").desc())
                    .limit(limit)
                )
            ).all()
            return [self._row(r) for r in rows]

    async def version_ids(self, tenant_id: str, name: str) -> list[str]:
        from sqlalchemy import collate, select

        from felix.db.models import SkillVersionRow

        async with self._session(tenant_id) as db:
            rows = await db.scalars(
                select(SkillVersionRow.version)
                .where(SkillVersionRow.tenant_id == tenant_id, SkillVersionRow.name == name)
                .order_by(collate(SkillVersionRow.version, "C"))
            )
            return list(rows.all())

    async def list_files(self, tenant_id: str, name: str, version: str) -> list[dict[str, Any]]:
        from sqlalchemy import collate, select

        from felix.db.models import SkillFileRow

        async with self._session(tenant_id) as db:
            rows = (
                await db.scalars(
                    select(SkillFileRow)
                    .where(
                        SkillFileRow.tenant_id == tenant_id,
                        SkillFileRow.name == name,
                        SkillFileRow.version == version,
                    )
                    .order_by(collate(SkillFileRow.path, "C"))
                )
            ).all()
            return [self._row(r) for r in rows]

    async def count_pending(self, tenant_id: str, origin_manifest_id: str) -> int:
        from sqlalchemy import func, select

        from felix.db.models import SkillVersionRow

        async with self._session(tenant_id) as db:
            count = await db.scalar(
                select(func.count())
                .select_from(SkillVersionRow)
                .where(
                    SkillVersionRow.tenant_id == tenant_id,
                    SkillVersionRow.origin_manifest_id == origin_manifest_id,
                    SkillVersionRow.status == "draft",
                    SkillVersionRow.source == "agent",
                )
            )
            return int(count or 0)

    async def insert_version(
        self, tenant_id: str, row: dict[str, Any], files: list[dict[str, Any]], *, created_by: str, at: int
    ) -> None:
        from typing import cast

        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy.exc import IntegrityError

        from felix.db.models import SkillFileRow, SkillRow, SkillVersionRow

        name, version = row["name"], row["version"]
        async with self._session(tenant_id) as db:
            skill = pg_insert(cast(Any, SkillRow.__table__)).values(
                tenant_id=tenant_id,
                name=name,
                live_version=None,
                created_by=created_by,
                created_at=at,
                updated_at=at,
            )
            await db.execute(
                skill.on_conflict_do_update(index_elements=["tenant_id", "name"], set_={"updated_at": at})
            )
            db.add(SkillVersionRow(**{**row, "tenant_id": tenant_id}))
            for f in files:
                db.add(SkillFileRow(tenant_id=tenant_id, name=name, version=version, **f))
            try:
                await db.commit()
            except IntegrityError as exc:
                await db.rollback()
                if getattr(exc.orig, "sqlstate", None) == "23505":
                    raise SkillVersionExists(f"{name}@{version}") from exc
                raise

    async def delete_draft(self, tenant_id: str, name: str, version: str) -> None:
        from sqlalchemy import delete, exists, select

        from felix.db.models import SkillFileRow, SkillRow, SkillVersionRow

        async with self._session(tenant_id) as db:
            gone = await db.execute(
                delete(SkillVersionRow).where(
                    SkillVersionRow.tenant_id == tenant_id,
                    SkillVersionRow.name == name,
                    SkillVersionRow.version == version,
                    SkillVersionRow.status == "draft",
                )
            )
            if getattr(gone, "rowcount", 0):
                await db.execute(
                    delete(SkillFileRow).where(
                        SkillFileRow.tenant_id == tenant_id,
                        SkillFileRow.name == name,
                        SkillFileRow.version == version,
                    )
                )
                others = select(SkillVersionRow.version).where(
                    SkillVersionRow.tenant_id == tenant_id, SkillVersionRow.name == name
                )
                await db.execute(
                    delete(SkillRow).where(
                        SkillRow.tenant_id == tenant_id, SkillRow.name == name, ~exists(others)
                    )
                )
            await db.commit()

    async def _locked_version(self, db: Any, tenant_id: str, name: str, version: str) -> Any:
        from sqlalchemy import select

        from felix.db.models import SkillVersionRow

        return await db.scalar(
            select(SkillVersionRow)
            .where(
                SkillVersionRow.tenant_id == tenant_id,
                SkillVersionRow.name == name,
                SkillVersionRow.version == version,
            )
            .with_for_update()
        )

    async def _locked_skill(self, db: Any, tenant_id: str, name: str) -> Any:
        from sqlalchemy import select

        from felix.db.models import SkillRow

        return await db.scalar(
            select(SkillRow).where(SkillRow.tenant_id == tenant_id, SkillRow.name == name).with_for_update()
        )

    async def reject(self, tenant_id: str, name: str, version: str, *, by: str, note: str, at: int) -> None:
        async with self._session(tenant_id) as db:
            # The skill row first, as `publish` locks it, so a reject and a publish of the same
            # draft serialise on one lock and the second sees the first's status.
            await self._locked_skill(db, tenant_id, name)
            row = await self._locked_version(db, tenant_id, name, version)
            if row is None or row.status != "draft":
                await db.rollback()
                raise SkillStateConflict(f"{name}@{version}")
            row.status, row.decided_by, row.decision_note, row.decided_at = "archived", by, note, at
            await db.commit()

    async def publish(
        self, tenant_id: str, name: str, version: str, *, from_statuses: Collection[str], by: str, at: int
    ) -> str | None:
        from sqlalchemy import update

        from felix.db.models import SkillVersionRow

        async with self._session(tenant_id) as db:
            skill = await self._locked_skill(db, tenant_id, name)
            row = await self._locked_version(db, tenant_id, name, version)
            if skill is None or row is None or row.status not in from_statuses:
                await db.rollback()
                raise SkillStateConflict(f"{name}@{version}")
            previous = skill.live_version
            await db.execute(
                update(SkillVersionRow)
                .where(
                    SkillVersionRow.tenant_id == tenant_id,
                    SkillVersionRow.name == name,
                    SkillVersionRow.version != version,
                    SkillVersionRow.status == "published",
                )
                .values(status="archived")
            )
            row.status, row.decided_by, row.decided_at = "published", by, at
            if row.published_at is None:
                row.published_at = at
            skill.live_version, skill.updated_at = version, at
            await db.commit()
            return previous

    async def archive_skill(self, tenant_id: str, name: str, *, by: str, at: int) -> str | None:
        from sqlalchemy import update

        from felix.db.models import SkillVersionRow

        async with self._session(tenant_id) as db:
            skill = await self._locked_skill(db, tenant_id, name)
            if skill is None:
                await db.rollback()
                raise SkillStateConflict(name)
            previous = skill.live_version
            await db.execute(
                update(SkillVersionRow)
                .where(
                    SkillVersionRow.tenant_id == tenant_id,
                    SkillVersionRow.name == name,
                    SkillVersionRow.status == "published",
                )
                .values(status="archived", decided_by=by, decided_at=at)
            )
            skill.live_version, skill.updated_at = None, at
            await db.commit()
            return previous


_memory_store = InMemorySkillLibraryStore()


def get_skill_library_store(settings: Settings | None = None) -> SkillLibraryStore:
    """The library store for these settings: the process twin under `memory://`, else Postgres.

    Selected exactly as `skills/store.py:get_skill_activation_store` selects, so a deployment
    that keeps activations in memory keeps the library there too.
    """
    if settings is None:
        return _memory_store
    url = settings.database_url
    if ":memory:" in url or "sqlite" in url or url.startswith("memory://"):
        return _memory_store
    return PostgresSkillLibraryStore(settings)


def clear_memory() -> None:
    """Drop the in-memory library. Test seam, matching the other `memory://` stores."""
    _memory_store.clear()


__all__ = [
    "MAX_LIBRARY_SKILLS",
    "MAX_VERSIONS_LISTED",
    "InMemorySkillLibraryStore",
    "PostgresSkillLibraryStore",
    "SkillLibraryStore",
    "SkillStateConflict",
    "SkillVersionExists",
    "clear_memory",
    "get_skill_library_store",
]
