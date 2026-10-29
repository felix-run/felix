"""Connecting a person's Google account, so Felix can read their calendar or draft their mail.

Separate from Google sign-in (`felix.auth.google`), which proves who someone is and keeps nothing
of Google's. Connecting asks Google for an **offline** grant with the scopes a person chooses --
calendar, mail, Drive -- and keeps its refresh token in the person's vault
(`felix.auth.connections`, provider `google`), so tools can act as them later, in a job or a
durable run as much as in a chat.

The flow is sign-in's shape, with two differences that matter. It starts from a request the person
makes *as themselves* (`POST /connections/google/start`), whose sealed HttpOnly flow cookie names
them. And the callback, a browser navigation that carries no bearer, stores nothing: it exchanges
the code (`exchange`) and leaves the grant sealed in a one-load cookie, which the app hands back
as the person (`POST /connections/google/finish`), and which is kept (`store`) only when that
person is the one who started. A flow cookie planted in someone else's browser -- by a sibling
subdomain, a plain-http answer, script on the app's origin -- so brings back that person's grant
to a finish they make as themselves, which refuses it and drops it.

The account stored is the Google account Google's ID token names (`sub`), labelled with its
verified email; it need not be the account the person signs in with. `FELIX_GOOGLE_ACCOUNTS`
governs sign-in, not this: whoever is a person here may connect a Google account of theirs.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

import httpx

from felix.auth import connections
from felix.auth.connections import ConnectionRefused, OAuthGrant
from felix.auth.google import GOOGLE_AUTHORIZE_URL, GOOGLE_TOKEN_URL, google_http_client, verify_id_token
from felix.logging_setup import loggable

if TYPE_CHECKING:
    from felix.config import Settings

logger = logging.getLogger("felix.auth.google_connect")

PROVIDER = "google"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
CONNECT_CALLBACK_PATH = "/connections/google/callback"
# What a person can connect, by the name a client and a tool use for it. Least privilege per
# tool: `gmail_compose` drafts and cannot read the inbox; reading is `gmail_read`.
SCOPES: Mapping[str, str] = {
    "calendar_read": "https://www.googleapis.com/auth/calendar.readonly",
    "calendar_events": "https://www.googleapis.com/auth/calendar.events",
    "gmail_read": "https://www.googleapis.com/auth/gmail.readonly",
    "gmail_compose": "https://www.googleapis.com/auth/gmail.compose",
    "drive_read": "https://www.googleapis.com/auth/drive.readonly",
}
# Who the account is: the ID token's `sub` and verified email. Always asked.
_IDENTITY = ("openid", "email")


class ConnectError(Exception):
    """A connection that did not complete. `code` goes back to the app in the fragment
    (`#felix_connect_error=<code>`); the message is for the log and never leaves the process."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def connect_enabled(settings: Settings) -> bool:
    """Sign-in's OAuth client and cookie key, a redirect origin, and a vault key. Asked per
    request, as sign-in's is: the secrets may be hydrated after `Settings` validates."""
    from felix.auth.google import redirect_enabled

    return redirect_enabled(settings) and connections.store_enabled(settings)


def public_connect_paths(settings: Settings) -> frozenset[str]:
    """The one path Google's redirect lands on: a browser navigation carries no bearer token."""
    return frozenset({CONNECT_CALLBACK_PATH}) if connect_enabled(settings) else frozenset()


def scope_urls(names: Sequence[str]) -> list[str]:
    """Google's scope URLs for these names; `ValueError` naming one that is not offered."""
    unknown = sorted(set(names) - set(SCOPES))
    if unknown or not names:
        raise ValueError(
            f"unknown scope(s) {unknown}; offered: {', '.join(SCOPES)}" if unknown else "no scopes"
        )
    return [SCOPES[n] for n in dict.fromkeys(names)]


def scope_names(granted: str) -> list[str]:
    """The offered names a grant's space-separated scope URLs cover, in offered order."""
    urls = set(granted.split())
    return [name for name, url in SCOPES.items() if url in urls]


def consent_url(
    settings: Settings,
    *,
    redirect_uri: str,
    state: str,
    nonce: str,
    code_challenge: str,
    scopes: Sequence[str],
) -> str:
    query = {
        "client_id": settings.google_client_id.strip(),
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join([*_IDENTITY, *scope_urls(scopes)]),
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        # Offline, for a refresh token; `consent`, because Google sends a refresh token only on a
        # consent screen; incremental, so connecting Drive later keeps the calendar grant.
        "access_type": "offline",
        "prompt": "consent select_account",
        "include_granted_scopes": "true",
    }
    return f"{GOOGLE_AUTHORIZE_URL}?{urlencode(query)}"


async def _token_call(http: httpx.AsyncClient, form: dict[str, str]) -> tuple[int, dict[str, Any]]:
    try:
        resp = await http.post(GOOGLE_TOKEN_URL, data=form, headers={"accept": "application/json"})
    except httpx.HTTPError as exc:
        raise ConnectError(
            "google_unavailable", f"token endpoint unreachable: {type(exc).__name__}"
        ) from None
    try:
        body = resp.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise ConnectError(
            "google_unavailable", f"token endpoint answered {resp.status_code} with no JSON object"
        )
    return resp.status_code, body


def _grant(body: Mapping[str, Any]) -> OAuthGrant:
    def seconds(value: object) -> int:
        return value if isinstance(value, int) and value > 0 else 0

    return OAuthGrant(
        access_token=str(body.get("access_token") or ""),
        expires_in=seconds(body.get("expires_in")),
        refresh_token=str(body.get("refresh_token") or ""),
        refresh_expires_in=seconds(body.get("refresh_token_expires_in")),
        scopes=str(body.get("scope") or ""),
    )


