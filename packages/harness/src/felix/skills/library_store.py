"""Rows for the tenant skill library: `skill`, `skill_version`, `skill_file`.

Data access only. What a version may become, and who may make it so, is `skills/library.py`;
this module stores what it decides and enforces the things only storage can — that a version is
written once (its primary key), that a status change lands only on the state it was decided
against (`SkillStateConflict`), so a publish racing a reject cannot both win, and that the
pending cap is exact: a capped `insert_version` counts the origin manifest's agent drafts and
writes its own in one transaction, under an advisory lock per `(tenant, origin manifest)`
(`pending_lock_key`), so saves racing at the cap land one at a time (`SkillPendingFull`).

File bytes are not here. They live in the object store under `library_object_key`, and a
`skill_file` row records the digest and size of what was written there.
"""

from __future__ import annotations

import copy
from collections.abc import Collection
from typing import Any, Literal, Protocol, runtime_checkable

from felix.config import Settings

# What one catalog load reads from the library, at most. A catalog is offered to the model in
# the system prompt, so a tenant with more live skills than this has outgrown a flat catalog.
MAX_LIBRARY_SKILLS = 500
MAX_VERSIONS_LISTED = 500
# Versions one skill may hold. Each is rows plus objects that nothing collects, so a loop
# saving the same skill is bounded per name as well as per manifest (the pending cap).
MAX_VERSIONS_PER_SKILL = 200

SkillStatus = Literal["draft", "published", "archived"]

# The library's own prefix in the object store, deliberately not `skills/`. The operator's
# layout there is `skills/{tenant}/{name}[/{version}]/SKILL.md`, which `load_skill_from_store`
# reads for a declared ref; library bytes under the same keys would let an agent's draft
# overwrite an operator's pinned skill, and be served by a ref that pinned the draft's version.
LIBRARY_PREFIX = "skill-library"


def library_object_key(tenant_id: str, name: str, version: str, path: str) -> str:
    """Where one file of one library version lives. The only spelling of that key."""
    return f"{LIBRARY_PREFIX}/{tenant_id}/{name}/{version}/{path}"


# Where the review queue resumes: the `(created_at, name, version)` of the last draft a page held.
DraftCursor = tuple[int, str, str]

# What `summarize` reports of each skill's newest version: enough to filter and sort a listing
# by, without the review record a detail page reads.
SUMMARY_COLUMNS = ("version", "status", "source", "quality_score", "security_status", "created_at")


class SkillVersionExists(Exception):
    """The `(tenant, name, version)` row is already there — two saves raced to one version."""


class SkillPendingFull(Exception):
    """The origin manifest already holds ``held`` agent drafts, and ``limit`` is its cap."""

    def __init__(self, held: int, limit: int) -> None:
        super().__init__(f"{held} of {limit}")
        self.held, self.limit = held, limit


def pending_lock_key(tenant_id: str, origin_manifest_id: str) -> str:
    """The advisory lock a capped draft save takes: one per origin manifest."""
    return f"skill_drafts:{tenant_id}:{origin_manifest_id}"


class SkillStateConflict(Exception):
    """The version is not in the state the change was decided against, or does not exist."""


class SkillLiveMismatch(Exception):
    """A publish named the live version it expected, and the skill's live version is another."""


class _AnyLive:
    """`publish(expected_live=ANY_LIVE)`: whatever is live, as before callers could say."""

    def __repr__(self) -> str:
        return "ANY_LIVE"


ANY_LIVE = _AnyLive()
# What a publish expects to be live: a version, None (nothing), or `ANY_LIVE` (no expectation).
ExpectedLive = str | None | _AnyLive


