"""GitHub login: start a device flow, then trade the approved device code for a Felix token.

Both routes are unauthenticated — they are how a caller gets a credential — and are public
only while `FELIX_GITHUB_CLIENT_ID` is set (`felix.auth.middleware._is_public_path`). With
login off they answer 404, and under `jwt`/`api_key` the middleware 401s them first.

Starting a flow spends from the OAuth app's own GitHub quota, so `/device` has a per-client
bucket of its own (`FELIX_GITHUB_DEVICE_STARTS_PER_HOUR`) on top of the global limit; `/token`
is polled every few seconds by design and stays under the global limit only.

A `device_code` is a bearer secret until it is redeemed: it is never logged or audited here.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from felix.auth.github import (
    GitHubLoginError,
    LoginErrorCode,
    exchange_device_code,
    is_enabled,
    start_device_flow,
)
from pydantic import BaseModel, Field

logger = logging.getLogger("felix_api.auth_github")

router = APIRouter(tags=["Auth"])

DEVICE_START_WINDOW_S = 3600


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


class LoginTokenOut(BaseModel):
    access_token: str
    token_type: Literal["Bearer"] = "Bearer"
    expires_in: int
    tenant: str
    scopes: list[str]


class LoginErrorOut(BaseModel):
    """Every refusal. `interval` accompanies 428; `tenants` accompanies 409."""

    error: LoginErrorCode
    message: str
    interval: int | None = None
    tenants: list[str] | None = None


_ERRORS: dict[int | str, dict[str, Any]] = {
    status: {"model": LoginErrorOut} for status in (400, 403, 409, 428, 502, 503)
}


def _settings_if_enabled(request: Request) -> Any:
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


@router.post("/device", response_model=DeviceStart, responses={429: {}, **_ERRORS})
async def start_github_login(request: Request) -> Any:
    from felix.security.rate_limit import client_key

    settings = _settings_if_enabled(request)
    limiter = request.app.state.rate_limit_config.backend
    allowed = await limiter.hit(
        f"github-device:{client_key(request, settings)}",
        limit=settings.github_device_starts_per_hour,
        window_seconds=DEVICE_START_WINDOW_S,
    )
    if not allowed:
        return JSONResponse(
            {"error": "rate_limited"},
            status_code=429,
            headers={"retry-after": str(DEVICE_START_WINDOW_S)},
        )
    try:
        code = await start_device_flow(settings)
    except GitHubLoginError as exc:
        logger.warning("github device flow start failed: %s", exc.code)
        return _refusal(exc)
    return DeviceStart(
        device_code=code.device_code,
        user_code=code.user_code,
        verification_uri=code.verification_uri,
        expires_in=code.expires_in,
        interval=code.interval,
    )


@router.post("/token", response_model=LoginTokenOut, responses=_ERRORS)
async def redeem_github_login(body: TokenRequest, request: Request) -> Any:
    from felix.audit import store as audit_store

    settings = _settings_if_enabled(request)
    try:
        minted = await exchange_device_code(settings, body.device_code, tenant=body.tenant)
    except GitHubLoginError as exc:
        return _refusal(exc)
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
        },
    )
    return LoginTokenOut(
        access_token=minted.access_token,
        expires_in=minted.expires_in,
        tenant=minted.tenant,
        scopes=list(minted.scopes),
    )
