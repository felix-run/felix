"""Usage meters — buffered token/turn events flushed like audit."""

from __future__ import annotations

import time
import uuid
from typing import Any

from sqlalchemy import select

from felix.buffers import DurableBuffer
from felix.config import Settings
from felix.db.models import UsageEvent
from felix.db.session import _use_memory, get_session_factory


def now_ms() -> int:
    return int(time.time() * 1000)


_pending = DurableBuffer("usage")
_memory_events: list[dict[str, Any]] = []


def _event_dict(row: UsageEvent | dict[str, Any]) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    return {
        "id": row.id,
        "tenant_id": row.tenant_id,
        "ts": row.ts,
        "manifest_id": row.manifest_id,
        "model_id": row.model_id,
        "kind": row.kind,
        "tokens_input": row.tokens_input,
        "tokens_output": row.tokens_output,
        "cache_creation": row.cache_creation,
        "cache_read": row.cache_read,
        "wire_model_id": row.wire_model_id,
        "cost_usd": row.cost_usd,
        "meta_json": row.meta_json,
    }


def record_tokens(
    settings: Settings,
    *,
    tenant_id: str,
    manifest_id: str,
    model_id: str = "",
    tokens_input: int = 0,
    tokens_output: int = 0,
    cache_creation: int = 0,
    cache_read: int = 0,
    wire_model_id: str = "",
    cost_usd: float = 0.0,
    meta: dict[str, Any] | None = None,
) -> None:
    """Buffer a token-usage event for later flush.

    `model_id` is the logical route name and is what is reported; `wire_model_id` is the
    provider's id the row was priced by. Cost arrives already priced — `record_usage` is
    the one pricer, at the one moment the wire id, the rates and any manifest override are
    all in hand — and is fixed on the row: nothing recomputes it later, because later the
    override and the route are gone.
    """
    _ = settings
    event = {
        "id": uuid.uuid4().hex,
        "tenant_id": tenant_id or "default",
        "ts": now_ms(),
        "manifest_id": manifest_id or "",
        "model_id": model_id or "",
        "kind": "tokens",
        "tokens_input": int(tokens_input or 0),
        "tokens_output": int(tokens_output or 0),
        "cache_creation": int(cache_creation or 0),
        "cache_read": int(cache_read or 0),
        "wire_model_id": wire_model_id or "",
        "cost_usd": float(cost_usd or 0.0),
        "meta_json": meta or {},
    }
    _pending.append(event)


async def query(
    settings: Settings,
    tenant_id: str,
    *,
    limit: int = 50,
    cursor: str | None = None,
    manifest_id: str | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    if _use_memory(settings):
        items = [e for e in _memory_events if e["tenant_id"] == tenant_id]
        if manifest_id is not None:
            items = [e for e in items if e["manifest_id"] == manifest_id]
        if cursor is not None:
            cursor_ts = int(cursor)
            items = [e for e in items if e["ts"] < cursor_ts]
        items.sort(key=lambda e: e["ts"], reverse=True)
        page = items[:limit]
        next_cursor = str(page[-1]["ts"]) if len(items) > limit and page else None
        return [_event_dict(e) for e in page], next_cursor

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        stmt = (
            select(UsageEvent)
            .where(UsageEvent.tenant_id == tenant_id)
            .order_by(UsageEvent.ts.desc())
            .limit(limit + 1)
        )
        if manifest_id is not None:
            stmt = stmt.where(UsageEvent.manifest_id == manifest_id)
        if cursor is not None:
            stmt = stmt.where(UsageEvent.ts < int(cursor))
        rows = (await db.scalars(stmt)).all()
        page = rows[:limit]
        next_cursor = str(page[-1].ts) if len(rows) > limit else None
        return [_event_dict(r) for r in page], next_cursor


async def flush_pending(settings: Settings) -> int:
    """Drain buffered usage events to Postgres (or memory)."""
    batch = _pending.take()
    if not batch:
        return 0

    try:
        await _write_batch(settings, batch)
    except Exception:
        # Usage drives billing — a failed commit must not silently lose the meter.
        _pending.requeue(batch)
        raise
    return len(batch)


async def _write_batch(settings: Settings, batch: list[dict[str, Any]]) -> None:
    if _use_memory(settings):
        _memory_events.extend(batch)
        return

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        for event in batch:
            db.add(
                UsageEvent(
                    tenant_id=event["tenant_id"],
                    id=event["id"],
                    ts=event["ts"],
                    manifest_id=event.get("manifest_id", ""),
                    model_id=event.get("model_id", ""),
                    kind=event.get("kind", "tokens"),
                    tokens_input=int(event.get("tokens_input") or 0),
                    tokens_output=int(event.get("tokens_output") or 0),
                    cache_creation=int(event.get("cache_creation") or 0),
                    cache_read=int(event.get("cache_read") or 0),
                    wire_model_id=event.get("wire_model_id", ""),
                    cost_usd=float(event.get("cost_usd") or 0.0),
                    meta_json=event.get("meta_json") or {},
                )
            )
        await db.commit()


SUMMARY_DEFAULT_WINDOW_MS = 30 * 24 * 60 * 60 * 1000
_SUMMED_COLUMNS = ("tokens_input", "tokens_output", "cache_creation", "cache_read", "cost_usd")


def _day(ts: int) -> str:
    """The UTC date a row falls on — the same bucket the SQL arm computes with `to_char`."""
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ts / 1000, UTC).strftime("%Y-%m-%d")