@runtime_checkable
class SkillLibraryStore(Protocol):
    async def get_skill(self, tenant_id: str, name: str) -> dict[str, Any] | None: ...

    async def get_skills(self, tenant_id: str, names: Collection[str]) -> dict[str, dict[str, Any]]: ...

    async def list_skills(
        self, tenant_id: str, *, limit: int = MAX_LIBRARY_SKILLS, after: str | None = None
    ) -> list[dict[str, Any]]: ...

    async def summarize(self, tenant_id: str, names: Collection[str]) -> dict[str, dict[str, Any]]: ...

    async def list_drafts(
        self, tenant_id: str, *, limit: int = MAX_VERSIONS_LISTED, after: DraftCursor | None = None
    ) -> list[dict[str, Any]]: ...

    async def list_live(self, tenant_id: str, *, limit: int = MAX_LIBRARY_SKILLS) -> list[dict[str, Any]]: ...

    async def get_version(self, tenant_id: str, name: str, version: str) -> dict[str, Any] | None: ...

    async def list_versions(
        self, tenant_id: str, name: str, *, limit: int = MAX_VERSIONS_LISTED
    ) -> list[dict[str, Any]]: ...

    async def version_ids(self, tenant_id: str, name: str) -> list[str]: ...

    async def list_files(self, tenant_id: str, name: str, version: str) -> list[dict[str, Any]]: ...

    async def count_pending(self, tenant_id: str, origin_manifest_id: str) -> int: ...

    async def insert_version(
        self,
        tenant_id: str,
        row: dict[str, Any],
        files: list[dict[str, Any]],
        *,
        created_by: str,
        at: int,
        max_pending: int | None = None,
    ) -> None:
        """Write a version once (`SkillVersionExists`). With ``max_pending``, refused
        (`SkillPendingFull`) when the row's origin manifest already holds that many agent
        drafts, counted in the same transaction as the write."""
        ...

    async def delete_draft(self, tenant_id: str, name: str, version: str) -> None: ...

    async def reject(
        self, tenant_id: str, name: str, version: str, *, by: str, note: str, at: int
    ) -> None: ...

    async def publish(
        self,
        tenant_id: str,
        name: str,
        version: str,
        *,
        from_statuses: Collection[str],
        by: str,
        at: int,
        expected_live: ExpectedLive = ANY_LIVE,
    ) -> str | None: ...

    async def buildable_versions(self, tenant_id: str, names: Collection[str]) -> dict[str, list[str]]: ...

    async def archive_skill(self, tenant_id: str, name: str, *, by: str, at: int) -> str | None: ...


