"""A person's stored connections to other services, and an operator's view of everyone's.

`GET /connections` and `DELETE /connections/{provider}/{account}` are the caller's own: who they
have connected, and a way to disconnect, which withdraws the grant at the provider too. The
tenant's listing and revocation (`/connections/tenant`) need `connections:admin`. No route here
returns a credential; storing one is the business of each provider's consent flow
(`felix.auth.connections`).

Google's is here, in three steps. `POST /connections/google/start` (the caller, authenticated)
answers with the URL to send the browser to and sets a sealed flow cookie naming them. Google
returns the browser to `GET /connections/google/callback` -- public, since a navigation carries no
bearer -- which checks that cookie, exchanges the code, and leaves the grant sealed in a one-load
cookie, storing nothing. The app then calls `POST /connections/google/finish` as the person, which
keeps it only for the person who started (`felix.auth.google_connect`). The callback's outcome
goes back to the app as a fragment: `#felix_connect=pending` or `#felix_connect_error=<code>`.
"""

from __future__ import annotations

import hmac
import logging
import secrets
import time
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

logger = logging.getLogger("felix_api.connections")

router = APIRouter(tags=["Connections"])


class ConnectionOut(BaseModel):
    """Who, what and whether it still works. Never a credential."""

    provider: str
    account: str
    label: str
    kind: Literal["oauth", "secret"]
    status: Literal["active", "revoked"]
    scopes: list[str]
    created_at: int
    updated_at: int
    # When the stored grant itself expires (epoch ms); 0 when the provider did not say.
    expires_at: int


class ConnectionList(BaseModel):
    items: list[ConnectionOut]


class TenantConnectionOut(ConnectionOut):
    # `issuer|subject`.
    person: str


class TenantConnectionList(BaseModel):
    items: list[TenantConnectionOut]


class RevokeConnection(BaseModel):
    person: str = Field(min_length=1, description="`issuer|subject`, as the tenant listing shows it.")
    provider: str = Field(min_length=1)
    account: str = Field(min_length=1)


def _person(request: Request) -> tuple[str, str]:
    from felix.auth.mgmt import request_person

    return request_person(request)


# --- Google ---------------------------------------------------------------------------------

_GOOGLE_FLOW = "felix_google_connect_flow"
_GOOGLE_HANDOFF = "felix_google_connect_handoff"
_GOOGLE_FLOW_PURPOSE = "google-connect-flow"
_GOOGLE_HANDOFF_PURPOSE = "google-connect-handoff"
# From Google's return to the app asking to keep the grant: one page load.
GOOGLE_HANDOFF_TTL_S = 120


def cookie_name(base: str, *, secure: bool) -> str:
    """`__Host-` over https: such a cookie is only ever set by this host, on `/`, Secure -- no
    sibling subdomain and no plain-http answer can plant one."""
    return f"__Host-{base}" if secure else base


def _read_cookie(request: Request, settings: Any, base: str) -> str:
    """Only the `__Host-` name where every origin is https: reading the bare one too would let a
    planted cookie in through the door the prefix closes. The bare name is for http (dev)."""
    if _secure(settings):
        return request.cookies.get(cookie_name(base, secure=True), "")
    return request.cookies.get(cookie_name(base, secure=True)) or request.cookies.get(base, "")


class GoogleConnectStart(BaseModel):
    return_to: str = Field(description="Where the browser comes back to; an allowed origin.")
    scopes: list[str] = Field(
        min_length=1,
        description="What to connect: calendar_read, calendar_events, gmail_read, gmail_compose, drive_read.",
    )


class GoogleConnectUrl(BaseModel):
    authorize_url: str


def _google_settings(request: Request) -> Any:
    from felix.auth.google_connect import connect_enabled

    settings = request.app.state.settings
    if not connect_enabled(settings):
        raise HTTPException(status_code=404, detail="not_found")
    return settings


def _google_seal(settings: Any, purpose: str, data: dict[str, Any]) -> str:
    from felix.auth import google
    from felix.auth.github_connections import seal_json

    return seal_json(settings, purpose, data, key=google.cookie_key(settings))


def _google_unseal(settings: Any, purpose: str, value: str) -> dict[str, Any] | None:
    from felix.auth import google
    from felix.auth.github_connections import SealError, unseal_json

    if not value:
        return None
    try:
        data = unseal_json(settings, purpose, value, key=google.cookie_key(settings))
    except SealError:
        return None
    return data if int(data.get("e", 0)) >= int(time.time()) else None


