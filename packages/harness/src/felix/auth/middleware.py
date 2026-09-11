"""Auth middleware — none / api_key / jwt modes for FastAPI/Starlette."""

from __future__ import annotations

import inspect
import json
import logging
from collections.abc import Sequence
from functools import lru_cache
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from felix.auth.context import (
    ANONYMOUS,
    BUILTIN_AUTH_MODES,
    AuthContext,
    Principal,
    assert_valid_tenant_id,
    require_scope,
)
from felix.auth.jwt import parse_verifiers, verify_jwt
from felix.config import Settings, get_settings
from felix.context import AuthContext as CtxAuth
from felix.context import LimitState, RequestContext, async_run_with_context
from felix.security.constant_time import constant_time_equal
from felix.security.rate_limit import PROBE_PATHS

logger = logging.getLogger("felix.auth.middleware")


async def _call_authenticator(
    builder: Any, settings: Settings, request: Request
) -> AuthContext | JSONResponse:
    """Invoke a plugin authenticator.

    ``builder(settings)`` yields the authenticator; it is then called with the
    request. Either step may be async, and the authenticator may be a plain
    callable or an object exposing ``authenticate``.
    """
    authenticator = builder(settings)
    if inspect.isawaitable(authenticator):
        authenticator = await authenticator
    call = getattr(authenticator, "authenticate", authenticator)
    result = call(request)
    if inspect.isawaitable(result):
        result = await result
    if not isinstance(result, (AuthContext, JSONResponse)):
        raise TypeError(
            f"plugin authenticator returned {type(result).__name__}, expected AuthContext or JSONResponse"
        )
    return result


# Unauthenticated access allowed even when auth_mode is jwt/api_key (probes + discovery).
# Probe paths come from one shared set, because kubelet sends no credential and the
# rate limiter has to skip the same paths — see `PROBE_PATHS` for the incident.
# /metrics is NOT public: its counters carry tenant-supplied manifest ids and remote
# MCP tool names as label values, so an anonymous scrape discloses every tenant's
# manifest and tool names. The API reference is not public either unless the operator
# says so (`FELIX_DOCS_PUBLIC`): it describes every route, including the management
# ones, and was the one map of the surface an unauthenticated caller could read.
_PUBLIC_EXACT = PROBE_PATHS
_PUBLIC_PREFIX = ("/.well-known/",)
DOCS_PATHS = frozenset({"/docs", "/openapi.json"})


def _is_public_path(path: str, *, docs_public: bool = False) -> bool:
    if path in _PUBLIC_EXACT:
        return True
    if docs_public and path in DOCS_PATHS:
        return True
    return any(path.startswith(p) for p in _PUBLIC_PREFIX)


@lru_cache(maxsize=4)
def _parse_api_keys(raw: str) -> dict[str, dict[str, Any]]:
    """Parsed once per distinct configuration, not once per request.

    Measured at 15.6 us for fifty keys, on every authenticated request in `api_key`
    mode. Keyed on the raw settings string, so a rotated configuration invalidates this
    for free and nothing has to remember to.

    An earlier version of this change also replaced the scan below with a SHA-256
    digest index, on the audit's suggestion. It was dropped. The scan is 0.58 us at
    five keys and 1.13 us at ten -- the shape real deployments have -- against 15.58 us
    for the parse, so the index optimised the wrong thing, and it put a hash of a
    credential into the code where every future reader has to work out whether it is
    password storage. It is not, but code that needs that argument is worse than code
    that does not.
    """
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


