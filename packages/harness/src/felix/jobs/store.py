"""Scheduled job CRUD."""

from __future__ import annotations

import logging
import time
import uuid
from copy import deepcopy
from typing import Any

from sqlalchemy import collate, delete, select

from felix.config import Settings
from felix.db.models import Job, JobRun
from felix.db.session import _use_memory, get_session_factory
from felix.jobs.schedule import stored_zone

logger = logging.getLogger("felix.jobs.store")

now_ms = lambda: int(time.time() * 1000)

_memory_jobs: dict[tuple[str, str], dict[str, Any]] = {}
_memory_runs: dict[tuple[str, str, str], dict[str, Any]] = {}


class BothTargets(ValueError):
    """A job naming a manifest and a workflow: a firing does one or the other, so a row holding
    both would silently drop one."""

    def __init__(self) -> None:
        super().__init__("a job names a manifest or a workflow, not both")


# `payload.scheduled_by`: what marks a job as written by an agent tool (`schedule_task`) or a
# heartbeat enrolment, with the manifest and the conversation it came from. Only those two writers
# set it; `PUT /jobs` drops a caller's and carries the stored one only for the job's own writer.
SCHEDULED_BY = "scheduled_by"


def scheduled_by(job: dict[str, Any], tool: str | None = None) -> dict[str, Any] | None:
    """The job's `scheduled_by` record, or None -- the one reader of it.

    Only a record naming the manifest the job runs counts: a writer re-saving a task or heartbeat
    over `PUT /jobs` with another `manifest_id` keeps the record (it is theirs) but the job is no
    longer the thing it describes. ``tool`` narrows it to one writer (`schedule_task`,
    `heartbeat`); None takes either."""
    by = (job.get("payload") or {}).get(SCHEDULED_BY)
    if not isinstance(by, dict) or by.get("manifest") != job.get("manifest_id"):
        return None
    if tool is not None and by.get("tool") != tool:
        return None
    return by


def reset_jobs_for_tests() -> None:
    """Clear the in-memory jobs and their run history."""
    _memory_jobs.clear()
    _memory_runs.clear()


def _job_dict(row: Job | dict[str, Any]) -> dict[str, Any]:
    if isinstance(row, dict):
        return {
            "tenant_id": row["tenant_id"],
            "name": row["name"],
            "schedule": row.get("schedule", ""),
            "timezone": stored_zone(row.get("timezone")),
            "manifest_id": row.get("manifest_id", ""),
            "workflow_id": row.get("workflow_id", ""),
            "workflow_revision": row.get("workflow_revision", ""),
            "last_run_at": row.get("last_run_at"),
            "next_run_at": row.get("next_run_at"),
            "last_status": row.get("last_status", ""),
            "last_error": row.get("last_error", ""),
            "created_at": row["created_at"],
            # `deepcopy`, because this branch is handing back the object it stored: a caller
            # that edits the payload it read would edit the store. Postgres deserializes a
            # fresh dict per read, so without this the twin is a different kind of object.
            "payload": deepcopy(row.get("payload_json") or row.get("payload") or {}),
            "enabled": row.get("enabled", False),
            "created_by": row.get("created_by"),
            "created_auth": deepcopy(row.get("created_auth")),
            "health": deepcopy(row.get("health_json") or {}),
        }
    return {
        "tenant_id": row.tenant_id,
        "name": row.name,
        "schedule": row.schedule,
        "timezone": stored_zone(row.timezone),
        "manifest_id": row.manifest_id,
        "workflow_id": row.workflow_id,
        "workflow_revision": row.workflow_revision,
        "last_run_at": row.last_run_at,
        "next_run_at": row.next_run_at,
        "last_status": row.last_status,
        "last_error": row.last_error,
        "created_at": row.created_at,
        "payload": row.payload_json,
        "enabled": row.enabled,
        "created_by": row.created_by,
        "created_auth": row.created_auth,
        "health": row.health_json or {},
    }