def _secure(settings: Any) -> bool:
    from felix.auth import google

    return all(o.startswith("https://") for o in google.redirect_origins(settings))


@router.post("/google/start", response_model=GoogleConnectUrl)
async def start_google_connect(body: GoogleConnectStart, request: Request) -> Any:
    """Begin connecting your Google account. Send the browser to `authorize_url`; this browser
    alone can come back, by the sealed cookie this response sets, and you alone can keep what it
    brings (`/google/finish`)."""
    from felix.auth import google
    from felix.auth.google_connect import consent_url, scope_urls

    from felix_api.routes._login_redirect import FLOW_TTL_S, pkce_pair, return_target, set_cookie

    settings = _google_settings(request)
    tenant, person = _person(request)
    origin, target = return_target(google.redirect_origins(settings), body.return_to)
    try:
        scope_urls(body.scopes)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="unknown_scope") from exc
    state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    verifier, challenge = pkce_pair()
    redirect_uri = f"{origin}{settings.google_connect_callback_path}"
    flow = {
        "s": state,
        "n": nonce,
        "v": verifier,
        "r": target,
        "u": redirect_uri,
        "t": tenant,
        "p": person,
        "e": int(time.time()) + FLOW_TTL_S,
    }
    url = consent_url(
        settings,
        redirect_uri=redirect_uri,
        state=state,
        nonce=nonce,
        code_challenge=challenge,
        scopes=body.scopes,
    )
    response = JSONResponse(GoogleConnectUrl(authorize_url=url).model_dump())
    secure = target.startswith("https://")
    # Lax: the callback is a top-level GET navigation back from accounts.google.com.
    set_cookie(
        response,
        cookie_name(_GOOGLE_FLOW, secure=secure),
        _google_seal(settings, _GOOGLE_FLOW_PURPOSE, flow),
        max_age=FLOW_TTL_S,
        secure=secure,
        same_site="lax",
    )
    return response


@router.get("/google/callback", status_code=302)
async def return_from_google(request: Request, state: str = "", code: str = "", error: str = "") -> Any:
    """Google's return. Checks `state` against this browser's flow cookie, exchanges the code and
    verifies who the Google account is -- and stores nothing. The grant waits, sealed, in a
    one-load cookie for the app to keep with `/google/finish` as the person who started; a flow
    cookie planted in someone else's browser therefore never puts their grant in its owner's vault.
    """
    from felix.auth.google_connect import ConnectError, Flow, exchange

    from felix_api.routes._login_redirect import back, clear_cookie, set_cookie

    settings = _google_settings(request)
    flow = _google_unseal(settings, _GOOGLE_FLOW_PURPOSE, _read_cookie(request, settings, _GOOGLE_FLOW))
    if flow is None:
        # No flow this browser started (or one long expired): nowhere known to send it back to.
        return JSONResponse(
            {"error": "connect_expired", "message": "start connecting again"}, status_code=400
        )
    target = str(flow["r"])
    secure = target.startswith("https://")

    def back_with(fragment: str) -> RedirectResponse:
        response = back(target, fragment)
        clear_cookie(response, cookie_name(_GOOGLE_FLOW, secure=secure), secure=secure)
        return response

    if not state or not hmac.compare_digest(state, str(flow["s"])):
        logger.warning("google connect callback with a state this browser did not start")
        return back_with("felix_connect_error=state_mismatch")
    if error or not code:
        reason = "access_denied" if error == "access_denied" else "google_config_error"
        return back_with(f"felix_connect_error={reason}")
    try:
        pending = await exchange(
            settings,
            Flow(
                tenant_id=str(flow["t"]),
                person=str(flow["p"]),
                code_verifier=str(flow["v"]),
                redirect_uri=str(flow["u"]),
                nonce=str(flow["n"]),
            ),
            code,
        )
    except ConnectError as exc:
        logger.warning("google connect failed code=%s", exc.code)
        return back_with(f"felix_connect_error={exc.code}")
    except Exception as exc:
        logger.warning("google connect failed error=%s", type(exc).__name__)
        return back_with("felix_connect_error=connect_failed")
    handoff = {
        **pending.sealable(),
        "t": flow["t"],
        "p": flow["p"],
        "e": int(time.time()) + GOOGLE_HANDOFF_TTL_S,
    }
    response = back_with("felix_connect=pending")
    # Strict: only the app's own same-site request (`/google/finish`) carries it.
    set_cookie(
        response,
        cookie_name(_GOOGLE_HANDOFF, secure=secure),
        _google_seal(settings, _GOOGLE_HANDOFF_PURPOSE, handoff),
        max_age=GOOGLE_HANDOFF_TTL_S,
        secure=secure,
        same_site="strict",
    )
    return response


