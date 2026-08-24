"""Auth middleware — none / api_key / jwt modes for FastAPI/Starlette."""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from felix.auth.context import ANONYMOUS, AuthContext, Principal, require_scope
from felix.auth.jwt import parse_verifiers, verify_jwt
from felix.config import Settings, get_settings
from felix.context import AuthContext as CtxAuth
from felix.context import LimitState, RequestContext, async_run_with_context
from felix.security.constant_time import constant_time_equal

logger = logging.getLogger("felix.auth.middleware")

# Unauthenticated access allowed even when auth_mode is jwt/api_key (probes + discovery).
# /metrics is NOT public: its counters carry tenant-supplied manifest ids and remote
# MCP tool names as label values, so an anonymous scrape discloses every tenant's
# manifest and tool names.
_PUBLIC_EXACT = frozenset({"/health", "/docs", "/openapi.json", "/redoc"})
_PUBLIC_PREFIX = ("/.well-known/",)


def _is_public_path(path: str) -> bool:
    if path in _PUBLIC_EXACT:
        return True
    return any(path.startswith(p) for p in _PUBLIC_PREFIX)


def _parse_api_keys(raw: str) -> dict[str, dict[str, Any]]:
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

    if _is_public_path(path):
        return ANONYMOUS

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
        keys = _parse_api_keys(settings.auth_api_keys)
        matched: dict[str, Any] | None = None
        for k, meta in keys.items():
            if constant_time_equal(token, k):
                matched = meta if isinstance(meta, dict) else {}
                break
        if matched is None:
            return JSONResponse(
                {"error": "unauthorized", "reason": "invalid_api_key"},
                status_code=401,
            )
        scopes_raw = matched.get("scopes") or []
        scopes = frozenset(str(s) for s in scopes_raw)
        return AuthContext(
            principal=Principal(
                subject=str(matched.get("sub") or "api_key"),
                tenant_id=str(matched.get("tenant_id") or "default"),
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
