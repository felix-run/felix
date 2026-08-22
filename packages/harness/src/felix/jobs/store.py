"""Scheduled job CRUD."""

from __future__ import annotations

import time
import uuid
from typing import Any

from sqlalchemy import delete, select

from felix.config import Settings
from felix.db.models import Job, JobRun
from felix.db.session import _use_memory, get_session_factory

now_ms = lambda: int(time.time() * 1000)

_memory_jobs: dict[tuple[str, str], dict[str, Any]] = {}
_memory_runs: dict[tuple[str, str, str], dict[str, Any]] = {}


def _job_dict(row: Job | dict[str, Any]) -> dict[str, Any]:
    if isinstance(row, dict):
        return {
            "tenant_id": row["tenant_id"],
            "name": row["name"],
            "schedule": row.get("schedule", ""),
            "manifest_id": row.get("manifest_id", ""),
            "last_run_at": row.get("last_run_at"),
            "next_run_at": row.get("next_run_at"),
            "last_status": row.get("last_status", ""),
            "last_error": row.get("last_error", ""),
            "created_at": row["created_at"],
            "payload": row.get("payload_json") or row.get("payload") or {},
            "enabled": row.get("enabled", False),
        }
    return {
        "tenant_id": row.tenant_id,
        "name": row.name,
        "schedule": row.schedule,
        "manifest_id": row.manifest_id,
        "last_run_at": row.last_run_at,
        "next_run_at": row.next_run_at,
        "last_status": row.last_status,
        "last_error": row.last_error,
        "created_at": row.created_at,
        "payload": row.payload_json,
        "enabled": row.enabled,
    }


def _run_dict(row: JobRun | dict[str, Any]) -> dict[str, Any]:
    if isinstance(row, dict):
        return {
            "tenant_id": row["tenant_id"],
            "job_name": row["job_name"],
            "run_id": row["run_id"],
            "started_at": row["started_at"],
            "finished_at": row.get("finished_at"),
            "status": row.get("status", "ok"),
            "error": row.get("error", ""),
            "result": row.get("result_json") or row.get("result") or {},
        }
    return {
        "tenant_id": row.tenant_id,
        "job_name": row.job_name,
        "run_id": row.run_id,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "status": row.status,
        "error": row.error,
        "result": row.result_json,
    }


async def list_jobs(settings: Settings, tenant_id: str) -> list[dict[str, Any]]:
    if _use_memory(settings):
        return [
            _job_dict(row)
            for (t, _), row in _memory_jobs.items()
            if t == tenant_id
        ]

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        rows = (
            await db.scalars(select(Job).where(Job.tenant_id == tenant_id))
        ).all()
        return [_job_dict(r) for r in rows]


async def get_job(
    settings: Settings, tenant_id: str, name: str
) -> dict[str, Any] | None:
    if _use_memory(settings):
        row = _memory_jobs.get((tenant_id, name))
        return _job_dict(row) if row else None

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        row = await db.get(Job, (tenant_id, name))
        return _job_dict(row) if row else None


async def put_job(
    settings: Settings,
    tenant_id: str,
    name: str,
    *,
    schedule: str = "",
    manifest_id: str = "",
    payload: dict[str, Any] | None = None,
    enabled: bool = True,
) -> dict[str, Any]:
    ts = now_ms()

    if _use_memory(settings):
        existing = _memory_jobs.get((tenant_id, name))
        row = {
            "tenant_id": tenant_id,
            "name": name,
            "schedule": schedule,
            "manifest_id": manifest_id,
            "last_run_at": existing.get("last_run_at") if existing else None,
            "next_run_at": existing.get("next_run_at") if existing else None,
            "last_status": existing.get("last_status", "") if existing else "",
            "last_error": existing.get("last_error", "") if existing else "",
            "created_at": existing["created_at"] if existing else ts,
            "payload_json": payload or {},
            "enabled": enabled,
        }
        _memory_jobs[(tenant_id, name)] = row
        return _job_dict(row)

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        row = await db.get(Job, (tenant_id, name))
        if row is None:
            row = Job(
                tenant_id=tenant_id,
                name=name,
                schedule=schedule,
                manifest_id=manifest_id,
                created_at=ts,
                payload_json=payload or {},
                enabled=enabled,
            )
            db.add(row)
        else:
            row.schedule = schedule
            row.manifest_id = manifest_id
            row.payload_json = payload or {}
            row.enabled = enabled
        await db.commit()
        return _job_dict(row)