def _run_dict(row: JobRun | dict[str, Any]) -> dict[str, Any]:
    if isinstance(row, dict):
        return {
            "tenant_id": row["tenant_id"],
            "job_name": row["job_name"],
            "run_id": row["run_id"],
            "started_at": row["started_at"],
            "finished_at": row.get("finished_at"),
            "status": row.get("status", "ok"),
            "error": row.get("error", ""),
            "result": deepcopy(row.get("result_json") or row.get("result") or {}),
        }
    return {
        "tenant_id": row.tenant_id,
        "job_name": row.job_name,
        "run_id": row.run_id,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "status": row.status,
        "error": row.error,
        "result": row.result_json,
    }


async def list_jobs(settings: Settings, tenant_id: str) -> list[dict[str, Any]]:
    if _use_memory(settings):
        rows = [row for (t, _), row in _memory_jobs.items() if t == tenant_id]
        # By name on both arms. Neither ordered at all: the twin returned dict insertion order
        # and Postgres whatever the plan produced, so `GET /jobs` could list an operator's jobs
        # differently on two consecutive calls, and the twin and the store disagreed.
        return [_job_dict(row) for row in sorted(rows, key=lambda r: r["name"])]

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        # `COLLATE "C"` so the two arms agree. `ORDER BY name` uses the database collation,
        # and CI's image inherits en_US.utf8, which sorts `alpha` before `Zeta` and ignores
        # punctuation at the primary level — where Python's code-point sort on the twin puts
        # every capital first. `C` *is* byte order, and UTF-8 byte order is code-point order,
        # so collating to it makes the two identical for all input rather than just ASCII.
        # Job names are unvalidated URL path segments, so mixed case and punctuation are
        # reachable, unlike the `uuid4().hex` ids the same hazard is benign for.
        ordered = select(Job).where(Job.tenant_id == tenant_id).order_by(collate(Job.name, "C"))
        found = (await db.scalars(ordered)).all()
        return [_job_dict(r) for r in found]


async def list_tenants_with_jobs(settings: Settings) -> list[str]:
    """Every tenant that has at least one job.

    The cron sweep previously defaulted to ``tenant_id="default"`` and the worker never
    passed one, so no other tenant's scheduled jobs ever fired.
    """
    if _use_memory(settings):
        return sorted({tid for tid, _ in _memory_jobs})

    from felix.db.session import rls_bypass

    factory = get_session_factory(settings=settings)
    # Cross-tenant maintenance, like retention: without a bypass this runs with no
    # app.tenant_id GUC and RLS returns nothing.
    with rls_bypass():
        async with factory() as db:
            rows = (await db.execute(select(Job.tenant_id).distinct())).scalars().all()
            return sorted({str(r) for r in rows})


async def get_job(settings: Settings, tenant_id: str, name: str) -> dict[str, Any] | None:
    if _use_memory(settings):
        row = _memory_jobs.get((tenant_id, name))
        return _job_dict(row) if row else None

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        row = await db.get(Job, (tenant_id, name))
        return _job_dict(row) if row else None


