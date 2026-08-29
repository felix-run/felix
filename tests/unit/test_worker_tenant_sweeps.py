"""The periodic scans must cover every tenant, not just `default`.

`run_anomaly_scan` and `run_continuous_eval` both default to `tenant_id="default"`
and the worker cron passed nothing, so on a multi-tenant deployment anomaly
detection and canary benchmarking silently covered one tenant. `run_due_jobs` had
the identical bug and was fixed once already — these two were not swept up with it.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.audit.store import list_tenants_with_events, record_event
from felix.config import Settings
from felix.jobs.anomaly import run_anomaly_scan_all_tenants
from felix.manifests.store import list_tenants_with_active

SETTINGS = Settings(database_url="memory://sweeps")


@pytest.fixture(autouse=True)
def _clean_audit() -> Any:
    from felix.audit.store import _memory_events, _pending

    _pending.clear()
    _memory_events.clear()
    yield
    _pending.clear()
    _memory_events.clear()


async def _flush(tenants: list[str]) -> None:
    from felix.audit.store import flush_pending

    for tenant in tenants:
        record_event(SETTINGS, tenant, "tool_call", manifest_id="m", status="ok")
    await flush_pending(SETTINGS)


@pytest.mark.asyncio
async def test_every_tenant_with_audit_events_is_enumerated() -> None:
    await _flush(["acme", "beta", "default"])

    assert await list_tenants_with_events(SETTINGS) == ["acme", "beta", "default"]


@pytest.mark.asyncio
async def test_the_anomaly_sweep_visits_every_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    """The regression: the cron scanned `default` and nothing else."""
    await _flush(["acme", "beta", "default"])
    seen: list[str] = []

    async def _record(settings: Settings, *, tenant_id: str = "default") -> list[dict]:
        seen.append(tenant_id)
        return []

    monkeypatch.setattr("felix.jobs.anomaly.run_anomaly_scan", _record)

    await run_anomaly_scan_all_tenants(SETTINGS)

    assert sorted(seen) == ["acme", "beta", "default"]


@pytest.mark.asyncio
async def test_one_tenants_failure_does_not_stop_the_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Otherwise a single bad tenant silently disables detection for the rest."""
    await _flush(["acme", "beta", "default"])
    scanned: list[str] = []

    async def _explode(settings: Settings, *, tenant_id: str = "default") -> list[dict]:
        if tenant_id == "beta":
            raise RuntimeError("bad data")
        scanned.append(tenant_id)
        return [{"tenant": tenant_id}]

    monkeypatch.setattr("felix.jobs.anomaly.run_anomaly_scan", _explode)

    findings = await run_anomaly_scan_all_tenants(SETTINGS)

    assert sorted(scanned) == ["acme", "default"]
    assert len(findings) == 2


@pytest.mark.asyncio
async def test_the_continuous_eval_sweep_visits_every_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    from felix.jobs.continuous_eval import run_continuous_eval_all_tenants

    seen: list[str] = []

    async def _tenants(settings: Settings) -> list[str]:
        return ["acme", "beta"]

    async def _record(settings: Settings, *, tenant_id: str = "default") -> dict[str, Any]:
        seen.append(tenant_id)
        return {"runs": 1}

    monkeypatch.setattr("felix.manifests.store.list_tenants_with_active", _tenants)
    monkeypatch.setattr("felix.jobs.continuous_eval.run_continuous_eval", _record)

    result = await run_continuous_eval_all_tenants(SETTINGS)

    assert sorted(seen) == ["acme", "beta"]
    assert result == {"runs": 2, "tenants": 2}


@pytest.mark.asyncio
async def test_manifest_tenant_enumeration_is_distinct() -> None:
    from felix.manifests import store as manifest_store

    manifest_store._memory_active.clear()
    for tenant in ("acme", "acme", "beta"):
        manifest_store._memory_active[(tenant, f"m-{tenant}")] = {"version": 1}

    try:
        assert await list_tenants_with_active(SETTINGS) == ["acme", "beta"]
    finally:
        manifest_store._memory_active.clear()


# --- the wiring ---------------------------------------------------------------
#
# The sweeps above are only worth anything if the cron calls them. Reverting the two
# worker tasks to the single-tenant functions left the rest of this file green.


@pytest.mark.asyncio
async def test_the_anomaly_cron_sweeps_every_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    from felix_worker import tasks as worker_tasks

    called: list[str] = []

    async def _sweep(settings: Settings) -> list[dict]:
        called.append("all_tenants")
        return []

    monkeypatch.setattr("felix.jobs.anomaly.run_anomaly_scan_all_tenants", _sweep)
    await worker_tasks.anomaly_scan.original_func()

    assert called == ["all_tenants"], "cron must call the all-tenants sweep"


@pytest.mark.asyncio
async def test_the_continuous_eval_cron_sweeps_every_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    from felix_worker import tasks as worker_tasks

    called: list[str] = []

    async def _sweep(settings: Settings) -> dict[str, Any]:
        called.append("all_tenants")
        return {"runs": 0}

    monkeypatch.setattr("felix.jobs.continuous_eval.run_continuous_eval_all_tenants", _sweep)
    await worker_tasks.continuous_eval.original_func()

    assert called == ["all_tenants"], "cron must call the all-tenants sweep"
