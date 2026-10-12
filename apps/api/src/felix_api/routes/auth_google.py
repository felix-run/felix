"""Google sign-in by redirect: `/authorize` → Google → `/callback` → the app collects at `/exchange`.

Public only while `felix.auth.google.redirect_enabled` (client id, client secret, cookie key and
a redirect origin all set), and only at exactly these three paths; otherwise they answer 404, and
under `jwt` the middleware 401s them first. The shape — cookies, fragments, the one-load handoff —
is `_login_redirect`'s, shared with GitHub's.

Only the accounts in `FELIX_GOOGLE_ACCOUNTS` get a token. A refused account comes back to the app
as `#felix_login_error=not_allowed` and is logged with its Google id for the operator.
"""

from __future__ import annotations

import hmac
import logging
import secrets
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from felix.auth import google
from felix.auth.github import LoginToken
from felix.auth.github_connections import SealError, seal_json, unseal_json
from felix.config import Settings

from felix_api.routes._login_redirect import (
    FLOW_TTL_S,
    HANDOFF_TTL_S,
    LoginErrorOut,
    LoginTokenOut,
    back,
    clear_cookie,
    pkce_pair,
    return_target,
    set_cookie,
)

logger = logging.getLogger("felix_api.auth_google")

router = APIRouter(tags=["Auth"])

# Sealed with FELIX_GOOGLE_COOKIE_KEY and HttpOnly, like GitHub's pair.
FLOW_COOKIE = "felix_google_flow"
HANDOFF_COOKIE = "felix_google_handoff"
_FLOW_PURPOSE = "google-sign-in-flow"
_HANDOFF_PURPOSE = "google-sign-in-handoff"


def _settings(request: Request) -> Settings:
    settings = request.app.state.settings
    if not google.redirect_enabled(settings):
        raise HTTPException(status_code=404, detail="not_found")
    return settings


def _seal(settings: Settings, purpose: str, data: dict[str, Any]) -> str:
    return seal_json(settings, purpose, data, key=google.cookie_key(settings))


def _unseal(settings: Settings, purpose: str, value: str) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        data = unseal_json(settings, purpose, value, key=google.cookie_key(settings))
    except SealError:
        return None
    return data if int(data.get("e", 0)) >= int(time.time()) else None


def _https(url: str) -> bool:
    return url.startswith("https://")


def _signed_in(settings: Settings, target: str, minted: LoginToken, account: google.GoogleAccount) -> Any:
    """Audit the sign-in, then send the browser back with the token waiting in a one-load cookie."""
    from felix.audit import store as audit_store

    # Audited in the tenant the token is for; a refusal has no tenant and is logged instead.
    audit_store.record_event(
        settings,
        minted.tenant,
        "google_login",
        principal_subj=minted.subject,
        status="minted",
        payload={
            "email": account.email,
            "scopes": list(minted.scopes),
            "manifests": list(account.manifests) if account.manifests is not None else None,
            "expires_in": minted.expires_in,
        },
    )
    token = LoginTokenOut(
        access_token=minted.access_token,
        expires_in=minted.expires_in,
        tenant=minted.tenant,
        scopes=list(minted.scopes),
        email=minted.email,
        name=minted.display_name,
        picture=minted.avatar_url,
    )
    handoff = {"tok": token.model_dump(), "e": int(time.time()) + HANDOFF_TTL_S}
    response = back(target, "felix_login=ok")
    clear_cookie(response, FLOW_COOKIE, secure=_https(target))
    # Strict: only the app's own same-site request (`/exchange`) carries it.
    set_cookie(
        response,
        HANDOFF_COOKIE,
        _seal(settings, _HANDOFF_PURPOSE, handoff),
        max_age=HANDOFF_TTL_S,
        secure=_https(target),
        same_site="strict",
    )
    return response


@router.get("/authorize", include_in_schema=True, status_code=302)
async def start_google_redirect(request: Request, return_to: str) -> Any:
    """Send a browser to Google to sign in, returning to `return_to` (an allowed origin).

    `state`, the `nonce` and the PKCE verifier live in a sealed HttpOnly cookie, so only the
    browser that started this sign-in can finish it.
    """
    settings = _settings(request)
    origin, target = return_target(google.redirect_origins(settings), return_to)
    state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    verifier, challenge = pkce_pair()
    flow = {
        "s": state,
        "n": nonce,
        "v": verifier,
        "r": target,
        "o": origin,
        "e": int(time.time()) + FLOW_TTL_S,
    }
    response = RedirectResponse(
        google.authorize_url(
            settings,
            redirect_uri=f"{origin}{settings.google_callback_path}",
            state=state,
            nonce=nonce,
            code_challenge=challenge,
        ),
        status_code=302,
    )
    # Lax: the callback is a top-level GET navigation back from accounts.google.com.
    set_cookie(
        response,
        FLOW_COOKIE,
        _seal(settings, _FLOW_PURPOSE, flow),
        max_age=FLOW_TTL_S,
        secure=_https(target),
        same_site="lax",
    )
    return response


@router.get("/callback", include_in_schema=True, status_code=302)
async def finish_google_redirect(request: Request, state: str = "", code: str = "", error: str = "") -> Any:
    """Google's return. Checks `state` against this browser's flow cookie, exchanges the code,
    verifies the ID token, checks the allowlist, mints, and redirects back to the app with the
    token waiting in a one-load cookie."""
    settings = _settings(request)
    flow = _unseal(settings, _FLOW_PURPOSE, request.cookies.get(FLOW_COOKIE, ""))
    if flow is None:
        # No flow this browser started (or one long expired): there is nowhere known to send the
        # person back to, and nothing here may be trusted to name one.
        return JSONResponse(
            {"error": "sign_in_expired", "message": "start signing in again"}, status_code=400
        )
    target = str(flow["r"])

    def refuse(reason: str) -> Any:
        response = back(target, f"felix_login_error={reason}")
        clear_cookie(response, FLOW_COOKIE, secure=_https(target))
        return response

    if not state or not hmac.compare_digest(state, str(flow["s"])):
        logger.warning("google sign-in callback with a state this browser did not start")
        return refuse("state_mismatch")
    if error or not code:
        # `access_denied` when the person cancelled on Google's screen.
        return refuse("access_denied" if error == "access_denied" else "google_config_error")
    try:
        minted, account = await google.exchange_code(
            settings,
            code,
            code_verifier=str(flow["v"]),
            redirect_uri=f"{flow['o']}{settings.google_callback_path}",
            nonce=str(flow["n"]),
        )
    except google.GoogleLoginError as exc:
        return refuse(exc.code)
    return _signed_in(settings, target, minted, account)


@router.post("/exchange", response_model=LoginTokenOut, responses={400: {"model": LoginErrorOut}})
async def collect_google_redirect(request: Request) -> Any:
    """The app collects the token its sign-in left waiting, once. The cookie is cleared whether
    or not it opened, so a second call — a reload, a replay — finds nothing."""
    settings = _settings(request)
    handoff = _unseal(settings, _HANDOFF_PURPOSE, request.cookies.get(HANDOFF_COOKIE, ""))
    if handoff is None:
        response = JSONResponse(
            {"error": "expired_token", "message": "no sign-in is waiting; start signing in again"},
            status_code=400,
        )
    else:
        response = JSONResponse(LoginTokenOut.model_validate(handoff["tok"]).model_dump())
    # The request does not say whether it came over https; Secure on a clearing cookie only
    # narrows where the browser accepts it, so set it whenever this deployment is https-only.
    clear_cookie(response, HANDOFF_COOKIE, secure=all(_https(o) for o in google.redirect_origins(settings)))
    return response