async def put_job(
    settings: Settings,
    tenant_id: str,
    name: str,
    *,
    schedule: str = "",
    timezone: str | None = None,
    manifest_id: str = "",
    workflow_id: str = "",
    workflow_revision: str = "",
    payload: dict[str, Any] | None = None,
    enabled: bool = True,
    created_by: str | None = None,
    created_auth: dict[str, Any] | None = None,
    first_run_at: int | None = None,
) -> dict[str, Any]:
    """Create or replace a job. Raises `ScheduleError` when `schedule` is outside the grammar, or
    `timezone` is not a zone or is given with a schedule it cannot move (`parse_schedule`), and
    `BothTargets` for a job naming a manifest and a workflow.

    `timezone` is the IANA zone the job's cron is read in; None is UTC. Replaced like every other
    field: a rewrite that leaves it out puts the job back on UTC.

    `created_by` and `created_auth` are whoever wrote it this time and the authority its runs
    carry (`felix.jobs.scheduler.creator_auth`) -- replaced on every write, because whoever
    rewrites the prompt is who the job now runs for. None records no one: such a job runs with no
    scopes, as every job did before they were recorded.

    A one-shot schedule (`at:`) is due at its moment from the write that sets it, and refused if
    that moment has passed. Rewriting a job with the one-shot schedule it already has keeps its
    due time -- None once it has fired, so a rewrite never fires it again.

    `first_run_at` is when a recurring job this write *creates* is first due, for a writer that
    wants it to wait for its schedule (`jobs/agent_tools.py`); by default a new job is due on the
    next sweep. A rewrite ignores it, and so does a one-shot, which is due at its own moment.
    """
    from felix.jobs.schedule import ScheduleError, parse_schedule

    if manifest_id and workflow_id:
        raise BothTargets()
    timezone = stored_zone(timezone)
    parsed = parse_schedule(schedule, timezone)
    ts = now_ms()

    def _due_on_new_schedule() -> int | None:
        """The due time of a job created, or given a new schedule, by this write: a one-shot's moment,
        else `first_run_at` -- None unless asked, so a new recurring job is due on the next sweep."""
        if parsed.at_ms is None:
            return first_run_at
        if parsed.at_ms <= ts:
            raise ScheduleError("schedule: at: is in the past")
        return parsed.at_ms

    def _next_run_at(existing: tuple[str, str | None] | None, existing_next: int | None) -> int | None:
        # A new zone is a new schedule: `0 8 * * *` in New York and in London are different slots.
        same = existing == (schedule, timezone)
        if parsed.at_ms is not None:
            return existing_next if same else _due_on_new_schedule()
        # A changed schedule fires at whichever comes first: the due time already held, or
        # the new schedule's next slot. Keeping only the old one left a job moved from
        # `@daily` to `*/5` waiting until tomorrow; taking only the new one would hide a run
        # that is already overdue (`test_republishing_a_job_keeps_its_run_state`).
        if existing is None or same or existing_next is None:
            return existing_next
        following = parsed.next_after(ts)
        # Made manual only: it holds no due time, so nothing reads one as overdue.
        return None if following is None else min(existing_next, following)

    if _use_memory(settings):
        existing = _memory_jobs.get((tenant_id, name))
        row = {
            "tenant_id": tenant_id,
            "name": name,
            "schedule": schedule,
            "timezone": timezone,
            "manifest_id": manifest_id,
            "workflow_id": workflow_id,
            "workflow_revision": workflow_revision,
            "last_run_at": existing.get("last_run_at") if existing else None,
            "next_run_at": _next_run_at(
                (existing["schedule"], existing.get("timezone")), existing.get("next_run_at")
            )
            if existing
            else _due_on_new_schedule(),
            "last_status": _status_after_write(existing.get("last_status", ""), enabled) if existing else "",
            "last_error": (
                _error_after_write(existing.get("last_status", ""), existing.get("last_error", ""), enabled)
                if existing
                else ""
            ),
            "created_at": existing["created_at"] if existing else ts,
            "payload_json": deepcopy(payload) if payload else {},
            "enabled": enabled,
            "created_by": created_by,
            "created_auth": deepcopy(created_auth),
            # The watch's, never the writer's: a re-save leaves an open incident open (`set_health`).
            "health_json": deepcopy(existing.get("health_json") or {}) if existing else {},
        }
        _memory_jobs[(tenant_id, name)] = row
        return _job_dict(row)

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        row = await db.get(Job, (tenant_id, name))
        if row is None:
            row = Job(
                tenant_id=tenant_id,
                name=name,
                schedule=schedule,
                timezone=timezone,
                next_run_at=_due_on_new_schedule(),
                manifest_id=manifest_id,
                workflow_id=workflow_id,
                workflow_revision=workflow_revision,
                created_at=ts,
                payload_json=deepcopy(payload) if payload else {},
                enabled=enabled,
                created_by=created_by,
                created_auth=deepcopy(created_auth),
            )
            db.add(row)
        else:
            row.next_run_at = _next_run_at((row.schedule, row.timezone), row.next_run_at)
            row.schedule = schedule
            row.timezone = timezone
            row.manifest_id = manifest_id
            row.workflow_id = workflow_id
            row.workflow_revision = workflow_revision
            row.payload_json = deepcopy(payload) if payload else {}
            row.last_error = _error_after_write(row.last_status, row.last_error, enabled)
            row.last_status = _status_after_write(row.last_status, enabled)
            row.enabled = enabled
            row.created_by = created_by
            row.created_auth = deepcopy(created_auth)
        await db.commit()
        return _job_dict(row)


