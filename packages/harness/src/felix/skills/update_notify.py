"""`skill.update_available`: tell an operator's webhook that an imported skill's origin moved.

A recorded check of a skill's stored origin -- the sweep, a check of the stored ref, the listing
with `refresh` (`importer.record_upstream`) -- that finds a kept-file digest other than the
skill's newest version's queues one signed event for it, unless that digest is the one already
queued. A `?ref=` what-if is never recorded, so it never notifies. An import records the digest
too, and never queues: what it recorded is the newest version's own digest, by construction. Five
choices shape it:

* **The operator's endpoints, bound per tenant.** `FELIX_SKILL_UPDATE_WEBHOOKS` lists
  `tenant=endpoint_id` pairs naming `FELIX_WEBHOOK_ENDPOINTS` ids, each of which must open to that
  tenant. There is no wildcard: one tenant's skill names and sources never reach an endpoint
  another tenant's operator bound. A tenant with no pair queues nothing.
* **Metadata only.** Names, the canonical source, the ref, commits, digests and times -- never a
  file, a diff or a description. Upstream text is untrusted, and the payload leaves the
  deployment.
* **Queued at detection, delivered by the worker.** Detection writes the row and nothing else: no
  HTTP call on the check, listing or import path, so a slow or failing receiver costs those paths
  nothing. `deliver_due_notifications` sends it with what completion webhooks use
  (`durability.webhooks.WebhookSender`): the signing, the egress-pinned client, the backoff,
  `FELIX_WEBHOOK_MAX_ATTEMPTS` and the dead letter.
* **The newest digest only.** The delivery lives on the skill's `skill_upstream` row (migration
  0030). A newer digest found before an older one was delivered replaces it; a delivery that
  finishes after that does not overwrite it (`save_notification`, guarded on the generation); and
  a check older than the one that queued what is there replaces nothing.
* **Never an update nobody can take.** A queued event is `superseded` -- never sent -- when the
  origin has moved on from its digest, or the skill's newest version already holds it (someone
  imported it): checked by every recorded check that finds the newest version current, and again
  before each delivery.

The `webhook-id` is derived from (tenant, skill, digest, generation): the same on every retry of
one queued event and to every endpoint, which is what a receiver dedupes on, and new when a digest
comes back after another was queued.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from felix.config import Settings
from felix.observability.metrics import record_counter
from felix.skills.upstream_store import get_upstream_store

logger = logging.getLogger("felix.skills.update_notify")

EVENT_TYPE = "skill.update_available"
# The delivery sweep's `skill_job_lease` row. A lease, unlike the run-webhook sweep, by choice:
# the per-tenant share below only bounds a tick when one sweep hands the tick out, and a sweep on
# every worker at once would multiply each tenant's share. Each row is claimed as well, so a sweep
# that dies mid-batch leaves its rows to the next one once the claim lapses, not for ever.
NOTIFY_LEASE = "skill_update_notify"
NOTIFY_BATCH = 50
# One tenant's share of a tick, so a tenant with hundreds of skills, or one slow receiver, cannot
# hold every other tenant's events behind it.
NOTIFY_PER_TENANT = 10
NOTIFY_CLAIM_MS = 120_000
# No delivery or endpoint starts past this, well inside the claim and the lease.
NOTIFY_SWEEP_BUDGET_MS = NOTIFY_CLAIM_MS // 2
# Who the audit trail says queued it: the harness, on a check's behalf.
NOTIFIER = "skill-upstream"


@dataclass(frozen=True, slots=True)
class CheckFacts:
    """What a recorded check already knows, so a notification need not read or ask it again:
    the skill's newest version (``head``), the tenant's ``cooldown`` (an `importer.Cooldown`),
    when the upstream commit says it was made, and how many files its own diff found changed
    against ``head``. Each left None is read here (``head``, ``cooldown``) or left out."""

    head: Mapping[str, Any] | None = None
    cooldown: Any | None = None
    committed_at: int | None = None
    changed_files: int | None = None


def _now_ms() -> int:
    from felix.durability.fibers import now_ms

    return now_ms()


def endpoints_for(settings: Settings, tenant_id: str) -> tuple[str, ...]:
    """The endpoint ids ``tenant_id``'s update notifications go to; empty is off for it."""
    from felix.durability.webhooks import parse_tenant_endpoint_bindings

    return parse_tenant_endpoint_bindings(settings.skill_update_webhooks).get(tenant_id, ())


