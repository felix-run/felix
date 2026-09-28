"""Plan CRUD."""

from __future__ import annotations

import time
from typing import Any, Final

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


class _Keep:
    """Sentinel: leave the stored value alone (a fresh row gets the column default)."""

    def __repr__(self) -> str:
        return "KEEP"


KEEP: Final = _Keep()


class PlanConflict(Exception):
    """A conditional write found the plan changed (or gone) since the caller read it.

    ``current`` is the row as it stands now — ``None`` when it no longer exists — so
    a caller can re-apply its change to what is actually there rather than guess.
    """

    def __init__(self, plan_id: str, current: dict[str, Any] | None) -> None:
        super().__init__(f"plan {plan_id!r} changed since it was read")
        self.plan_id = plan_id
        self.current = current


async def put_plan(
    settings: Settings,
    tenant_id: str,
    plan_id: str,
    *,
    plan: dict[str, Any],
    manifest_id: str | _Keep = KEEP,
    expires_at: int | _Keep | None = KEEP,
    expected_updated_at: int | None = None,
) -> dict[str, Any]:
    """Create or replace a plan.

    ``manifest_id`` and ``expires_at`` default to ``KEEP``: a caller that does not
    name them leaves them as stored. They used to default to ``""`` and ``None``
    and were assigned on every write, so replacing only the plan body detached the
    row from its manifest and exempted it from retention.

    ``expected_updated_at`` makes the write conditional: it succeeds only if the row
    exists with exactly that ``updated_at``, and raises :class:`PlanConflict`
    otherwise. The agent's ``plan_update_step`` is a read-modify-write and an
    operator's ``PUT /plans/{id}`` replaces the whole body, so without it whichever
    landed second silently erased the other. ``updated_at`` is bumped past its
    previous value on every write, so two writes in one millisecond still differ.
    """
    ts = now_ms()

    if _use_memory(settings):
        # No await between the check and the store, so this is atomic on the loop.
        existing = _memory_plans.get((tenant_id, plan_id))
        if expected_updated_at is not None and (
            existing is None or existing["updated_at"] != expected_updated_at
        ):
            raise PlanConflict(plan_id, _plan_dict(existing) if existing else None)
        row = {
            "id": plan_id,
            "tenant_id": tenant_id,
            "manifest_id": _resolve(manifest_id, existing.get("manifest_id", "") if existing else ""),
            "created_at": existing["created_at"] if existing else ts,
            "updated_at": max(ts, existing["updated_at"] + 1) if existing else ts,
            "expires_at": _resolve(expires_at, existing.get("expires_at") if existing else None),
            "plan_json": plan,
        }
        _memory_plans[(tenant_id, plan_id)] = row
        return _plan_dict(row)

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        # Locked so the precondition and the write see the same row: a concurrent
        # writer waits here rather than slipping in between them.
        current = await db.get(Plan, (tenant_id, plan_id), with_for_update=True)
        if expected_updated_at is not None and (current is None or current.updated_at != expected_updated_at):
            snapshot = _plan_dict(current) if current is not None else None
            await db.rollback()
            raise PlanConflict(plan_id, snapshot)
        if current is None:
            current = Plan(
                tenant_id=tenant_id,
                id=plan_id,
                manifest_id=_resolve(manifest_id, ""),
                created_at=ts,
                updated_at=ts,
                expires_at=_resolve(expires_at, None),
                plan_json=plan,
            )
            db.add(current)
        else:
            current.manifest_id = _resolve(manifest_id, current.manifest_id)
            current.updated_at = max(ts, current.updated_at + 1)
            current.expires_at = _resolve(expires_at, current.expires_at)
            current.plan_json = plan
        await db.commit()
        return _plan_dict(current)


def _resolve[T](value: T | _Keep, stored: T) -> T:
    return stored if isinstance(value, _Keep) else value


async def delete_plan(settings: Settings, tenant_id: str, plan_id: str) -> bool:
    if _use_memory(settings):
        return _memory_plans.pop((tenant_id, plan_id), None) is not None

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        result = await db.execute(delete(Plan).where(Plan.tenant_id == tenant_id, Plan.id == plan_id))
        await db.commit()
        return result.rowcount > 0


__all__ = ["KEEP", "PlanConflict", "delete_plan", "get_plan", "list_plans", "put_plan"]
