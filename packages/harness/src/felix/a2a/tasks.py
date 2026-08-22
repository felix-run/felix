"""A2A task store — Postgres + memory:// fallback."""

from __future__ import annotations

import time
from typing import Any

from felix.config import Settings
from felix.db.models import A2ATask
from felix.db.session import _use_memory, get_session_factory


def now_ms() -> int:
    return int(time.time() * 1000)

_memory_tasks: dict[tuple[str, str], dict[str, Any]] = {}


def _task_dict(row: A2ATask | dict[str, Any]) -> dict[str, Any]:
    if isinstance(row, dict):
        stored = dict(row.get("task_json") or row)
        stored.setdefault("id", row.get("id"))
        stored.setdefault("status", row.get("status_json") or stored.get("status"))
        stored.setdefault("artifacts", row.get("artifacts_json") or stored.get("artifacts") or [])
        if row.get("manifest_id") and "manifest" not in stored:
            stored["manifest"] = row["manifest_id"]
        stored["updated_at"] = row.get("updated_at", stored.get("updated_at"))
        return stored
    stored = dict(row.task_json or {})
    stored["id"] = row.id
    stored["status"] = row.status_json or stored.get("status") or {}
    stored["artifacts"] = row.artifacts_json or stored.get("artifacts") or []
    if row.manifest_id:
        stored.setdefault("manifest", row.manifest_id)
    stored["updated_at"] = row.updated_at
    return stored


async def put_task(
    settings: Settings, tenant_id: str, task: dict[str, Any]
) -> dict[str, Any]:
    task_id = str(task["id"])
    ts = now_ms()
    status = task.get("status") if isinstance(task.get("status"), dict) else {"state": "unknown"}
    artifacts = list(task.get("artifacts") or [])
    manifest_id = str(task.get("manifest") or task.get("manifest_id") or "")
    payload = dict(task)
    payload["updated_at"] = ts

    if _use_memory(settings):
        existing = _memory_tasks.get((tenant_id, task_id))
        created_at = existing["created_at"] if existing else ts
        row = {
            "tenant_id": tenant_id,
            "id": task_id,
            "manifest_id": manifest_id,
            "status_json": status,
            "artifacts_json": artifacts,
            "task_json": payload,
            "created_at": created_at,
            "updated_at": ts,
        }
        _memory_tasks[(tenant_id, task_id)] = row
        return _task_dict(row)

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        row = await db.get(A2ATask, (tenant_id, task_id))
        if row is None:
            row = A2ATask(
                tenant_id=tenant_id,
                id=task_id,
                manifest_id=manifest_id,
                status_json=status,
                artifacts_json=artifacts,
                task_json=payload,
                created_at=ts,
                updated_at=ts,
            )
            db.add(row)
        else:
            row.manifest_id = manifest_id
            row.status_json = status
            row.artifacts_json = artifacts
            row.task_json = payload
            row.updated_at = ts
        await db.commit()
        return _task_dict(row)


async def get_task(
    settings: Settings, tenant_id: str, task_id: str
) -> dict[str, Any] | None:
    if _use_memory(settings):
        row = _memory_tasks.get((tenant_id, task_id))
        return _task_dict(row) if row else None

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        row = await db.get(A2ATask, (tenant_id, task_id))
        return _task_dict(row) if row else None


async def cancel_task(
    settings: Settings, tenant_id: str, task_id: str
) -> dict[str, Any] | None:
    existing = await get_task(settings, tenant_id, task_id)
    if existing is None:
        return None
    existing["status"] = {"state": "canceled", "timestamp": now_ms()}
    return await put_task(settings, tenant_id, existing)


def clear_tasks() -> None:
    """Test helper — clear in-memory task map."""
    _memory_tasks.clear()


__all__ = ["cancel_task", "clear_tasks", "get_task", "put_task"]
