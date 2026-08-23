"""Run due scheduled jobs — optionally invoke the configured manifest."""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from felix.config import Settings
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.jobs import store as jobs_store
from felix.patterns.types import ChatMessage, InvokeInput

logger = logging.getLogger("felix.jobs.scheduler")


def now_ms() -> int:
    return int(time.time() * 1000)


def next_run_at_ms(schedule: str, from_ms: int | None = None) -> int:
    """Compute next fire time from a simple schedule string.

    Supported:
    * empty → +60s
    * integer seconds (``300``)
    * ``every:30s`` / ``every:5m`` / ``@every 5m``
    * cron ``*/N * * * *`` → every N minutes
    * otherwise → +60s
    """
    base = from_ms if from_ms is not None else now_ms()
    s = (schedule or "").strip().lower()
    if not s:
        return base + 60_000
    if s.isdigit():
        return base + max(int(s), 1) * 1000
    m = re.match(r"^(?:@?every[:\s]+)(\d+)\s*([smh])$", s)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        mult = {"s": 1_000, "m": 60_000, "h": 3_600_000}[unit]
        return base + max(n, 1) * mult
    if s.startswith("*/") and " " in s:
        try:
            n = int(s.split()[0][2:])
            return base + max(n, 1) * 60_000
        except ValueError:
            pass
    return base + 60_000


async def _invoke_job_manifest(
    settings: Settings,
    *,
    tenant_id: str,
    job: dict[str, Any],
) -> dict[str, Any]:
    manifest_id = str(job.get("manifest_id") or "")
    if not manifest_id:
        return {"status": "skipped", "reason": "no_manifest"}

    from felix.runtime import build_tenant_agent, resolve_tenant_manifest
    from felix.tools.builtins import default_tool_provider

    payload = job.get("payload") or {}
    prompt = str(payload.get("prompt") or payload.get("message") or "ping")
    provider = default_tool_provider()

    auth = AuthContext(tenant_id=tenant_id, principal_sub="cron", anonymous=False)
    thread = f"{tenant_id}:job:{job['name']}"
    resolved = await resolve_tenant_manifest(settings, tenant_id, manifest_id, thread_id=thread)
    req_ctx = RequestContext(settings=settings, auth=auth, manifest_id=manifest_id, thread_id=thread)
    async with async_run_with_context(req_ctx):
        agent = await build_tenant_agent(
            settings,
            manifest=resolved.manifest,
            tools=provider,
            tenant_id=tenant_id,
        )
        result = await agent.invoke(
            InvokeInput(
                messages=[ChatMessage(role="user", content=prompt)],
                thread_id=thread,
            )
        )
    return {
        "status": "ok",
        "answer": result.final.content if result.final else "",
    }


async def run_due_jobs(settings: Settings, *, tenant_id: str = "default") -> int:
    """Execute enabled jobs whose next_run_at is due. Returns count fired."""
    jobs = await jobs_store.list_jobs(settings, tenant_id)
    fired = 0
    ts = now_ms()
    for job in jobs:
        if not job.get("enabled"):
            continue
        next_run = job.get("next_run_at")
        if next_run is not None and next_run > ts:
            continue
        # Claim the job *before* invoking it. touch_run used to run only after the
        # invocation finished, so the every-minute cron re-fired the same job on every
        # tick until the first run completed.
        try:
            await jobs_store.touch_run(
                settings,
                tenant_id,
                job["name"],
                last_run_at=ts,
                next_run_at=next_run_at_ms(str(job.get("schedule") or ""), ts),
                last_status="running",
            )
        except Exception:
            logger.warning("job_claim_failed name=%s", job.get("name"), exc_info=True)
            continue

        try:
            result: dict[str, Any] = {"status": "ok"}
            if job.get("manifest_id"):
                try:
                    result = await _invoke_job_manifest(settings, tenant_id=tenant_id, job=job)
                except Exception as exc:
                    logger.exception("job_invoke_failed name=%s", job.get("name"))
                    result = {"status": "error", "error": str(exc)}

            status = "ok" if result.get("status") == "ok" else "error"
            await jobs_store.record_run(
                settings,
                tenant_id,
                job["name"],
                status=status,
                started_at=ts,
                finished_at=now_ms(),
                error=str(result.get("error") or ""),
                result=result,
            )
            # Do not write `enabled=True` back from a stale read — that silently
            # re-enabled a job an operator had just disabled.
            await jobs_store.touch_run(
                settings,
                tenant_id,
                job["name"],
                last_run_at=ts,
                next_run_at=next_run_at_ms(str(job.get("schedule") or ""), ts),
                last_status=status,
                last_error=str(result.get("error") or ""),
            )
            fired += 1
        except Exception:
            logger.exception("job_failed name=%s", job.get("name"))
            await jobs_store.record_run(
                settings,
                tenant_id,
                job["name"],
                status="error",
                started_at=ts,
                finished_at=now_ms(),
                error="execution failed",
            )
    return fired


async def run_due_jobs_all_tenants(settings: Settings) -> int:
    """Run due jobs for every tenant that has any. Returns total fired.

    ``run_due_jobs`` defaults to ``tenant_id="default"`` and the worker cron never passed
    one, so no other tenant's scheduled jobs ever fired.
    """
    total = 0
    for tenant_id in await jobs_store.list_tenants_with_jobs(settings):
        try:
            total += await run_due_jobs(settings, tenant_id=tenant_id)
        except Exception:
            # One tenant's bad job must not stop every other tenant's schedule.
            logger.exception("job_sweep_failed tenant=%s", tenant_id)
    return total


__all__ = ["next_run_at_ms", "run_due_jobs", "run_due_jobs_all_tenants"]
