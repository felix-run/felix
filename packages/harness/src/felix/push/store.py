"""Push subscriptions: the browsers a tenant asked the harness to wake.

Keyed by `(tenant_id, sha256(endpoint))`. Re-subscribing from the same browser replaces its
own row -- a browser rotates its keys without changing its endpoint -- and the tenant in the
key means one tenant's subscribe can never land on another's row.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, cast

from sqlalchemy import collate, delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from felix.config import Settings
from felix.db.models import PushSubscriptionRow
from felix.db.session import _use_memory, tenant_session

now_ms = lambda: int(time.time() * 1000)

# A tenant's ceiling. Every waiting run fans out to all of them, so the bound is on the
# work one approval can cause, and it is far above a team's worth of phones and laptops.
MAX_SUBSCRIPTIONS_PER_TENANT = 200
# Sends in a row a subscription may fail before it is dropped. One bad network minute should
# not unsubscribe anyone; a subscription that has not worked for this many waits never will,
# and left alone it would hold a slot under the cap for good.
MAX_CONSECUTIVE_FAILURES = 5

_memory_subscriptions: dict[tuple[str, str], dict[str, Any]] = {}


def reset_push_for_tests() -> None:
    """Clear the in-memory subscriptions."""
    _memory_subscriptions.clear()


def subscription_id(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()


def _subscription_dict(row: PushSubscriptionRow | dict[str, Any]) -> dict[str, Any]:
    data = (
        dict(row)
        if isinstance(row, dict)
        else {c.key: getattr(row, c.key) for c in PushSubscriptionRow.__table__.columns}
    )
    return {
        "id": data["id"],
        "tenant_id": data["tenant_id"],
        "endpoint": data["endpoint"],
        "p256dh": data["p256dh"],
        "auth": data["auth"],
        "principal_subj": data.get("principal_subj") or "",
        "created_at": data["created_at"],
        "last_ok_at": data.get("last_ok_at"),
        "failures": data.get("failures") or 0,
    }


class TooManySubscriptions(Exception):
    """The tenant is at `MAX_SUBSCRIPTIONS_PER_TENANT` and this endpoint is not one of them."""


async def upsert(
    settings: Settings,
    tenant_id: str,
    *,
    endpoint: str,
    p256dh: str,
    auth: str,
    principal_subj: str = "",
) -> dict[str, Any]:
    sub_id = subscription_id(endpoint)
    ts = now_ms()
    values = {
        "tenant_id": tenant_id,
        "id": sub_id,
        "endpoint": endpoint,
        "p256dh": p256dh,
        "auth": auth,
        "principal_subj": principal_subj,
        "created_at": ts,
        "last_ok_at": None,
        "failures": 0,
    }
    if _use_memory(settings):
        key = (tenant_id, sub_id)
        if key not in _memory_subscriptions:
            count = sum(1 for t, _ in _memory_subscriptions if t == tenant_id)
            if count >= MAX_SUBSCRIPTIONS_PER_TENANT:
                raise TooManySubscriptions
        else:
            # As the Postgres arm's `set_`: when it first subscribed, and when a push last
            # landed, are facts about the browser that new keys do not change.
            previous = _memory_subscriptions[key]
            values.update(created_at=previous["created_at"], last_ok_at=previous.get("last_ok_at"))
        _memory_subscriptions[key] = values
        return _subscription_dict(values)

    async with tenant_session(settings, tenant_id) as db:
        # Count-then-insert is a race under READ COMMITTED: two subscribes at 199 both see room.
        # One lock per tenant for the length of this transaction makes the cap a cap.
        await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"push:{tenant_id}"})
        exists = await db.scalar(
            select(PushSubscriptionRow.id).where(
                PushSubscriptionRow.tenant_id == tenant_id, PushSubscriptionRow.id == sub_id
            )
        )
        if exists is None:
            count = await db.scalar(
                select(func.count())
                .select_from(PushSubscriptionRow)
                .where(PushSubscriptionRow.tenant_id == tenant_id)
            )
            if (count or 0) >= MAX_SUBSCRIPTIONS_PER_TENANT:
                raise TooManySubscriptions
        stmt = pg_insert(cast(Any, PushSubscriptionRow.__table__)).values(values)
        await db.execute(
            stmt.on_conflict_do_update(
                index_elements=["tenant_id", "id"],
                # `created_at` stays: it is when this browser first subscribed.
                set_={
                    "endpoint": stmt.excluded.endpoint,
                    "p256dh": stmt.excluded.p256dh,
                    "auth": stmt.excluded.auth,
                    "principal_subj": stmt.excluded.principal_subj,
                    # A browser re-subscribing brings new keys; its old failures say nothing.
                    "failures": 0,
                },
            )
        )
        await db.commit()
        row = await db.get(PushSubscriptionRow, (tenant_id, sub_id))
        return _subscription_dict(row if row is not None else values)


async def remove(settings: Settings, tenant_id: str, endpoint: str) -> bool:
    """Forget one browser. True when there was a row to forget."""
    sub_id = subscription_id(endpoint)
    if _use_memory(settings):
        return _memory_subscriptions.pop((tenant_id, sub_id), None) is not None
    async with tenant_session(settings, tenant_id) as db:
        result = await db.execute(
            delete(PushSubscriptionRow).where(
                PushSubscriptionRow.tenant_id == tenant_id, PushSubscriptionRow.id == sub_id
            )
        )
        await db.commit()
        return bool(getattr(result, "rowcount", 0))


async def list_for_tenant(settings: Settings, tenant_id: str) -> list[dict[str, Any]]:
    """Every subscription a send to this tenant reaches, oldest first."""
    if _use_memory(settings):
        rows = [r for (t, _), r in _memory_subscriptions.items() if t == tenant_id]
        rows.sort(key=lambda r: (r["created_at"], r["id"]))
        return [_subscription_dict(r) for r in rows[:MAX_SUBSCRIPTIONS_PER_TENANT]]
    async with tenant_session(settings, tenant_id) as db:
        rows = (
            await db.scalars(
                select(PushSubscriptionRow)
                .where(PushSubscriptionRow.tenant_id == tenant_id)
                .order_by(PushSubscriptionRow.created_at, collate(PushSubscriptionRow.id, "C"))
                .limit(MAX_SUBSCRIPTIONS_PER_TENANT)
            )
        ).all()
        return [_subscription_dict(r) for r in rows]


async def record_outcomes(
    settings: Settings,
    tenant_id: str,
    *,
    delivered: list[str],
    failed: list[str],
    gone: list[str],
) -> None:
    """Write one send's results in one transaction, however many browsers it reached.

    `delivered` clears a row's failure count; `failed` adds one and drops any row that reaches
    `MAX_CONSECUTIVE_FAILURES`; `gone` -- a push service's 404/410, or a host no longer allowed
    -- drops the row at once. One session per send rather than per browser: a fan-out to a
    tenant's every subscription must not take the database pool from the run that caused it.
    """
    if not (delivered or failed or gone):
        return
    ts = now_ms()
    if _use_memory(settings):
        for sub_id in delivered:
            row = _memory_subscriptions.get((tenant_id, sub_id))
            if row is not None:
                row.update(last_ok_at=ts, failures=0)
        for sub_id in failed:
            row = _memory_subscriptions.get((tenant_id, sub_id))
            if row is not None:
                row["failures"] = int(row.get("failures") or 0) + 1
                if row["failures"] >= MAX_CONSECUTIVE_FAILURES:
                    del _memory_subscriptions[(tenant_id, sub_id)]
        for sub_id in gone:
            _memory_subscriptions.pop((tenant_id, sub_id), None)
        return
    async with tenant_session(settings, tenant_id) as db:
        mine = PushSubscriptionRow.tenant_id == tenant_id
        if delivered:
            await db.execute(
                update(PushSubscriptionRow)
                .where(mine, PushSubscriptionRow.id.in_(delivered))
                .values(last_ok_at=ts, failures=0)
            )
        if failed:
            await db.execute(
                update(PushSubscriptionRow)
                .where(mine, PushSubscriptionRow.id.in_(failed))
                .values(failures=PushSubscriptionRow.failures + 1)
            )
            await db.execute(
                delete(PushSubscriptionRow).where(
                    mine,
                    PushSubscriptionRow.id.in_(failed),
                    PushSubscriptionRow.failures >= MAX_CONSECUTIVE_FAILURES,
                )
            )
        if gone:
            await db.execute(delete(PushSubscriptionRow).where(mine, PushSubscriptionRow.id.in_(gone)))
        await db.commit()


__all__ = [
    "MAX_CONSECUTIVE_FAILURES",
    "MAX_SUBSCRIPTIONS_PER_TENANT",
    "TooManySubscriptions",
    "list_for_tenant",
    "record_outcomes",
    "remove",
    "reset_push_for_tests",
    "subscription_id",
    "upsert",
]
