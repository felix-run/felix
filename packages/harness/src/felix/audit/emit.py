"""Emit structured audit events from the agent loop."""

from __future__ import annotations

import logging
from typing import Any

from felix.context import try_get_context

logger = logging.getLogger("felix.audit.emit")


def emit_agent_audit(
    event_type: str,
    *,
    status: str = "",
    payload: dict[str, Any] | None = None,
    manifest_id: str = "",
) -> None:
    """Best-effort audit emit when a RequestContext with settings is installed."""
    ctx = try_get_context()
    if ctx is None or ctx.settings is None:
        return
    try:
        from felix.audit import store as audit_store

        audit_store.record_event(
            ctx.settings,
            ctx.auth.tenant_id,
            event_type,
            manifest_id=manifest_id or ctx.manifest_id,
            principal_subj=getattr(ctx.auth, "principal_sub", "") or "",
            status=status,
            payload=payload or {},
        )
        ctx.limit_state.audit_count = int(getattr(ctx.limit_state, "audit_count", 0) or 0) + 1
    except Exception:
        # An audit event that cannot be recorded is a governance gap, not a detail.
        logger.warning("audit emit failed for %s", event_type, exc_info=True)


def record_offline_event(
    settings: Any,
    tenant_id: str,
    event_type: str,
    *,
    principal: str,
    payload: dict[str, Any],
    status: str = "ok",
    manifest_id: str = "",
) -> None:
    """An audit event for work done outside a request: a management route acting for an
    operator, or a worker job. `emit_agent_audit` needs a `RequestContext`, which neither has,
    so this names the tenant and principal itself. Never raises; a failed write is logged."""
    try:
        from felix.audit import store as audit_store

        audit_store.record_event(
            settings,
            tenant_id,
            event_type,
            manifest_id=manifest_id,
            principal_subj=principal,
            status=status,
            payload=payload,
        )
    except Exception:
        logger.warning("audit write failed for %s", event_type, exc_info=True)


__all__ = ["emit_agent_audit", "record_offline_event"]
