"""Readiness, request correlation, and SSE robustness.

`/health` returned a static ok while the Helm chart wired **both** the readiness and the
liveness probe to it — so a pod with a dead database reported Ready and took traffic, and
a real dependency outage never restarted anything. There was also no request id anywhere,
`FELIX_LOG_LEVEL` was never applied, and an SSE stream that failed mid-flight simply
stopped under an already-sent 200 OK with no error event and no `[DONE]`.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

import pytest
from felix.config import Settings
from felix.health import check_readiness
from felix.logging_setup import (
    REQUEST_ID_HEADER,
    RequestIdFilter,
    configure_logging,
    get_request_id,
    new_request_id,
    reset_request_id,
    set_request_id,
)


def _settings(**kw: object) -> Settings:
    base: dict[str, object] = {
        "database_url": "memory://ops",
        "object_store": "memory",
        "redis_url": "",
        "allow_insecure": True,
        "auth_mode": "none",
        "host": "127.0.0.1",
        "environment": "development",
    }
    base.update(kw)
    return Settings(**base)  # type: ignore[arg-type]


# --- readiness -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ready_when_dependencies_are_reachable() -> None:
    report = await check_readiness(_settings())
    assert report.ready is True
    assert {p.name for p in report.probes} == {"database", "redis", "object_store"}


@pytest.mark.asyncio
async def test_not_ready_when_a_dependency_is_down() -> None:
    """This is the case /health could not express."""
    report = await check_readiness(_settings(redis_url="redis://127.0.0.1:1/0"))
    assert report.ready is False
    redis = next(p for p in report.probes if p.name == "redis")
    assert redis.ok is False
    assert redis.detail, "a failed probe should say why"


@pytest.mark.asyncio
async def test_one_failure_does_not_hide_the_others() -> None:
    report = await check_readiness(_settings(redis_url="redis://127.0.0.1:1/0"))
    assert next(p for p in report.probes if p.name == "database").ok is True


@pytest.mark.asyncio
async def test_a_hanging_probe_fails_rather_than_hanging(monkeypatch: pytest.MonkeyPatch) -> None:
    """A probe that never returns is a probe that fails."""
    import felix.health as health

    async def _hang(settings):
        await asyncio.sleep(60)

    monkeypatch.setattr(health, "probe_redis", _hang)
    monkeypatch.setattr(health, "PROBE_TIMEOUT_S", 0.1)
    report = await asyncio.wait_for(check_readiness(_settings()), 5)
    assert report.ready is False
    assert "timed out" in next(p for p in report.probes if p.name == "redis").detail


@pytest.mark.asyncio
async def test_ready_endpoint_returns_503_when_not_ready() -> None:
    from felix_api.app import create_app
    from httpx import ASGITransport, AsyncClient

    app = create_app(settings=_settings(redis_url="redis://127.0.0.1:1/0"), plugins=[])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/ready")
    assert resp.status_code == 503
    assert resp.json()["status"] == "not_ready"


@pytest.mark.asyncio
async def test_live_does_no_io_and_stays_200_when_deps_are_down() -> None:
    """Liveness must not restart a healthy process because a database blipped."""
    from felix_api.app import create_app
    from httpx import ASGITransport, AsyncClient

    app = create_app(settings=_settings(redis_url="redis://127.0.0.1:1/0"), plugins=[])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for path in ("/live", "/health"):
            resp = await client.get(path)
            assert resp.status_code == 200, path
            assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_probe_paths_need_no_credential_under_a_real_auth_mode() -> None:
    """kubelet sends no Authorization header.

    Only /health was public, while the Helm chart probed /ready and /live — so under
    api_key or jwt both probes got 401, the pod never became Ready, and liveness
    restarted it. Compose probes /health, which is why local runs never showed it.
    /metrics stays 401 so this test cannot pass by making everything public.
    """
    from felix_api.app import create_app
    from httpx import ASGITransport, AsyncClient

    settings = _settings(
        auth_mode="api_key",
        auth_api_keys='{"k-probe-test-not-a-secret": {"tenant_id": "t", "sub": "s", "scopes": []}}',
    )
    app = create_app(settings=settings, plugins=[])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for path in ("/live", "/ready", "/health"):
            resp = await client.get(path)
            assert resp.status_code == 200, (path, resp.text)
        assert (await client.get("/metrics")).status_code == 401


def _deployed_probe_paths() -> set[str]:
    """Every HTTP path a deploy artefact probes, read from the artefacts themselves."""
    root = Path(__file__).resolve().parents[2]
    paths: set[str] = set()
    for chart in (root / "deploy/helm/felix/templates").glob("*.yaml"):
        # Probe blocks only: the ServiceMonitor's `path: /metrics` is a scrape target and
        # is authenticated on purpose. The block is matched as a whole so key order
        # under `httpGet:` does not matter.
        for block in re.findall(r"httpGet:\n((?:[ \t]+\S.*\n)+)", chart.read_text()):
            paths.update(re.findall(r"path:\s*(/\S+)", block))
    dockerfile = (root / "deploy/docker/Dockerfile").read_text()
    paths.update(re.findall(r"http://127\.0\.0\.1:8080(/[a-z]*)", dockerfile))
    for compose in (root / "deploy/docker").glob("compose*.yml"):
        paths.update(re.findall(r"http://127\.0\.0\.1:8080(/[a-z]*)", compose.read_text()))
    return paths


def test_every_deployed_probe_path_is_a_declared_probe_path() -> None:
    """A probe path added to a chart must be declared in PROBE_PATHS, which feeds both
    allowlists — so the fix for a failure here is a deliberate, named widening, never
    an edit to the auth allowlist directly.

    Reading the paths out of the artefacts pins the failure shape this had: the chart
    moved to /ready and /live and the allowlist did not move with it.
    """
    from felix.auth.middleware import _PUBLIC_EXACT
    from felix.security.rate_limit import PROBE_PATHS, SKIP_EXACT

    probed = _deployed_probe_paths()
    assert {"/ready", "/live", "/health"} <= probed, probed
    assert probed <= PROBE_PATHS, probed - PROBE_PATHS
    assert PROBE_PATHS <= _PUBLIC_EXACT and PROBE_PATHS <= SKIP_EXACT
    assert "/metrics" not in PROBE_PATHS


def _counting_probe(monkeypatch: pytest.MonkeyPatch, *, delay: float = 0.0) -> list[int]:
    """Replace the dependency probe with a counter; returns the single-element count."""
    import felix.health as health

    calls = [0]

    async def _probe(settings):
        calls[0] += 1
        if delay:
            await asyncio.sleep(delay)
        return health.ReadinessReport(ready=True)

    monkeypatch.setattr(health, "_probe_dependencies", _probe)
    monkeypatch.setattr(health, "_cached_report", None)
    monkeypatch.setattr(health, "_inflight", None)
    return calls


@pytest.mark.asyncio
async def test_ready_route_serves_a_cached_report(monkeypatch: pytest.MonkeyPatch) -> None:
    """Through the route, because the route is the production caller: if it stopped
    asking for the cache, a public unthrottled path would probe three dependencies per
    anonymous request."""
    from felix_api.app import create_app
    from httpx import ASGITransport, AsyncClient

    calls = _counting_probe(monkeypatch)
    app = create_app(settings=_settings(), plugins=[])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for _ in range(5):
            assert (await client.get("/ready")).status_code == 200
    assert calls[0] == 1


@pytest.mark.asyncio
async def test_readiness_cache_expires_and_is_per_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    import felix.health as health

    calls = _counting_probe(monkeypatch)
    now = [1000.0]
    monkeypatch.setattr(health.time, "monotonic", lambda: now[0])
    settings = _settings()
    await check_readiness(settings, max_age_s=60)
    await check_readiness(settings, max_age_s=60)
    assert calls[0] == 1, "the second call within the window should not probe again"
    now[0] += 61
    await check_readiness(settings, max_age_s=60)
    assert calls[0] == 2, "a report older than max_age_s is not served"
    await check_readiness(settings, max_age_s=0)
    assert calls[0] == 3, "max_age_s=0 always probes"
    await check_readiness(_settings(), max_age_s=60)
    assert calls[0] == 4, "a different configuration never inherits another's report"


@pytest.mark.asyncio
async def test_concurrent_readiness_callers_share_one_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """A burst during a slow probe must not multiply the probe: with a blackholed
    dependency the window is PROBE_TIMEOUT_S, on the pod that is already degraded."""
    calls = _counting_probe(monkeypatch, delay=0.05)
    settings = _settings()
    reports = await asyncio.gather(*(check_readiness(settings) for _ in range(20)))
    assert all(r.ready for r in reports)
    assert calls[0] == 1


@pytest.mark.asyncio
async def test_a_cancelled_caller_does_not_drop_the_shared_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """A client that opens /ready and aborts must not discard the report the other
    waiters share, nor leave the guard so the next request starts a fresh probe — that
    loop would restore the per-request amplification the cache removes."""
    import felix.health as health

    calls = _counting_probe(monkeypatch, delay=0.05)
    settings = _settings()
    first = asyncio.ensure_future(check_readiness(settings))
    others = [asyncio.ensure_future(check_readiness(settings)) for _ in range(2)]
    await asyncio.sleep(0.01)
    first.cancel()
    reports = await asyncio.gather(*others)
    assert all(r.ready for r in reports)
    assert first.cancelled()
    assert health._inflight is None, "the finished task must clear its own guard"
    await check_readiness(settings)
    assert calls[0] == 1, "the report survived its creator being cancelled"


@pytest.mark.asyncio
async def test_ready_body_carries_no_probe_detail() -> None:
    """The route is public: a failed probe's detail is the exception text, which names
    internal hosts, ports and database users. The body says which probe failed, not why."""
    from felix_api.app import create_app
    from httpx import ASGITransport, AsyncClient

    app = create_app(settings=_settings(redis_url="redis://127.0.0.1:1/0"), plugins=[])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = (await client.get("/ready")).json()
    assert body["checks"]["redis"]["ok"] is False
    assert not any("detail" in check for check in body["checks"].values()), body
    assert "127.0.0.1" not in str(body)


def test_endpoints_have_no_z_suffix() -> None:
    """House naming: /ready and /live, never /readyz or /livez."""
    from felix_api.app import create_app

    app = create_app(settings=_settings(), plugins=[])
    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/ready" in paths and "/live" in paths
    assert not {"/readyz", "/healthz", "/livez"} & paths


# --- request correlation ----------------------------------------------------------


def test_request_id_is_scoped_to_the_context() -> None:
    assert get_request_id() == ""
    token = set_request_id("abc123")
    assert get_request_id() == "abc123"
    reset_request_id(token)
    assert get_request_id() == ""


def test_generated_ids_are_distinct() -> None:
    assert new_request_id() != new_request_id()


def test_filter_attaches_the_id_to_plain_logging_records() -> None:
    """The codebase logs through the stdlib everywhere; the id has to arrive there."""
    record = logging.LogRecord("n", logging.INFO, "p", 1, "m", None, None)
    token = set_request_id("corr-1")
    try:
        RequestIdFilter().filter(record)
    finally:
        reset_request_id(token)
    assert record.request_id == "corr-1"  # type: ignore[attr-defined]


def test_filter_uses_a_placeholder_outside_a_request() -> None:
    record = logging.LogRecord("n", logging.INFO, "p", 1, "m", None, None)
    RequestIdFilter().filter(record)
    assert record.request_id == "-"  # type: ignore[attr-defined]


def test_log_level_is_actually_applied() -> None:
    """FELIX_LOG_LEVEL was never passed to the logging module."""
    configure_logging(_settings(log_level="WARNING"))
    assert logging.getLogger().level == logging.WARNING
    configure_logging(_settings(log_level="DEBUG"))
    assert logging.getLogger().level == logging.DEBUG


def test_configure_logging_does_not_stack_handlers() -> None:
    for _ in range(3):
        configure_logging(_settings())
    ours = [h for h in logging.getLogger().handlers if getattr(h, "_felix", False)]
    assert len(ours) == 1


@pytest.mark.asyncio
async def test_request_id_is_echoed_and_honoured() -> None:
    from felix_api.app import create_app
    from httpx import ASGITransport, AsyncClient

    app = create_app(settings=_settings(), plugins=[])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        generated = await client.get("/health")
        assert generated.headers.get(REQUEST_ID_HEADER)

        supplied = await client.get("/health", headers={REQUEST_ID_HEADER: "caller-trace-1"})
        assert supplied.headers.get(REQUEST_ID_HEADER) == "caller-trace-1"


# --- SSE ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_is_emitted_during_a_quiet_stream() -> None:
    """A long tool call emits nothing and proxy idle timeouts are commonly 60s, so a
    healthy run was being disconnected mid-flight."""
    from felix_api.routes._sse import HEARTBEAT, with_heartbeat

    async def _slow():
        await asyncio.sleep(0.25)
        yield "first"

    seen = [item async for item in with_heartbeat(_slow(), interval=0.05)]
    assert HEARTBEAT in seen
    assert seen[-1] == "first"


@pytest.mark.asyncio
async def test_heartbeat_passes_events_through_unchanged() -> None:
    from felix_api.routes._sse import HEARTBEAT, with_heartbeat

    async def _fast():
        for i in range(3):
            yield i

    seen = [item async for item in with_heartbeat(_fast(), interval=5.0)]
    assert seen == [0, 1, 2]
    assert HEARTBEAT not in seen


@pytest.mark.asyncio
async def test_heartbeat_propagates_upstream_errors() -> None:
    from felix_api.routes._sse import with_heartbeat

    async def _boom():
        yield "one"
        raise RuntimeError("upstream died")

    with pytest.raises(RuntimeError, match="upstream died"):
        _ = [item async for item in with_heartbeat(_boom(), interval=5.0)]
