"""Run due scheduled jobs — optionally invoke the configured manifest."""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from felix.config import Settings
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.jobs import store as jobs_store
from felix.jobs.schedule import ScheduleError, parse_schedule
from felix.logging_setup import loggable
from felix.patterns.types import ChatMessage, InvokeInput

logger = logging.getLogger("felix.jobs.scheduler")


def now_ms() -> int:
    return int(time.time() * 1000)


def next_run_at_ms(schedule: str, from_ms: int | None = None) -> int:
    """When `schedule` next fires after `from_ms` (default: now). The grammar is `felix.jobs.schedule`.

    Raises `ScheduleError` for a schedule outside it -- never a guessed interval.
    """
    base = from_ms if from_ms is not None else now_ms()
    return parse_schedule(schedule).next_after(base)


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
    # One thread per job by default, so a digest job keeps its own history. `fresh_thread`
    # gives each firing a thread of its own: a job that works a *different* ticket every run
    # must not carry ticket N's transcript into ticket N+1's context.
    thread = f"{tenant_id}:job:{job['name']}"
    if payload.get("fresh_thread"):
        # A uuid, not the clock: two firings in one millisecond — a sweep that claims twice,
        # a test — would share a thread again, which is the one thing this exists to prevent.
        thread = f"{thread}:{uuid.uuid4().hex[:16]}"
    resolved = await resolve_tenant_manifest(settings, tenant_id, manifest_id, thread_id=thread)
    req_ctx = RequestContext(settings=settings, auth=auth, manifest_id=manifest_id, thread_id=thread)
    async with async_run_with_context(req_ctx):
        # The prompt is job payload, writable with `jobs:write`. The compiled agent screens
        # it like any user turn; a refusal raises and is recorded as an error run.
        messages = [ChatMessage(role="user", content=prompt)]
        agent = await build_tenant_agent(
            settings,
            manifest=resolved.manifest,
            sub_agents=resolved.sub_agents,
            tools=provider,
            tenant_id=tenant_id,
            # A job runs as `cron`, for no one: the tenant's library only.
            skill_owner=None,
        )
        result = await agent.invoke(InvokeInput(messages=messages, thread_id=thread))
    return {
        "status": "ok",
        "answer": result.final.content if result.final else "",
    }


async def fire_job(
    settings: Settings,
    tenant_id: str,
    job: dict[str, Any],
    *,
    started_at: int,
    trigger: str = "schedule",
    requested_by: str = "",
) -> dict[str, Any]:
    """Run one job now and record it. Returns the run row.

    The one path a job runs by, whether cron found it due or someone asked for it: the same
    identity (`cron`, with no scopes — a manual run does not borrow the caller's), the same
    thread, the same screening of its prompt, the same run record. A manual run records its
    `trigger` and who asked, and leaves the schedule where it was.
    """
    try:
        result: dict[str, Any] = {"status": "ok"}
        if job.get("manifest_id"):
            try:
                result = await _invoke_job_manifest(settings, tenant_id=tenant_id, job=job)
            except Exception as exc:
                logger.exception("job_invoke_failed name=%s", job.get("name"))
                result = {"status": "error", "error": str(exc)}
        result["trigger"] = trigger
        if requested_by:
            result["requested_by"] = requested_by

        status = "ok" if result.get("status") == "ok" else "error"
        run = await jobs_store.record_run(
            settings,
            tenant_id,
            job["name"],
            status=status,
            started_at=started_at,
            finished_at=now_ms(),
            error=str(result.get("error") or ""),
            result=result,
        )
        # Do not write `enabled=True` back from a stale read — that silently
        # re-enabled a job an operator had just disabled. And never the schedule: a manual run
        # leaves it where it was, and a scheduled one was advanced by its claim (`claim_run`).
        # Writing it again here moved it *back* when a run outlasted its interval -- the next
        # tick had already claimed the next slot, and this rewrote that slot as due.
        await jobs_store.touch_run(
            settings,
            tenant_id,
            job["name"],
            last_run_at=started_at,
            next_run_at=jobs_store.KEEP_SCHEDULE,
            last_status=status,
            last_error=str(result.get("error") or ""),
        )
        return run
    except Exception:
        logger.exception("job_failed name=%s", job.get("name"))
        return await jobs_store.record_run(
            settings,
            tenant_id,
            job["name"],
            status="error",
            started_at=started_at,
            finished_at=now_ms(),
            error="execution failed",
            result={"trigger": trigger},
        )


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
        try:
            following = next_run_at_ms(str(job.get("schedule") or ""), ts)
        except ScheduleError as exc:
            # Stored before the schedule was validated on write. It used to fire every minute
            # whatever it said; now it does not fire, and says why where the operator looks.
            if job.get("last_error") != str(exc):
                logger.warning("job_schedule_invalid name=%s", loggable(str(job.get("name")), limit=200))
                await jobs_store.touch_run(
                    settings,
                    tenant_id,
                    job["name"],
                    last_run_at=job.get("last_run_at"),
                    next_run_at=jobs_store.KEEP_SCHEDULE,
                    last_status="error",
                    last_error=str(exc),
                )
            continue
        # Claim the job *before* invoking it. touch_run used to run only after the
        # invocation finished, so the every-minute cron re-fired the same job on every
        # tick until the first run completed. And the claim is conditional on the due time
        # this tick read, so a second worker -- or this worker's next tick, overlapping a
        # slow one -- that read the same due job does not fire it again.
        try:
            claimed = await jobs_store.claim_run(
                settings,
                tenant_id,
                job["name"],
                seen_next_run_at=next_run,
                last_run_at=ts,
                next_run_at=following,
            )
        except Exception:
            logger.warning("job_claim_failed name=%s", job.get("name"), exc_info=True)
            continue
        if not claimed:
            logger.info("job_already_claimed name=%s", job.get("name"))
            continue

        await fire_job(settings, tenant_id, job, started_at=ts)
        fired += 1
    return fired


async def run_due_jobs_all_tenants(settings: Settings) -> int:
    """Run due jobs for every tenant that has any. Returns total fired.

    ``run_due_jobs`` defaults to ``tenant_id="default"`` and the worker cron never passed
    one, so no other tenant's scheduled jobs ever fired.
    """
    from felix.db.session import rls_tenant

    total = 0
    for tenant_id in await jobs_store.list_tenants_with_jobs(settings):
        try:
            # Bind the tenant for the sweep. The worker has no request context, so nothing
            # else supplies `app.tenant_id`: under `FELIX_DATABASE_RLS` the policy then has no
            # tenant to match, filters every row, and this sweep reads an empty table and
            # reports success. Silent, and only on deployments where RLS is the isolation
            # mechanism -- the bundled compose role is superuser and skips the policy entirely.
            with rls_tenant(tenant_id):
                total += await run_due_jobs(settings, tenant_id=tenant_id)
        except Exception:
            # One tenant's bad job must not stop every other tenant's schedule.
            logger.exception("job_sweep_failed tenant=%s", loggable(tenant_id, limit=64))
    return total


__all__ = ["fire_job", "next_run_at_ms", "run_due_jobs", "run_due_jobs_all_tenants"]