@router.post("/google/finish", response_model=ConnectionOut)
async def finish_google_connect(request: Request) -> Any:
    """Keep the Google grant your browser just brought back, as yourself. Refused (403
    `not_your_connection`) unless you are the person who started it, and then dropped; the
    waiting cookie is cleared either way, so a reload finds nothing."""
    from felix.auth.google_connect import Pending, store

    from felix_api.routes._login_redirect import clear_cookie

    settings = _google_settings(request)
    tenant, person = _person(request)
    secure = _secure(settings)
    handoff = _google_unseal(
        settings, _GOOGLE_HANDOFF_PURPOSE, _read_cookie(request, settings, _GOOGLE_HANDOFF)
    )

    def answer(body: dict[str, Any], status: int = 200) -> JSONResponse:
        response = JSONResponse(body, status_code=status)
        for name in {cookie_name(_GOOGLE_HANDOFF, secure=True), _GOOGLE_HANDOFF}:
            clear_cookie(response, name, secure=secure or name.startswith("__Host-"))
        return response

    if handoff is None:
        return answer({"error": "connect_expired", "message": "start connecting again"}, 400)
    pending = Pending.unsealed(handoff)
    if (handoff["t"], handoff["p"]) != (tenant, person):
        # Someone else's flow finished in this browser: nobody keeps the grant. Dropped, not
        # revoked at Google: revoking ends the account's whole grant with this client, which
        # would disconnect every Felix connection of it -- a planted flow could do that on purpose.
        # Held by no one, it lives only in this cleared cookie, and the person can revoke it at
        # Google themselves.
        logger.warning("google connect finished by a person other than the one who started it")
        del pending
        return answer({"error": "not_your_connection", "message": "start connecting again"}, 403)
    try:
        stored = await store(settings, tenant, person, pending)
    except Exception as exc:
        logger.warning("google connect store failed error=%s", type(exc).__name__)
        return answer({"error": "connect_failed", "message": "connecting did not finish; try again"}, 500)
    return answer(ConnectionOut.model_validate(stored).model_dump())


# --- every provider ---------------------------------------------------------------------------


@router.get("", response_model=ConnectionList)
async def my_connections(request: Request) -> ConnectionList:
    """The services you have connected in this tenant."""
    from felix.auth import connections

    tenant, person = _person(request)
    rows = await connections.list_for_person(request.app.state.settings, tenant, person)
    return ConnectionList(items=[ConnectionOut.model_validate(r) for r in rows])


@router.get("/tenant", response_model=TenantConnectionList)
async def tenant_connections(request: Request) -> TenantConnectionList:
    """Every stored connection in your tenant, newest first (`connections:admin`)."""
    from felix.auth import connections
    from felix.auth.mgmt import auth_from_request, require_mgmt_scopes

    require_mgmt_scopes(request, "connections:admin")
    tenant = auth_from_request(request).principal.tenant_id
    rows = await connections.list_connections(request.app.state.settings, tenant)
    return TenantConnectionList(items=[TenantConnectionOut.model_validate(r) for r in rows])


@router.post("/tenant/revoke", status_code=204)
async def revoke_connection(body: RevokeConnection, request: Request) -> None:
    """Remove anyone's connection in your tenant and withdraw its grant (`connections:admin`).
    A body rather than a path: a subject or an account may hold any character."""
    from felix.auth import connections
    from felix.auth.mgmt import auth_from_request, require_mgmt_scopes

    require_mgmt_scopes(request, "connections:admin")
    principal = auth_from_request(request).principal
    found = await connections.remove_connection(
        request.app.state.settings,
        principal.tenant_id,
        body.person,
        body.provider,
        body.account,
        actor=principal.subject or "",
    )
    if not found:
        raise HTTPException(status_code=404, detail="not_found")


@router.delete("/{provider}/{account}", status_code=204)
async def remove_my_connection(provider: str, account: str, request: Request) -> None:
    """Disconnect one of your accounts here, and withdraw Felix's grant at the provider."""
    from felix.auth import connections

    tenant, person = _person(request)
    found = await connections.remove_connection(
        request.app.state.settings, tenant, person, provider, account, actor=person
    )
    if not found:
        raise HTTPException(status_code=404, detail="not_found")
