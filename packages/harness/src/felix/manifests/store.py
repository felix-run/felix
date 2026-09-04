"""Tenant manifest version store."""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from felix.config import Settings
from felix.db.models import ManifestActive, ManifestRow
from felix.db.session import _use_memory, get_session_factory
from felix.manifests.loader import parse_manifest
from felix.manifests.resolver import ActivePointer
from felix.manifests.schema import Manifest

now_ms = lambda: int(time.time() * 1000)

_memory_manifests: dict[tuple[str, str, int], dict[str, Any]] = {}
_memory_active: dict[tuple[str, str], dict[str, Any]] = {}


def reset_memory_store() -> None:
    """Drop every manifest held by the in-memory twin.

    Process-global, so without this a manifest written by one test is served to the next.
    A minimal stored manifest has no `auth.inbound` block, so writing one named `quick`
    shadows the bundled file and every later test resolving that name gets a 401 — which is
    how eleven unrelated tests failed at once.
    """
    _memory_manifests.clear()
    _memory_active.clear()


def _version_dict(row: ManifestRow | dict[str, Any]) -> dict[str, Any]:
    if isinstance(row, dict):
        return {
            "tenant_id": row["tenant_id"],
            "name": row["name"],
            "version": row["version"],
            "manifest": row["manifest_json"],
            "created_at": row["created_at"],
            "created_by": row.get("created_by", ""),
            "comment": row.get("comment", ""),
        }
    return {
        "tenant_id": row.tenant_id,
        "name": row.name,
        "version": row.version,
        "manifest": row.manifest_json,
        "created_at": row.created_at,
        "created_by": row.created_by,
        "comment": row.comment,
    }


def _active_dict(row: ManifestActive | dict[str, Any]) -> dict[str, Any]:
    if isinstance(row, dict):
        return {
            "tenant_id": row["tenant_id"],
            "name": row["name"],
            "version": row["version"],
            "updated_at": row["updated_at"],
            "updated_by": row.get("updated_by", ""),
            "canary_version": row.get("canary_version"),
            "canary_weight": row.get("canary_weight", 0),
        }
    return {
        "tenant_id": row.tenant_id,
        "name": row.name,
        "version": row.version,
        "updated_at": row.updated_at,
        "updated_by": row.updated_by,
        "canary_version": row.canary_version,
        "canary_weight": row.canary_weight,
    }


async def list_active(settings: Settings, tenant_id: str) -> list[dict[str, Any]]:
    if _use_memory(settings):
        return [_active_dict(row) for (t, n), row in _memory_active.items() if t == tenant_id]

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        rows = (await db.scalars(select(ManifestActive).where(ManifestActive.tenant_id == tenant_id))).all()
        return [_active_dict(r) for r in rows]


async def list_tenants_with_active(settings: Settings) -> list[str]:
    """Every tenant that has at least one active manifest pointer.

    `run_continuous_eval` defaulted to ``tenant_id="default"``, so a canary in any
    other tenant was never benchmarked.
    """
    if _use_memory(settings):
        return sorted({str(t) for (t, _n) in _memory_active})

    from felix.db.session import rls_bypass

    factory = get_session_factory(settings=settings)
    with rls_bypass():
        async with factory() as db:
            rows = (await db.execute(select(ManifestActive.tenant_id).distinct())).scalars().all()
            return sorted({str(r) for r in rows})


async def get_version(settings: Settings, tenant_id: str, name: str, version: int) -> dict[str, Any] | None:
    if _use_memory(settings):
        row = _memory_manifests.get((tenant_id, name, version))
        return _version_dict(row) if row else None

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        row = await db.get(ManifestRow, (tenant_id, name, version))
        return _version_dict(row) if row else None


