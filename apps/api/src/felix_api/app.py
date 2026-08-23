"""create_app — assemble the FastAPI agents harness surface."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from felix import __version__ as harness_version
from felix.auth.middleware import AuthMiddleware
from felix.config import Settings, get_settings
from felix.plugins import get_registry
from felix.security.rate_limit import (
    RateLimitConfig,
    check_rate_limit,
    should_skip_rate_limit,
)
from felix.tools.provider import ToolProvider
from starlette.responses import Response

from felix_api.composition import compose, installed_plugins
from felix_api.routes import (
    a2a,
    approvals,
    audit,
    chat,
    internal,
    jobs,
    manifests,
    mcp,
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
    rate_config = RateLimitConfig()

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

    @app.middleware("http")
    async def body_limit_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        cl = request.headers.get("content-length")
        if cl is not None:
            try:
                if int(cl) > body_limit:
                    return JSONResponse({"error": "payload_too_large"}, status_code=413)
            except ValueError:
                pass
        return await call_next(request)

    @app.middleware("http")
    async def rate_limit_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        path = request.url.path
        if should_skip_rate_limit(path):
            return await call_next(request)
        key: str | None = None
        for resolver in rate_key_resolvers:
            key = resolver(request)
            if key:
                break
        if not key:
            auth = getattr(request.state, "auth", None)
            tenant = "default"
            if auth is not None:
                principal = getattr(auth, "principal", None)
                tenant = getattr(principal, "tenant_id", None) or getattr(auth, "tenant_id", "default")
            key = f"tenant:{tenant}"
        allowed = await check_rate_limit(key, rate_config)
        if not allowed:
            return JSONResponse({"error": "rate_limited"}, status_code=429)
        return await call_next(request)

    app.add_middleware(
        AuthMiddleware,
        settings=cfg,
        self_authenticating_mounts=self_auth_mounts,
    )

    @app.get("/health", tags=["System"])
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "env": cfg.environment,
            "version": harness_version,
            "multi_region": False,
            "federation": None,
        }

    @app.get("/metrics", tags=["System"])
    async def metrics() -> Response:
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    app.include_router(openai_compat.router, prefix="/v1")
    app.include_router(chat.router, prefix="/chat")
    app.include_router(internal.router, prefix="/internal")
    app.include_router(audit.router, prefix="/audit")
    app.include_router(approvals.router, prefix="/approvals")
    app.include_router(plans.router, prefix="/plans")
    app.include_router(jobs.router, prefix="/jobs")
    app.include_router(manifests.router, prefix="/manifests")
    app.include_router(eval_routes.router, prefix="/eval")
    app.include_router(usage.router, prefix="/usage")
    app.include_router(a2a.router, prefix="/a2a")
    app.include_router(mcp.router, prefix="/mcp")
    app.include_router(well_known.router)

    for plugin in plugin_list:
        routes_fn = getattr(plugin, "routes", None)
        if callable(routes_fn):
            routes_fn(app, tools=tool_provider)

    return app
