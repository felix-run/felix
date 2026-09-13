"""Usage meters — buffered token/turn events flushed like audit."""

from __future__ import annotations

import time
import uuid
from typing import Any

from sqlalchemy import select

from felix.buffers import DurableBuffer
from felix.config import Settings
from felix.cursors import keyset_before, keyset_order, order_and_seek, take_page
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
        # `felix.cursors` owns the rule, not just the string: the twin and the store paged in
        # parallel here and their `next_cursor` predicates had already drifted apart.
        rows, next_cursor = take_page(order_and_seek(items, cursor), limit=limit)
        return [_event_dict(e) for e in rows], next_cursor

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        stmt = (
            select(UsageEvent)
            .where(UsageEvent.tenant_id == tenant_id)
            # `id` breaks the tie, and it is the second half of the primary key, so the
            # order is total. Without it `ORDER BY ts DESC` leaves rows in one millisecond in
            # whatever order the plan produces, and the cursor below cannot address them.
            .order_by(*keyset_order(UsageEvent.ts, UsageEvent.id))
            .limit(limit + 1)
        )
        if manifest_id is not None:
            stmt = stmt.where(UsageEvent.manifest_id == manifest_id)
        if cursor is not None:
            stmt = stmt.where(keyset_before(UsageEvent.ts, UsageEvent.id, cursor))
        found = (await db.scalars(stmt)).all()
        rows, next_cursor = take_page(found, limit=limit)
        return [_event_dict(r) for r in rows], next_cursor


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


def _summary_item_dict(row: dict[str, Any]) -> dict[str, Any]:
    """One summary bucket, on the wire.

    Named and spelled out for the same reason every other `_*_dict` here is: it is
    the only form a client can read the shape from. The response used to be built
    inline in both arms, so `felix-web`'s payload guard had nothing to compare a
    client type against and the area could not be guarded at all — the same gap
    `/documents` had before #213.

    It also gives the two arms one definition instead of two that agree by
    inspection. They have disagreed before, about the sort order of rows sharing a
    day.
    """
    return {
        "manifest_id": str(row.get("manifest_id") or ""),
        "model_id": str(row.get("model_id") or ""),
        "day": str(row.get("day") or ""),
        "calls": int(row.get("calls") or 0),
        "tokens_input": int(row.get("tokens_input") or 0),
        "tokens_output": int(row.get("tokens_output") or 0),
        "cache_creation": int(row.get("cache_creation") or 0),
        "cache_read": int(row.get("cache_read") or 0),
        # Rounded here rather than by the caller, so both arms and the totals below
        # agree on the precision a client sees.
        "cost_usd": round(float(row.get("cost_usd") or 0.0), 8),
    }


def _summary_totals_dict(items: list[dict[str, Any]]) -> dict[str, Any]:
    """The totals block — the same columns as an item, summed, without the keys that group."""
    totals: dict[str, Any] = {"calls": sum(i["calls"] for i in items)}
    for k in _SUMMED_COLUMNS:
        totals[k] = sum(i[k] for i in items)
    totals["cost_usd"] = round(float(totals["cost_usd"]), 8)
    return totals


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
    # Day descending, then manifest and model *ascending* — which is what the SQL arm does.
    # `reverse=True` over the whole tuple reversed all three, so the two arms disagreed about
    # the order of every row sharing a day. Two sorts rather than one because a string key
    # cannot be negated, and Python's sort is stable so the inner order survives the outer.
    rows = sorted(buckets.values(), key=lambda b: (b["manifest_id"], b["model_id"]))
    rows.sort(key=lambda b: b["day"], reverse=True)
    return [_summary_item_dict(dict(r)) for r in rows]


async def _summary_sql(
    settings: Settings, tenant_id: str, since_ms: int, until_ms: int, manifest_id: str | None
) -> list[dict[str, Any]]:
    from sqlalchemy import collate, func

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
        # `COLLATE "C"` for the same reason `jobs.list_jobs` uses it: these are text keys, and
        # `ORDER BY` on text uses the database collation while the twin sorts by code point.
        # `manifest_id` is tenant-supplied, so mixed case is reachable.
        .order_by(day.desc(), collate(UsageEvent.manifest_id, "C"), collate(UsageEvent.model_id, "C"))
    )
    if manifest_id is not None:
        stmt = stmt.where(UsageEvent.manifest_id == manifest_id)
    factory = get_session_factory(settings=settings)
    async with factory() as db:
        rows = (await db.execute(stmt)).mappings().all()
    # `sum(numeric)` is a Decimal whatever the column's result processor says;
    # `_summary_item_dict` is what coerces it, along with every other column.
    return [_summary_item_dict(dict(r)) for r in rows]


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
    return {
        "since_ms": since_ms,
        "until_ms": until_ms,
        "items": items,
        "totals": _summary_totals_dict(items),
    }


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