# Disabling a job is the operator's kill switch (`PUT /jobs/{name}` with `enabled: false`), and it
# clears a `running` mark: a worker that died mid-run leaves one that would otherwise hold the job
# for `RUNNING_STALE_MS` with no way round it. It does not stop a run still going -- nothing here
# can -- and once the job is re-enabled the next sweep may fire it beside such a run.
DISABLED_WHILE_RUNNING = "disabled while marked running"


def _status_after_write(status: str, enabled: bool) -> str:
    return "error" if not enabled and status == RUNNING else status


def _error_after_write(status: str, error: str, enabled: bool) -> str:
    return DISABLED_WHILE_RUNNING if not enabled and status == RUNNING else error


# `touch_run(next_run_at=KEEP_SCHEDULE)`: record a run without moving the schedule. A manual run
# must not, and passing the value it read would put back a stale one if cron moved it meanwhile.
KEEP_SCHEDULE: Any = object()


async def touch_run(
    settings: Settings,
    tenant_id: str,
    name: str,
    *,
    last_run_at: int | None,
    next_run_at: int | None = None,
    last_status: str = "ok",
    last_error: str = "",
) -> None:
    keep = next_run_at is KEEP_SCHEDULE
    if _use_memory(settings):
        row = _memory_jobs.get((tenant_id, name))
        if row is None:
            return
        row["last_run_at"] = last_run_at
        if not keep:
            row["next_run_at"] = next_run_at
        row["last_status"] = last_status
        row["last_error"] = last_error
        return

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        row = await db.get(Job, (tenant_id, name))
        if row is None:
            return
        row.last_run_at = last_run_at
        if not keep:
            row.next_run_at = next_run_at
        row.last_status = last_status
        row.last_error = last_error
        await db.commit()


# How long a run marked `running` holds its job against another claim. Past it the mark is taken
# for a run that died with its worker -- a crash, an OOM kill, a cancelled task -- and the job is
# claimable again: a mark nothing clears must not stop a job for good. Six hours is three times
# the longest a run can legitimately take: a run's own `max_wall_clock_seconds` is at most an
# hour, and the call it starts last may wait out an approval ttl of at most another hour
# (`manifests/schema.py`, both `MAX_INTEGRATION_TIMEOUT_S`). A fixed bound rather than one per
# schedule: a long interval does not make a run longer, and a short one must not let a slow run
# be fired over. `tests/unit/test_job_running_claim.py` holds it above those two bounds.
RUNNING = "running"
RUNNING_STALE_MS = 6 * 60 * 60 * 1000


def _free(row: dict[str, Any], at: int) -> bool:
    """Whether a twin row may be claimed at `at`: no run marked, or the mark gone stale."""
    if row.get("last_status") != RUNNING:
        return True
    started = row.get("last_run_at")
    return started is None or started < at - RUNNING_STALE_MS


def _free_clause(at: int) -> Any:
    """`_free` as a WHERE clause, so the claim stays one UPDATE that the row lock decides."""
    from sqlalchemy import or_

    return or_(
        Job.last_status.is_distinct_from(RUNNING),
        Job.last_run_at.is_(None),
        Job.last_run_at < at - RUNNING_STALE_MS,
    )


