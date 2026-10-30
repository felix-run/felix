"""A job that keeps failing is reported once at an operator's webhook, and the scheduler's heartbeat
is readable from outside.

Through the production app: the job written over `PUT /jobs/{name}`, fired by the worker's sweep
(`run_due_jobs_all_tenants`) on a moved clock, and watched by the worker's health sweep
(`run_job_health_sweep_all_tenants`), whose alert crosses the destinations ledger and the core
`webhook` kind to a real loopback receiver through the egress guard. The job fails because the
manifest it names does not exist yet; publishing it makes the next firing succeed.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from felix.manifests.loader import parse_manifest
from felix_ai.providers.scripted import ScriptedTurn

from tests.support.webhook_receiver import SECRET, receiver

AGENT = "flaky-agent"
KEYS = {"sk-e2e-ops": {"tenant_id": "default", "sub": "ops", "scopes": ["jobs:write", "jobs:read"]}}
OPS = {"Authorization": "Bearer sk-e2e-ops"}
SWEEP_GAP_MS = 6 * 60_000


def _env(url: str) -> dict[str, str]:
    return {
        "FELIX_AUTH_MODE": "api_key",
        "FELIX_AUTH_API_KEYS": json.dumps(KEYS),
        "FELIX_WEBHOOK_ENDPOINTS": json.dumps(
            # Not open to `default`: an operator destination is the harness posting about a
            # tenant, so the endpoint's own `tenants` (who may name it for run webhooks) is not read.
            {"ops": {"url": url, "secret": SECRET, "tenants": ["ops-only"]}}
        ),
        "FELIX_DESTINATIONS": json.dumps(
            {"ops-alerts": {"kind": "webhook", "endpoint": "ops", "operator": True}}
        ),
        "FELIX_JOB_ALERT_DESTINATION": "ops-alerts",
        "FELIX_JOB_FAILURE_THRESHOLD": "3",
    }


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Every clock the scheduler, the job store, the heartbeat and the watch read; moved by the test."""
    import time

    from felix.destinations import ledger
    from felix.jobs import scheduler, tick, watch
    from felix.jobs import store as jobs_store

    now = [int(time.time() * 1000)]
    for module in (scheduler, jobs_store, tick, watch, ledger):
        monkeypatch.setattr(module, "now_ms", lambda: now[0])
    return now


async def _fire_and_watch(app: Any, clock: list[int]) -> None:
    """One minute's sweep of the scheduler, then the health watch's, then the clock moves on."""
    from felix.jobs.scheduler import run_due_jobs_all_tenants
    from felix.jobs.watch import run_job_health_sweep_all_tenants

    assert await run_due_jobs_all_tenants(app.settings) == 1
    await run_job_health_sweep_all_tenants(app.settings)
    clock[0] += SWEEP_GAP_MS


async def _publish(app: Any, spec: dict[str, Any] | None = None) -> None:
    """A new version of the agent, made the active one -- the operator's edit."""
    from felix.manifests.store import activate_version, put_version

    manifest = parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": AGENT},
            "spec": {"pattern": "react", **(spec or {})},
        }
    )
    row = await put_version(app.settings, "default", AGENT, manifest)
    assert await activate_version(app.settings, "default", AGENT, version=row["version"])


async def _break(app: Any) -> None:
    """A version that cannot compile: it names a destination nobody registered."""
    await _publish(app, {"destinations": {"ids": ["nowhere"]}})


async def _health(app: Any) -> dict[str, Any]:
    answer = await app.client.get("/jobs/flaky", headers=OPS)
    assert answer.status_code == 200, answer.text
    return answer.json()["health"]


async def test_a_failing_job_alerts_once_and_again_only_after_a_success(boot: Any, clock: list[int]) -> None:
    async with receiver([204]) as (url, seen), boot([ScriptedTurn(content="done")], env=_env(url)) as app:
        written = await app.client.put(
            "/jobs/flaky",
            json={"schedule": "every:5m", "manifest_id": AGENT, "payload": {"prompt": "go"}},
            headers=OPS,
        )
        assert written.status_code == 200, written.text

        for _ in range(5):
            await _fire_and_watch(app, clock)
        assert len(seen) == 1, [r["body"] for r in seen]
        assert (await _health(app))["failing"]["alert"] == "posted"

        await _publish(app)
        await _fire_and_watch(app, clock)
        assert await _health(app) == {}
        assert len(seen) == 1

        await _break(app)
        for _ in range(4):
            await _fire_and_watch(app, clock)

    assert len(seen) == 2
    first, second = (json.loads(r["body"]) for r in seen)
    assert first["destination"] == second["destination"] == "ops-alerts"
    assert first["manifest_id"] == "felix:job-health"
    assert first["edition"].startswith("job-health:flaky:failing:")
    assert first["edition"] != second["edition"]
    assert first["title"] == "Felix job failing: `flaky`"
    assert "in tenant `default` has failed its last 3 runs" in first["text"]
    assert seen[0]["headers"]["webhook-id"] != seen[1]["headers"]["webhook-id"]


async def test_the_scheduler_heartbeat_goes_never_then_ok_then_stale(boot: Any, clock: list[int]) -> None:
    from felix.jobs.scheduler import run_due_jobs_all_tenants

    async with boot() as app:
        never = await app.client.get("/health/scheduler")
        await run_due_jobs_all_tenants(app.settings)
        fresh = await app.client.get("/health/scheduler")
        clock[0] += (app.settings.scheduler_stale_seconds + 1) * 1000
        stale = await app.client.get("/health/scheduler")

    assert (never.status_code, never.json()["status"]) == (503, "never")
    assert (fresh.status_code, fresh.json()["status"], fresh.json()["last_tick_age_s"]) == (200, "ok", 0)
    assert (stale.status_code, stale.json()["status"]) == (503, "stale")
