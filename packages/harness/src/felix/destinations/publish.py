"""Claim, deliver, settle: the one path an edition takes to a destination.

Shared by `post` (`tool.py`), which admits, reads and screens a model's text first, and by the
harness's own posts (`felix.jobs.watch`, a job health alert), whose text the harness wrote. Both
need the same guarantee from the ledger (`ledger.py`): of every call naming one edition, exactly
one delivers it, and an outcome nobody knows is never retried.

`publish` raises only when the ledger cannot be claimed -- whether the edition is already there is
then unknown, so nothing is sent and the caller says so. Everything after the claim is answered as
a `Published`: a kind that raised, outlived `DELIVERY_BUDGET_S` or answered no status is `unknown`
and settled `maybe_posted`, as the ledger requires.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Literal

from felix import destinations as _registry
from felix.destinations import Delivery, Destination, EditionKey, Post, destination_kind, ledger
from felix.logging_setup import loggable

logger = logging.getLogger("felix.destinations.publish")

PublishStatus = Literal["posted", "not_posted", "maybe_posted", "already_posted", "in_flight"]


@dataclass(frozen=True, slots=True)
class Published:
    """What became of one edition. `row` is the ledger's row when another call held the edition
    (`already_posted`, `in_flight`, or `maybe_posted` found there); `detail` is the kind's own words
    for a delivery this call made, uncut."""

    status: PublishStatus
    ref: str | None = None
    detail: str = ""
    row: dict[str, Any] | None = None
    #: Whether this call delivered (won the claim), as opposed to finding the edition held.
    delivered: bool = False


def held(row: dict[str, Any]) -> Published:
    """A ledger row another call holds, as the outcome a caller reports."""
    if row.get("status") == ledger.POSTED:
        return Published("already_posted", ref=row.get("ref"), row=row)
    if ledger.in_flight(row):
        return Published("in_flight", row=row)
    return Published("maybe_posted", row=row)


async def publish(
    settings: Any, key: EditionKey, dest: Destination, post: Post, *, thread_id: str = ""
) -> Published:
    """Deliver `post` as `key`'s edition to `dest`, at most once across every caller. Raises when
    the ledger cannot be claimed."""
    claim = await ledger.claim(settings, key, thread_id=thread_id)
    if not claim.won:
        return held(claim.row)
    delivery = await deliver(settings, key.tenant_id, dest, post)
    await _settle(settings, key, delivery.status, delivery.ref)
    status: PublishStatus = "maybe_posted" if delivery.status == "unknown" else delivery.status
    return Published(status, ref=delivery.ref, detail=str(delivery.detail or ""), delivered=True)


async def _settle(settings: Any, key: EditionKey, status: str, ref: str | None) -> None:
    """Record the delivery's outcome on the claimed row. A write that fails leaves it `posting`,
    which every later call reads as `in_flight` and then `maybe_posted` -- the safe side of not
    knowing."""
    try:
        if status == "not_posted":
            await ledger.release(settings, key)
        else:
            settled = ledger.POSTED if status == "posted" else ledger.MAYBE_POSTED
            await ledger.settle(settings, key, status=settled, ref=ref)
    except Exception:
        logger.warning(
            "post ledger settle failed destination=%s status=%s",
            loggable(key.destination, limit=64),
            status,
            exc_info=True,
        )


async def deliver(settings: Any, tenant_id: str, dest: Destination, post: Post) -> Delivery:
    """One delivery by `dest`'s kind, within `DELIVERY_BUDGET_S`; never raises."""
    adapter = destination_kind(dest.kind)
    if adapter is None:
        # Uninstalled since the compile; nothing was sent.
        return Delivery("not_posted", detail="the destination's kind is no longer installed")
    try:
        # Read at call time, so the bound is the registry's (and a test's) current one.
        async with asyncio.timeout(_registry.DELIVERY_BUDGET_S):
            delivery = await adapter.deliver(settings, tenant_id, dest, post)
    except TimeoutError:
        # Past the budget the harness stops waiting; whatever the kind sent may have landed.
        logger.warning("destination %s (%s) delivery outlived its budget", dest.name, dest.kind)
        return Delivery(
            "unknown", detail="the destination's delivery took too long, so it may have been posted"
        )
    except Exception:
        # An adapter that raised may have sent first: unknown, never a guess that it did not.
        logger.warning("destination %s (%s) delivery raised", dest.name, dest.kind, exc_info=True)
        return Delivery("unknown", detail="the destination's delivery failed partway")
    if getattr(delivery, "status", None) not in ("posted", "not_posted", "unknown"):
        logger.warning("destination %s (%s) answered no delivery status", dest.name, dest.kind)
        return Delivery("unknown", detail="the destination's delivery gave no outcome")
    return delivery


__all__ = ["PublishStatus", "Published", "deliver", "held", "publish"]
