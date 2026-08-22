"""Analytics warehouse Protocol — DuckDB recommended; ClickHouse / Doris scale-out.

Locked decision
---------------
* **Recommended:** ``duckdb`` — embedded, small-VM friendly, no extra Compose service
  (``uv sync --extra warehouse``, ``FELIX_WAREHOUSE=duckdb``).
* **Lean runtime default:** ``none`` — Postgres remains system of record; no spill.
* **Scale-out:** ``clickhouse`` first for high-volume audit/events.
* **Alternative:** ``doris`` only when you already operate Apache Doris / want
  MySQL-protocol BI.
* ``memory`` for unit tests.

Postgres remains the system of record. The warehouse is append-only spill for
audit / eval analytics — never the control-plane source of truth.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger("felix.warehouse")

AUDIT_DDL_COLUMNS = (
    "id VARCHAR",
    "tenant_id VARCHAR",
    "ts BIGINT",
    "event_type VARCHAR",
    "manifest_id VARCHAR",
    "status VARCHAR",
    "payload JSON",
)


@runtime_checkable
class Warehouse(Protocol):
    """Append-only analytics sink."""

    name: str

    async def export_audit_events(
        self, events: list[dict[str, Any]], *, table: str = "audit_events"
    ) -> int: ...

    async def ping(self) -> bool: ...


class NoneWarehouse:
    """No-op warehouse (FELIX_WAREHOUSE=none)."""

    name = "none"

    async def export_audit_events(
        self, events: list[dict[str, Any]], *, table: str = "audit_events"
    ) -> int:
        _ = (events, table)
        return 0

    async def ping(self) -> bool:
        return True


class MemoryWarehouse:
    """In-process warehouse for unit tests."""

    name = "memory"

    def __init__(self) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = {}

    async def export_audit_events(
        self, events: list[dict[str, Any]], *, table: str = "audit_events"
    ) -> int:
        bucket = self.tables.setdefault(table, [])
        for e in events:
            bucket.append(_normalize_event(e))
        return len(events)

    async def ping(self) -> bool:
        return True


class DuckDbWarehouse:
    """Default warehouse — embedded DuckDB file under FELIX_DATA_DIR/warehouse."""

    name = "duckdb"

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self):
        try:
            import duckdb
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "FELIX_WAREHOUSE=duckdb requires felix-harness[warehouse] "
                "(uv sync --extra warehouse)"
            ) from exc
        return duckdb.connect(str(self.path))

    async def ping(self) -> bool:
        con = self._connect()
        try:
            con.execute("SELECT 1")
            return True
        finally:
            con.close()

    async def export_audit_events(
        self, events: list[dict[str, Any]], *, table: str = "audit_events"
    ) -> int:
        if not events:
            return 0
        con = self._connect()
        try:
            con.execute(
                f"CREATE TABLE IF NOT EXISTS {table} ("
                + ", ".join(AUDIT_DDL_COLUMNS)
                + ")"
            )
            rows = [_event_tuple(e) for e in events]
            con.executemany(
                f"INSERT INTO {table} VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            logger.info(
                "warehouse_export backend=duckdb table=%s rows=%s path=%s",
                table,
                len(rows),
                self.path,
            )
            return len(rows)
        finally:
            con.close()


class ClickHouseWarehouse:
    """Scale-out warehouse for high-volume audit (optional extra)."""

    name = "clickhouse"

    def __init__(self, *, url: str, database: str = "felix") -> None:
        self.url = url.rstrip("/")
        self.database = database

    def _client(self):
        try:
            import clickhouse_connect
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "FELIX_WAREHOUSE=clickhouse requires felix-harness[warehouse-clickhouse] "
                "(uv sync --extra warehouse-clickhouse)"
            ) from exc
        # clickhouse_connect.get_client(host=..., ...) — support http(s)://host:port
        from urllib.parse import urlparse

        parsed = urlparse(self.url if "://" in self.url else f"http://{self.url}")
        return clickhouse_connect.get_client(
            host=parsed.hostname or "localhost",
            port=parsed.port or (8443 if parsed.scheme == "https" else 8123),
            database=self.database,
        )

    async def ping(self) -> bool:
        client = self._client()
        client.query("SELECT 1")
        return True

    async def export_audit_events(
        self, events: list[dict[str, Any]], *, table: str = "audit_events"
    ) -> int:
        if not events:
            return 0
        client = self._client()
        client.command(
            f"""
            CREATE TABLE IF NOT EXISTS {table} (
                id String,
                tenant_id String,
                ts Int64,
                event_type String,
                manifest_id String,
                status String,
                payload String
            ) ENGINE = MergeTree ORDER BY (tenant_id, ts, id)
            """
        )
        rows = [_event_row_json_payload(e) for e in events]
        client.insert(
            table,
            rows,
            column_names=[
                "id",
                "tenant_id",
                "ts",
                "event_type",
                "manifest_id",
                "status",
                "payload",
            ],
        )
        logger.info(
            "warehouse_export backend=clickhouse table=%s rows=%s", table, len(rows)
        )
        return len(rows)


class DorisWarehouse:
    """Apache Doris warehouse via MySQL protocol (optional extra)."""

    name = "doris"

    def __init__(self, *, url: str, database: str = "felix") -> None:
        self.url = url
        self.database = database

    def _connect(self):
        try:
            import pymysql
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "FELIX_WAREHOUSE=doris requires felix-harness[warehouse-doris] "
                "(uv sync --extra warehouse-doris)"
            ) from exc
        from urllib.parse import urlparse

        raw = self.url if "://" in self.url else f"mysql://{self.url}"
        parsed = urlparse(raw)
        return pymysql.connect(
            host=parsed.hostname or "localhost",
            port=parsed.port or 9030,
            user=parsed.username or "root",
            password=parsed.password or "",
            database=self.database,
            autocommit=True,
        )

    async def ping(self) -> bool:
        con = self._connect()
        try:
            with con.cursor() as cur:
                cur.execute("SELECT 1")
            return True
        finally:
            con.close()

    async def export_audit_events(
        self, events: list[dict[str, Any]], *, table: str = "audit_events"
    ) -> int:
        if not events:
            return 0
        con = self._connect()
        try:
            with con.cursor() as cur:
                cur.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {table} (
                        id VARCHAR(64),
                        tenant_id VARCHAR(128),
                        ts BIGINT,
                        event_type VARCHAR(64),
                        manifest_id VARCHAR(128),
                        status VARCHAR(32),
                        payload TEXT
                    )
                    DUPLICATE KEY(id, tenant_id)
                    DISTRIBUTED BY HASH(tenant_id) BUCKETS 4
                    PROPERTIES ("replication_num" = "1")
                    """
                )
                rows = [_event_row_json_payload(e) for e in events]
                cur.executemany(
                    f"INSERT INTO {table} "
                    "(id, tenant_id, ts, event_type, manifest_id, status, payload) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    rows,
                )
            logger.info(
                "warehouse_export backend=doris table=%s rows=%s", table, len(events)
            )
            return len(events)
        finally:
            con.close()


