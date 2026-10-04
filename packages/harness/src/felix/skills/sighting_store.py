"""When Felix first saw a skill's files: the clock the import cooldown runs on.

A commit's date is whatever the pusher set (`GIT_COMMITTER_DATE` is one environment variable), so
a cooldown measured on it is a cooldown the attacker chooses. This records, per tenant, the first
time a browse or an import saw a source's skill folder with a given tree digest -- a server-side
stamp, as Skillist's `publishedAt` is -- and the cooldown counts from there.

Recorded on every browse and every import attempt, whatever the cooldown is (even off), so turning
one on later honours what was already seen rather than holding every skill for its full length.
Insert-if-absent: the first stamp for a `(tenant, source, digest)` is the one that stays. The
retention sweep drops rows older than `SIGHTING_RETENTION_DAYS`.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable
from typing import Any, Protocol, runtime_checkable

from felix.config import Settings


@runtime_checkable
class SightingStore(Protocol):
    async def first_seen(
        self, tenant_id: str, pairs: Iterable[tuple[str, str]], *, at: int
    ) -> dict[tuple[str, str], int]:
        """Record each `(origin_source, tree_hash)` in ``pairs`` as seen at ``at`` unless it was
        seen before, and return when each was first seen."""
        ...

    async def prune(self, *, before: int) -> int:
        """Drop every tenant's sightings first seen before ``before``; how many went."""
        ...


class InMemorySightingStore:
    """The `memory://` twin."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str, str], int] = {}

    def clear(self) -> None:
        self._rows.clear()

    async def first_seen(
        self, tenant_id: str, pairs: Iterable[tuple[str, str]], *, at: int
    ) -> dict[tuple[str, str], int]:
        out: dict[tuple[str, str], int] = {}
        for source, digest in pairs:
            out[(source, digest)] = self._rows.setdefault((tenant_id, source, digest), at)
        return copy.deepcopy(out)

    async def prune(self, *, before: int) -> int:
        stale = [k for k, at in self._rows.items() if at < before]
        for key in stale:
            del self._rows[key]
        return len(stale)


class PostgresSightingStore:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def first_seen(
        self, tenant_id: str, pairs: Iterable[tuple[str, str]], *, at: int
    ) -> dict[tuple[str, str], int]:
        from typing import cast

        from sqlalchemy import and_, or_, select
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        from felix.db.models import SkillImportSightingRow
        from felix.db.session import tenant_session

        wanted = sorted(set(pairs))
        if not wanted:
            return {}
        rows = [
            {"tenant_id": tenant_id, "origin_source": s, "tree_hash": d, "first_seen_at": at}
            for s, d in wanted
        ]
        R = SkillImportSightingRow
        async with tenant_session(self._settings, tenant_id) as db:
            # DO NOTHING, then read back: a concurrent first sighting keeps whichever stamp landed.
            await db.execute(pg_insert(cast(Any, R.__table__)).values(rows).on_conflict_do_nothing())
            await db.commit()
            found = (
                await db.execute(
                    select(R.origin_source, R.tree_hash, R.first_seen_at).where(
                        R.tenant_id == tenant_id,
                        or_(*(and_(R.origin_source == s, R.tree_hash == d) for s, d in wanted)),
                    )
                )
            ).all()
        return {(r[0], r[1]): int(r[2]) for r in found}

    async def prune(self, *, before: int) -> int:
        from sqlalchemy import delete

        from felix.db.models import SkillImportSightingRow
        from felix.db.session import get_session_factory, rls_bypass

        # Cross-tenant maintenance: without the bypass RLS makes the DELETE a silent no-op.
        with rls_bypass():
            async with get_session_factory(settings=self._settings)() as db:
                gone = await db.execute(
                    delete(SkillImportSightingRow).where(SkillImportSightingRow.first_seen_at < before)
                )
                await db.commit()
                return int(getattr(gone, "rowcount", 0) or 0)


# How long a sighting is kept: past the longest cooldown a setting allows (365 days), so pruning
# never makes a skill seen long ago look new -- and if it did, it would only restart its clock.
SIGHTING_RETENTION_DAYS = 366

_memory = InMemorySightingStore()


def get_sighting_store(settings: Settings | None = None) -> SightingStore:
    from felix.skills.quality_store import postgres_settings

    pg = postgres_settings(settings)
    return _memory if pg is None else PostgresSightingStore(pg)


def clear_memory() -> None:
    _memory.clear()


__all__ = [
    "SIGHTING_RETENTION_DAYS",
    "InMemorySightingStore",
    "PostgresSightingStore",
    "SightingStore",
    "clear_memory",
    "get_sighting_store",
]
