"""The scheduler's heartbeat: when its last sweep finished, for something outside it to watch.

`felix-scheduler` stopping is silent from the inside. Nothing periodic fires -- including anything
that would notice -- and every job's `last_status` keeps reading whatever it last was. So each
completed `run_due_jobs_all_tenants` sweep stamps the time here (`record_tick`), and
`GET /health/scheduler` answers from the stamp (`scheduler_health`): `ok` while it is fresher than
`FELIX_SCHEDULER_STALE_SECONDS`, `stale` past it, `never` when there is none. A sweep is the
worker running a task the scheduler enqueued, so a stale stamp means either one has stopped -- or
the database the sweep reads, since a sweep that cannot list its tenants does not complete.

Kept in Redis: it is what crosses from the worker that writes the stamp to the API that reads it,
the way steer marks and workspace-note marks cross (`felix.redis_conn`). The worker cannot run
without it -- its broker is Redis -- so wherever a sweep can run, the stamp can be written. Each
process also keeps its own copy, the in-process fallback every Redis-backed mark here has: what a
single-process deployment, and every test under `memory://`, reads. A Redis the API cannot reach
reads as `unknown`, and the route answers 503 -- an alarm for an outage, never an `ok` for one.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Literal

from felix.redis_conn import RedisConnection

logger = logging.getLogger("felix.jobs.tick")

now_ms = lambda: int(time.time() * 1000)

# Not tenant-scoped: there is one scheduler per deployment.
TICK_KEY = "felix:scheduler:last_tick_ms"

_conn = RedisConnection(
    "scheduler tick",
    fallback_consequence="GET /health/scheduler on another process cannot see the scheduler's sweeps",
)
_local: dict[str, int] = {}

SchedulerStatus = Literal["ok", "stale", "never", "unknown"]


def reset_tick_for_tests() -> None:
    _local.clear()


async def record_tick(at_ms: int | None = None) -> None:
    """Stamp a completed sweep. Never raises: a stamp that cannot be written is the watch's alarm."""
    at = now_ms() if at_ms is None else at_ms
    # Never moved backwards, so a slow sweep finishing after a later one does not age the stamp.
    _local[TICK_KEY] = max(at, _local.get(TICK_KEY, 0))
    client = await _conn.get()
    if client is None:
        return
    try:
        # A Lua-free max: the stamp is written once a minute, so a lost race costs one minute's age.
        held = int(await client.get(TICK_KEY) or 0)
        if at > held:
            await client.set(TICK_KEY, str(at))
    except Exception:
        await _conn.fallback("scheduler tick write")


async def read_tick(settings: Any = None) -> tuple[int | None, bool]:
    """`(stamp, unreadable)`: when the last completed sweep finished, in epoch ms, or None if none
    was recorded; and whether a Redis this deployment uses could not be read -- then a missing
    stamp means "cannot tell", not "never". `settings` decides whether Redis is in use (default
    the process's own)."""
    from felix.config import get_settings, redis_url_in_use

    seen = _local.get(TICK_KEY)
    if not redis_url_in_use(settings if settings is not None else get_settings()):
        return seen, False
    client = await _conn.get()
    if client is None:
        return seen, True
    try:
        raw = await client.get(TICK_KEY)
    except Exception:
        await _conn.fallback("scheduler tick read")
        return seen, True
    if raw is not None:
        seen = max(int(raw), seen or 0)
    return seen, False


async def last_tick(settings: Any = None) -> int | None:
    """When the last completed sweep finished, in epoch ms, or None if none was recorded or read."""
    return (await read_tick(settings))[0]


@dataclass(frozen=True, slots=True)
class SchedulerHealth:
    status: SchedulerStatus
    last_tick_age_s: int | None
    stale_after_s: int

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "last_tick_age_s": self.last_tick_age_s,
            "stale_after_s": self.stale_after_s,
        }


async def scheduler_health(settings: Any) -> SchedulerHealth:
    """`ok`, `stale`, `never` -- or `unknown` when Redis could not be read and this process holds
    no stamp of its own -- and how old the last sweep is in whole seconds."""
    stale_after = int(settings.scheduler_stale_seconds)
    at, unreadable = await read_tick(settings)
    if at is None:
        return SchedulerHealth("unknown" if unreadable else "never", None, stale_after)
    # A stamp from a clock ahead of this one reads as zero, not negative.
    age = max(0, (now_ms() - at) // 1000)
    return SchedulerHealth("ok" if age <= stale_after else "stale", age, stale_after)


__all__ = [
    "TICK_KEY",
    "SchedulerHealth",
    "SchedulerStatus",
    "last_tick",
    "read_tick",
    "record_tick",
    "reset_tick_for_tests",
    "scheduler_health",
]