async def claim_run(
    settings: Settings,
    tenant_id: str,
    name: str,
    *,
    seen_next_run_at: int | None,
    last_run_at: int,
    next_run_at: int | None,
) -> bool:
    """Mark a due job running, only if its `next_run_at` is still the one this tick read and no
    run of it is in flight.

    `touch_run` writes unconditionally, so two scheduler ticks that read the same due job --
    two workers, or a slow tick overlapping the next minute's -- both claimed it and both fired
    it. Compare-and-set on `next_run_at`: the first claim moves it, and every later claim for
    the same due time finds it moved and gets False. A single `UPDATE ... WHERE` on Postgres, so
    the row lock decides between concurrent claims.

    And not while `last_status` is `running` (unless that mark is older than `RUNNING_STALE_MS`):
    the claim moved `next_run_at` to the next slot, so a run outlasting its interval found that
    slot due on a later tick and was fired a second time beside itself. Refused, the due time is
    left where it is, and the first tick after the run ends fires it once.
    """
    if _use_memory(settings):
        row = _memory_jobs.get((tenant_id, name))
        if row is None or row.get("next_run_at") != seen_next_run_at or not _free(row, last_run_at):
            return False
        row.update(last_run_at=last_run_at, next_run_at=next_run_at, last_status=RUNNING, last_error="")
        return True

    from sqlalchemy import update

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        result = await db.execute(
            update(Job)
            .where(
                Job.tenant_id == tenant_id,
                Job.name == name,
                Job.next_run_at.is_not_distinct_from(seen_next_run_at),
                _free_clause(last_run_at),
            )
            .values(last_run_at=last_run_at, next_run_at=next_run_at, last_status=RUNNING, last_error="")
        )
        await db.commit()
        return bool(getattr(result, "rowcount", 0))


async def claim_manual_run(settings: Settings, tenant_id: str, name: str, *, last_run_at: int) -> bool:
    """Mark a job running for a run someone asked for (`POST /jobs/{name}/run`), only if no run of
    it is in flight. The schedule is left where it is, as a manual run leaves it.

    The same mark `claim_run` writes and reads, so a sweep does not fire a job over a manual run
    and a manual run is not started over a scheduled one: both share the job's thread, and two
    runs on one thread interleave one transcript. False when the job is gone or running.
    """
    if _use_memory(settings):
        row = _memory_jobs.get((tenant_id, name))
        if row is None or not _free(row, last_run_at):
            return False
        row.update(last_run_at=last_run_at, last_status=RUNNING, last_error="")
        return True

    from sqlalchemy import update

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        result = await db.execute(
            update(Job)
            .where(Job.tenant_id == tenant_id, Job.name == name, _free_clause(last_run_at))
            .values(last_run_at=last_run_at, last_status=RUNNING, last_error="")
        )
        await db.commit()
        return bool(getattr(result, "rowcount", 0))


async def finish_run(
    settings: Settings, tenant_id: str, name: str, *, started_at: int, last_status: str, last_error: str = ""
) -> bool:
    """Record how a run ended -- only if the job still holds *this* run's claim.

    A run that outlived `RUNNING_STALE_MS` may have been reclaimed by another, whose claim wrote its
    own `last_run_at`. An unconditional write from the first run's end then erased the second's
    `running` mark, and the next tick fired the job beside it. Compare-and-set on `last_run_at`,
    which each claim sets to its run's `started_at`: one `UPDATE ... WHERE` on Postgres. False when
    the job is gone or another run has claimed it since; the schedule is never touched.
    """
    if _use_memory(settings):
        row = _memory_jobs.get((tenant_id, name))
        if row is None or row.get("last_run_at") != started_at:
            return False
        row.update(last_status=last_status, last_error=last_error)
        return True

    from sqlalchemy import update

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        result = await db.execute(
            update(Job)
            .where(Job.tenant_id == tenant_id, Job.name == name, Job.last_run_at == started_at)
            .values(last_status=last_status, last_error=last_error)
        )
        await db.commit()
        return bool(getattr(result, "rowcount", 0))


