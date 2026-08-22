"""Persist active skill sets per tenant+manifest (SkillActivation table / memory)."""

from __future__ import annotations

import time
from typing import Any, Protocol, runtime_checkable

from felix.config import Settings


@runtime_checkable
class SkillActivationStore(Protocol):
    async def get_active(self, tenant_id: str, manifest_id: str) -> list[str]: ...

    async def set_active(
        self, tenant_id: str, manifest_id: str, skills: list[str]
    ) -> list[str]: ...

    async def activate(
        self, tenant_id: str, manifest_id: str, name: str
    ) -> list[str]: ...

    async def deactivate(
        self, tenant_id: str, manifest_id: str, name: str
    ) -> list[str]: ...


class InMemorySkillActivationStore:
    def __init__(self) -> None:
        self._data: dict[tuple[str, str], list[str]] = {}

    async def get_active(self, tenant_id: str, manifest_id: str) -> list[str]:
        return list(self._data.get((tenant_id, manifest_id), []))

    async def set_active(
        self, tenant_id: str, manifest_id: str, skills: list[str]
    ) -> list[str]:
        unique = list(dict.fromkeys(skills))
        self._data[(tenant_id, manifest_id)] = unique
        return unique

    async def activate(
        self, tenant_id: str, manifest_id: str, name: str
    ) -> list[str]:
        current = await self.get_active(tenant_id, manifest_id)
        if name not in current:
            current.append(name)
        return await self.set_active(tenant_id, manifest_id, current)

    async def deactivate(
        self, tenant_id: str, manifest_id: str, name: str
    ) -> list[str]:
        current = [s for s in await self.get_active(tenant_id, manifest_id) if s != name]
        return await self.set_active(tenant_id, manifest_id, current)


class PostgresSkillActivationStore:
    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    async def get_active(self, tenant_id: str, manifest_id: str) -> list[str]:
        from sqlalchemy import select

        from felix.db.models import SkillActivation

        async with self._session_factory() as db:
            row = await db.get(SkillActivation, (tenant_id, manifest_id))
            if row is None:
                return []
            return list(row.active_skills or [])

    async def set_active(
        self, tenant_id: str, manifest_id: str, skills: list[str]
    ) -> list[str]:
        from felix.db.models import SkillActivation

        unique = list(dict.fromkeys(skills))
        now = int(time.time())
        async with self._session_factory() as db:
            row = await db.get(SkillActivation, (tenant_id, manifest_id))
            if row is None:
                db.add(
                    SkillActivation(
                        tenant_id=tenant_id,
                        manifest_id=manifest_id,
                        active_skills=unique,
                        updated_at=now,
                    )
                )
            else:
                row.active_skills = unique
                row.updated_at = now
            await db.commit()
        return unique

    async def activate(
        self, tenant_id: str, manifest_id: str, name: str
    ) -> list[str]:
        current = await self.get_active(tenant_id, manifest_id)
        if name not in current:
            current.append(name)
        return await self.set_active(tenant_id, manifest_id, current)

    async def deactivate(
        self, tenant_id: str, manifest_id: str, name: str
    ) -> list[str]:
        current = [s for s in await self.get_active(tenant_id, manifest_id) if s != name]
        return await self.set_active(tenant_id, manifest_id, current)


_memory_store = InMemorySkillActivationStore()


def get_skill_activation_store(
    settings: Settings | None = None,
) -> SkillActivationStore:
    if settings is None:
        return _memory_store
    url = settings.database_url
    if ":memory:" in url or "sqlite" in url or url.startswith("memory://"):
        return _memory_store
    from felix.db.session import get_session_factory

    return PostgresSkillActivationStore(get_session_factory(settings=settings))


__all__ = [
    "InMemorySkillActivationStore",
    "PostgresSkillActivationStore",
    "SkillActivationStore",
    "get_skill_activation_store",
]
