"""The ledger of editions posted to destinations: what makes "already posted?" a lookup, not a guess.

One row per tenant, destination, manifest and edition (`destination_posts`, migration 0049): an
edition is one agent's name for its post, so two agents posting `brief:2026-10-29` to one channel
are two posts. `post` claims
the row before it delivers -- an insert that succeeds only when no row exists, atomically on both
arms -- and the claim's winner is the only call that delivers that edition. What the delivery
answered then settles the row:

* `posted`: the destination confirmed it. `ref` is the provider's id. Every later call naming
  the edition is told it is already posted.
* no row (`release`): it certainly did not land, so the edition may be tried again.
* `maybe_posted`: nobody knows. Never retried by the harness; a person checks the destination.

A `posting` row is a delivery in flight, or one whose process died mid-delivery -- the second is
indistinguishable from a `maybe_posted` once it is old (`in_flight`), and is treated as one.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Literal, cast

from felix.config import Settings
from felix.db.models import DestinationPostRow
from felix.db.session import _use_memory, tenant_session

now_ms = lambda: int(time.time() * 1000)

PostStatus = Literal["posting", "posted", "maybe_posted"]
POSTING: PostStatus = "posting"
POSTED: PostStatus = "posted"
MAYBE_POSTED: PostStatus = "maybe_posted"

# A `posting` row younger than this is taken for a delivery still going on (`in_flight`): longer
# than any one delivery may take (`FELIX_WEBHOOK_TIMEOUT_SECONDS` is at most 60), so a row past it
# is one whose process stopped mid-delivery.
IN_FLIGHT_MS = 120_000

_memory_posts: dict[tuple[str, str, str, str], dict[str, Any]] = {}
# How often a claim retries when the row it lost to was released before it could be read.
_CLAIM_ATTEMPTS = 3


def reset_destination_posts_for_tests() -> None:
    _memory_posts.clear()


@dataclass(frozen=True, slots=True)
class Claim:
    """`won` when this call inserted the row and so is the one to deliver; otherwise `row` is the
    one that was already there."""

    won: bool
    row: dict[str, Any]


def in_flight(row: dict[str, Any], *, now: int | None = None) -> bool:
    """A `posting` row recent enough to be a delivery still going on."""
    ts = now_ms() if now is None else now
    return row.get("status") == POSTING and ts - int(row.get("updated_at") or 0) < IN_FLIGHT_MS


def _where(tenant_id: str, destination: str, manifest_id: str, edition: str) -> tuple[Any, ...]:
    return (
        DestinationPostRow.tenant_id == tenant_id,
        DestinationPostRow.destination == destination,
        DestinationPostRow.manifest_id == manifest_id,
        DestinationPostRow.edition == edition,
    )


def _row_dict(row: DestinationPostRow | dict[str, Any]) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    return {c.key: getattr(row, c.key) for c in DestinationPostRow.__table__.columns}


async def claim(
    settings: Settings, tenant_id: str, destination: str, manifest_id: str, edition: str, *, thread_id: str
) -> Claim:
    """Take the edition for this call (a new `posting` row), or the row another call already holds."""
    ts = now_ms()
    values: dict[str, Any] = {
        "tenant_id": tenant_id,
        "destination": destination,
        "edition": edition,
        "manifest_id": manifest_id,
        "status": POSTING,
        "ref": None,
        "thread_id": thread_id,
        "created_at": ts,
        "updated_at": ts,
    }
    key = (tenant_id, destination, manifest_id, edition)
    if _use_memory(settings):
        # No await between the check and the insert: the event loop makes this the atomic step.
        existing = _memory_posts.get(key)
        if existing is not None:
            return Claim(False, dict(existing))
        _memory_posts[key] = values
        return Claim(True, dict(values))

    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    table = cast(Any, DestinationPostRow.__table__)
    for _ in range(_CLAIM_ATTEMPTS):
        async with tenant_session(settings, tenant_id) as db:
            inserted = await db.execute(
                pg_insert(table)
                .values(values)
                .on_conflict_do_nothing(index_elements=["tenant_id", "destination", "manifest_id", "edition"])
                .returning(*table.c)
            )
            won = inserted.mappings().first()
            if won is not None:
                await db.commit()
                return Claim(True, dict(won))
            existing = await db.scalar(
                select(DestinationPostRow).where(*_where(tenant_id, destination, manifest_id, edition))
            )
            await db.commit()
            if existing is not None:
                return Claim(False, _row_dict(existing))
        # The row we lost to was released between the insert and the read: claim again.
    raise RuntimeError("destination post claim did not settle")


async def get(
    settings: Settings, tenant_id: str, destination: str, manifest_id: str, edition: str
) -> dict[str, Any] | None:
    if _use_memory(settings):
        row = _memory_posts.get((tenant_id, destination, manifest_id, edition))
        return dict(row) if row is not None else None
    async with tenant_session(settings, tenant_id) as db:
        row = await db.get(DestinationPostRow, (tenant_id, destination, manifest_id, edition))
        return _row_dict(row) if row is not None else None


async def settle(
    settings: Settings,
    tenant_id: str,
    destination: str,
    manifest_id: str,
    edition: str,
    *,
    status: PostStatus,
    ref: str | None = None,
) -> None:
    """Record what a claimed delivery answered: `posted` (with the provider's `ref`) or
    `maybe_posted`. Only a `posting` row moves: a settled one is history."""
    ts = now_ms()
    if _use_memory(settings):
        row = _memory_posts.get((tenant_id, destination, manifest_id, edition))
        if row is not None and row["status"] == POSTING:
            row.update(status=status, ref=ref, updated_at=ts)
        return
    from sqlalchemy import update

    async with tenant_session(settings, tenant_id) as db:
        await db.execute(
            update(DestinationPostRow)
            .where(
                *_where(tenant_id, destination, manifest_id, edition), DestinationPostRow.status == POSTING
            )
            .values(status=status, ref=ref, updated_at=ts)
        )
        await db.commit()


async def release(
    settings: Settings, tenant_id: str, destination: str, manifest_id: str, edition: str
) -> bool:
    """Drop a `posting` claim whose delivery certainly did not land, so the edition may be tried
    again. True when there was such a row. A settled row is never released."""
    if _use_memory(settings):
        key = (tenant_id, destination, manifest_id, edition)
        row = _memory_posts.get(key)
        if row is None or row["status"] != POSTING:
            return False
        del _memory_posts[key]
        return True
    from sqlalchemy import delete

    async with tenant_session(settings, tenant_id) as db:
        result = await db.execute(
            delete(DestinationPostRow).where(
                *_where(tenant_id, destination, manifest_id, edition), DestinationPostRow.status == POSTING
            )
        )
        await db.commit()
        return bool(getattr(result, "rowcount", 0))


__all__ = [
    "IN_FLIGHT_MS",
    "MAYBE_POSTED",
    "POSTED",
    "POSTING",
    "Claim",
    "PostStatus",
    "claim",
    "get",
    "in_flight",
    "release",
    "reset_destination_posts_for_tests",
    "settle",
]