async def set_schedule(
    settings: Settings,
    tenant_id: str,
    name: str,
    *,
    schedule: str,
    timezone: str | None,
    next_run_at: int | None,
    expected_schedule: str,
    expected_timezone: str | None,
) -> bool:
    """Move an existing job to `schedule` in `timezone`, due at `next_run_at` -- only if it still
    holds `expected_schedule` in `expected_timezone`. Nothing else about it changes: not `enabled`,
    not its writer.

    One `UPDATE ... WHERE`, never an upsert: a firing refreshing its own schedule must not
    re-create a job deleted meanwhile, nor overwrite one someone re-saved since it was read.
    False when the job is gone or its schedule moved. The caller validates `schedule`."""
    timezone, expected_timezone = stored_zone(timezone), stored_zone(expected_timezone)
    if _use_memory(settings):
        row = _memory_jobs.get((tenant_id, name))
        held = (row.get("schedule"), row.get("timezone")) if row is not None else None
        if row is None or held != (expected_schedule, expected_timezone):
            return False
        row.update(schedule=schedule, timezone=timezone, next_run_at=next_run_at)
        return True

    from sqlalchemy import update

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        result = await db.execute(
            update(Job)
            .where(
                Job.tenant_id == tenant_id,
                Job.name == name,
                Job.schedule == expected_schedule,
                Job.timezone.is_not_distinct_from(expected_timezone),
            )
            .values(schedule=schedule, timezone=timezone, next_run_at=next_run_at)
        )
        await db.commit()
        return bool(getattr(result, "rowcount", 0))


async def disable_job(settings: Settings, tenant_id: str, name: str) -> None:
    """Stop a job being swept, leaving everything else about it -- its writer, its history -- as
    it was. A no-op for a job that is gone."""
    if _use_memory(settings):
        row = _memory_jobs.get((tenant_id, name))
        if row is not None:
            row["enabled"] = False
        return

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        row = await db.get(Job, (tenant_id, name))
        if row is None:
            return
        row.enabled = False
        await db.commit()


async def set_health(
    settings: Settings, tenant_id: str, name: str, *, seen: dict[str, Any], health: dict[str, Any]
) -> bool:
    """Write the job's open health incidents (`felix.jobs.watch`) -- only if they are still `seen`,
    what the caller read. One `UPDATE ... WHERE` on Postgres, comparing the JSONB whole, so of two
    overlapping sweeps that read the same incidents only one records a new one, and so only one
    counts, audits and alerts it. False when the job is gone or its incidents moved."""
    if _use_memory(settings):
        row = _memory_jobs.get((tenant_id, name))
        if row is None or (row.get("health_json") or {}) != seen:
            return False
        row["health_json"] = deepcopy(health)
        return True

    from sqlalchemy import cast, update
    from sqlalchemy.dialects.postgresql import JSONB

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        result = await db.execute(
            update(Job)
            .where(Job.tenant_id == tenant_id, Job.name == name, Job.health_json == cast(seen, JSONB))
            .values(health_json=deepcopy(health))
        )
        await db.commit()
        return bool(getattr(result, "rowcount", 0))


async def delete_job(settings: Settings, tenant_id: str, name: str) -> bool:
    if _use_memory(settings):
        deleted = _memory_jobs.pop((tenant_id, name), None) is not None
        if deleted:
            for key in list(_memory_runs):
                if key[0] == tenant_id and key[1] == name:
                    _memory_runs.pop(key, None)
        return deleted

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        await db.execute(delete(JobRun).where(JobRun.tenant_id == tenant_id, JobRun.job_name == name))
        result = await db.execute(delete(Job).where(Job.tenant_id == tenant_id, Job.name == name))
        await db.commit()
        return result.rowcount > 0


