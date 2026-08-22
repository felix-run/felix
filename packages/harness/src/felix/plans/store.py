"""Plan CRUD."""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import delete, select

from felix.config import Settings
from felix.db.models import Plan
from felix.db.session import _use_memory, get_session_factory

now_ms = lambda: int(time.time() * 1000)

_memory_plans: dict[tuple[str, str], dict[str, Any]] = {}


def _plan_dict(row: Plan | dict[str, Any]) -> dict[str, Any]:
    if isinstance(row, dict):
        return {
            "id": row["id"],
            "tenant_id": row["tenant_id"],
            "manifest_id": row.get("manifest_id", ""),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "expires_at": row.get("expires_at"),
            "plan": row["plan_json"],
        }
    return {
        "id": row.id,
        "tenant_id": row.tenant_id,
        "manifest_id": row.manifest_id,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "expires_at": row.expires_at,
        "plan": row.plan_json,
    }


async def list_plans(settings: Settings, tenant_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
    if _use_memory(settings):
        items = [_plan_dict(row) for (t, _), row in _memory_plans.items() if t == tenant_id]
        items.sort(key=lambda r: r["updated_at"], reverse=True)
        return items[:limit]

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        rows = (
            await db.scalars(
                select(Plan).where(Plan.tenant_id == tenant_id).order_by(Plan.updated_at.desc()).limit(limit)
            )
        ).all()
        return [_plan_dict(r) for r in rows]


async def get_plan(settings: Settings, tenant_id: str, plan_id: str) -> dict[str, Any] | None:
    if _use_memory(settings):
        row = _memory_plans.get((tenant_id, plan_id))
        return _plan_dict(row) if row else None

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        row = await db.get(Plan, (tenant_id, plan_id))
        return _plan_dict(row) if row else None


async def put_plan(
    settings: Settings,
    tenant_id: str,
    plan_id: str,
    *,
    plan: dict[str, Any],
    manifest_id: str = "",
    expires_at: int | None = None,
) -> dict[str, Any]:
    ts = now_ms()

    if _use_memory(settings):
        existing = _memory_plans.get((tenant_id, plan_id))
        created_at = existing["created_at"] if existing else ts
        row = {
            "id": plan_id,
            "tenant_id": tenant_id,
            "manifest_id": manifest_id,
            "created_at": created_at,
            "updated_at": ts,
            "expires_at": expires_at,
            "plan_json": plan,
        }
        _memory_plans[(tenant_id, plan_id)] = row
        return _plan_dict(row)

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        row = await db.get(Plan, (tenant_id, plan_id))
        if row is None:
            row = Plan(
                tenant_id=tenant_id,
                id=plan_id,
                manifest_id=manifest_id,
                created_at=ts,
                updated_at=ts,
                expires_at=expires_at,
                plan_json=plan,
            )
            db.add(row)
        else:
            row.manifest_id = manifest_id
            row.updated_at = ts
            row.expires_at = expires_at
            row.plan_json = plan
        await db.commit()
        return _plan_dict(row)


async def delete_plan(settings: Settings, tenant_id: str, plan_id: str) -> bool:
    if _use_memory(settings):
        return _memory_plans.pop((tenant_id, plan_id), None) is not None

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        result = await db.execute(delete(Plan).where(Plan.tenant_id == tenant_id, Plan.id == plan_id))
        await db.commit()
        return result.rowcount > 0


__all__ = ["delete_plan", "get_plan", "list_plans", "put_plan"]
