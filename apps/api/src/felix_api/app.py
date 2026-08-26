"""create_app — assemble the FastAPI agents harness surface."""

from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from felix import __version__ as harness_version
from felix.auth.middleware import AuthMiddleware
from felix.config import Settings, get_settings
from felix.logging_setup import (
    configure_logging,
)
from felix.plugins import get_registry
from felix.security.rate_limit import (
    build_rate_limit_config,
)
from felix.tools.provider import ToolProvider
from starlette.responses import Response

from felix_api.composition import compose, installed_plugins
from felix_api.middleware import BodyLimitMiddleware, RateLimitMiddleware, RequestIdMiddleware
from felix_api.routes import (
    a2a,
    approvals,
    artifacts,
    audit,
    chat,
    internal,
    jobs,
    manifests,
    mcp,
    memory,
    openai_compat,
    plans,
    usage,
    well_known,
)
from felix_api.routes import (
    eval as eval_routes,
)

CORE_BODY_LIMIT_BYTES = 1024 * 1024


def create_app(
    *,
    settings: Settings | None = None,
    tools: ToolProvider | None = None,
    plugins: list[Any] | None = None,
) -> FastAPI:
    """Build the Felix API.

    Parameters
    ----------
    settings:
        Runtime settings; defaults to ``get_settings()``.
    tools:
        Pre-built tool provider (tests). Defaults to ``compose(settings)``.
    plugins:
        Feature plugins. Defaults to ``installed_plugins()``.
    """
    cfg = settings or get_settings()
    cfg.validate_runtime()
    # FELIX_LOG_LEVEL was never applied to the logging module, and structlog was a
    # dependency nothing imported.
    configure_logging(cfg)
    plugin_list = list(plugins if plugins is not None else installed_plugins())
    tool_provider = tools if tools is not None else compose(cfg)

    plugin_limits = [int(getattr(p, "body_limit_bytes", None) or 0) for p in plugin_list]
    body_limit = max([CORE_BODY_LIMIT_BYTES, *plugin_limits])
    self_auth_mounts = tuple(
        m for p in plugin_list for m in (getattr(p, "self_authenticating_mounts", ()) or ())
    )
    rate_key_resolvers = [
        p.rate_limit_key for p in plugin_list if callable(getattr(p, "rate_limit_key", None))
    ]
    # Settings-driven, and Redis-backed when one is configured: the limiter was
    # always in-process, so the effective ceiling was N_replicas x the limit.
    rate_config = build_rate_limit_config(cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from felix.flush import start_flush_task, stop_flush_task
        from felix.observability.tracing import setup_observability, shutdown_observability
        from felix.secrets import hydrate_secrets

        app.state.settings = cfg
        app.state.tools = tool_provider
        app.state.plugins = plugin_list
        await hydrate_secrets(cfg)
        setup_observability(cfg)
        for hook in get_registry()._startup_hooks:
            await hook(app)
        # The agent loop emits audit and usage events in *this* process; without a
        # flusher here they are never written and the buffer grows unbounded.
        flush_task = start_flush_task(cfg)
        app.state.flush_task = flush_task

        # Remote JWKS: verify_jwt is synchronous and on the request path, so it reads a
        # cache. Without this the `access` and `cognito` schemes have no key source at
        # all and every token from them fails closed.
        jwks_task = None
        if cfg.auth_mode == "jwt":
            from felix.auth.jwt import refresh_all_jwks, run_jwks_refresh_loop

            await refresh_all_jwks(cfg)
            jwks_task = asyncio.create_task(run_jwks_refresh_loop(cfg, interval_s=600.0))
        app.state.jwks_task = jwks_task
        try:
            yield
        finally:
            if jwks_task is not None:
                jwks_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await jwks_task
            await stop_flush_task(flush_task, cfg)
            # Release process-lifetime resources. The S3 client had no close() at all
            # and dispose_engine() was a no-op, so both lingered across worker recycles.
            from felix.db.session import dispose_engine
            from felix.session.notify import reset_notifications
            from felix.storage import close_object_stores

            with contextlib.suppress(Exception):
                await close_object_stores()
            # Thread notifications hold a Redis client, a pubsub connection and a
            # long-lived pump task -- the same shape as the resources above, which
            # lingered across worker recycles until they were joined here.
            with contextlib.suppress(Exception):
                await reset_notifications()
            with contextlib.suppress(Exception):
                await dispose_engine()
            shutdown_observability()

    app = FastAPI(
        title="Felix",
        version=harness_version,
        summary="Self-hostable managed agents harness.",
        description=(
            "Felix compiles an `apiVersion: felix/v1` manifest into a governed agent "
            "with durable fibers, memory, skills, eval, approvals, and sandboxes — "
            "exposed over OpenAI-compatible, A2A, MCP, and REST/SSE surfaces. "
            "Fork, rewind, and steer live runs."
        ),
        contact={
            "name": "Felix",
            "url": "https://docs.felix.run",
        },
        license_info={
            "name": "Apache-2.0",
            "url": "https://github.com/felix-run/felix/blob/main/LICENSE",
        },
        lifespan=lifespan,
    )
    # Eager state so ASGI tests / middleware work before lifespan starts.
    app.state.settings = cfg
    app.state.tools = tool_provider
    app.state.plugins = plugin_list

    # Middleware order. Starlette's add_middleware inserts at index 0, so the LAST one
    # registered is the OUTERMOST. Auth was once registered last and therefore ran
    # first, which meant a 401 returned before the rate limiter was ever consulted:
    # credential guessing was completely unthrottled.
    #
    # Registering auth -> rate limit -> body limit -> request id yields the runtime
    # order request id -> body limit -> rate limit -> auth. So every response carries a
    # correlation id including a 413, an oversized body is rejected before any other
    # work, and every request (including one that will 401) is counted.
    #
    # All four are pure ASGI. `@app.middleware("http")` would wrap each in
    # BaseHTTPMiddleware, which costs ~143us per request and ~76us per streamed token —
    # see felix_api.middleware. tests/unit/test_middleware_stack.py asserts none
    # creeps back in.
    app.add_middleware(
        AuthMiddleware,
        settings=cfg,
        self_authenticating_mounts=self_auth_mounts,
    )
    app.add_middleware(
        RateLimitMiddleware,
        config=rate_config,
        settings=cfg,
        key_resolvers=rate_key_resolvers,
    )
    app.add_middleware(BodyLimitMiddleware, limit=body_limit)
    app.add_middleware(RequestIdMiddleware)

    def _liveness() -> dict[str, Any]:
        # Deliberately does no I/O. Liveness answers "is this process running and its
        # event loop responsive"; a failure means restart me. Checking dependencies here
        # would restart a healthy pod because a database was briefly unavailable.
        return {
            "status": "ok",
            "env": cfg.environment,
            "version": harness_version,
            "multi_region": False,
            "federation": None,
        }

    @app.get("/health", tags=["System"])
    async def health() -> dict[str, Any]:
        """Liveness. Kept at this path because deploys and smoke tests point here."""
        return _liveness()

    @app.get("/live", tags=["System"])
    async def live() -> dict[str, Any]:
        """Liveness, named for what it is."""
        return _liveness()

    @app.get("/ready", tags=["System"])
    async def ready(response: Response) -> dict[str, Any]:
        """Readiness — can this process actually serve a request?

        `/health` returned a static ok while the Helm chart wired **both** probes to it,
        so a pod with a dead database reported Ready and took traffic. This one probes
        the dependencies and returns 503 when any of them is down, which takes the pod
        out of rotation without restarting it.
        """
        from felix.health import check_readiness

        report = await check_readiness(cfg)
        if not report.ready:
            response.status_code = 503
        return report.as_dict()

    @app.get("/metrics", tags=["System"])
    async def metrics() -> Response:
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    app.include_router(openai_compat.router, prefix="/v1")
    app.include_router(chat.router, prefix="/chat")
    app.include_router(internal.router, prefix="/internal")
    app.include_router(audit.router, prefix="/audit")
    app.include_router(artifacts.router, prefix="/artifacts")
    app.include_router(approvals.router, prefix="/approvals")
    app.include_router(plans.router, prefix="/plans")
    app.include_router(jobs.router, prefix="/jobs")
    app.include_router(manifests.router, prefix="/manifests")
    app.include_router(eval_routes.router, prefix="/eval")
    app.include_router(usage.router, prefix="/usage")
    app.include_router(memory.router, prefix="/memory")
    app.include_router(a2a.router, prefix="/a2a")
    app.include_router(mcp.router, prefix="/mcp")
    app.include_router(well_known.router)

    for plugin in plugin_list:
        routes_fn = getattr(plugin, "routes", None)
        if callable(routes_fn):
            routes_fn(app, tools=tool_provider)

    return app
