"""Every per-tenant sweep must bind `app.tenant_id` before it touches a store.

The HTTP path never has this problem: `AuthMiddleware` wraps each request in
`async_run_with_context`, which binds `rls_tenant(...)`, so the fifty-odd tenant-scoped store
functions inherit it and none of them binds explicitly. The worker has no request context, so
nothing supplies it there.

Under `FELIX_DATABASE_RLS` that is silent rather than loud. `_rls_after_begin` finds no tenant,
leaves the policy to filter, and every read comes back empty — so the sweep scans an empty table
and reports success. No error, no log, just periodic work that quietly stops happening: the
scheduled jobs of every tenant, the anomaly scan, the canary benchmark.

These tests observe the context variable at the moment each sweep calls into its per-tenant
worker. That needs a spy rather than a real store call, because the binding is scoped to the
call and has been unwound by the time it returns — the spy substitutes nothing, it only records
what was bound while it ran.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.config import Settings
from felix.db.session import _rls_tenant


def _settings() -> Settings:
    return Settings(database_url="memory://sweeps", object_store="memory")


@pytest.mark.asyncio
async def test_the_job_sweep_binds_each_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    from felix.jobs import scheduler
    from felix.jobs import store as jobs_store

    settings = _settings()
    for tenant in ("acme", "globex"):
        await jobs_store.put_job(settings, tenant, "nightly", schedule="@daily", enabled=True)

    seen: list[tuple[str, str | None]] = []

    async def _spy(_settings: Any, *, tenant_id: str) -> int:
        seen.append((tenant_id, _rls_tenant.get()))
        return 0

    monkeypatch.setattr(scheduler, "run_due_jobs", _spy)
    await scheduler.run_due_jobs_all_tenants(settings)

    assert seen, "the sweep found no tenants to run"
    assert all(bound == tenant for tenant, bound in seen), seen


@pytest.mark.asyncio
async def test_the_anomaly_sweep_binds_each_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    from felix.audit import store as audit_store
    from felix.jobs import anomaly

    settings = _settings()
    audit_store._pending.reset_for_tests()
    audit_store._memory_events.clear()
    for tenant in ("acme", "globex"):
        audit_store.record_event(settings, tenant, "tool_call", status="ok")
    await audit_store.flush_pending(settings)

    seen: list[tuple[str, str | None]] = []

    async def _spy(_settings: Any, *, tenant_id: str) -> list[dict[str, Any]]:
        seen.append((tenant_id, _rls_tenant.get()))
        return []

    monkeypatch.setattr(anomaly, "run_anomaly_scan", _spy)
    await anomaly.run_anomaly_scan_all_tenants(settings)

    assert seen, "the sweep found no tenants to scan"
    assert all(bound == tenant for tenant, bound in seen), seen


@pytest.mark.asyncio
async def test_the_continuous_eval_sweep_binds_each_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    from felix.jobs import continuous_eval
    from felix.manifests import store as manifest_store
    from felix.manifests.loader import parse_manifest

    settings = _settings()
    manifest = parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "agent"},
            "spec": {"pattern": "react"},
        }
    )
    for tenant in ("acme", "globex"):
        await manifest_store.put_version(settings, tenant, "agent", manifest)

    seen: list[tuple[str, str | None]] = []

    async def _spy(_settings: Any, *, tenant_id: str) -> dict[str, Any]:
        seen.append((tenant_id, _rls_tenant.get()))
        return {"runs": 0}

    monkeypatch.setattr(continuous_eval, "run_continuous_eval", _spy)
    await continuous_eval.run_continuous_eval_all_tenants(settings)

    assert seen, "the sweep found no tenants to evaluate"
    assert all(bound == tenant for tenant, bound in seen), seen


@pytest.mark.asyncio
async def test_the_binding_does_not_leak_past_the_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each tenant's binding is unwound before the next, and none survives the loop.

    A binding that leaked would be worse than none: the next tenant's queries would run under
    the previous tenant's policy, which is a cross-tenant read rather than an empty one.
    """
    from felix.jobs import scheduler
    from felix.jobs import store as jobs_store

    settings = _settings()
    for tenant in ("acme", "globex"):
        await jobs_store.put_job(settings, tenant, "nightly", schedule="@daily", enabled=True)

    async def _spy(_settings: Any, *, tenant_id: str) -> int:
        assert _rls_tenant.get() == tenant_id
        return 0

    monkeypatch.setattr(scheduler, "run_due_jobs", _spy)
    assert _rls_tenant.get() is None

    await scheduler.run_due_jobs_all_tenants(settings)

    assert _rls_tenant.get() is None, "the sweep left a tenant bound behind it"
