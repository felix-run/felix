"""The worker's heartbeat: that a `felix-worker` finished starting and is still running, and which
version it is.

0.19.0 rolled green with a worker that never started: its startup check refused the config, while
`/health` and `/ready` -- which only the API answers -- kept saying ok, and the first thing to
notice was a durable run that stayed pending. So once the worker's startup has passed everything
that can refuse it, it stamps the time and its version here every `BEAT_INTERVAL_S` (`record_beat`),
and `GET /health/worker` answers from the stamp (`worker_health`): `ok` while it is fresher than
`FELIX_WORKER_STALE_SECONDS`, `stale` past it, `never` when there is none, `unknown` when Redis
could not be read.

Each beat also carries `boot`, a random id drawn once per worker start, so `deploy/gcp/roll.sh` can
tell a worker its restart started from the one it replaced: re-rolling the same version, the old
worker's last beat reads `ok`, with that version, for a minute after it is gone. An id rather than
the start time, because the route is public and a start time would publish every restart.

The beat says the process started and its event loop runs; it does not say a queue is draining.
`/health/scheduler` covers that half, since a scheduler sweep is a task a worker ran.

Kept in Redis for the reason the scheduler's stamp is (`felix.jobs.tick`): the worker cannot run
without it, and it is what crosses to the API. One key, not one per worker: with several workers
the freshest beat is the one reported, so a single dead replica among live ones does not show.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any, Literal

from felix.redis_conn import RedisConnection

now_ms = lambda: int(time.time() * 1000)

BEAT_KEY = "felix:worker:beat"
# `FELIX_WORKER_STALE_SECONDS` may not go below two of these.
BEAT_INTERVAL_S = 15

_conn = RedisConnection(
    "worker beat",
    fallback_consequence="GET /health/worker on another process cannot see this worker",
)
_local: dict[str, Beat] = {}

WorkerStatus = Literal["ok", "stale", "never", "unknown"]


@dataclass(frozen=True, slots=True)
class Beat:
    at: int
    version: str
    #: Drawn once per worker start: two beats with the same `boot` came from the same process.
    boot: str


def reset_beat_for_tests() -> None:
    _local.clear()


def _parse(raw: Any) -> Beat | None:
    try:
        held = json.loads(raw)
        return Beat(int(held["at"]), str(held["version"]), str(held["boot"]))
    except Exception:
        return None


async def record_beat(beat: Beat) -> None:
    """Stamp a beat. Never raises: a beat that cannot be written is the route's alarm."""
    _local[BEAT_KEY] = beat
    client = await _conn.get()
    if client is None:
        return
    try:
        await client.set(BEAT_KEY, json.dumps(dataclasses.asdict(beat)))
    except Exception:
        await _conn.fallback("worker beat write")


async def read_beat(settings: Any = None) -> tuple[Beat | None, bool]:
    """`(beat, unreadable)`: the newest beat recorded, or None; and whether a Redis this deployment
    uses could not be read -- then None means "cannot tell", not "never"."""
    from felix.config import get_settings, redis_url_in_use

    seen = _local.get(BEAT_KEY)
    if not redis_url_in_use(settings if settings is not None else get_settings()):
        return seen, False
    client = await _conn.get()
    if client is None:
        return seen, True
    try:
        raw = await client.get(BEAT_KEY)
    except Exception:
        await _conn.fallback("worker beat read")
        return seen, True
    held = _parse(raw) if raw is not None else None
    if held is not None and (seen is None or held.at >= seen.at):
        seen = held
    return seen, False


async def beat_until(version: str, stop: asyncio.Event) -> None:
    """Beat now, then every `BEAT_INTERVAL_S` until `stop` is set, under one `boot` for the lot."""
    boot = secrets.token_hex(8)
    while True:
        await record_beat(Beat(now_ms(), version, boot))
        try:
            await asyncio.wait_for(stop.wait(), timeout=BEAT_INTERVAL_S)
            return
        except TimeoutError:
            continue


@dataclass(frozen=True, slots=True)
class WorkerHealth:
    status: WorkerStatus
    last_beat_age_s: int | None
    stale_after_s: int
    version: str | None
    boot: str | None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


async def worker_health(settings: Any) -> WorkerHealth:
    """`ok`, `stale`, `never` or `unknown`, how old the newest beat is in whole seconds, and the
    version and boot id of the worker that sent it."""
    stale_after = int(settings.worker_stale_seconds)
    beat, unreadable = await read_beat(settings)
    if beat is None:
        return WorkerHealth("unknown" if unreadable else "never", None, stale_after, None, None)
    # A stamp from a clock ahead of this one reads as zero, not negative.
    age = max(0, (now_ms() - beat.at) // 1000)
    status: WorkerStatus = "ok" if age <= stale_after else "stale"
    return WorkerHealth(status, age, stale_after, beat.version, beat.boot)


__all__ = [
    "BEAT_INTERVAL_S",
    "BEAT_KEY",
    "Beat",
    "WorkerHealth",
    "WorkerStatus",
    "beat_until",
    "read_beat",
    "record_beat",
    "reset_beat_for_tests",
    "worker_health",
]
