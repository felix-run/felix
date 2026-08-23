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

    monkeypatch.setattr(health, "_probe_redis", _hang)
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
    from felix_api.routes.chat import _HEARTBEAT, _with_heartbeat

    async def _slow():
        await asyncio.sleep(0.25)
        yield "first"

    seen = [item async for item in _with_heartbeat(_slow(), interval=0.05)]
    assert _HEARTBEAT in seen
    assert seen[-1] == "first"


@pytest.mark.asyncio
async def test_heartbeat_passes_events_through_unchanged() -> None:
    from felix_api.routes.chat import _HEARTBEAT, _with_heartbeat

    async def _fast():
        for i in range(3):
            yield i

    seen = [item async for item in _with_heartbeat(_fast(), interval=5.0)]
    assert seen == [0, 1, 2]
    assert _HEARTBEAT not in seen


@pytest.mark.asyncio
async def test_heartbeat_propagates_upstream_errors() -> None:
    from felix_api.routes.chat import _with_heartbeat

    async def _boom():
        yield "one"
        raise RuntimeError("upstream died")

    with pytest.raises(RuntimeError, match="upstream died"):
        _ = [item async for item in _with_heartbeat(_boom(), interval=5.0)]