async def authenticate_request(
    request: Request,
    settings: Settings,
    *,
    self_authenticating_mounts: Sequence[str] = (),
) -> AuthContext | JSONResponse:
    """Resolve AuthContext or return a 401 JSONResponse.

    When ``auth_mode`` is ``api_key`` or ``jwt``, missing credentials fail closed
    (401) except for public probe/discovery paths.
    """
    path = request.url.path
    auth_header = request.headers.get("authorization") or ""
    mode = settings.auth_mode

    if any(path == m or path.startswith(f"{m}/") for m in self_authenticating_mounts):
        return ANONYMOUS

    if mode == "none":
        return ANONYMOUS

    if _is_public_path(path, docs_public=settings.docs_public):
        return ANONYMOUS

    # A plugin-registered mode wins over the built-ins, so an optional package can
    # add an auth scheme (OIDC, mTLS, a vendor SSO) without editing core. Built-in
    # mode names are not overridable — a plugin cannot silently weaken `api_key`.
    if mode not in BUILTIN_AUTH_MODES:
        from felix.plugins import get_registry

        builder = get_registry().authenticator_builder(mode)
        if builder is None:
            logger.error("unknown FELIX_AUTH_MODE %r and no plugin registered it", mode)
            return JSONResponse(
                {"error": "unauthorized", "reason": "unknown_auth_mode"},
                status_code=401,
            )
        try:
            return await _call_authenticator(builder, settings, request)
        except Exception:
            logger.exception("plugin authenticator for mode %r failed", mode)
            return JSONResponse(
                {"error": "unauthorized", "reason": "authenticator_error"},
                status_code=401,
            )

    if mode == "api_key":
        token = ""
        if auth_header.lower().startswith("bearer ") or auth_header.lower().startswith("apikey "):
            token = auth_header.split(" ", 1)[1].strip()
        else:
            token = request.headers.get("x-api-key") or ""
        if not token:
            return JSONResponse(
                {"error": "unauthorized", "reason": "missing_credentials"},
                status_code=401,
            )
        matched: dict[str, Any] | None = None
        # No early break. Stopping at the match makes the total time depend on *which*
        # key matched, which is the leak `constant_time_equal` exists to close, one
        # level up. Every configured key is compared on every request either way.
        for key, meta in _parse_api_keys(settings.auth_api_keys).items():
            if constant_time_equal(token, key):
                matched = meta if isinstance(meta, dict) else {}
        if matched is None:
            return JSONResponse(
                {"error": "unauthorized", "reason": "invalid_api_key"},
                status_code=401,
            )
        scopes_raw = matched.get("scopes") or []
        scopes = frozenset(str(s) for s in scopes_raw)
        claimed_tenant = str(matched.get("tenant_id") or "default")
        try:
            # The key's tenant_id is operator-supplied config, but it is still the ownership
            # boundary — refuse it at the door so it is a 401 rather than a later failure on
            # the first write.
            assert_valid_tenant_id(claimed_tenant)
        except ValueError as exc:
            logger.warning("api_key rejected: %s", exc)
            return JSONResponse(
                {"error": "unauthorized", "reason": "invalid_tenant"},
                status_code=401,
            )
        return AuthContext(
            principal=Principal(
                subject=str(matched.get("sub") or "api_key"),
                tenant_id=claimed_tenant,
                scopes=scopes,
                issuer="api_key",
                scheme="api_key",
            ),
            outbound_token=ANONYMOUS.outbound_token,
            anonymous=False,
            raw_claims=dict(matched),
        )

    # jwt mode
    if not auth_header.lower().startswith("bearer "):
        return JSONResponse(
            {"error": "unauthorized", "reason": "missing_credentials"},
            status_code=401,
            headers={"www-authenticate": 'Bearer error="invalid_token"'},
        )
    token = auth_header[7:].strip()
    configs = parse_verifiers(settings.jwt_verifiers)
    if not configs:
        return JSONResponse(
            {"error": "unauthorized", "reason": "no_verifiers_configured"},
            status_code=401,
            headers={"www-authenticate": 'Bearer error="invalid_token"'},
        )
    result = verify_jwt(token, configs, jwks_public=settings.jwks_public, settings=settings)
    if not result.ok:
        return JSONResponse(
            {"error": "unauthorized", "reason": result.reason},
            status_code=401,
            headers={"www-authenticate": 'Bearer error="invalid_token"'},
        )
    return AuthContext(
        principal=result.principal,
        outbound_token=ANONYMOUS.outbound_token,
        anonymous=False,
        raw_claims=result.payload,
    )


class AuthMiddleware:
    """Pure-ASGI middleware installing RequestContext for each request.

    Was a `BaseHTTPMiddleware`, which cost ~143us per request and handed every
    response chunk across a memory object stream — measurably the largest single
    overhead on the SSE path.

    The `async with` wraps the whole ASGI call, so the context covers the response
    body and not just the handler. Under `BaseHTTPMiddleware` that happened to hold
    too, despite `call_next` returning as soon as the response *started*: it runs the
    downstream app in a child task, and `start_soon` gives that task a copy of the
    context, so resetting the token here never reached it. Relying on that was
    accidental. Here it is structural, which is what the test guards.
    """

    def __init__(
        self,
        app: Any,
        *,
        settings: Settings | None = None,
        self_authenticating_mounts: Sequence[str] = (),
    ) -> None:
        self.app = app
        self.settings = settings
        self.self_authenticating_mounts = tuple(self_authenticating_mounts)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        settings = self.settings or get_settings()
        request = Request(scope, receive)
        auth_or_resp = await authenticate_request(
            request,
            settings,
            self_authenticating_mounts=self.self_authenticating_mounts,
        )
        if isinstance(auth_or_resp, JSONResponse):
            await auth_or_resp(scope, receive, send)
            return
        auth = auth_or_resp
        scope.setdefault("state", {})["auth"] = auth
        ctx_auth = CtxAuth(
            principal_sub=auth.principal.subject or "anonymous",
            tenant_id=auth.principal.tenant_id,
            scopes=auth.principal.scopes,
            anonymous=auth.anonymous,
            raw_claims=auth.raw_claims,
            scheme=getattr(auth.principal, "scheme", "anonymous"),
        )
        req_ctx = RequestContext(
            settings=settings,
            auth=ctx_auth,
            limit_state=LimitState(),
        )
        async with async_run_with_context(req_ctx):
            await self.app(scope, receive, send)


def require_authenticated(auth: AuthContext) -> None:
    if auth.anonymous or not auth.principal.subject:
        raise PermissionError("authentication required")


__all__ = [
    "AuthMiddleware",
    "authenticate_request",
    "require_authenticated",
    "require_scope",
]