def event_id(tenant_id: str, name: str, tree_hash: str, generation: int) -> str:
    """The `webhook-id` of one queued event: stable across its retries and endpoints, so a
    receiver dedupes on it, and new each time the skill queues -- a digest that returns after
    another was queued is news again. Hashed rather than spelled out, so no tenant id or skill
    name reaches a header in a grammar it was not validated for."""
    parts = (tenant_id, name, tree_hash, str(generation))
    return f"skill_update_{hashlib.sha256(chr(0).join(parts).encode()).hexdigest()[:32]}"


def _event(
    tenant_id: str,
    head: Mapping[str, Any],
    state: Mapping[str, Any],
    *,
    eligible_at: int,
    committed_at: int | None,
    changed_files: int | None,
) -> dict[str, Any]:
    """The payload: metadata a receiver can act on, and nothing read from a file."""
    event: dict[str, Any] = {
        "type": EVENT_TYPE,
        "tenant_id": tenant_id,
        "skill": head["name"],
        "source": state["origin_source"],
        "ref": state["origin_ref"],
        "current": {
            "version": head["version"],
            "commit": head.get("origin_commit"),
            "tree_hash": head.get("origin_tree_hash"),
        },
        "upstream": {
            "commit": state["upstream_commit"],
            "tree_hash": state["upstream_tree_hash"],
            "committed_at": committed_at,
            "first_seen_at": state["first_seen_at"],
        },
        "eligible_at": eligible_at,
        "checked_at": state["checked_at"],
    }
    if changed_files is not None:
        event["changed_files"] = changed_files
    return event


async def _head(settings: Settings, tenant_id: str, name: str) -> Mapping[str, Any] | None:
    """The skill's newest version that was not rejected, if it is an import; else None."""
    from felix.skills import library
    from felix.skills.github import SkillNotImported
    from felix.skills.upstream import imported_head

    try:
        return (await imported_head(settings, tenant_id, name))[1]
    except library.SkillNotFound, SkillNotImported:
        return None


async def notify_if_new(
    settings: Settings,
    tenant_id: str,
    name: str,
    state: Mapping[str, Any],
    facts: CheckFacts | None = None,
) -> bool:
    """Queue ``name``'s `skill.update_available` for the check ``state`` recorded, when the tenant
    has an endpoint and the digest is neither the skill's newest version's nor the one already
    queued; or, when the newest version holds what the origin does, supersede a queued one.
    Whether one was queued. Nothing here asks GitHub. Never raises: it runs after a recorded
    check, which a notification must not fail."""
    try:
        return await _queue(settings, tenant_id, name, state, facts or CheckFacts())
    except Exception:
        from felix.logging_setup import loggable

        logger.warning(
            "queueing the update notification of %s/%s failed",
            loggable(tenant_id, limit=128),
            loggable(name, limit=64),
            exc_info=True,
        )
        return False


async def _queue(
    settings: Settings, tenant_id: str, name: str, state: Mapping[str, Any], facts: CheckFacts
) -> bool:
    endpoints = endpoints_for(settings, tenant_id)
    tree_hash = state.get("upstream_tree_hash")
    if not endpoints or not tree_hash or state.get("first_seen_at") is None:
        return False
    head = facts.head if facts.head is not None else await _head(settings, tenant_id, name)
    if head is None or head.get("origin_source") != state.get("origin_source"):
        return False
    store = get_upstream_store(settings)
    checked_at = int(state["checked_at"])
    if head.get("origin_tree_hash") == tree_hash:
        # The newest version holds what the origin does: whatever is queued is moot.
        if await store.cancel_notification(tenant_id, name, checked_at=checked_at):
            record_counter("felix_skill_update_notification", {"outcome": "superseded"})
        return False
    cooldown = facts.cooldown
    if cooldown is None:
        from felix.skills import importer

        cooldown = await importer.cooldown_for(settings, tenant_id, checked_at)
    event = _event(
        tenant_id,
        head,
        state,
        eligible_at=cooldown.eligible_at(int(state["first_seen_at"])),
        committed_at=facts.committed_at,
        changed_files=facts.changed_files,
    )
    queued, previous = await store.enqueue_notification(
        tenant_id,
        name,
        tree_hash=str(tree_hash),
        checked_at=checked_at,
        state={"event": event, "endpoints": {ep: {"status": "pending", "attempts": 0} for ep in endpoints}},
        due_at=_now_ms(),
    )
    if not queued:
        return False
    record_counter("felix_skill_update_notification", {"outcome": "enqueued"})
    if previous == "pending":
        record_counter("felix_skill_update_notification", {"outcome": "superseded"})
    _audit_queued(settings, tenant_id, event, endpoints, superseded=previous == "pending")
    return True


