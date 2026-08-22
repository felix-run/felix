"""Warehouse Protocol — DuckDB recommended; memory for unit tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from felix.audit import store as audit_store
from felix.config import Settings
from felix.warehouse import (
    DuckDbWarehouse,
    MemoryWarehouse,
    NoneWarehouse,
    build_warehouse,
    duckdb_path,
)


def test_build_none_by_default() -> None:
    settings = Settings(database_url="memory://test")
    assert settings.warehouse == "none"
    wh = build_warehouse(settings)
    assert isinstance(wh, NoneWarehouse)
    assert wh.name == "none"


def test_build_memory() -> None:
    settings = Settings(database_url="memory://test", warehouse="memory")
    wh = build_warehouse(settings)
    assert isinstance(wh, MemoryWarehouse)


def test_duckdb_path_default(tmp_path: Path) -> None:
    settings = Settings(database_url="memory://test", data_dir=str(tmp_path))
    path = duckdb_path(settings)
    assert path == tmp_path / "warehouse" / "felix.duckdb"
    assert path.parent.is_dir()


@pytest.mark.asyncio
async def test_memory_export_and_ping() -> None:
    wh = MemoryWarehouse()
    assert await wh.ping() is True
    n = await wh.export_audit_events(
        [
            {
                "id": "a1",
                "tenant_id": "t1",
                "ts": 1,
                "event_type": "tool_call",
                "manifest_id": "quick",
                "status": "ok",
                "payload": {"tool": "search"},
            }
        ]
    )
    assert n == 1
    assert len(wh.tables["audit_events"]) == 1
    assert wh.tables["audit_events"][0]["event_type"] == "tool_call"


@pytest.mark.asyncio
async def test_audit_flush_exports_into_shared_memory_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flush should call warehouse.export_audit_events with the batch."""
    settings = Settings(database_url="memory://test", warehouse="memory")
    shared = MemoryWarehouse()
    monkeypatch.setattr(
        "felix.warehouse.build_warehouse",
        lambda _s: shared,
    )
    audit_store._pending.clear()
    audit_store._memory_events.clear()

    audit_store.record_event(
        settings,
        "t1",
        "tool_call",
        manifest_id="quick",
        status="ok",
        payload_json={"tool": "search"},
    )
    assert await audit_store.flush_pending(settings) == 1
    assert len(shared.tables.get("audit_events", [])) == 1
    assert shared.tables["audit_events"][0]["payload"] == {"tool": "search"}


@pytest.mark.asyncio
async def test_duckdb_export_when_extra_installed(tmp_path: Path) -> None:
    pytest.importorskip("duckdb")
    path = tmp_path / "felix.duckdb"
    wh = DuckDbWarehouse(path)
    assert await wh.ping() is True
    n = await wh.export_audit_events(
        [
            {
                "id": "d1",
                "tenant_id": "t1",
                "ts": 42,
                "event_type": "chat",
                "manifest_id": "quick",
                "status": "ok",
                "payload": {"n": 1},
            }
        ]
    )
    assert n == 1
    assert path.is_file()
