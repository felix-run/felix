"""Emit structured audit events from the agent loop."""

from __future__ import annotations

from typing import Any

from felix.context import try_get_context


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
        pass


__all__ = ["emit_agent_audit"]