def _audit_queued(
    settings: Settings,
    tenant_id: str,
    event: Mapping[str, Any],
    endpoints: tuple[str, ...],
    *,
    superseded: bool,
) -> None:
    from felix.audit.emit import record_offline_event

    record_offline_event(
        settings,
        tenant_id,
        "skill_update_notification_queued",
        principal=NOTIFIER,
        payload={
            "skill": event["skill"],
            "source": event["source"],
            "version": event["current"]["version"],
            "upstream_commit": event["upstream"]["commit"],
            "upstream_tree_hash": event["upstream"]["tree_hash"],
            "endpoints": list(endpoints),
            "superseded": superseded,
        },
    )


# -- delivery ------------------------------------------------------------------------------------


async def _moved_past(settings: Settings, row: Mapping[str, Any]) -> bool:
    """Whether the queued digest is no longer news: the origin's last recorded digest is another,
    or the skill's newest version already holds it (or the skill is no longer an import)."""
    tree_hash = row["notified_tree_hash"]
    if row.get("upstream_tree_hash") != tree_hash:
        return True
    head = await _head(settings, str(row["tenant_id"]), str(row["name"]))
    return head is None or head.get("origin_tree_hash") == tree_hash


async def _deliver_row(settings: Settings, row: Mapping[str, Any], sender: Any, deadline: int) -> None:
    from felix.durability.webhooks import canonical_body, delivery_outcome

    tenant_id, name = str(row["tenant_id"]), str(row["name"])
    tree_hash, generation = str(row["notified_tree_hash"]), int(row["notify_generation"])
    store = get_upstream_store(settings)
    state = dict(row.get("notify_state") or {})
    if await _moved_past(settings, row):
        superseded = await store.save_notification(
            tenant_id,
            name,
            generation=generation,
            status="superseded",
            due_at=None,
            attempts=int(row.get("notify_attempts") or 0),
            state=state,
        )
        if superseded:
            record_counter("felix_skill_update_notification", {"outcome": "superseded"})
        return
    bound = set(endpoints_for(settings, tenant_id))
    endpoints = {ep: dict(v or {}) for ep, v in (state.get("endpoints") or {}).items()}
    state["endpoints"] = await sender.attempt(
        endpoints,
        tenant_id=tenant_id,
        # Built once, at detection, and stored: every retry sends the same bytes under the same id.
        body=canonical_body(state.get("event") or {}),
        message_id=lambda _ep: event_id(tenant_id, name, tree_hash, generation),
        subject="a skill update",
        # An endpoint unbound from the tenant since detection is dead for it, not still sent to.
        still_routed=lambda ep: ep in bound,
        deadline=deadline,
    )
    status, due_at = delivery_outcome(state["endpoints"])
    attempts = max((int(v.get("attempts") or 0) for v in state["endpoints"].values()), default=0)
    written = await store.save_notification(
        tenant_id, name, generation=generation, status=status, due_at=due_at, attempts=attempts, state=state
    )
    if not written:
        logger.info("skill update notification queued again during delivery; its outcome is dropped")


async def deliver_due_notifications(settings: Settings) -> int:
    """Send every queued `skill.update_available` that is due, at most `NOTIFY_PER_TENANT` of a
    tenant's a tick. Returns how many were claimed."""
    from felix.durability.webhooks import WebhookSender, parse_webhook_endpoints
    from felix.skills.quality_store import sweep_lock

    async with sweep_lock(settings, name=NOTIFY_LEASE, lease_ms=NOTIFY_CLAIM_MS) as lease:
        if lease is None:
            return 0
        now = _now_ms()
        due = await get_upstream_store(settings).claim_notifications(
            now=now, claim_until=now + NOTIFY_CLAIM_MS, limit=NOTIFY_BATCH, per_tenant=NOTIFY_PER_TENANT
        )
        if not due:
            return 0
        from felix.secrets import build_secrets

        sender = WebhookSender(
            settings, parse_webhook_endpoints(settings), build_secrets(settings), "skill_update"
        )
        deadline = _now_ms() + NOTIFY_SWEEP_BUDGET_MS
        for row in due:
            if _now_ms() > deadline or not await lease.renew():
                break
            try:
                await _deliver_row(settings, row, sender, deadline)
            except Exception:
                # The bookkeeping itself; the claim lapses and the next sweep tries again.
                logger.warning("a skill update notification could not be recorded", exc_info=True)
        return len(due)


__all__ = [
    "EVENT_TYPE",
    "NOTIFY_BATCH",
    "NOTIFY_CLAIM_MS",
    "NOTIFY_LEASE",
    "NOTIFY_PER_TENANT",
    "NOTIFY_SWEEP_BUDGET_MS",
    "CheckFacts",
    "deliver_due_notifications",
    "endpoints_for",
    "event_id",
    "notify_if_new",
]