@dataclass(frozen=True, slots=True)
class Flow:
    """What the start sealed into this browser's cookie: whose vault, and how to finish."""

    tenant_id: str
    person: str
    code_verifier: str
    redirect_uri: str
    nonce: str


@dataclass(frozen=True, slots=True)
class Pending:
    """A grant Google gave and Felix has not stored: the callback holds it (sealed, one load)
    until the person it is for asks to keep it, authenticated (`store`)."""

    account: str
    label: str
    grant: OAuthGrant

    def sealable(self) -> dict[str, Any]:
        g = self.grant
        return {
            "a": self.account,
            "l": self.label,
            "g": [g.access_token, g.expires_in, g.refresh_token, g.refresh_expires_in, g.scopes],
        }

    @classmethod
    def unsealed(cls, data: Mapping[str, Any]) -> Pending:
        access, expires, refresh, refresh_expires, scopes = data["g"]
        return cls(
            account=str(data["a"]),
            label=str(data["l"]),
            grant=OAuthGrant(str(access), int(expires), str(refresh), int(refresh_expires), str(scopes)),
        )


async def exchange(
    settings: Settings, flow: Flow, code: str, *, client: httpx.AsyncClient | None = None
) -> Pending:
    """The callback's half: code → Google's grant → the verified account. Stores nothing: a
    callback is a browser navigation, and which person a grant is for is decided by `store`,
    which the person calls as themselves."""
    if client is None:
        async with google_http_client(settings) as owned:
            return await exchange(settings, flow, code, client=owned)
    grant, claims = await _exchange(settings, flow, code, client)
    return Pending(account=str(claims["sub"]), label=str(claims["email"]), grant=grant)


async def store(settings: Settings, tenant_id: str, person: str, pending: Pending) -> dict[str, Any]:
    """Keep a pending grant in this person's vault."""
    stored = await connections.save_oauth(
        settings,
        tenant_id,
        person=person,
        provider=PROVIDER,
        account=pending.account,
        grant=pending.grant,
        label=pending.label,
    )
    logger.info(
        "google connected for person=%s scopes=%s",
        loggable(person, limit=200),
        ",".join(scope_names(pending.grant.scopes)),
    )
    return stored


async def _exchange(
    settings: Settings, flow: Flow, code: str, client: httpx.AsyncClient
) -> tuple[OAuthGrant, dict[str, Any]]:
    from felix.auth.google import GoogleLoginError

    status, body = await _token_call(
        client,
        {
            "code": code,
            "client_id": settings.google_client_id.strip(),
            "client_secret": settings.google_client_secret.strip(),
            "redirect_uri": flow.redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": flow.code_verifier,
        },
    )
    if status != 200:
        if body.get("error") == "invalid_grant":
            raise ConnectError("invalid_grant", "the authorization code was refused")
        if status >= 500:
            raise ConnectError("google_unavailable", f"token endpoint answered {status}")
        raise ConnectError("google_config_error", f"token endpoint answered {status}")
    id_token = body.get("id_token")
    if not isinstance(id_token, str) or not id_token:
        raise ConnectError("google_config_error", "no id_token in the grant")
    try:
        claims = await verify_id_token(settings, id_token, nonce=flow.nonce, http=client)
    except GoogleLoginError as exc:
        raise ConnectError(exc.code, "the ID token did not verify") from None
    grant = _grant(body)
    if not grant.refresh_token:
        # Google withholds it when it was granted before without a consent screen; `prompt=consent`
        # asks for one, so this is a client or account setting, not a retry.
        raise ConnectError("no_refresh_token", "Google sent no refresh token")
    return grant, claims


class GoogleConnectionProvider:
    """How the vault refreshes and withdraws a Google grant. Google keeps the refresh token on a
    refresh (it does not rotate), and refuses a revoked or expired one with `invalid_grant`."""

    name = PROVIDER

    async def refresh(self, settings: Settings, refresh_token: str) -> OAuthGrant:
        async with google_http_client(settings) as http:
            status, body = await _token_call(
                http,
                {
                    "client_id": settings.google_client_id.strip(),
                    "client_secret": settings.google_client_secret.strip(),
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
            )
        if status == 200 and body.get("access_token"):
            return _grant(body)
        if body.get("error") == "invalid_grant":
            raise ConnectionRefused("google refused the refresh token")
        raise RuntimeError(f"google token endpoint answered {status}")

    async def revoke(self, settings: Settings, token: str) -> None:
        async with google_http_client(settings) as http:
            # Revoking the refresh token revokes the whole grant at Google -- for this Google
            # account and this OAuth client, wherever else it is connected.
            resp = await http.post(GOOGLE_REVOKE_URL, data={"token": token})
        if resp.status_code != 200:
            # Raised so the vault logs it: the row is gone, the grant may still be live at Google.
            raise RuntimeError(f"google revoke answered {resp.status_code}")


provider = GoogleConnectionProvider()
connections.register_connection_provider(provider)


__all__ = [
    "CONNECT_CALLBACK_PATH",
    "PROVIDER",
    "SCOPES",
    "ConnectError",
    "Flow",
    "GoogleConnectionProvider",
    "Pending",
    "connect_enabled",
    "consent_url",
    "exchange",
    "public_connect_paths",
    "scope_names",
    "scope_urls",
    "store",
]
