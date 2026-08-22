"""Eval dataset and run store."""

from __future__ import annotations

import time
import uuid
from typing import Any

from sqlalchemy import select

from felix.config import Settings
from felix.db.models import EvalDataset, EvalDatasetItem, EvalRun
from felix.db.session import _use_memory, get_session_factory

now_ms = lambda: int(time.time() * 1000)

_memory_datasets: dict[tuple[str, str], dict[str, Any]] = {}
_memory_items: dict[tuple[str, str, str], dict[str, Any]] = {}
_memory_runs: dict[tuple[str, str], dict[str, Any]] = {}


def _dataset_dict(
    row: EvalDataset | dict[str, Any], *, items: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    if isinstance(row, dict):
        return {
            "tenant_id": row["tenant_id"],
            "name": row["name"],
            "description": row.get("description", ""),
            "created_at": row["created_at"],
            "items": items if items is not None else row.get("items", []),
        }
    return {
        "tenant_id": row.tenant_id,
        "name": row.name,
        "description": row.description,
        "created_at": row.created_at,
        "items": items or [],
    }


def _item_dict(row: EvalDatasetItem | dict[str, Any]) -> dict[str, Any]:
    if isinstance(row, dict):
        return {
            "item_id": row["item_id"],
            "user_input": row["user_input"],
            "rubric": row.get("rubric_json") or row.get("rubric") or {},
            "created_at": row["created_at"],
        }
    return {
        "item_id": row.item_id,
        "user_input": row.user_input,
        "rubric": row.rubric_json,
        "created_at": row.created_at,
    }


def _run_dict(row: EvalRun | dict[str, Any]) -> dict[str, Any]:
    if isinstance(row, dict):
        return {
            "id": row["id"],
            "tenant_id": row["tenant_id"],
            "dataset_name": row["dataset_name"],
            "candidate_manifest": row["candidate_manifest"],
            "started_at": row["started_at"],
            "finished_at": row.get("finished_at"),
            "status": row.get("status", "in_progress"),
            "pass_count": row.get("pass_count", 0),
            "fail_count": row.get("fail_count", 0),
            "scores": row.get("scores_json") or row.get("scores") or [],
            "manifest_version": row.get("manifest_version"),
        }
    return {
        "id": row.id,
        "tenant_id": row.tenant_id,
        "dataset_name": row.dataset_name,
        "candidate_manifest": row.candidate_manifest,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "status": row.status,
        "pass_count": row.pass_count,
        "fail_count": row.fail_count,
        "scores": row.scores_json,
        "manifest_version": row.manifest_version,
    }


async def list_datasets(settings: Settings, tenant_id: str) -> list[dict[str, Any]]:
    if _use_memory(settings):
        return [_dataset_dict(row) for (t, _), row in _memory_datasets.items() if t == tenant_id]

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        rows = (await db.scalars(select(EvalDataset).where(EvalDataset.tenant_id == tenant_id))).all()
        return [_dataset_dict(r) for r in rows]


async def put_dataset(
    settings: Settings,
    tenant_id: str,
    name: str,
    *,
    description: str = "",
    items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    ts = now_ms()
    item_rows = items or []

    if _use_memory(settings):
        existing = _memory_datasets.get((tenant_id, name))
        row = {
            "tenant_id": tenant_id,
            "name": name,
            "description": description,
            "created_at": existing["created_at"] if existing else ts,
        }
        _memory_datasets[(tenant_id, name)] = row
        for item in item_rows:
            item_id = item.get("item_id") or uuid.uuid4().hex
            _memory_items[(tenant_id, name, item_id)] = {
                "tenant_id": tenant_id,
                "dataset_name": name,
                "item_id": item_id,
                "user_input": item.get("user_input", ""),
                "rubric_json": item.get("rubric") or item.get("rubric_json") or {},
                "created_at": ts,
            }
        stored_items = [
            _item_dict(i) for (t, d, _), i in _memory_items.items() if t == tenant_id and d == name
        ]
        return _dataset_dict(row, items=stored_items)

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        row = await db.get(EvalDataset, (tenant_id, name))
        if row is None:
            row = EvalDataset(
                tenant_id=tenant_id,
                name=name,
                description=description,
                created_at=ts,
            )
            db.add(row)
        else:
            row.description = description
        for item in item_rows:
            item_id = item.get("item_id") or uuid.uuid4().hex
            db.add(
                EvalDatasetItem(
                    tenant_id=tenant_id,
                    dataset_name=name,
                    item_id=item_id,
                    user_input=item.get("user_input", ""),
                    rubric_json=item.get("rubric") or item.get("rubric_json") or {},
                    created_at=ts,
                )
            )
        await db.commit()
        stored = (
            await db.scalars(
                select(EvalDatasetItem).where(
                    EvalDatasetItem.tenant_id == tenant_id,
                    EvalDatasetItem.dataset_name == name,
                )
            )
        ).all()
        return _dataset_dict(row, items=[_item_dict(i) for i in stored])


async def get_dataset(settings: Settings, tenant_id: str, name: str) -> dict[str, Any] | None:
    if _use_memory(settings):
        row = _memory_datasets.get((tenant_id, name))
        if row is None:
            return None
        items = [_item_dict(i) for (t, d, _), i in _memory_items.items() if t == tenant_id and d == name]
        return _dataset_dict(row, items=items)

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        row = await db.get(EvalDataset, (tenant_id, name))
        if row is None:
            return None
        items = (
            await db.scalars(
                select(EvalDatasetItem).where(
                    EvalDatasetItem.tenant_id == tenant_id,
                    EvalDatasetItem.dataset_name == name,
                )
            )
        ).all()
        return _dataset_dict(row, items=[_item_dict(i) for i in items])


async def list_runs(settings: Settings, tenant_id: str) -> list[dict[str, Any]]:
    if _use_memory(settings):
        items = [_run_dict(row) for (t, _), row in _memory_runs.items() if t == tenant_id]
        items.sort(key=lambda r: r["started_at"], reverse=True)
        return items

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        rows = (
            await db.scalars(
                select(EvalRun).where(EvalRun.tenant_id == tenant_id).order_by(EvalRun.started_at.desc())
            )
        ).all()
        return [_run_dict(r) for r in rows]


async def get_run(settings: Settings, tenant_id: str, run_id: str) -> dict[str, Any] | None:
    if _use_memory(settings):
        row = _memory_runs.get((tenant_id, run_id))
        return _run_dict(row) if row else None

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        row = await db.get(EvalRun, (tenant_id, run_id))
        return _run_dict(row) if row else None


async def create_run(
    settings: Settings,
    *,
    tenant_id: str,
    dataset_name: str,
    candidate_manifest: str,
    manifest_version: int | None = None,
    status: str = "in_progress",
    pass_count: int = 0,
    fail_count: int = 0,
    scores: list[Any] | None = None,
    finished_at: int | None = None,
) -> dict[str, Any]:
    run_id = uuid.uuid4().hex
    started_at = now_ms()

    if _use_memory(settings):
        row = {
            "id": run_id,
            "tenant_id": tenant_id,
            "dataset_name": dataset_name,
            "candidate_manifest": candidate_manifest,
            "started_at": started_at,
            "finished_at": finished_at,
            "status": status,
            "pass_count": pass_count,
            "fail_count": fail_count,
            "scores_json": scores or [],
            "manifest_version": manifest_version,
        }
        _memory_runs[(tenant_id, run_id)] = row
        return _run_dict(row)

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        row = EvalRun(
            tenant_id=tenant_id,
            id=run_id,
            dataset_name=dataset_name,
            candidate_manifest=candidate_manifest,
            started_at=started_at,
            finished_at=finished_at,
            status=status,
            pass_count=pass_count,
            fail_count=fail_count,
            scores_json=scores or [],
            manifest_version=manifest_version,
        )
        db.add(row)
        await db.commit()
        return _run_dict(row)


async def complete_run(
    settings: Settings,
    tenant_id: str,
    run_id: str,
    *,
    pass_count: int = 0,
    fail_count: int = 0,
    scores: list[Any] | None = None,
) -> dict[str, Any] | None:
    finished_at = now_ms()

    if _use_memory(settings):
        row = _memory_runs.get((tenant_id, run_id))
        if row is None:
            return None
        row["status"] = "completed"
        row["finished_at"] = finished_at
        row["pass_count"] = pass_count
        row["fail_count"] = fail_count
        row["scores_json"] = scores or []
        return _run_dict(row)

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        row = await db.get(EvalRun, (tenant_id, run_id))
        if row is None:
            return None
        row.status = "completed"
        row.finished_at = finished_at
        row.pass_count = pass_count
        row.fail_count = fail_count
        row.scores_json = scores or []
        await db.commit()
        return _run_dict(row)


__all__ = [
    "complete_run",
    "create_run",
    "get_dataset",
    "get_run",
    "list_datasets",
    "list_runs",
    "put_dataset",
]