def _summary_memory(
    tenant_id: str, since_ms: int, until_ms: int, manifest_id: str | None
) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str, str], dict[str, Any]] = {}
    for e in _memory_events:
        if e["tenant_id"] != tenant_id or not (since_ms <= e["ts"] < until_ms):
            continue
        if manifest_id is not None and e["manifest_id"] != manifest_id:
            continue
        key = (e["manifest_id"], e["model_id"], _day(e["ts"]))
        bucket = buckets.setdefault(
            key,
            {
                "manifest_id": key[0],
                "model_id": key[1],
                "day": key[2],
                "calls": 0,
                **dict.fromkeys(_SUMMED_COLUMNS, 0),
            },
        )
        bucket["calls"] += 1
        for k in _SUMMED_COLUMNS:
            bucket[k] += e.get(k) or 0
    return sorted(buckets.values(), key=lambda b: (b["day"], b["manifest_id"], b["model_id"]), reverse=True)


async def _summary_sql(
    settings: Settings, tenant_id: str, since_ms: int, until_ms: int, manifest_id: str | None
) -> list[dict[str, Any]]:
    from sqlalchemy import func

    # UTC explicitly: `to_timestamp` yields a timestamptz and `to_char` would otherwise
    # render it in the session's time zone, splitting a day differently from the twin.
    day = func.to_char(func.timezone("UTC", func.to_timestamp(UsageEvent.ts / 1000.0)), "YYYY-MM-DD")
    stmt = (
        select(
            UsageEvent.manifest_id,
            UsageEvent.model_id,
            day.label("day"),
            func.count().label("calls"),
            *[func.coalesce(func.sum(getattr(UsageEvent, k)), 0).label(k) for k in _SUMMED_COLUMNS],
        )
        .where(UsageEvent.tenant_id == tenant_id, UsageEvent.ts >= since_ms, UsageEvent.ts < until_ms)
        .group_by(UsageEvent.manifest_id, UsageEvent.model_id, day)
        .order_by(day.desc(), UsageEvent.manifest_id, UsageEvent.model_id)
    )
    if manifest_id is not None:
        stmt = stmt.where(UsageEvent.manifest_id == manifest_id)
    factory = get_session_factory(settings=settings)
    async with factory() as db:
        rows = (await db.execute(stmt)).mappings().all()
    # `sum(numeric)` is a Decimal whatever the column's result processor says.
    return [{**dict(r), "cost_usd": float(r["cost_usd"])} for r in rows]


async def summary(
    settings: Settings,
    tenant_id: str,
    *,
    since_ms: int | None = None,
    until_ms: int | None = None,
    manifest_id: str | None = None,
) -> dict[str, Any]:
    """Spend grouped by manifest, model and UTC day, with totals — "what did tenant X
    spend last month" in one call. Defaults to the last thirty days."""
    # The window is half-open, so the default upper bound is one past "now": a row written
    # in the same millisecond as the query would otherwise fall outside it.
    until_ms = int(until_ms if until_ms is not None else now_ms() + 1)
    since_ms = int(since_ms if since_ms is not None else until_ms - SUMMARY_DEFAULT_WINDOW_MS)
    if _use_memory(settings):
        items = _summary_memory(tenant_id, since_ms, until_ms, manifest_id)
    else:
        items = await _summary_sql(settings, tenant_id, since_ms, until_ms, manifest_id)
    for item in items:
        item["cost_usd"] = round(float(item["cost_usd"]), 8)
    totals = {
        "calls": sum(i["calls"] for i in items),
        **{k: sum(i[k] for i in items) for k in _SUMMED_COLUMNS},
    }
    totals["cost_usd"] = round(float(totals["cost_usd"]), 8)
    return {"since_ms": since_ms, "until_ms": until_ms, "items": items, "totals": totals}


def pending_count() -> int:
    return len(_pending)


def pending_buffer() -> DurableBuffer:
    """The process-local usage buffer (diagnostics, metrics, tests)."""
    return _pending


def clear_memory() -> None:
    """Test helper."""
    _pending.reset_for_tests()
    _memory_events.clear()


__all__ = [
    "clear_memory",
    "flush_pending",
    "pending_buffer",
    "pending_count",
    "query",
    "record_tokens",
    "summary",
]