def is_rejected(row: dict[str, Any]) -> bool:
    """A draft that was rejected: archived without ever having gone live. A version that was
    published and later superseded is archived too, and is not rejected."""
    return row.get("status") == "archived" and row.get("published_at") is None


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
    "origin_source": None,
    "origin_ref": None,
    "origin_commit": None,
    "origin_tree_hash": None,
    "origin_license": None,
    "origin_committed_at": None,
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

    async def get_skills(self, tenant_id: str, names: Collection[str]) -> dict[str, dict[str, Any]]:
        found = {n: self._skills.get((tenant_id, n)) for n in set(names)}
        return {n: copy.deepcopy(r) for n, r in found.items() if r is not None}

    async def list_skills(
        self, tenant_id: str, *, limit: int = MAX_LIBRARY_SKILLS, after: str | None = None
    ) -> list[dict[str, Any]]:
        rows = sorted(
            (r for (t, n), r in self._skills.items() if t == tenant_id and (after is None or n > after)),
            key=lambda r: r["name"],
        )
        return copy.deepcopy(rows[:limit])

    async def summarize(self, tenant_id: str, names: Collection[str]) -> dict[str, dict[str, Any]]:
        wanted = set(names)
        out: dict[str, dict[str, Any]] = {}
        for (t, n, _), row in self._versions.items():
            if t != tenant_id or n not in wanted:
                continue
            entry = out.setdefault(n, {"latest": None, "pending": 0})
            entry["pending"] += row["status"] == "draft"
            latest = entry["latest"]
            if latest is None or (row["created_at"], row["version"]) > (
                latest["created_at"],
                latest["version"],
            ):
                entry["latest"] = {k: row[k] for k in SUMMARY_COLUMNS}
        return copy.deepcopy(out)

    async def list_drafts(
        self, tenant_id: str, *, limit: int = MAX_VERSIONS_LISTED, after: DraftCursor | None = None
    ) -> list[dict[str, Any]]:
        rows = [
            r
            for (t, _, _), r in self._versions.items()
            if t == tenant_id
            and r["status"] == "draft"
            and (after is None or (r["created_at"], r["name"], r["version"]) > after)
        ]
        # Oldest first, ending on the primary key so a page boundary falls on one row.
        rows.sort(key=lambda r: (r["created_at"], r["name"], r["version"]))
        return copy.deepcopy(rows[:limit])

    async def list_live(self, tenant_id: str, *, limit: int = MAX_LIBRARY_SKILLS) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for (t, name), skill in self._skills.items():
            version = skill["live_version"]
            if t != tenant_id or not version:
                continue
            files = self._files.get((t, name, version), [])
            digest = next((f["sha256"] for f in files if f["path"] == "SKILL.md"), None)
            rows.append({"name": name, "version": version, "sha256": digest})
        rows = sorted(rows, key=lambda r: r["name"])
        return rows[:limit]

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
        return self._pending(tenant_id, origin_manifest_id)

    def _pending(self, tenant_id: str, origin_manifest_id: str) -> int:
        return sum(
            1
            for (t, _, _), r in self._versions.items()
            if t == tenant_id
            and r["status"] == "draft"
            and r["source"] == "agent"
            and r.get("origin_manifest_id") == origin_manifest_id
        )

    async def insert_version(
        self,
        tenant_id: str,
        row: dict[str, Any],
        files: list[dict[str, Any]],
        *,
        created_by: str,
        at: int,
        max_pending: int | None = None,
    ) -> None:
        name, version = row["name"], row["version"]
        # No await from the count to the write: one event loop cannot interleave another save.
        if (
            max_pending is not None
            and (held := self._pending(tenant_id, str(row.get("origin_manifest_id")))) >= max_pending
        ):
            raise SkillPendingFull(held, max_pending)
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
        self,
        tenant_id: str,
        name: str,
        version: str,
        *,
        from_statuses: Collection[str],
        by: str,
        at: int,
        expected_live: ExpectedLive = ANY_LIVE,
    ) -> str | None:
        row = self._versions.get((tenant_id, name, version))
        skill = self._skills.get((tenant_id, name))
        if row is None or skill is None or row["status"] not in from_statuses:
            raise SkillStateConflict(f"{name}@{version}")
        previous = skill["live_version"]
        if expected_live is not ANY_LIVE and previous != expected_live:
            raise SkillLiveMismatch(f"{name} is live at {previous}, not {expected_live}")
        for (t, n, v), other in self._versions.items():
            if t == tenant_id and n == name and v != version and other["status"] == "published":
                other["status"] = "archived"
        row.update(status="published", decided_by=by, decided_at=at)
        if row.get("published_at") is None:
            row["published_at"] = at
        skill.update(live_version=version, updated_at=at)
        return previous

    async def buildable_versions(self, tenant_id: str, names: Collection[str]) -> dict[str, list[str]]:
        wanted = set(names)
        out: dict[str, list[str]] = {}
        for (t, n, v), row in self._versions.items():
            if t == tenant_id and n in wanted and not is_rejected(row):
                out.setdefault(n, []).append(v)
        return {n: sorted(vs) for n, vs in out.items()}

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

    async def get_skills(self, tenant_id: str, names: Collection[str]) -> dict[str, dict[str, Any]]:
        """Several skills by name in one query, for a page that names many."""
        from sqlalchemy import select

        from felix.db.models import SkillRow

        if not names:
            return {}
        async with self._session(tenant_id) as db:
            rows = (
                await db.scalars(
                    select(SkillRow).where(
                        SkillRow.tenant_id == tenant_id, SkillRow.name.in_(list(set(names)))
                    )
                )
            ).all()
            return {r.name: self._row(r) for r in rows}

    async def list_skills(
        self, tenant_id: str, *, limit: int = MAX_LIBRARY_SKILLS, after: str | None = None
    ) -> list[dict[str, Any]]:
        from sqlalchemy import collate, select

        from felix.db.models import SkillRow

        stmt = select(SkillRow).where(SkillRow.tenant_id == tenant_id)
        if after is not None:
            stmt = stmt.where(collate(SkillRow.name, "C") > after)
        async with self._session(tenant_id) as db:
            rows = (
                await db.scalars(
                    # "C" so the order is the memory twin's codepoint order, whatever the
                    # database's default collation.
                    stmt.order_by(collate(SkillRow.name, "C")).limit(limit)
                )
            ).all()
            return [self._row(r) for r in rows]

    async def summarize(self, tenant_id: str, names: Collection[str]) -> dict[str, dict[str, Any]]:
        """Each named skill's newest version and how many of its versions are drafts: two
        queries for a page of skills, rather than two per skill."""
        from sqlalchemy import collate, func, select

        from felix.db.models import SkillVersionRow

        if not names:
            return {}
        names = list(names)
        columns = [getattr(SkillVersionRow, c) for c in SUMMARY_COLUMNS]
        async with self._session(tenant_id) as db:
            newest = (
                await db.execute(
                    select(SkillVersionRow.name, *columns)
                    .where(SkillVersionRow.tenant_id == tenant_id, SkillVersionRow.name.in_(names))
                    .distinct(SkillVersionRow.name)
                    .order_by(
                        SkillVersionRow.name,
                        SkillVersionRow.created_at.desc(),
                        collate(SkillVersionRow.version, "C").desc(),
                    )
                )
            ).all()
            pending = (
                await db.execute(
                    select(SkillVersionRow.name, func.count())
                    .where(
                        SkillVersionRow.tenant_id == tenant_id,
                        SkillVersionRow.name.in_(names),
                        SkillVersionRow.status == "draft",
                    )
                    .group_by(SkillVersionRow.name)
                )
            ).all()
        out: dict[str, dict[str, Any]] = {
            r[0]: {"latest": dict(zip(SUMMARY_COLUMNS, r[1:], strict=True)), "pending": 0} for r in newest
        }
        for name, count in pending:
            out.setdefault(name, {"latest": None, "pending": 0})["pending"] = int(count)
        return out

    async def list_drafts(
        self, tenant_id: str, *, limit: int = MAX_VERSIONS_LISTED, after: DraftCursor | None = None
    ) -> list[dict[str, Any]]:
        """Every draft in the tenant, oldest first: the review queue, read along
        `idx_skill_version_status_age`."""
        from sqlalchemy import collate, literal, select, tuple_

        from felix.db.models import SkillVersionRow

        stmt = select(SkillVersionRow).where(
            SkillVersionRow.tenant_id == tenant_id, SkillVersionRow.status == "draft"
        )
        if after is not None:
            key = tuple_(
                SkillVersionRow.created_at,
                collate(SkillVersionRow.name, "C"),
                collate(SkillVersionRow.version, "C"),
            )
            stmt = stmt.where(key > tuple_(literal(after[0]), literal(after[1]), literal(after[2])))
        async with self._session(tenant_id) as db:
            rows = (
                await db.scalars(
                    stmt.order_by(
                        SkillVersionRow.created_at,
                        collate(SkillVersionRow.name, "C"),
                        collate(SkillVersionRow.version, "C"),
                    ).limit(limit)
                )
            ).all()
            return [self._row(r) for r in rows]

    async def list_live(self, tenant_id: str, *, limit: int = MAX_LIBRARY_SKILLS) -> list[dict[str, Any]]:
        """Each live skill's version and SKILL.md digest, in one query for a catalog load."""
        from sqlalchemy import and_, collate, select

        from felix.db.models import SkillFileRow, SkillRow

        async with self._session(tenant_id) as db:
            rows = (
                await db.execute(
                    select(SkillRow.name, SkillRow.live_version, SkillFileRow.sha256)
                    .outerjoin(
                        SkillFileRow,
                        and_(
                            SkillFileRow.tenant_id == SkillRow.tenant_id,
                            SkillFileRow.name == SkillRow.name,
                            SkillFileRow.version == SkillRow.live_version,
                            SkillFileRow.path == "SKILL.md",
                        ),
                    )
                    .where(SkillRow.tenant_id == tenant_id, SkillRow.live_version.is_not(None))
                    .order_by(collate(SkillRow.name, "C"))
                    .limit(limit)
                )
            ).all()
            return [{"name": r[0], "version": r[1], "sha256": r[2]} for r in rows]

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
        async with self._session(tenant_id) as db:
            return await self._pending(db, tenant_id, origin_manifest_id)

    @staticmethod
    async def _pending(db: Any, tenant_id: str, origin_manifest_id: str) -> int:
        from sqlalchemy import func, select

        from felix.db.models import SkillVersionRow

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
        self,
        tenant_id: str,
        row: dict[str, Any],
        files: list[dict[str, Any]],
        *,
        created_by: str,
        at: int,
        max_pending: int | None = None,
    ) -> None:
        from typing import cast

        from sqlalchemy import text
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy.exc import IntegrityError

        from felix.db.models import SkillFileRow, SkillRow, SkillVersionRow

        name, version = row["name"], row["version"]
        async with self._session(tenant_id) as db:
            if max_pending is not None:
                # Transaction-scoped, so it holds behind a transaction-mode pooler and is gone
                # at the commit or rollback below.
                origin = str(row.get("origin_manifest_id"))
                await db.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                    {"k": pending_lock_key(tenant_id, origin)},
                )
                if (held := await self._pending(db, tenant_id, origin)) >= max_pending:
                    raise SkillPendingFull(held, max_pending)
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
        self,
        tenant_id: str,
        name: str,
        version: str,
        *,
        from_statuses: Collection[str],
        by: str,
        at: int,
        expected_live: ExpectedLive = ANY_LIVE,
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
            # Checked under the skill row's lock: two reviewers who both saw `0.1.0` live cannot
            # both move it.
            if expected_live is not ANY_LIVE and previous != expected_live:
                await db.rollback()
                raise SkillLiveMismatch(f"{name} is live at {previous}, not {expected_live}")
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

    async def buildable_versions(self, tenant_id: str, names: Collection[str]) -> dict[str, list[str]]:
        """Each named skill's versions that are not rejected (`is_rejected`), in one query."""
        from sqlalchemy import select

        from felix.db.models import SkillVersionRow as V

        if not names:
            return {}
        async with self._session(tenant_id) as db:
            rows = (
                await db.execute(
                    select(V.name, V.version).where(
                        V.tenant_id == tenant_id,
                        V.name.in_(list(set(names))),
                        ~((V.status == "archived") & V.published_at.is_(None)),
                    )
                )
            ).all()
        out: dict[str, list[str]] = {}
        for name, version in rows:
            out.setdefault(name, []).append(version)
        return {n: sorted(vs) for n, vs in out.items()}

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
    "ANY_LIVE",
    "LIBRARY_PREFIX",
    "MAX_LIBRARY_SKILLS",
    "MAX_VERSIONS_LISTED",
    "MAX_VERSIONS_PER_SKILL",
    "SUMMARY_COLUMNS",
    "DraftCursor",
    "ExpectedLive",
    "InMemorySkillLibraryStore",
    "PostgresSkillLibraryStore",
    "SkillLibraryStore",
    "SkillLiveMismatch",
    "SkillPendingFull",
    "SkillStateConflict",
    "SkillStatus",
    "SkillVersionExists",
    "clear_memory",
    "get_skill_library_store",
    "is_rejected",
    "library_object_key",
    "pending_lock_key",
]