def _normalize_event(e: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(e.get("id") or ""),
        "tenant_id": str(e.get("tenant_id") or ""),
        "ts": int(e.get("ts") or 0),
        "event_type": str(e.get("event_type") or ""),
        "manifest_id": str(e.get("manifest_id") or ""),
        "status": str(e.get("status") or ""),
        "payload": e.get("payload") or e.get("payload_json") or {},
    }


def _event_tuple(e: dict[str, Any]) -> tuple[Any, ...]:
    n = _normalize_event(e)
    return (
        n["id"],
        n["tenant_id"],
        n["ts"],
        n["event_type"],
        n["manifest_id"],
        n["status"],
        n["payload"],
    )


def _event_row_json_payload(e: dict[str, Any]) -> list[Any]:
    n = _normalize_event(e)
    return [
        n["id"],
        n["tenant_id"],
        n["ts"],
        n["event_type"],
        n["manifest_id"],
        n["status"],
        json.dumps(n["payload"]),
    ]


def duckdb_path(settings: object) -> Path:
    custom = getattr(settings, "warehouse_path", "") or ""
    if custom:
        path = Path(custom)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    root = Path(getattr(settings, "data_dir", "./data")) / "warehouse"
    root.mkdir(parents=True, exist_ok=True)
    return root / "felix.duckdb"


def build_warehouse(settings: object) -> Warehouse:
    """Factory from FELIX_WAREHOUSE=none|duckdb|clickhouse|doris|memory.

    Locked recommendation: use ``duckdb`` for analytics on small VMs
    (``uv sync --extra warehouse``). Scale-out → ``clickhouse`` first;
    ``doris`` only if you already run Apache Doris / want MySQL-protocol BI.
    Runtime lean default is ``none`` (Postgres remains system of record).
    """
    backend = (getattr(settings, "warehouse", "none") or "none").lower()
    if backend in {"none", ""}:
        return NoneWarehouse()
    if backend == "memory":
        return MemoryWarehouse()
    if backend == "duckdb":
        return DuckDbWarehouse(duckdb_path(settings))
    if backend == "clickhouse":
        return ClickHouseWarehouse(
            url=getattr(settings, "warehouse_url", "") or "http://localhost:8123",
            database=getattr(settings, "warehouse_database", "") or "felix",
        )
    if backend == "doris":
        return DorisWarehouse(
            url=getattr(settings, "warehouse_url", "") or "mysql://localhost:9030",
            database=getattr(settings, "warehouse_database", "") or "felix",
        )
    raise ValueError(f"Unknown FELIX_WAREHOUSE={backend!r}")


async def export_audit_events(
    settings: object,
    events: list[dict[str, Any]],
    *,
    table: str = "audit_events",
) -> int:
    """Convenience: build warehouse from settings and export."""
    wh = build_warehouse(settings)
    return await wh.export_audit_events(events, table=table)


# Back-compat alias
warehouse_path = duckdb_path

__all__ = [
    "ClickHouseWarehouse",
    "DorisWarehouse",
    "DuckDbWarehouse",
    "MemoryWarehouse",
    "NoneWarehouse",
    "Warehouse",
    "build_warehouse",
    "duckdb_path",
    "export_audit_events",
    "warehouse_path",
]
