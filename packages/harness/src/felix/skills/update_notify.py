"""`skill.update_available`: tell an operator's webhook that an imported skill's origin moved.

A recorded check of a skill's stored origin -- the sweep, a check of the stored ref, the listing
with `refresh`, an import (`importer.record_upstream`) -- that finds a kept-file digest other than
the skill's newest version's queues one signed event for it, unless that digest was already
queued. A `?ref=` what-if is never recorded, so it never notifies. Four choices shape it:

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
  (`durability.webhooks.attempt_endpoints`): the signing, the egress-pinned client, the backoff,
  `FELIX_WEBHOOK_MAX_ATTEMPTS` and the dead letter.
* **The newest digest only.** The delivery lives on the skill's `skill_upstream` row (migration
  0030), so a newer digest found before an older one was delivered replaces it, and a delivery
  that finishes after that does not overwrite the newer one (`save_notification`). A receiver is
  never sent an update the origin has already moved past.

The `webhook-id` is derived from (tenant, skill, digest), so it is the same on every retry and to
every endpoint: what a receiver dedupes on.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from typing import Any

from felix.config import Settings
from felix.observability.metrics import record_counter
from felix.skills.upstream_store import get_upstream_store

logger = logging.getLogger("felix.skills.update_notify")

EVENT_TYPE = "skill.update_available"
# The delivery sweep's `skill_job_lease` row: one sweep at a time across workers, as the check
# sweep holds its own. Each row is claimed as well, so a sweep that dies mid-batch leaves its rows
# to the next one once the claim lapses, not for ever.
NOTIFY_LEASE = "skill_update_notify"
NOTIFY_BATCH = 50
NOTIFY_CLAIM_MS = 120_000
# No delivery starts past this, well inside the claim and the lease: see `WEBHOOK_SWEEP_BUDGET_MS`.
NOTIFY_SWEEP_BUDGET_MS = NOTIFY_CLAIM_MS // 2
# Who the audit trail says queued it: the harness, on a check's behalf.
NOTIFIER = "skill-upstream"


def _now_ms() -> int:
    from felix.durability.fibers import now_ms

    return now_ms()


def parse_skill_update_webhooks(raw: str) -> dict[str, tuple[str, ...]]:
    """`FELIX_SKILL_UPDATE_WEBHOOKS` -- comma-separated `tenant=endpoint_id` -- as tenant to its
    endpoint ids, in the order given, each once. Raises `ValueError` naming the bad entry; whether
    each id is registered for its tenant is `Settings`' check, against `FELIX_WEBHOOK_ENDPOINTS`."""
    out: dict[str, list[str]] = {}
    for entry in (e.strip() for e in (raw or "").split(",")):
        if not entry:
            continue
        tenant, sep, endpoint = (p.strip() for p in entry.partition("="))
        if not sep or not tenant or not endpoint:
            raise ValueError(f"entry {entry!r} must be tenant=endpoint_id")
        if tenant == "*" or "*" in endpoint:
            raise ValueError(f"entry {entry!r}: there is no wildcard; bind each tenant to its endpoint")
        ids = out.setdefault(tenant, [])
        if endpoint not in ids:
            ids.append(endpoint)
    return {tenant: tuple(ids) for tenant, ids in out.items()}


def validate_skill_update_webhooks(settings: Any) -> None:
    """Every bound id is a registered endpoint that opens to its tenant. `ValueError` otherwise,
    one message for both: an operator's typo and an endpoint fenced to other tenants are fixed in
    the same place."""
    from felix.durability.webhooks import parse_webhook_endpoints

    bindings = parse_skill_update_webhooks(getattr(settings, "skill_update_webhooks", ""))
    if not bindings:
        return
    registry = parse_webhook_endpoints(settings)
    for tenant, ids in bindings.items():
        for name in ids:
            endpoint = registry.get(name)
            if endpoint is None or not endpoint.allows(tenant):
                raise ValueError(
                    f"{tenant}={name}: no FELIX_WEBHOOK_ENDPOINTS endpoint {name!r} registered for {tenant!r}"
                )


def endpoints_for(settings: Settings, tenant_id: str) -> tuple[str, ...]:
    """The endpoint ids ``tenant_id``'s update notifications go to; empty is off for it."""
    return parse_skill_update_webhooks(settings.skill_update_webhooks).get(tenant_id, ())


def event_id(tenant_id: str, name: str, tree_hash: str) -> str:
    """The `webhook-id` of one skill's update to one digest: stable across retries and endpoints,
    so a receiver dedupes on it. Hashed rather than spelled out, so no tenant id or skill name
    reaches a header in a grammar it was not validated for."""
    digest = hashlib.sha256("\x00".join((tenant_id, name, tree_hash)).encode()).hexdigest()
    return f"skill_update_{digest[:32]}"


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