async def touch_run(
    settings: Settings,
    tenant_id: str,
    name: str,
    *,
    last_run_at: int,
    next_run_at: int | None = None,
    last_status: str = "ok",
    last_error: str = "",
) -> None:
    if _use_memory(settings):
        row = _memory_jobs.get((tenant_id, name))
        if row is None:
            return
        row["last_run_at"] = last_run_at
        row["next_run_at"] = next_run_at
        row["last_status"] = last_status
        row["last_error"] = last_error
        return

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        row = await db.get(Job, (tenant_id, name))
        if row is None:
            return
        row.last_run_at = last_run_at
        row.next_run_at = next_run_at
        row.last_status = last_status
        row.last_error = last_error
        await db.commit()


async def delete_job(settings: Settings, tenant_id: str, name: str) -> bool:
    if _use_memory(settings):
        deleted = _memory_jobs.pop((tenant_id, name), None) is not None
        if deleted:
            for key in list(_memory_runs):
                if key[0] == tenant_id and key[1] == name:
                    _memory_runs.pop(key, None)
        return deleted

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        await db.execute(
            delete(JobRun).where(
                JobRun.tenant_id == tenant_id, JobRun.job_name == name
            )
        )
        result = await db.execute(
            delete(Job).where(Job.tenant_id == tenant_id, Job.name == name)
        )
        await db.commit()
        return result.rowcount > 0


async def list_runs(
    settings: Settings, tenant_id: str, name: str, *, limit: int = 20
) -> list[dict[str, Any]]:
    if _use_memory(settings):
        items = [
            _run_dict(row)
            for (t, j, _), row in _memory_runs.items()
            if t == tenant_id and j == name
        ]
        items.sort(key=lambda r: r["started_at"], reverse=True)
        return items[:limit]

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        rows = (
            await db.scalars(
                select(JobRun)
                .where(JobRun.tenant_id == tenant_id, JobRun.job_name == name)
                .order_by(JobRun.started_at.desc())
                .limit(limit)
            )
        ).all()
        return [_run_dict(r) for r in rows]


async def record_run(
    settings: Settings,
    tenant_id: str,
    name: str,
    *,
    status: str = "ok",
    error: str = "",
    result: dict[str, Any] | None = None,
    started_at: int | None = None,
    finished_at: int | None = None,
) -> dict[str, Any]:
    """Persist a job run row (helper for scheduler)."""
    run_id = uuid.uuid4().hex
    started = started_at or now_ms()
    finished = finished_at or now_ms()

    if _use_memory(settings):
        row = {
            "tenant_id": tenant_id,
            "job_name": name,
            "run_id": run_id,
            "started_at": started,
            "finished_at": finished,
            "status": status,
            "error": error,
            "result_json": result or {},
        }
        _memory_runs[(tenant_id, name, run_id)] = row
        return _run_dict(row)

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        row = JobRun(
            tenant_id=tenant_id,
            job_name=name,
            run_id=run_id,
            started_at=started,
            finished_at=finished,
            status=status,
            error=error,
            result_json=result or {},
        )
        db.add(row)
        await db.commit()
        return _run_dict(row)


__all__ = [
    "delete_job",
    "get_job",
    "list_jobs",
    "list_runs",
    "put_job",
    "record_run",
    "touch_run",
]
