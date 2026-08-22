"""Extend thread_state — leaf, labels, session name, phase, thinking level."""

from __future__ import annotations

import time
from typing import Any

from felix.config import Settings
from felix.session.tree import get_leaf as _mem_get_leaf
from felix.session.tree import set_label as _mem_set_label
from felix.session.tree import set_leaf as _mem_set_leaf

# In-process extras keyed by thread_id (also mirrored into labels_json for Postgres).
_meta_by_thread: dict[str, dict[str, Any]] = {}


def _mem_meta(thread_id: str) -> dict[str, Any]:
    if thread_id not in _meta_by_thread:
        _meta_by_thread[thread_id] = {
            "session_name": None,
            "phase": "idle",
            "thinking_level": "off",
            "model_id": None,
            "parent_session_id": None,
            "labels": {},
            "created_at": int(time.time() * 1000),
            "updated_at": int(time.time() * 1000),
            "revision": 0,
        }
    return _meta_by_thread[thread_id]


def _use_memory(settings: Settings | None) -> bool:
    if settings is None:
        return True
    url = settings.database_url
    return ":memory:" in url or "sqlite" in url or url.startswith("memory://")


async def persist_leaf(
    *,
    settings: Settings | None,
    tenant_id: str,
    thread_id: str,
    leaf_event_id: str | None,
) -> None:
    """Update in-memory leaf and optionally Postgres thread_state."""
    _mem_set_leaf(thread_id, leaf_event_id)
    meta = _mem_meta(thread_id)
    meta["updated_at"] = int(time.time() * 1000)
    meta["revision"] = int(meta.get("revision") or 0) + 1
    if _use_memory(settings):
        return
    from felix.db.models import ThreadState
    from felix.db.session import get_session_factory

    now = int(time.time())
    factory = get_session_factory(settings=settings)
    async with factory() as db:
        row = await db.get(ThreadState, (tenant_id, thread_id))
        labels = dict(meta)
        labels["labels"] = dict(meta.get("labels") or {})
        if row is None:
            db.add(
                ThreadState(
                    tenant_id=tenant_id,
                    thread_id=thread_id,
                    leaf_event_id=leaf_event_id,
                    labels_json=labels,
                    updated_at=now,
                )
            )
        else:
            row.leaf_event_id = leaf_event_id
            existing = dict(row.labels_json or {})
            existing.update(labels)
            row.labels_json = existing
            row.updated_at = now
        await db.commit()


async def load_leaf(
    *,
    settings: Settings | None,
    tenant_id: str,
    thread_id: str,
) -> str | None:
    mem = _mem_get_leaf(thread_id)
    if mem is not None:
        return mem
    if _use_memory(settings):
        return None
    from felix.db.models import ThreadState
    from felix.db.session import get_session_factory

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        row = await db.get(ThreadState, (tenant_id, thread_id))
        if row and row.leaf_event_id:
            _mem_set_leaf(thread_id, row.leaf_event_id)
            if row.labels_json:
                meta = _mem_meta(thread_id)
                meta.update({k: v for k, v in row.labels_json.items() if k != "labels"})
                if isinstance(row.labels_json.get("labels"), dict):
                    meta["labels"] = dict(row.labels_json["labels"])
            return row.leaf_event_id
    return None


async def update_thread_meta(
    *,
    settings: Settings | None,
    tenant_id: str,
    thread_id: str,
    **fields: Any,
) -> dict[str, Any]:
    """Merge session metadata (name, phase, thinking_level, model_id, labels, …)."""
    meta = _mem_meta(thread_id)
    for key, value in fields.items():
        if key == "labels" and isinstance(value, dict):
            labels = dict(meta.get("labels") or {})
            labels.update(value)
            # None clears a label
            for k, v in list(labels.items()):
                if v is None:
                    labels.pop(k, None)
                    _mem_set_label(k, None)
                else:
                    _mem_set_label(k, str(v))
            meta["labels"] = labels
        elif value is not None or key in {"session_name", "parent_session_id", "model_id"}:
            meta[key] = value
    meta["updated_at"] = int(time.time() * 1000)
    meta["revision"] = int(meta.get("revision") or 0) + 1

    if _use_memory(settings):
        return dict(meta)

    from felix.db.models import ThreadState
    from felix.db.session import get_session_factory

    now = int(time.time())
    factory = get_session_factory(settings=settings)
    async with factory() as db:
        row = await db.get(ThreadState, (tenant_id, thread_id))
        payload = dict(meta)
        if row is None:
            db.add(
                ThreadState(
                    tenant_id=tenant_id,
                    thread_id=thread_id,
                    leaf_event_id=_mem_get_leaf(thread_id),
                    labels_json=payload,
                    updated_at=now,
                )
            )
        else:
            existing = dict(row.labels_json or {})
            existing.update(payload)
            row.labels_json = existing
            row.updated_at = now
        await db.commit()
    return dict(meta)


async def get_thread_meta(
    *,
    settings: Settings | None,
    tenant_id: str,
    thread_id: str,
) -> dict[str, Any]:
    await load_leaf(settings=settings, tenant_id=tenant_id, thread_id=thread_id)
    if not _use_memory(settings) and thread_id not in _meta_by_thread:
        from felix.db.models import ThreadState
        from felix.db.session import get_session_factory

        factory = get_session_factory(settings=settings)
        async with factory() as db:
            row = await db.get(ThreadState, (tenant_id, thread_id))
            if row and row.labels_json:
                meta = _mem_meta(thread_id)
                for k, v in row.labels_json.items():
                    if k == "labels" and isinstance(v, dict):
                        meta["labels"] = dict(v)
                    else:
                        meta[k] = v
    return dict(_mem_meta(thread_id))


async def list_thread_metadata(
    *,
    settings: Settings | None,
    tenant_id: str,
) -> list[dict[str, Any]]:
    """List durable session metadata for a tenant."""
    items: list[dict[str, Any]] = []
    if _use_memory(settings):
        for tid, meta in _meta_by_thread.items():
            if tid.startswith(f"{tenant_id}:") or tenant_id == "default":
                items.append(
                    {
                        "id": tid,
                        "createdAt": meta.get("created_at"),
                        "updatedAt": meta.get("updated_at"),
                        "parentSessionId": meta.get("parent_session_id"),
                        "sessionName": meta.get("session_name"),
                    }
                )
        return items

    from sqlalchemy import select

    from felix.db.models import ThreadState
    from felix.db.session import get_session_factory

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        rows = (await db.scalars(select(ThreadState).where(ThreadState.tenant_id == tenant_id))).all()
        for row in rows:
            lj = row.labels_json or {}
            items.append(
                {
                    "id": row.thread_id,
                    "createdAt": lj.get("created_at") or row.updated_at * 1000,
                    "updatedAt": lj.get("updated_at") or row.updated_at * 1000,
                    "parentSessionId": lj.get("parent_session_id"),
                    "sessionName": lj.get("session_name"),
                }
            )
    return items


def reset_thread_meta_for_tests() -> None:
    _meta_by_thread.clear()


__all__ = [
    "get_thread_meta",
    "list_thread_metadata",
    "load_leaf",
    "persist_leaf",
    "reset_thread_meta_for_tests",
    "update_thread_meta",
]
