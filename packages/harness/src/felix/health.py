"""Dependency probes shared by ``GET /ready`` and ``felix doctor``.

`/health` returned a static ``{"status": "ok"}`` while the Helm chart wired **both** the
readiness and the liveness probe to it. A pod with a dead database therefore reported
Ready and received traffic, and conversely a genuine dependency outage never restarted
anything — the probe could not fail for any reason short of the process being gone.

Readiness and liveness answer different questions, so they get different endpoints:

* **live** — is this process running and its event loop responsive? Cheap, no I/O. A
  failure here means "restart me".
* **ready** — can this process actually serve a request? Checks its dependencies. A
  failure means "take me out of rotation", not "restart me".

Keeping the probes here rather than inline in the route means `felix doctor` and the
endpoint cannot drift apart on what "reachable" means.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from functools import partial
from typing import Any

logger = logging.getLogger("felix.health")

# A probe that hangs is a probe that fails. Kubernetes gives up on its own timeout
# anyway; bounding here makes the failure legible instead of a client-side timeout.
PROBE_TIMEOUT_S = 3.0

# `/ready` is public and exempt from rate limiting, because kubelet sends no credential
# and treats a 429 as a failed probe. Left uncached, that makes three dependency pings
# per anonymous request — a cheap way to load the database from outside. A report this
# old is served as-is; kubelet's period is 10s, so it never sees a stale answer.
READINESS_CACHE_S = 2.0


@dataclass(frozen=True)
class ProbeResult:
    name: str
    ok: bool
    detail: str = ""
    duration_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"ok": self.ok, "duration_ms": self.duration_ms}
        if self.detail:
            out["detail"] = self.detail
        return out


@dataclass
class ReadinessReport:
    ready: bool
    probes: list[ProbeResult] = field(default_factory=list)

    def as_dict(self, *, include_detail: bool = True) -> dict[str, Any]:
        """``include_detail=False`` is what the public route sends: a probe's detail is
        the exception text, which names internal hosts, ports and database users."""
        checks = {p.name: p.as_dict() for p in self.probes}
        if not include_detail:
            for check in checks.values():
                check.pop("detail", None)
        return {"status": "ready" if self.ready else "not_ready", "checks": checks}


# Keyed on the settings object by identity: one process has one, so this is a single
# entry in production, and two configurations never share a report. The in-flight task
# is shared too, so callers arriving during a probe await it instead of probing again —
# without that, a blackholed dependency turns every request in its 3s window into three
# more connections, on the pod that is already degraded.
_cached_report: tuple[Any, float, ReadinessReport] | None = None
_inflight: tuple[Any, asyncio.Task[ReadinessReport]] | None = None


async def timed_probe(name: str, coro: Any) -> ProbeResult:
    start = time.monotonic()
    try:
        detail = await asyncio.wait_for(coro, PROBE_TIMEOUT_S)
        ok = True
    except TimeoutError:
        detail, ok = f"timed out after {PROBE_TIMEOUT_S:g}s", False
    except Exception as exc:
        detail, ok = str(exc)[:160], False
    return ProbeResult(name, ok, str(detail or ""), int((time.monotonic() - start) * 1000))


async def _probe_database(settings: Any) -> str:
    url = getattr(settings, "database_url", "") or ""
    if url.startswith("memory://"):
        return "memory://"
    from sqlalchemy import text

    from felix.db.session import get_engine

    engine = get_engine(url)
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return "reachable"


async def probe_redis(settings: Any) -> str:
    url = (getattr(settings, "redis_url", "") or "").strip()
    if not url:
        return "not configured"
    import redis.asyncio as redis

    client = redis.from_url(url)
    try:
        await client.ping()
    finally:
        await client.aclose()
    return "reachable"


async def _probe_object_store(settings: Any) -> str:
    from felix.storage import get_object_store

    store = get_object_store(settings)
    # `exists` on a key that will not be there: cheap, and unlike a write it needs no
    # cleanup and no write permission.
    await store.exists("__felix_readiness_probe__")
    return getattr(settings, "object_store", "") or "ok"


async def _probe_jwks(settings: Any) -> str:
    """Every configured verifier can verify a token right now.

    A remote key set never fetched or past its TTL is not served, a shared issuer with no
    audience is refused, a local key that does not import verifies nothing: in each the
    pod 401s every token from that issuer while its database and Redis probes stay
    green. The probe fails only when *no* verifier is usable — then the pod cannot
    authenticate anyone and leaves rotation; a partial failure stays ready and is
    logged, so one issuer's outage (or a blip past the TTL on one replica) does not take
    the deployment off the Service for the issuers that still work.
    """
    from felix.auth.jwt import verifier_status

    status = verifier_status(settings)
    unusable = [(cfg, why) for cfg, why in status if why is not None]
    detail = "; ".join(f"{cfg.scheme}:{cfg.issuer}: {why}" for cfg, why in unusable)
    if unusable and len(unusable) == len(status):
        raise RuntimeError(detail)
    if unusable:
        logger.warning("jwt verifiers degraded: %s", detail)
        return f"degraded: {detail}"
    return "every verifier usable"


async def check_readiness(settings: Any, *, max_age_s: float = READINESS_CACHE_S) -> ReadinessReport:
    """Probe every dependency this process needs to serve a request.

    A report younger than ``max_age_s`` is served as-is, and concurrent callers share one
    probe. Cached by default so the next caller cannot forget; pass ``0`` to always probe.
    """
    global _inflight
    if max_age_s > 0 and _cached_report is not None:
        owner, at, report = _cached_report
        if owner is settings and time.monotonic() - at < max_age_s:
            return report
    if _inflight is not None and _inflight[0] is settings and not _inflight[1].done():
        return await asyncio.shield(_inflight[1])
    task = asyncio.ensure_future(_probe_dependencies(settings))
    _inflight = (settings, task)
    # The task stores its own result. Storing it from this caller would tie the cache
    # to this caller surviving: a request cancelled mid-probe (client disconnect) would
    # discard the report and clear the guard while the probe kept running, and a loop
    # of open-and-abort would restore the amplification the cache exists to remove.
    task.add_done_callback(partial(_store_report, settings))
    return await asyncio.shield(task)


def _store_report(settings: Any, task: asyncio.Task[ReadinessReport]) -> None:
    global _cached_report, _inflight
    if _inflight is not None and _inflight[1] is task:
        _inflight = None
    if not task.cancelled() and task.exception() is None:
        _cached_report = (settings, time.monotonic(), task.result())


async def _probe_dependencies(settings: Any) -> ReadinessReport:
    from felix.auth.jwt import uses_jwt_verifiers

    timed = [
        timed_probe("database", _probe_database(settings)),
        timed_probe("redis", probe_redis(settings)),
        timed_probe("object_store", _probe_object_store(settings)),
    ]
    if uses_jwt_verifiers(settings):
        timed.append(timed_probe("jwks", _probe_jwks(settings)))
    probes = list(await asyncio.gather(*timed))
    return ReadinessReport(ready=all(p.ok for p in probes), probes=probes)


__all__ = [
    "PROBE_TIMEOUT_S",
    "READINESS_CACHE_S",
    "ProbeResult",
    "ReadinessReport",
    "check_readiness",
    "probe_redis",
    "timed_probe",
]
