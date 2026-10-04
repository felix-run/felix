"""GitHub login: start a device flow, then trade the approved device code for a Felix token.

Both routes are unauthenticated — they are how a caller gets a credential — and are public
only while `FELIX_GITHUB_CLIENT_ID` is set (`felix.auth.github.public_login_paths`, exactly
these two paths). With login off they answer 404, and under `jwt`/`api_key` the middleware
401s them first.

Starting a flow spends from the OAuth app's own GitHub quota, so `/device` has two hourly
buckets in a limiter store of its own, on top of the global limit: one per client
(`FELIX_GITHUB_DEVICE_STARTS_PER_HOUR`, an IPv6 client keyed by its /64) and one for the whole
deployment (`..._TOTAL`), because a quota every client shares is not protected by a per-client
key. `/token` is polled every few seconds by design and stays under the global limit only.

A `device_code` is a bearer secret until it is redeemed: it is never logged or audited here.

`/actions` is the headless path, public only while `FELIX_GITHUB_OIDC_AUDIENCE` is set: a
workflow posts its GitHub Actions ID token and gets a Felix token back. Verifying it is a local
signature check, so it stays under the global limit only. The ID token is never logged or
audited either; the run it speaks for (repository, ref, workflow file, event, run) is.

`GET /auth/methods` (`methods_router`, mounted at `/auth`) says which of these a client can use,
so a browser can decide whether to offer GitHub login without starting a flow to find out.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from felix.auth.github import (
    AUTH_METHODS_PATH,
    GitHubLoginError,
    LoginErrorCode,
    LoginToken,
    actions_enabled,
    authorize_url,
    exchange_device_code,
    exchange_web_code,
    is_enabled,
    redirect_enabled,
    redirect_origins,
    start_device_flow,
)
from felix.config import Settings
from pydantic import BaseModel, Field

logger = logging.getLogger("felix_api.auth_github")

router = APIRouter(tags=["Auth"])
methods_router = APIRouter(tags=["Auth"])
# `/github/connection(s)`: a person's stored GitHub connection, and the operator's view of them.
connection_router = APIRouter(tags=["Auth"])

# The redirect sign-in's two cookies. Both are sealed with FELIX_GITHUB_TOKEN_KEY (so nothing in
# them is readable or forgeable by the browser) and HttpOnly (so nothing in the page can read them).
FLOW_COOKIE = "felix_github_flow"
HANDOFF_COOKIE = "felix_github_handoff"
# From `/authorize` to the callback: how long a person may spend on GitHub's screen.
FLOW_TTL_S = 600
# From the callback to the page collecting its token: one page load.
HANDOFF_TTL_S = 60

DEVICE_START_WINDOW_S = 3600
# One IPv6 subscriber is routinely handed a /64; keyed per address they would be 2**64 clients.
DEVICE_START_IPV6_PREFIX = 64


class DeviceStart(BaseModel):
    """Show `user_code` and `verification_uri` to the person; poll `/token` every `interval` s."""

    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


class TokenRequest(BaseModel):
    model_config = {"extra": "forbid"}

    device_code: str = Field(min_length=1, max_length=256)
    # Required only when membership grants more than one tenant (409 `tenant_ambiguous`).
    tenant: str | None = Field(default=None, min_length=1, max_length=128)


class ActionsTokenRequest(BaseModel):
    model_config = {"extra": "forbid"}

    # The workflow's ID token, requested with `audience` = FELIX_GITHUB_OIDC_AUDIENCE.
    id_token: str = Field(min_length=1, max_length=8192)
    # Required only when the workflow is granted more than one tenant (409 `tenant_ambiguous`).
    tenant: str | None = Field(default=None, min_length=1, max_length=128)


class LoginTokenOut(BaseModel):
    access_token: str
    token_type: Literal["Bearer"] = "Bearer"
    expires_in: int
    tenant: str
    scopes: list[str]
    # The GitHub user who logged in; empty from `/actions`, because a workflow is not a person.
    github_login: str = ""


class AuthMethodsOut(BaseModel):
    """How a caller can get a credential here, and whether it needs one."""

    github_device: bool
    github_redirect: bool
    bearer_required: bool


class LoginErrorOut(BaseModel):
    """Every refusal. `interval` accompanies 428; `tenants` accompanies 409."""

    error: LoginErrorCode
    message: str
    interval: int | None = None
    tenants: list[str] | None = None


_ERRORS: dict[int | str, dict[str, Any]] = {
    status: {"model": LoginErrorOut} for status in (400, 403, 409, 428, 429, 502, 503)
}
_ACTIONS_ERRORS: dict[int | str, dict[str, Any]] = {
    status: {"model": LoginErrorOut} for status in (401, 403, 409, 502)
}


def _settings_if_enabled(request: Request) -> Settings:
    settings = request.app.state.settings
    if not is_enabled(settings):
        raise HTTPException(status_code=404, detail="not_found")
    return settings


def _refusal(exc: GitHubLoginError) -> JSONResponse:
    body = LoginErrorOut(
        error=exc.code,
        message=str(exc),
        interval=exc.interval,
        tenants=list(exc.tenants) if exc.tenants else None,
    )
    headers = {"retry-after": str(exc.interval)} if exc.interval is not None else None
    return JSONResponse(body.model_dump(exclude_none=True), status_code=exc.status, headers=headers)


def _start_refused(message: str) -> JSONResponse:
    return _refusal(GitHubLoginError(LoginErrorCode.RATE_LIMITED, message, interval=DEVICE_START_WINDOW_S))


def _warn_cap_reached(request: Request, settings: Settings) -> None:
    """Once per window, not per refusal: under the attack the cap exists for, every refused start
    would otherwise be a WARNING line, and the attacker chooses how many there are."""
    now = time.monotonic()
    last = getattr(request.app.state, "github_device_cap_warned_at", None)
    if last is not None and now - last < DEVICE_START_WINDOW_S:
        return
    request.app.state.github_device_cap_warned_at = now
    logger.warning(
        "github device-flow starts hit the deployment cap (%d/h); every new login is refused until "
        "the window turns. Many clients are starting flows, or the cap is too low",
        settings.github_device_starts_per_hour_total,
    )


@router.post("/device", response_model=DeviceStart, responses=_ERRORS)
async def start_github_login(request: Request) -> Any:
    from felix.security.rate_limit import client_key

    settings = _settings_if_enabled(request)
    limiter = request.app.state.github_device_limiter
    # Per client first, so a start refused there never spends from the deployment's total.
    if not await limiter.hit(
        f"github-device:{client_key(request, settings, ipv6_prefix=DEVICE_START_IPV6_PREFIX)}",
        limit=settings.github_device_starts_per_hour,
        window_seconds=DEVICE_START_WINDOW_S,
    ):
        return _start_refused("too many logins started from this address; try again later")
    if not await limiter.hit(
        "github-device:*",
        limit=settings.github_device_starts_per_hour_total,
        window_seconds=DEVICE_START_WINDOW_S,
    ):
        _warn_cap_reached(request, settings)
        return _start_refused("too many logins are being started on this server; try again later")
    try:
        code = await start_device_flow(settings)
    except GitHubLoginError as exc:
        logger.warning("github device flow start failed: %s", exc.code)
        return _refusal(exc)
    return DeviceStart.model_validate(code, from_attributes=True)


@router.post("/token", response_model=LoginTokenOut, responses=_ERRORS)
async def redeem_github_login(body: TokenRequest, request: Request) -> Any:
    settings = _settings_if_enabled(request)
    try:
        minted = await exchange_device_code(settings, body.device_code, tenant=body.tenant)
    except GitHubLoginError as exc:
        return _refusal(exc)
    await _after_login(settings, minted, method="device")
    return LoginTokenOut(
        access_token=minted.access_token,
        expires_in=minted.expires_in,
        tenant=minted.tenant,
        scopes=list(minted.scopes),
        github_login=minted.github_login,
    )


@router.post("/actions", response_model=LoginTokenOut, responses=_ACTIONS_ERRORS)
async def redeem_github_actions_login(body: ActionsTokenRequest, request: Request) -> Any:
    from felix.audit import store as audit_store
    from felix.auth.github_actions import exchange_actions_token

    settings = request.app.state.settings
    if not actions_enabled(settings):
        raise HTTPException(status_code=404, detail="not_found")
    try:
        login = await exchange_actions_token(settings, body.id_token, tenant=body.tenant)
    except GitHubLoginError as exc:
        return _refusal(exc)
    minted = login.token
    audit_store.record_event(
        settings,
        minted.tenant,
        "github_actions_login",
        principal_subj=minted.subject,
        status="minted",
        payload={**login.run.audit(), "scopes": list(minted.scopes), "expires_in": minted.expires_in},
    )
    return LoginTokenOut(
        access_token=minted.access_token,
        expires_in=minted.expires_in,
        tenant=minted.tenant,
        scopes=list(minted.scopes),
    )


@methods_router.get(AUTH_METHODS_PATH.removeprefix("/auth"), response_model=AuthMethodsOut)
async def auth_methods(request: Request) -> AuthMethodsOut:
    """Which ways in this deployment offers, for a client that holds no credential yet.

    Public in every auth mode (`felix.auth.middleware`), because it is how an anonymous caller
    learns how to get a credential. It reads settings only — no database, no call to GitHub —
    and sits under the global rate limit like any other request; asking it never starts a
    device flow or spends from `/device`'s hourly budget.

    - `github_device`: `POST /auth/github/device` and `/token` are served (login configured).
    - `github_redirect`: browser sign-in by redirect is served (`GET /auth/github/authorize`).
    - `bearer_required`: this deployment verifies bearer credentials (`auth_mode` is not
      `none`). A proxy in front of the harness that would accept a browser's own
      `Authorization: Bearer` in place of its shared key must accept it only while this is
      true: under `none` the harness verifies nothing, so any bearer at all would walk past the
      proxy's only lock.
    """
    settings: Settings = request.app.state.settings
    return AuthMethodsOut(
        github_device=is_enabled(settings),
        github_redirect=redirect_enabled(settings),
        bearer_required=settings.auth_mode != "none",
    )


def _token_out(minted: LoginToken) -> LoginTokenOut:
    return LoginTokenOut(
        access_token=minted.access_token,
        expires_in=minted.expires_in,
        tenant=minted.tenant,
        scopes=list(minted.scopes),
        github_login=minted.github_login,
    )


async def _after_login(settings: Settings, minted: LoginToken, *, method: str) -> None:
    """What every person's sign-in does once Felix has minted: audit it, and keep the GitHub
    connection when there is one worth keeping (`github_connections.save_connection`)."""
    from felix.audit import store as audit_store
    from felix.auth import github_connections

    # Audited in the tenant the token is for: that tenant's operators are the ones who need
    # to see who logged in to it, with what. A refusal has no tenant and is logged instead.
    audit_store.record_event(
        settings,
        minted.tenant,
        "github_login",
        principal_subj=minted.subject,
        status="minted",
        payload={
            "github_login": minted.github_login,
            "scopes": list(minted.scopes),
            "expires_in": minted.expires_in,
            "method": method,
        },
    )
    if minted.github is None:
        return
    stored = await github_connections.save_connection(
        settings,
        minted.tenant,
        github_user_id=minted.github_user_id,
        github_login=minted.github_login,
        grant=minted.github,
        principal_subj=minted.subject,
    )
    if stored is not None:
        github_connections.record_stored(settings, minted.tenant, stored, minted.subject)


# --- browser sign-in by redirect ------------------------------------------------------------
#
# `/authorize` → github.com → `/callback` → the app, which collects its token at `/exchange`.
# The harness serves no pages, so every outcome of the callback is a redirect back to the app
# with a fragment (`#felix_login=ok` or `#felix_login_error=<code>`); a fragment never reaches a
# server or a log. The token itself never appears in a URL: the callback leaves it in a sealed
# HttpOnly cookie that lives for one page load, and `/exchange` hands it to the page and clears it.


def _redirect_settings(request: Request) -> Settings:
    settings = request.app.state.settings
    if not redirect_enabled(settings):
        raise HTTPException(status_code=404, detail="not_found")
    return settings


def _return_target(settings: Settings, return_to: str) -> tuple[str, str]:
    """`return_to` checked against FELIX_GITHUB_REDIRECT_ORIGINS, as (origin, the URL itself).
    Raises 400 for anything else: this is the only redirect target a caller can name."""
    from urllib.parse import urlsplit

    parts = urlsplit(return_to)
    origin = f"{parts.scheme}://{parts.netloc}"
    if not parts.scheme or not parts.netloc or origin not in redirect_origins(settings):
        raise HTTPException(status_code=400, detail="return_to_not_allowed")
    if not parts.path.startswith("/") or parts.path.startswith("//"):
        raise HTTPException(status_code=400, detail="return_to_not_allowed")
    # The fragment is ours to write on the way back.
    return origin, return_to.split("#", 1)[0]


def _cookie(response: Any, name: str, value: str, *, max_age: int, secure: bool, same_site: str) -> None:
    response.set_cookie(
        name, value, max_age=max_age, path="/", secure=secure, httponly=True, samesite=same_site
    )


def _clear(response: Any, name: str, *, secure: bool) -> None:
    """Expire a sign-in cookie, with the same attributes it was set with."""
    response.delete_cookie(name, path="/", secure=secure, httponly=True, samesite="lax")


def _back(return_to: str, fragment: str) -> Any:
    from fastapi.responses import RedirectResponse

    return RedirectResponse(f"{return_to}#{fragment}", status_code=302)


@router.get("/authorize", include_in_schema=True, status_code=302)
async def start_github_redirect(request: Request, return_to: str, tenant: str | None = None) -> Any:
    """Send a browser to GitHub to sign in, returning to `return_to` (an allowed origin).

    `state` and the PKCE verifier live in a sealed HttpOnly cookie, so only the browser that
    started this sign-in can finish it: a callback carrying someone else's `state` is refused.
    """
    import base64
    import hashlib
    import secrets as pysecrets

    from fastapi.responses import RedirectResponse
    from felix.auth.github_connections import seal_json

    settings = _redirect_settings(request)
    origin, target = _return_target(settings, return_to)
    state = pysecrets.token_urlsafe(32)
    verifier = pysecrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    redirect_uri = f"{origin}{settings.github_callback_path}"
    flow = {
        "s": state,
        "v": verifier,
        "r": target,
        "o": origin,
        "t": tenant or "",
        "e": int(time.time()) + FLOW_TTL_S,
    }
    response = RedirectResponse(
        authorize_url(settings, redirect_uri=redirect_uri, state=state, code_challenge=challenge),
        status_code=302,
    )
    # Lax: the callback is a top-level navigation back from github.com, which Lax lets through.
    _cookie(
        response,
        FLOW_COOKIE,
        seal_json(settings, "github-sign-in-flow", flow),
        max_age=FLOW_TTL_S,
        secure=target.startswith("https://"),
        same_site="lax",
    )
    return response


@router.get("/callback", include_in_schema=True, status_code=302)
async def finish_github_redirect(
    request: Request,
    state: str = "",
    code: str = "",
    error: str = "",
) -> Any:
    """GitHub's return. Checks `state` against this browser's flow cookie, exchanges the code,
    mints, and redirects back to the app with the token waiting in a one-load cookie."""
    import hmac
    from urllib.parse import quote

    from felix.auth.github_connections import SealError, seal_json, unseal_json

    settings = _redirect_settings(request)
    sealed = request.cookies.get(FLOW_COOKIE, "")
    try:
        flow = unseal_json(settings, "github-sign-in-flow", sealed) if sealed else None
    except SealError:
        flow = None
    if flow is None or int(flow.get("e", 0)) < int(time.time()):
        # No flow this browser started (or one long expired): there is nowhere known to send the
        # person back to, and nothing here may be trusted to name one.
        return JSONResponse(
            {"error": "sign_in_expired", "message": "start signing in again"}, status_code=400
        )
    target = str(flow["r"])
    if not state or not hmac.compare_digest(state, str(flow["s"])):
        logger.warning("github sign-in callback with a state this browser did not start")
        response = _back(target, "felix_login_error=state_mismatch")
        _clear(response, FLOW_COOKIE, secure=target.startswith("https://"))
        return response
    if error or not code:
        # `access_denied` when the person cancelled on GitHub's screen.
        reason = "access_denied" if error == "access_denied" else "github_config_error"
        response = _back(target, f"felix_login_error={reason}")
        _clear(response, FLOW_COOKIE, secure=target.startswith("https://"))
        return response
    try:
        minted = await exchange_web_code(
            settings,
            code,
            code_verifier=str(flow["v"]),
            redirect_uri=f"{flow['o']}{settings.github_callback_path}",
            tenant=str(flow.get("t") or "") or None,
        )
    except GitHubLoginError as exc:
        fragment = f"felix_login_error={exc.code.value}"
        if exc.tenants:
            fragment += "&tenants=" + quote(",".join(exc.tenants))
        response = _back(target, fragment)
        _clear(response, FLOW_COOKIE, secure=target.startswith("https://"))
        return response
    await _after_login(settings, minted, method="redirect")
    handoff = {"tok": _token_out(minted).model_dump(), "e": int(time.time()) + HANDOFF_TTL_S}
    response = _back(target, "felix_login=ok")
    _clear(response, FLOW_COOKIE, secure=target.startswith("https://"))
    # Strict: only the app's own same-site request (`/exchange`) carries it.
    _cookie(
        response,
        HANDOFF_COOKIE,
        seal_json(settings, "github-sign-in-handoff", handoff),
        max_age=HANDOFF_TTL_S,
        secure=target.startswith("https://"),
        same_site="strict",
    )
    return response


@router.post("/exchange", response_model=LoginTokenOut, responses={400: {"model": LoginErrorOut}})
async def collect_github_redirect(request: Request) -> Any:
    """The app collects the token its sign-in left waiting, once. The cookie is cleared whether
    or not it opened, so a second call — a reload, a replay — finds nothing."""
    from felix.auth.github_connections import SealError, unseal_json

    settings = _redirect_settings(request)
    sealed = request.cookies.get(HANDOFF_COOKIE, "")
    try:
        handoff = unseal_json(settings, "github-sign-in-handoff", sealed) if sealed else None
    except SealError:
        handoff = None
    if handoff is None or int(handoff.get("e", 0)) < int(time.time()):
        response = JSONResponse(
            {"error": "expired_token", "message": "no sign-in is waiting; start signing in again"},
            status_code=400,
        )
    else:
        response = JSONResponse(LoginTokenOut.model_validate(handoff["tok"]).model_dump())
    # The request does not say whether it came over https; Secure on a clearing cookie only
    # narrows where the browser accepts it, so set it whenever this deployment is https-only.
    _clear(response, HANDOFF_COOKIE, secure=all(o.startswith("https://") for o in redirect_origins(settings)))
    return response


# --- a person's stored GitHub connection ------------------------------------------------------


class GitHubConnectionOut(BaseModel):
    """Who the connection is for and whether it still works. Never a token."""

    github_user_id: int
    github_login: str
    status: Literal["active", "revoked"]
    created_at: int
    updated_at: int
    refresh_expires_at: int


class GitHubConnectionState(BaseModel):
    connected: bool
    connection: GitHubConnectionOut | None = None


class GitHubConnectionList(BaseModel):
    items: list[GitHubConnectionOut]


def _github_user_id(request: Request) -> tuple[str, int, str]:
    """The caller's tenant, GitHub user id and subject; 403 for a principal that is not a person
    who signed in with GitHub (an API key, a workflow, an operator's minted token)."""
    from felix.auth.mgmt import auth_from_request

    principal = auth_from_request(request).principal
    subject = principal.subject or ""
    prefix, _, raw = subject.partition(":")
    if prefix != "github" or not raw.isdigit():
        raise HTTPException(status_code=403, detail="not_a_github_sign_in")
    return principal.tenant_id, int(raw), subject


@connection_router.get("/connection", response_model=GitHubConnectionState)
async def my_github_connection(request: Request) -> GitHubConnectionState:
    """Whether Felix holds a GitHub connection for you in this tenant."""
    from felix.auth import github_connections

    tenant, user_id, _ = _github_user_id(request)
    row = await github_connections.get_connection(request.app.state.settings, tenant, user_id)
    return GitHubConnectionState(
        connected=row is not None and row["status"] == "active",
        connection=GitHubConnectionOut.model_validate(row) if row else None,
    )


@connection_router.delete("/connection", status_code=204)
async def remove_my_github_connection(request: Request) -> None:
    """Forget your GitHub connection here and withdraw the App's authorization at GitHub.
    Signing out of chat-ui calls this. Your Felix token is unaffected until it expires."""
    from felix.auth import github_connections

    tenant, user_id, subject = _github_user_id(request)
    await github_connections.remove_connection(
        request.app.state.settings, tenant, user_id, principal_subj=subject
    )


@connection_router.get("/connections", response_model=GitHubConnectionList)
async def list_github_connections(request: Request) -> GitHubConnectionList:
    """Every stored GitHub connection in your tenant (`github:admin`)."""
    from felix.auth import github_connections
    from felix.auth.mgmt import auth_from_request, require_mgmt_scopes

    require_mgmt_scopes(request, "github:admin")
    tenant = auth_from_request(request).principal.tenant_id
    rows = await github_connections.list_connections(request.app.state.settings, tenant)
    return GitHubConnectionList(items=[GitHubConnectionOut.model_validate(r) for r in rows])


@connection_router.delete("/connections/{github_user_id}", status_code=204)
async def remove_github_connection(github_user_id: int, request: Request) -> None:
    """Revoke anyone's GitHub connection in your tenant (`github:admin`)."""
    from felix.auth import github_connections
    from felix.auth.mgmt import auth_from_request, require_mgmt_scopes

    require_mgmt_scopes(request, "github:admin")
    principal = auth_from_request(request).principal
    found = await github_connections.remove_connection(
        request.app.state.settings,
        principal.tenant_id,
        github_user_id,
        principal_subj=principal.subject,
    )
    if not found:
        raise HTTPException(status_code=404, detail="not_found")