async def notify_if_new(
    settings: Settings,
    tenant_id: str,
    name: str,
    state: Mapping[str, Any],
    *,
    committed_at: int | None = None,
    changed_files: int | None = None,
) -> bool:
    """Queue ``name``'s `skill.update_available` for the check ``state`` recorded, when its digest
    is not the skill's newest version's nor the one last queued and the tenant has an endpoint.
    Whether one was queued. ``committed_at`` and ``changed_files`` ride along when the caller
    already knows them; nothing here asks GitHub. Never raises: it runs after a recorded check,
    which a notification must not fail."""
    try:
        return await _queue(settings, tenant_id, name, state, committed_at, changed_files)
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
    settings: Settings,
    tenant_id: str,
    name: str,
    state: Mapping[str, Any],
    committed_at: int | None,
    changed_files: int | None,
) -> bool:
    endpoints = endpoints_for(settings, tenant_id)
    tree_hash = state.get("upstream_tree_hash")
    if not endpoints or not tree_hash or state.get("first_seen_at") is None:
        return False
    from felix.skills import importer, library
    from felix.skills.github import SkillNotImported
    from felix.skills.upstream import imported_head

    try:
        _, head = await imported_head(settings, tenant_id, name)
    except library.SkillNotFound, SkillNotImported:
        return False
    if head.get("origin_tree_hash") == tree_hash or head.get("origin_source") != state.get("origin_source"):
        return False
    cooldown = await importer.cooldown_for(settings, tenant_id, int(state["checked_at"]))
    event = _event(
        tenant_id,
        head,
        state,
        eligible_at=cooldown.eligible_at(int(state["first_seen_at"])),
        committed_at=committed_at,
        changed_files=changed_files,
    )
    queued, previous = await get_upstream_store(settings).enqueue_notification(
        tenant_id,
        name,
        tree_hash=str(tree_hash),
        state={"event": event, "endpoints": {ep: {"status": "pending", "attempts": 0} for ep in endpoints}},
        due_at=_now_ms(),
    )
    if not queued:
        return False
    record_counter("felix_skill_update_notification", {"outcome": "enqueued"})
    if previous == "pending":
        record_counter("felix_skill_update_notification", {"outcome": "superseded"})
    from felix.audit.emit import record_offline_event

    record_offline_event(
        settings,
        tenant_id,
        "skill_update_notification_queued",
        principal=NOTIFIER,
        payload={
            "skill": name,
            "source": event["source"],
            "version": event["current"]["version"],
            "upstream_commit": event["upstream"]["commit"],
            "upstream_tree_hash": tree_hash,
            "endpoints": list(endpoints),
            "superseded": previous == "pending",
        },
    )
    return True


# -- delivery ------------------------------------------------------------------------------------


async def _deliver_row(settings: Settings, row: Mapping[str, Any], registry: Any, provider: Any) -> None:
    from felix.durability.webhooks import attempt_endpoints

    tenant_id, name, tree_hash = str(row["tenant_id"]), str(row["name"]), str(row["notified_tree_hash"])
    state = dict(row.get("notify_state") or {})
    endpoints = {ep: dict(v or {}) for ep, v in (state.get("endpoints") or {}).items()}
    # Built once, at detection, and stored: every retry sends the same bytes under the same id.
    body = json.dumps(state.get("event") or {}, separators=(",", ":"), sort_keys=True).encode()
    bound = set(endpoints_for(settings, tenant_id))
    status, due_at = await attempt_endpoints(
        settings,
        endpoints,
        tenant_id=tenant_id,
        registry=registry,
        provider=provider,
        body=body,
        message_id=lambda _ep: event_id(tenant_id, name, tree_hash),
        metric="felix_skill_update_delivery",
        subject="a skill update",
        # An endpoint unbound from the tenant since detection is dead for it, not still sent to.
        still_routed=lambda ep: ep in bound,
    )
    state["endpoints"] = endpoints
    attempts = max((int(v.get("attempts") or 0) for v in endpoints.values()), default=0)
    written = await get_upstream_store(settings).save_notification(
        tenant_id, name, tree_hash=tree_hash, status=status, due_at=due_at, attempts=attempts, state=state
    )
    if not written:
        logger.info("skill update notification superseded during delivery")


async def deliver_due_notifications(settings: Settings) -> int:
    """Send every queued `skill.update_available` that is due. Returns how many were claimed."""
    from felix.durability.webhooks import parse_webhook_endpoints
    from felix.skills.quality_store import sweep_lock

    async with sweep_lock(settings, name=NOTIFY_LEASE, lease_ms=NOTIFY_CLAIM_MS) as lease:
        if lease is None:
            return 0
        now = _now_ms()
        due = await get_upstream_store(settings).claim_notifications(
            now=now, claim_until=now + NOTIFY_CLAIM_MS, limit=NOTIFY_BATCH
        )
        if not due:
            return 0
        from felix.secrets import build_secrets

        registry = parse_webhook_endpoints(settings)
        provider = build_secrets(settings)
        deadline = _now_ms() + NOTIFY_SWEEP_BUDGET_MS
        for row in due:
            if _now_ms() > deadline or not await lease.renew():
                break
            try:
                await _deliver_row(settings, row, registry, provider)
            except Exception:
                # The bookkeeping itself; the claim lapses and the next sweep tries again.
                logger.warning("a skill update notification could not be recorded", exc_info=True)
        return len(due)


__all__ = [
    "EVENT_TYPE",
    "NOTIFY_BATCH",
    "NOTIFY_CLAIM_MS",
    "NOTIFY_LEASE",
    "NOTIFY_SWEEP_BUDGET_MS",
    "deliver_due_notifications",
    "endpoints_for",
    "event_id",
    "notify_if_new",
    "parse_skill_update_webhooks",
    "validate_skill_update_webhooks",
]