async def list_runs(
    settings: Settings, tenant_id: str, name: str, *, limit: int = 20
) -> list[dict[str, Any]]:
    # Clamped here as well as at the route, so direct callers get the same answer from both
    # arms. The route rejects a negative `limit` now, but before it did, one sliced the twin's
    # list from the end (a 200 with the wrong rows) while Postgres refused it outright (a
    # 500) — the same request, two answers, depending only on which backend was configured.
    limit = max(0, limit)
    if _use_memory(settings):
        items = [_run_dict(row) for (t, j, _), row in _memory_runs.items() if t == tenant_id and j == name]
        # `run_id` breaks the tie, and it is the third part of the primary key, so the order
        # is total. It is also always `uuid4().hex` from `record_run` — lowercase hex, which
        # sorts identically under every collation — so unlike `Job.name` above, the two arms
        # agree here without a `COLLATE` — under every *common* collation, at least; an ICU
        # one with `numeric=true` would compare digit runs numerically. The cost of that is
        # which of two runs sharing a millisecond comes first, so it does not earn one.
        #
        # `started_at` is milliseconds and a sweep records a burst of runs, so ties
        # are ordinary — and with none, "the most recent two of five" was whichever two the
        # backend happened to return. Python's sort is stable, so on this arm it was the two
        # *oldest*: the operator reading a job's recent history got its first attempts.
        items.sort(key=lambda r: (r["started_at"], r["run_id"]), reverse=True)
        return items[:limit]

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        rows = (
            await db.scalars(
                select(JobRun)
                .where(JobRun.tenant_id == tenant_id, JobRun.job_name == name)
                .order_by(JobRun.started_at.desc(), JobRun.run_id.desc())
                .limit(limit)
            )
        ).all()
        return [_run_dict(r) for r in rows]


# A run's `status`: `ok`, `error`, or `skipped` -- a firing that ran nothing because a limit held it
# (a heartbeat over `max_firings_per_day`). A skipped run is a record that a firing was due, not a
# run: it never counts toward that limit (`count_runs_since`), and trimming keeps none of it back.
SKIPPED = "skipped"
DAY_MS = 86_400_000
# Rows one trim deletes at most: a history from before trimming existed is cut down over a few
# runs, never in one statement holding the job's rows.
TRIM_BATCH = 1000


def utc_day_start(at_ms: int) -> int:
    """The start of the UTC day holding `at_ms`. Unix time has no leap seconds, so a day is exact."""
    return at_ms - at_ms % DAY_MS


async def count_runs_since(settings: Settings, tenant_id: str, name: str, since_ms: int) -> int:
    """How many runs of the job started at or after `since_ms` and ran -- every status but
    `SKIPPED`. One indexed count: `idx_job_runs_history` is `(tenant_id, job_name, started_at DESC,
    run_id DESC)`, so the range on `started_at` is read from the index; `status` is checked per row."""
    if _use_memory(settings):
        return sum(
            1
            for (t, j, _), row in _memory_runs.items()
            if t == tenant_id and j == name and row["started_at"] >= since_ms and row.get("status") != SKIPPED
        )

    from sqlalchemy import func

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        counted = await db.scalar(
            select(func.count())
            .select_from(JobRun)
            .where(
                JobRun.tenant_id == tenant_id,
                JobRun.job_name == name,
                JobRun.started_at >= since_ms,
                JobRun.status != SKIPPED,
            )
        )
        return int(counted or 0)