async def put_version(
    settings: Settings,
    tenant_id: str,
    name: str,
    manifest: Manifest,
    *,
    created_by: str = "",
    comment: str = "",
) -> dict[str, Any]:
    manifest_json = manifest.model_dump(mode="json")
    ts = now_ms()

    if _use_memory(settings):
        versions = [v for (t, n, v) in _memory_manifests if t == tenant_id and n == name]
        next_version = max(versions, default=0) + 1
        row = {
            "tenant_id": tenant_id,
            "name": name,
            "version": next_version,
            "manifest_json": manifest_json,
            "created_at": ts,
            "created_by": created_by,
            "comment": comment,
        }
        _memory_manifests[(tenant_id, name, next_version)] = row
        if (tenant_id, name) not in _memory_active:
            _memory_active[(tenant_id, name)] = {
                "tenant_id": tenant_id,
                "name": name,
                "version": next_version,
                "updated_at": ts,
                "updated_by": created_by,
                "canary_version": None,
                "canary_weight": 0,
            }
        return _version_dict(row)

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        max_version = await db.scalar(
            select(func.coalesce(func.max(ManifestRow.version), 0)).where(
                ManifestRow.tenant_id == tenant_id,
                ManifestRow.name == name,
            )
        )
        next_version = int(max_version or 0) + 1
        row = ManifestRow(
            tenant_id=tenant_id,
            name=name,
            version=next_version,
            manifest_json=manifest_json,
            created_at=ts,
            created_by=created_by,
            comment=comment,
        )
        db.add(row)
        stmt = insert(ManifestActive).values(
            tenant_id=tenant_id,
            name=name,
            version=next_version,
            updated_at=ts,
            updated_by=created_by,
            canary_version=None,
            canary_weight=0,
        )
        stmt = stmt.on_conflict_do_nothing(index_elements=["tenant_id", "name"])
        await db.execute(stmt)
        await db.commit()
        return _version_dict(row)


async def set_canary(
    settings: Settings,
    tenant_id: str,
    name: str,
    *,
    canary_version: int | None,
    canary_weight: int,
    updated_by: str = "",
) -> dict[str, Any] | None:
    ts = now_ms()

    if canary_version is not None:
        version_row = await get_version(settings, tenant_id, name, canary_version)
        if version_row is None:
            raise LookupError(f"Unknown canary version: {name}@{canary_version}")

    if _use_memory(settings):
        active = _memory_active.get((tenant_id, name))
        if active is None:
            return None
        active["canary_version"] = canary_version
        active["canary_weight"] = canary_weight
        active["updated_at"] = ts
        active["updated_by"] = updated_by
        return _active_dict(active)

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        active = await db.get(ManifestActive, (tenant_id, name))
        if active is None:
            return None
        active.canary_version = canary_version
        active.canary_weight = canary_weight
        active.updated_at = ts
        active.updated_by = updated_by
        await db.commit()
        return _active_dict(active)


async def activate_version(
    settings: Settings,
    tenant_id: str,
    name: str,
    *,
    version: int,
    updated_by: str = "",
    comment: str = "",
) -> dict[str, Any] | None:
    _ = comment
    ts = now_ms()

    if _use_memory(settings):
        if (tenant_id, name, version) not in _memory_manifests:
            return None
        active = _memory_active.get((tenant_id, name))
        if active is None:
            return None
        active["version"] = version
        active["updated_at"] = ts
        active["updated_by"] = updated_by
        active["canary_version"] = None
        active["canary_weight"] = 0
        return _active_dict(active)

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        if await db.get(ManifestRow, (tenant_id, name, version)) is None:
            return None
        active = await db.get(ManifestActive, (tenant_id, name))
        if active is None:
            return None
        active.version = version
        active.updated_at = ts
        active.updated_by = updated_by
        active.canary_version = None
        active.canary_weight = 0
        await db.commit()
        return _active_dict(active)


class PostgresManifestStore:
    """ManifestStore protocol adapter for the resolver."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def get_active(self, tenant_id: str, name: str) -> ActivePointer | None:
        if _use_memory(self._settings):
            active = _memory_active.get((tenant_id, name))
            if active is None:
                return None
            return ActivePointer(
                version=active["version"],
                canary_version=active.get("canary_version"),
                canary_weight=active.get("canary_weight", 0),
            )

        factory = get_session_factory(settings=self._settings)
        async with factory() as db:
            active = await db.get(ManifestActive, (tenant_id, name))
            if active is None:
                return None
            return ActivePointer(
                version=active.version,
                canary_version=active.canary_version,
                canary_weight=active.canary_weight,
            )

    async def get_version(self, tenant_id: str, name: str, version: int) -> Manifest | None:
        row = await get_version(self._settings, tenant_id, name, version)
        if row is None:
            return None
        # Through the loader, so a row stored before a schema tightening fails with the
        # operator-readable message rather than a raw ValidationError.
        return parse_manifest(row["manifest"])


__all__ = [
    "PostgresManifestStore",
    "activate_version",
    "get_version",
    "list_active",
    "put_version",
    "set_canary",
]
