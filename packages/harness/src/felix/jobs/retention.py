"""Prune aged audit events, expired plans, and superseded memories."""

from __future__ import annotations

import logging
import time

from sqlalchemy import delete

from felix.config import Settings
from felix.db.models import AuditEvent, MemoryVector, Plan
from felix.db.session import _use_memory, get_session_factory

logger = logging.getLogger("felix.jobs.retention")

# Defaults — overridable via settings.extras later.
AUDIT_TTL_MS = 30 * 24 * 60 * 60 * 1000  # 30 days
MEMORY_SUPERSEDED_GRACE_MS = 7 * 24 * 60 * 60 * 1000

now_ms = lambda: int(time.time() * 1000)


async def run_retention_sweep(settings: Settings) -> dict[str, int]:
    """Delete expired control-plane rows. Returns counts per table."""
    ts = now_ms()
    audit_cutoff = ts - AUDIT_TTL_MS
    mem_cutoff = ts - MEMORY_SUPERSEDED_GRACE_MS
    counts = {"audit_events": 0, "plans": 0, "memory_vectors": 0}

    if _use_memory(settings):
        from felix.audit import store as audit_store
        from felix.memory import store as memory_store
        from felix.plans import store as plans_store

        # Audit: drop buffered events older than cutoff (memory path uses buffer only).
        pending = getattr(audit_store, "_pending", None)
        if isinstance(pending, list):
            before = len(pending)
            pending[:] = [e for e in pending if int(e.get("ts") or 0) >= audit_cutoff]
            counts["audit_events"] = before - len(pending)

        # Plans: delete expired
        for tenant in {"default"}:
            for plan in list(await plans_store.list_plans(settings, tenant, limit=500)):
                exp = plan.get("expires_at")
                if exp is not None and exp < ts:
                    if await plans_store.delete_plan(settings, tenant, plan["id"]):
                        counts["plans"] += 1

        rows = getattr(memory_store, "_memory_rows", {})
        if isinstance(rows, dict):
            drop_keys = [
                k
                for k, r in rows.items()
                if r.get("superseded_seq") is not None and int(r.get("created_at") or 0) < mem_cutoff
            ]
            for k in drop_keys:
                rows.pop(k, None)
            counts["memory_vectors"] = len(drop_keys)

        logger.info("retention_sweep %s", counts)
        return counts

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        r = await db.execute(delete(AuditEvent).where(AuditEvent.ts < audit_cutoff))
        counts["audit_events"] = int(r.rowcount or 0)

        r = await db.execute(delete(Plan).where(Plan.expires_at.is_not(None), Plan.expires_at < ts))
        counts["plans"] = int(r.rowcount or 0)

        r = await db.execute(
            delete(MemoryVector).where(
                MemoryVector.superseded_seq.is_not(None),
                MemoryVector.created_at < mem_cutoff,
            )
        )
        counts["memory_vectors"] = int(r.rowcount or 0)
        await db.commit()

    logger.info("retention_sweep %s", counts)
    return counts


__all__ = ["run_retention_sweep"]
