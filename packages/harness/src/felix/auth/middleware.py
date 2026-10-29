"""Auth middleware — none / api_key / jwt modes for FastAPI/Starlette."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from starlette._utils import get_route_path
from starlette.requests import Request
from starlette.responses import JSONResponse

from felix.auth.api_keys import ApiKeyEntry, parse_api_keys
from felix.auth.context import (
    ANONYMOUS,
    BUILTIN_AUTH_MODES,
    AuthContext,
    Principal,
    assert_valid_tenant_id,
    require_scope,
)
from felix.auth.github import AUTH_METHODS_PATH
from felix.auth.jwt import parse_verifiers, verify_jwt
from felix.auth.persons import caller_person
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


DOCS_PATHS = frozenset({"/docs", "/openapi.json"})


@dataclass(frozen=True, slots=True)
class AnonymousPaths:
    """The paths a request reaches with no credential, as exact paths and prefixes."""

    exact: frozenset[str]
    prefixes: tuple[str, ...]

    def admits(self, path: str) -> bool:
        return path in self.exact or any(path.startswith(p) for p in self.prefixes)

    def every_path(self) -> tuple[str, ...]:
        return (*self.exact, *self.prefixes)


def anonymous_paths(settings: Settings | None) -> AnonymousPaths:
    """Every path `authenticate_request` answers without a credential -- the one list.

    With `settings`, the ones this configuration opens. With None, every one any configuration
    could open, which is what a plugin's self-authenticating mount may not overlap: the mount check
    and the door read the same rules, so a path added here is reserved there too.

    Probe paths come from one shared set, because kubelet sends no credential and the rate limiter
    has to skip the same paths -- see `PROBE_PATHS` for the incident. `/metrics` is NOT public: its
    counters carry tenant-supplied manifest ids and remote MCP tool names as label values, so an
    anonymous scrape discloses every tenant's manifest and tool names. The API reference is public
    only if the operator says so (`FELIX_DOCS_PUBLIC`): it describes every route, the management
    ones included. `/auth/methods` is public whatever login is configured: it is how an anonymous
    client finds out how to get a credential (not a probe, so the rate limiter still counts it).
    A login's paths are how a caller *gets* a credential, so they cannot require one. A trigger is
    signed by the event's sender, not by a bearer; its route verifies the signature before it reads
    the body and runs as the trigger's own principal (`felix.triggers`). Google's return from
    connecting an account (`google_connect`) is a browser navigation, so it carries no bearer
    either: the sealed flow cookie the authenticated start set is what identifies the person.
    """
    from felix.auth import github, google, google_connect
    from felix.triggers import TRIGGER_PATH_PREFIX, triggers_configured

    every = settings is None
    exact = set(PROBE_PATHS) | {AUTH_METHODS_PATH}
    prefixes = ["/.well-known/"]
    if every or settings.docs_public:
        exact |= DOCS_PATHS
    if every:
        exact |= github.GITHUB_LOGIN_PATHS | github.GITHUB_REDIRECT_PATHS | google.GOOGLE_REDIRECT_PATHS
        exact.add(google_connect.CONNECT_CALLBACK_PATH)
        exact.add(github.GITHUB_ACTIONS_LOGIN_PATH)
    else:
        exact |= github.public_login_paths(settings) | google.public_login_paths(settings)
        exact |= google_connect.public_connect_paths(settings)
    if every or triggers_configured(settings):
        prefixes.append(TRIGGER_PATH_PREFIX)
    return AnonymousPaths(exact=frozenset(exact), prefixes=tuple(prefixes))


def _path_root(path: str) -> str:
    """`/chat/sessions/{id}` -> `/chat`; `""` for the root itself."""
    head = path.strip("/").split("/", 1)[0]
    return f"/{head}" if head else ""


def _mount_defect(mount: object, reserved_roots: frozenset[str]) -> str | None:
    """Why `mount` may not be self-authenticating, completing "which ...", or None if it may be."""
    if not isinstance(mount, str):
        return f"is a {type(mount).__name__}, not a path"
    if not mount.startswith("/") or len(mount) < 2:
        return "must be a path under '/', not the root and not empty"
    if mount.endswith("/"):
        return "must not end with '/'"
    if ".." in mount or "//" in mount or any(c.isspace() for c in mount):
        return "must not contain '..', '//' or whitespace"
    root = _path_root(mount)
    if root in reserved_roots:
        return f"overlaps the core route prefix {root!r}"
    return None


def validate_self_authenticating_mounts(
    mounts: Sequence[tuple[str, object]], core_paths: Sequence[str]
) -> None:
    """Refuse to boot on a plugin mount that would make a core path anonymous.

    `authenticate_request` returns ANONYMOUS for every path equal to a mount or under it, before
    the auth mode is even read. So a plugin declaring `""` opened every route, `/` likewise, and
    `/chat` opened the chat surface. A mount is held to one rule: its first path segment is its
    own. Every core route and every anonymous path (`anonymous_paths(None)`) is reduced to its
    first segment (`/chat/sessions/{id}` to `/chat`), and a mount whose first segment is one of
    those -- equal to a core path, a prefix of one, or under one -- is refused, naming the plugin.

    `mounts` is (plugin name, mount); `core_paths` is the app's routes *before* any plugin router
    is added, so a plugin cannot reserve a root by registering it first.
    """
    core_and_anonymous = [*core_paths, *anonymous_paths(None).every_path()]
    reserved = frozenset(root for root in map(_path_root, core_and_anonymous) if root)
    for plugin, mount in mounts:
        defect = _mount_defect(mount, reserved)
        if defect is not None:
            raise RuntimeError(
                f"plugin {plugin!r} declares self-authenticating mount {mount!r}, which {defect}. "
                "Every request under a self-authenticating mount skips Felix's authentication, "
                "so the API refuses to start with it."
            )


@lru_cache(maxsize=4)
def _parse_api_keys(raw: str) -> dict[str, ApiKeyEntry]:
    """`parse_api_keys`, once per distinct configuration rather than once per request.

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

    `validate_runtime` refuses a malformed list at boot under `api_key`; this is for a caller that
    never ran it, and fails closed. The message is the parser's, which never quotes a key.
    """
    try:
        return parse_api_keys(raw)
    except ValueError as exc:
        logger.error("FELIX_AUTH_API_KEYS %s; no API key will authenticate", exc)
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
    # The path the router matches, with any ASGI `root_path` taken off: a mount, a probe and a
    # route are all spelled without it, so comparing the raw path would test something else.
    path = get_route_path(request.scope)
    auth_header = request.headers.get("authorization") or ""
    mode = settings.auth_mode

    if any(path == m or path.startswith(f"{m}/") for m in self_authenticating_mounts):
        return ANONYMOUS

    if mode == "none":
        return ANONYMOUS

    if anonymous_paths(settings).admits(path):
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
        matched: ApiKeyEntry | None = None
        # No early break. Stopping at the match makes the total time depend on *which*
        # key matched, which is the leak `constant_time_equal` exists to close, one
        # level up. Every configured key is compared on every request either way.
        for key, entry in _parse_api_keys(settings.auth_api_keys).items():
            if constant_time_equal(token, key):
                matched = entry
        if matched is None:
            return JSONResponse(
                {"error": "unauthorized", "reason": "invalid_api_key"},
                status_code=401,
            )
        claimed_tenant = matched.tenant_id
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
                subject=matched.subject,
                tenant_id=claimed_tenant,
                scopes=matched.scopes,
                issuer="api_key",
                scheme="api_key",
            ),
            outbound_token=ANONYMOUS.outbound_token,
            anonymous=False,
            raw_claims=dict(matched.claims),
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
            skill_owner=caller_skill_owner(auth),
            person=caller_person(
                issuer=auth.principal.issuer, subject=auth.principal.subject, anonymous=auth.anonymous
            ),
        )
        req_ctx = RequestContext(
            settings=settings,
            auth=ctx_auth,
            limit_state=LimitState(),
        )
        async with async_run_with_context(req_ctx):
            await self.app(scope, receive, send)


def caller_skill_owner(auth: AuthContext) -> str | None:
    """The personal skill library of the caller ``auth`` verified, or None. Here, because this
    is the last place the principal's issuer is known: the request context keeps the subject
    alone, and a subject is unique only within its issuer."""
    from felix.skills.library_keys import personal_owner

    if auth.anonymous:
        return None
    if auth.principal.scheme == "api_key" and not auth.raw_claims.get("sub"):
        # A key configured without its own `sub` is named `api_key`, like every other such key
        # in the tenant: one library for all of them would be no one's own.
        return None
    return personal_owner(auth.principal.issuer, auth.principal.subject)


def require_authenticated(auth: AuthContext) -> None:
    if auth.anonymous or not auth.principal.subject:
        raise PermissionError("authentication required")


__all__ = [
    "AnonymousPaths",
    "AuthMiddleware",
    "anonymous_paths",
    "authenticate_request",
    "require_authenticated",
    "require_scope",
    "validate_self_authenticating_mounts",
]
