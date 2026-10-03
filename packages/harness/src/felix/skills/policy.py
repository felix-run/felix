"""A tenant's skill publish policy: read it, set it, drop it.

The rule for combining a tenant's row with the deployment's settings is `publish_gate`'s
(`publish_policy`: tighten only). This module is the one path that reads a row, so the gate and
`GET /skill-library/-/policy` cannot disagree about which policy is in force, and the only one
that writes it, so every change is audited.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from felix.config import Settings
from felix.skills.publish_gate import TUNABLE_FIELDS, PublishPolicy, publish_policy
from felix.skills.quality_store import get_skill_policy_store

now_ms = lambda: int(time.time() * 1000)


@dataclass(slots=True, frozen=True)
class PolicyState:
    """The policy in force, what the tenant itself set (None without a row), and who set it."""

    policy: PublishPolicy
    tenant: PublishPolicy | None = None
    updated_at: int | None = None
    updated_by: str | None = None


def _state(settings: Settings, row: Mapping[str, Any] | None) -> PolicyState:
    if row is None:
        return PolicyState(policy=publish_policy(settings, None))
    return PolicyState(
        policy=publish_policy(settings, row),
        tenant=PublishPolicy.from_row(row),
        updated_at=row.get("updated_at"),
        updated_by=row.get("updated_by"),
    )


async def load_publish_policy(settings: Settings, tenant_id: str) -> PolicyState:
    """The policy in force for ``tenant_id``.

    Not caught: a gate that cannot read the tenant's policy must not quietly fall back to the
    settings, which may be the lower bar.
    """
    return _state(settings, await get_skill_policy_store(settings).get(tenant_id))


def _audit(
    settings: Settings, tenant_id: str, event: str, by: str, before: PolicyState, after: PolicyState
) -> None:
    from felix.audit.emit import record_offline_event

    record_offline_event(
        settings,
        tenant_id,
        event,
        principal=by,
        payload={
            "before": {**before.policy.to_row(), "source": before.policy.source},
            "after": {**after.policy.to_row(), "source": after.policy.source},
            "tenant": after.tenant.to_row() if after.tenant else None,
        },
    )


async def set_publish_policy(
    settings: Settings, tenant_id: str, changes: Mapping[str, Any], *, by: str
) -> PolicyState:
    """Change the fields in ``changes`` of the tenant's own policy and return the state now.

    The rest keep the tenant's value, or the settings' when the tenant has no row yet. Any value
    is stored as sent -- ranges are the caller's to validate -- and the policy in force is the
    settings tightened by it, so a value looser than the deployment's is stored and outvoted.
    Audited as `skill_policy_updated`.
    """
    store = get_skill_policy_store(settings)
    before = _state(settings, await store.get(tenant_id))
    base = before.tenant or PublishPolicy.from_settings(settings)
    row = {**base.to_row(), **{f: changes[f] for f in TUNABLE_FIELDS if f in changes}}
    after = _state(settings, await store.put(tenant_id, {**row, "updated_at": now_ms(), "updated_by": by}))
    _audit(settings, tenant_id, "skill_policy_updated", by, before, after)
    return after


async def delete_publish_policy(settings: Settings, tenant_id: str, *, by: str) -> PolicyState:
    """Drop the tenant's policy, so the settings alone decide. Audited as `skill_policy_deleted`
    when there was one to drop."""
    store = get_skill_policy_store(settings)
    before = _state(settings, await store.get(tenant_id))
    if await store.delete(tenant_id):
        after = _state(settings, None)
        _audit(settings, tenant_id, "skill_policy_deleted", by, before, after)
        return after
    return before


def policy_body(state: PolicyState) -> dict[str, Any]:
    """What `GET /skill-library/-/policy` returns: the policy in force, the tenant's own values,
    and the stamp."""
    return {
        **asdict(state.policy),
        "tenant_values": state.tenant.to_row() if state.tenant else None,
        "updated_at": state.updated_at,
        "updated_by": state.updated_by,
    }


__all__ = [
    "PolicyState",
    "delete_publish_policy",
    "load_publish_policy",
    "policy_body",
    "set_publish_policy",
]
