"""Scheduled jobs must fire for every tenant, exactly once per due window.

`run_due_jobs` takes `tenant_id: str = "default"` and the worker cron called it with no
argument, so **every non-default tenant's scheduled jobs silently never fired**. The
claim also happened after the invocation, so the minute-cron re-fired the same job on
every tick until the first run completed, and the write-back passed `enabled=True`
from a stale read, silently re-enabling a job an operator had just disabled.
"""

from __future__ import annotations

import pytest
from felix.config import Settings
from felix.jobs import store as jobs_store
from felix.jobs.scheduler import run_due_jobs, run_due_jobs_all_tenants


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url="memory://cron",
        object_store="memory",
        allow_insecure=True,
        auth_mode="none",
        environment="development",
    )


@pytest.fixture(autouse=True)
def _clean() -> None:
    jobs_store._memory_jobs.clear()
    jobs_store._memory_runs.clear()


async def _job(s: Settings, tenant: str, name: str, *, enabled: bool = True) -> None:
    await jobs_store.put_job(
        s, tenant, name, schedule="*/5 * * * *", manifest_id="", payload={}, enabled=enabled
    )


@pytest.mark.asyncio
async def test_every_tenant_with_jobs_is_swept(settings: Settings) -> None:
    await _job(settings, "default", "a")
    await _job(settings, "acme", "b")
    await _job(settings, "globex", "c")

    tenants = await jobs_store.list_tenants_with_jobs(settings)
    assert tenants == ["acme", "default", "globex"]

    fired = await run_due_jobs_all_tenants(settings)
    assert fired == 3, "non-default tenants' jobs never fired before this change"


@pytest.mark.asyncio
async def test_default_only_sweep_misses_other_tenants(settings: Settings) -> None:
    """Documents the old behaviour that the regression above replaces."""
    await _job(settings, "acme", "b")
    assert await run_due_jobs(settings) == 0  # tenant_id defaults to "default"
    assert await run_due_jobs(settings, tenant_id="acme") == 1


@pytest.mark.asyncio
async def test_job_is_claimed_before_running(settings: Settings) -> None:
    """A second tick in the same window must not re-fire the job."""
    await _job(settings, "default", "a")
    assert await run_due_jobs(settings) == 1
    assert await run_due_jobs(settings) == 0, "job re-fired before its next window"


@pytest.mark.asyncio
async def test_disabled_job_is_not_re_enabled(settings: Settings) -> None:
    await _job(settings, "default", "a")
    await run_due_jobs(settings)
    # operator disables it between ticks
    await _job(settings, "default", "a", enabled=False)
    row = await jobs_store.get_job(settings, "default", "a")
    assert row is not None and row["enabled"] is False
    await run_due_jobs(settings)
    row = await jobs_store.get_job(settings, "default", "a")
    assert row is not None
    assert row["enabled"] is False, "stale write-back re-enabled a disabled job"


@pytest.mark.asyncio
async def test_one_bad_tenant_does_not_stop_the_others(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _job(settings, "acme", "b")
    await _job(settings, "globex", "c")

    real = run_due_jobs

    async def _flaky(s: Settings, *, tenant_id: str = "default") -> int:
        if tenant_id == "acme":
            raise RuntimeError("acme is broken")
        return await real(s, tenant_id=tenant_id)

    monkeypatch.setattr("felix.jobs.scheduler.run_due_jobs", _flaky)
    assert await run_due_jobs_all_tenants(settings) == 1


@pytest.mark.asyncio
async def test_no_jobs_means_no_tenants(settings: Settings) -> None:
    assert await jobs_store.list_tenants_with_jobs(settings) == []
    assert await run_due_jobs_all_tenants(settings) == 0
