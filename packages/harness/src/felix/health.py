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
import time
from dataclasses import dataclass, field
from typing import Any

# A probe that hangs is a probe that fails. Kubernetes gives up on its own timeout
# anyway; bounding here makes the failure legible instead of a client-side timeout.
PROBE_TIMEOUT_S = 3.0


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

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "ready" if self.ready else "not_ready",
            "checks": {p.name: p.as_dict() for p in self.probes},
        }


async def _timed(name: str, coro: Any) -> ProbeResult:
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


async def _probe_redis(settings: Any) -> str:
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


async def check_readiness(settings: Any) -> ReadinessReport:
    """Probe every dependency this process needs to serve a request."""
    results = await asyncio.gather(
        _timed("database", _probe_database(settings)),
        _timed("redis", _probe_redis(settings)),
        _timed("object_store", _probe_object_store(settings)),
    )
    probes = list(results)
    return ReadinessReport(ready=all(p.ok for p in probes), probes=probes)


__all__ = ["PROBE_TIMEOUT_S", "ProbeResult", "ReadinessReport", "check_readiness"]