async def trim_runs(settings: Settings, tenant_id: str, name: str, *, keep: int, protect_since: int) -> int:
    """Delete the job's runs past its newest `keep` -- newest by `(started_at, run_id)`, the order
    `list_runs` reads -- except any that ran at or after `protect_since` (a run that is not
    `SKIPPED`): those are what a heartbeat's daily cap counts, so trimming them would let it fire
    again. At most `TRIM_BATCH`, oldest first. `keep` 0 or less trims nothing. How many went."""
    if keep <= 0:
        return 0

    def _doomed(row: dict[str, Any]) -> bool:
        return row["started_at"] < protect_since or row.get("status") == SKIPPED

    if _use_memory(settings):
        rows = [row for (t, j, _), row in _memory_runs.items() if t == tenant_id and j == name]
        rows.sort(key=lambda r: (r["started_at"], r["run_id"]), reverse=True)
        # Past the newest `keep`, oldest first, as the Postgres arm takes its batch.
        doomed = [row for row in rows[keep:] if _doomed(row)]
        doomed.sort(key=lambda r: (r["started_at"], r["run_id"]))
        gone = doomed[:TRIM_BATCH]
        for row in gone:
            _memory_runs.pop((row["tenant_id"], row["job_name"], row["run_id"]), None)
        return len(gone)

    from sqlalchemy import or_, tuple_

    mine = (JobRun.tenant_id == tenant_id, JobRun.job_name == name)
    factory = get_session_factory(settings=settings)
    async with factory() as db:
        # The oldest run still kept; everything strictly older than it is past the newest `keep`.
        last_kept = (
            await db.execute(
                select(JobRun.started_at, JobRun.run_id)
                .where(*mine)
                .order_by(JobRun.started_at.desc(), JobRun.run_id.desc())
                .offset(keep - 1)
                .limit(1)
            )
        ).first()
        if last_kept is None:
            return 0
        doomed = (
            select(JobRun.run_id)
            .where(
                *mine,
                tuple_(JobRun.started_at, JobRun.run_id) < tuple_(last_kept[0], last_kept[1]),
                or_(JobRun.started_at < protect_since, JobRun.status == SKIPPED),
            )
            .order_by(JobRun.started_at, JobRun.run_id)
            .limit(TRIM_BATCH)
        )
        result = await db.execute(delete(JobRun).where(*mine, JobRun.run_id.in_(doomed)))
        await db.commit()
        return int(getattr(result, "rowcount", 0) or 0)


async def record_run(
    settings: Settings,
    tenant_id: str,
    name: str,
    *,
    status: str = "ok",
    error: str = "",
    result: dict[str, Any] | None = None,
    started_at: int | None = None,
    finished_at: int | None = None,
) -> dict[str, Any]:
    """Persist a job run row (helper for scheduler), then trim the job's history to
    `FELIX_JOB_RUNS_KEEP` (`trim_runs`) -- every job's, not only a heartbeat's."""
    run = await _insert_run(
        settings,
        tenant_id,
        name,
        status=status,
        error=error,
        result=result,
        started_at=started_at,
        finished_at=finished_at,
    )
    try:
        await trim_runs(
            settings, tenant_id, name, keep=settings.job_runs_keep, protect_since=utc_day_start(now_ms())
        )
    except Exception:
        # The run is recorded; a history left one row long until the next run trims it is not a
        # reason to lose that.
        logger.warning("job_runs_trim_failed", exc_info=True)
    return run


async def _insert_run(
    settings: Settings,
    tenant_id: str,
    name: str,
    *,
    status: str,
    error: str,
    result: dict[str, Any] | None,
    started_at: int | None,
    finished_at: int | None,
) -> dict[str, Any]:
    run_id = uuid.uuid4().hex
    started = started_at or now_ms()
    finished = finished_at or now_ms()

    if _use_memory(settings):
        row = {
            "tenant_id": tenant_id,
            "job_name": name,
            "run_id": run_id,
            "started_at": started,
            "finished_at": finished,
            "status": status,
            "error": error,
            "result_json": deepcopy(result) if result else {},
        }
        _memory_runs[(tenant_id, name, run_id)] = row
        return _run_dict(row)

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        row = JobRun(
            tenant_id=tenant_id,
            job_name=name,
            run_id=run_id,
            started_at=started,
            finished_at=finished,
            status=status,
            error=error,
            result_json=deepcopy(result) if result else {},
        )
        db.add(row)
        await db.commit()
        return _run_dict(row)


__all__ = [
    "RUNNING",
    "RUNNING_STALE_MS",
    "SCHEDULED_BY",
    "SKIPPED",
    "BothTargets",
    "claim_manual_run",
    "claim_run",
    "count_runs_since",
    "delete_job",
    "disable_job",
    "finish_run",
    "get_job",
    "list_jobs",
    "list_runs",
    "put_job",
    "record_run",
    "reset_jobs_for_tests",
    "scheduled_by",
    "set_health",
    "set_schedule",
    "touch_run",
    "trim_runs",
    "utc_day_start",
]
