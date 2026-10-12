"""Google sign-in: a browser redirect, Google's ID token, an allowlist, a self-issued Felix JWT.

Sign-in is for the accounts in `FELIX_GOOGLE_ACCOUNTS` and nobody else. There is no signup: a
Google account in no entry is refused after Google has said who it is, and the refusal is
logged with the account's id so an operator can pin it. Google's own access token is used for
nothing; the ID token from the code exchange is verified here (signature from Google's published
keys, `iss`, `aud` = our client id, `nonce` = the one this browser's flow cookie carries) and
then dropped.

An entry is keyed by the Google-verified email, which is what an operator knows before anyone
has signed in. Its optional `sub` pins it to one Google account: an address can be recycled, or
moved to another account, and `sub` cannot. Its optional `manifests` pins the minted token to
those agents (`felix.auth.context.MANIFESTS_CLAIM`, enforced in `felix.manifests.inbound_auth`)
— the account of a child who should reach one supervised agent and nothing else.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

import httpx
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwt import JWTClaimsRegistry

from felix.auth.context import MANIFESTS_CLAIM
from felix.auth.github import LoginToken, check_redirect_origins, self_verifier, verify_login_probe
from felix.auth.oidc_keys import JwksCache
from felix.logging_setup import loggable

if TYPE_CHECKING:
    from felix.config import Settings

logger = logging.getLogger("felix.auth.google")

GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
# Google documents both spellings and issues either.
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
GOOGLE_ALGORITHMS = ["RS256"]
GOOGLE_SCOPE = "openid email"
SUBJECT_PREFIX = "google:"

GOOGLE_LOGIN_PREFIX = "/auth/google"
# Public only while redirect sign-in is configured; an exact set rather than the prefix, so a
# plugin router that happens to mount under it stays behind auth.
GOOGLE_REDIRECT_PATHS = frozenset(
    {f"{GOOGLE_LOGIN_PREFIX}/authorize", f"{GOOGLE_LOGIN_PREFIX}/callback", f"{GOOGLE_LOGIN_PREFIX}/exchange"}
)


def _unpinnable(scopes: tuple[str, ...]) -> list[str]:
    """Scopes a pinned account may not carry: anything but `<area>:read`.

    A pin keeps a credential to some agents, and a write scope reaches around it: `manifests:write`
    rewrites the pinned manifest's guardrails, `jobs:write` and `eval:write` run any manifest as
    a machine actor that no pin follows, and `admin`/`*` pass every scope check there is.
    """
    return sorted(s for s in scopes if not (s.endswith(":read") and s.count(":") == 1 and s != ":read"))


_ACCOUNT_KEYS = frozenset({"tenant", "scopes", "manifests", "sub"})
_SUB_RE = re.compile(r"[0-9]{1,255}")


class GoogleLoginError(Exception):
    """A sign-in that did not produce a token. `code` is closed: it becomes the fragment the app
    reads (`#felix_login_error=<code>`), and Google's own strings never do."""

    CODES = frozenset({"not_allowed", "invalid_id_token", "google_unavailable", "google_config_error"})

    def __init__(self, code: str, message: str) -> None:
        if code not in self.CODES:
            raise ValueError(f"unknown google login error code {code!r}")
        super().__init__(message)
        self.code = code


def _unavailable(detail: str) -> GoogleLoginError:
    logger.warning("google login: %s", detail)
    return GoogleLoginError("google_unavailable", "Google sign-in is unavailable; try again shortly")


def _config_error(detail: str) -> GoogleLoginError:
    logger.error("google login: %s", detail)
    return GoogleLoginError("google_config_error", "Google sign-in is misconfigured on this server")


def _invalid(detail: str) -> GoogleLoginError:
    # The reason goes to the log; the caller learns only that the token was refused.
    logger.warning("google login: ID token refused: %s", detail)
    return GoogleLoginError("invalid_id_token", "Google's answer could not be verified")


# --- the allowlist ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GoogleAccount:
    email: str
    tenant: str
    scopes: tuple[str, ...]
    # None: no pin. Never empty: a pin to nothing is a typo, refused at parse.
    manifests: tuple[str, ...] | None = None
    sub: str = ""


def _str_list(value: Any, what: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ValueError(f"{what} must be a list of non-empty strings")
    return tuple(dict.fromkeys(v.strip() for v in value))


def parse_accounts(raw: str) -> Mapping[str, GoogleAccount]:
    """FELIX_GOOGLE_ACCOUNTS, keyed by lower-cased email. Raises ValueError naming the entry."""
    if not raw.strip():
        return MappingProxyType({})
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ValueError("is not JSON") from exc
    if not isinstance(data, dict):
        raise ValueError('must be a JSON object: {"<email>": {"tenant": ...}}')
    out: dict[str, GoogleAccount] = {}
    for key, entry in data.items():
        email = str(key).strip().lower()
        if "@" not in email or email.startswith("@") or email.endswith("@"):
            raise ValueError(f"{key!r} is not an email address")
        if email in out:
            raise ValueError(f"{email!r} is listed twice")
        if not isinstance(entry, dict):
            raise ValueError(f"{email}: must be an object")
        if unknown := set(entry) - _ACCOUNT_KEYS:
            raise ValueError(f"{email}: unknown keys {sorted(unknown)}")
        tenant = entry.get("tenant")
        if not isinstance(tenant, str) or not tenant.strip():
            raise ValueError(f"{email}: `tenant` is required")
        manifests = None
        if "manifests" in entry:
            manifests = _str_list(entry["manifests"], f"{email}: `manifests`")
            if not manifests:
                raise ValueError(f"{email}: `manifests` is empty; omit it for no pin")
        sub = entry.get("sub", "")
        if not isinstance(sub, str) or (sub and not _SUB_RE.fullmatch(sub)):
            raise ValueError(f"{email}: `sub` must be the numeric Google account id, as a string")
        out[email] = GoogleAccount(
            email=email,
            tenant=tenant.strip(),
            scopes=_str_list(entry.get("scopes", []), f"{email}: `scopes`"),
            manifests=manifests,
            sub=sub,
        )
    return MappingProxyType(out)


# --- configuration ------------------------------------------------------------------------


def redirect_origins(settings: Settings) -> tuple[str, ...]:
    """FELIX_GOOGLE_REDIRECT_ORIGINS, each normalised to `scheme://host[:port]`."""
    return tuple(o.strip().rstrip("/") for o in settings.google_redirect_origins.split(",") if o.strip())


def redirect_enabled(settings: Settings) -> bool:
    """Sign-in needs the client secret (the code exchange), the cookie key and somewhere to
    return to. Asked per request: the secrets may be hydrated from a secrets backend after
    `Settings` validates."""
    return bool(
        settings.google_client_id.strip()
        and settings.google_client_secret.strip()
        and settings.google_cookie_key.strip()
        and redirect_origins(settings)
    )


def public_login_paths(settings: Settings) -> frozenset[str]:
    return GOOGLE_REDIRECT_PATHS if redirect_enabled(settings) else frozenset()


def cookie_key(settings: Settings) -> bytes:
    """FELIX_GOOGLE_COOKIE_KEY as 32 raw bytes. Raises ValueError, naming what is wrong."""
    from felix.auth.github_connections import decode_key

    return decode_key(settings.google_cookie_key.strip())


def google_http_client(settings: Settings) -> httpx.AsyncClient:
    """The client every Google call uses; tests replace this one seam."""
    from felix.security.egress import safe_async_client

    return safe_async_client(timeout=settings.google_timeout_seconds)


def authorize_url(
    settings: Settings, *, redirect_uri: str, state: str, nonce: str, code_challenge: str
) -> str:
    query = {
        "client_id": settings.google_client_id.strip(),
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": GOOGLE_SCOPE,
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        # Always the account chooser: a family computer is usually signed in as a grown-up.
        "prompt": "select_account",
    }
    return f"{GOOGLE_AUTHORIZE_URL}?{urlencode(query)}"


# --- the code exchange and the ID token ---------------------------------------------------


def new_key_cache() -> JwksCache:
    """Google's published signing keys. Global on purpose: they are Google's, not a tenant's."""
    return JwksCache(GOOGLE_JWKS_URL, "Google", _unavailable)


_keys = new_key_cache()


async def _token_response(
    settings: Settings, http: httpx.AsyncClient, *, code: str, code_verifier: str, redirect_uri: str
) -> str:
    """Trade the code for Google's tokens and return only the ID token."""
    form = {
        "code": code,
        "client_id": settings.google_client_id.strip(),
        "client_secret": settings.google_client_secret.strip(),
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "code_verifier": code_verifier,
    }
    try:
        resp = await http.post(GOOGLE_TOKEN_URL, data=form, headers={"accept": "application/json"})
    except httpx.HTTPError as exc:
        raise _unavailable(f"token endpoint unreachable: {exc}") from exc
    try:
        body = resp.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise _unavailable(f"token endpoint answered {resp.status_code} with no JSON object")
    if resp.status_code != 200:
        error = str(body.get("error", ""))
        if error == "invalid_grant":
            # An expired, replayed or mismatched code: the person starts again.
            raise _invalid("the authorization code was refused (invalid_grant)")
        if resp.status_code >= 500:
            raise _unavailable(f"token endpoint answered {resp.status_code}")
        raise _config_error(f"token endpoint refused the OAuth client: {loggable(error, limit=80)}")
    id_token = body.get("id_token")
    if not isinstance(id_token, str) or not id_token:
        raise _config_error("token endpoint returned no id_token; is `openid` among the scopes?")
    return id_token


async def verify_id_token(
    settings: Settings, id_token: str, *, nonce: str, http: httpx.AsyncClient
) -> dict[str, Any]:
    """Google's signature, then every claim this sign-in depends on. Returns the claims."""
    from felix.auth.jwt import JWT_LEEWAY_S

    try:
        token = jwt.decode(id_token, await _keys.get(http), algorithms=GOOGLE_ALGORITHMS)
    except (JoseError, ValueError) as exc:
        # Possibly a key Google rotated in since the last fetch; `get` decides whether to ask.
        try:
            token = jwt.decode(id_token, await _keys.get(http, refresh=True), algorithms=GOOGLE_ALGORITHMS)
        except (JoseError, ValueError) as again:
            raise _invalid(f"signature: {again}") from exc
    registry = JWTClaimsRegistry(
        leeway=JWT_LEEWAY_S,
        iss={"essential": True, "values": list(GOOGLE_ISSUERS)},
        aud={"essential": True, "value": settings.google_client_id.strip()},
        exp={"essential": True},
        iat={"essential": True},
        sub={"essential": True},
        # The nonce binds this ID token to the flow this browser started: one issued for another
        # sign-in (to this client, or replayed) carries a different one.
        nonce={"essential": True, "value": nonce},
    )
    try:
        registry.validate(token.claims)
    except JoseError as exc:
        raise _invalid(f"claims: {exc}") from exc
    claims = dict(token.claims)
    if not isinstance(claims.get("sub"), str) or not _SUB_RE.fullmatch(claims["sub"]):
        raise _invalid("sub is not a Google account id")
    # Google sends a JSON boolean. A string "true" or an absent claim is not verification.
    if claims.get("email_verified") is not True or not isinstance(claims.get("email"), str):
        raise _invalid("email missing or not verified")
    return claims


def admit(settings: Settings, claims: Mapping[str, Any]) -> GoogleAccount:
    """The allowlist entry this verified account matches, or a refusal naming it in the log."""
    email = str(claims["email"]).strip().lower()
    sub = str(claims["sub"])
    account = parse_accounts(settings.google_accounts).get(email)
    if account is None or (account.sub and account.sub != sub):
        why = "is not in FELIX_GOOGLE_ACCOUNTS" if account is None else "does not match the entry's `sub`"
        # The operator's way to pin or add an account: the log line carries both ids.
        logger.warning(
            "google login refused: %s (google:%s) %s",
            loggable(email, limit=320),
            loggable(sub, limit=64),
            why,
        )
        raise GoogleLoginError("not_allowed", "this Google account may not sign in here")
    return account


def mint_google_token(settings: Settings, *, sub: str, account: GoogleAccount) -> LoginToken:
    """The one way a Google sign-in becomes a Felix token; the boot probe mints through it too."""
    from felix.auth.jwt import mint_token

    claims: dict[str, Any] = {"idp": "google", "email": account.email}
    if account.manifests is not None:
        claims[MANIFESTS_CLAIM] = list(account.manifests)
    if (verifier := self_verifier(settings)) is not None and verifier.audience:
        claims["aud"] = verifier.audience
    subject = f"{SUBJECT_PREFIX}{sub}"
    ttl = settings.google_login_ttl_seconds
    token = mint_token(
        settings,
        sub=subject,
        tenant_id=account.tenant,
        scopes=list(account.scopes),
        ttl_seconds=ttl,
        extra_claims=claims,
    )
    return LoginToken(
        access_token=token, expires_in=ttl, tenant=account.tenant, scopes=account.scopes, subject=subject
    )


async def exchange_code(
    settings: Settings,
    code: str,
    *,
    code_verifier: str,
    redirect_uri: str,
    nonce: str,
    client: httpx.AsyncClient | None = None,
) -> tuple[LoginToken, GoogleAccount]:
    """The callback's whole job: code → ID token → verified claims → allowlist → Felix token."""
    if client is None:
        async with google_http_client(settings) as owned:
            return await exchange_code(
                settings,
                code,
                code_verifier=code_verifier,
                redirect_uri=redirect_uri,
                nonce=nonce,
                client=owned,
            )
    id_token = await _token_response(
        settings, client, code=code, code_verifier=code_verifier, redirect_uri=redirect_uri
    )
    claims = await verify_id_token(settings, id_token, nonce=nonce, http=client)
    account = admit(settings, claims)
    minted = mint_google_token(settings, sub=str(claims["sub"]), account=account)
    logger.info(
        "google login minted a token for %s (google:%s) tenant=%s scopes=%s manifests=%s",
        loggable(account.email, limit=320),
        loggable(claims["sub"], limit=64),
        account.tenant,
        ",".join(account.scopes),
        ",".join(account.manifests) if account.manifests is not None else "*",
    )
    return minted, account


# --- boot -----------------------------------------------------------------------------------


def _check_pinned_accounts(accounts: Mapping[str, GoogleAccount]) -> None:
    """A pin that the account's own scopes or tenant would reach around is refused at boot."""
    unpinned_tenants = {a.tenant for a in accounts.values() if a.manifests is None}
    for account in accounts.values():
        if account.manifests is None:
            continue
        if extra := _unpinnable(account.scopes):
            raise RuntimeError(
                f"FELIX_GOOGLE_ACCOUNTS: {account.email} is pinned to manifests, so it may carry only "
                f"`<area>:read` scopes; {extra} would reach around the pin"
            )
        if account.tenant in unpinned_tenants:
            # Threads, history and search are shared across a tenant: a child in a parent's
            # tenant could read every conversation the parent has had.
            raise RuntimeError(
                f"FELIX_GOOGLE_ACCOUNTS: {account.email} is pinned to manifests but shares tenant "
                f"{account.tenant!r} with an unpinned account; give pinned accounts a tenant of their own"
            )


def validate_google_config(settings: Settings) -> None:
    """Refuse to start a sign-in that would fail on first use, or mint a token every request 401s."""
    from felix.auth.jwt import SELF_ISSUER, uses_jwt_verifiers

    try:
        accounts = parse_accounts(settings.google_accounts)
    except ValueError as exc:
        raise RuntimeError(f"FELIX_GOOGLE_ACCOUNTS: {exc}") from exc
    if not settings.google_client_id.strip():
        if accounts:
            logger.warning(
                "FELIX_GOOGLE_ACCOUNTS is set but FELIX_GOOGLE_CLIENT_ID is not; Google sign-in is off"
            )
        return
    if settings.google_cookie_key.strip():
        try:
            cookie_key(settings)
        except ValueError as exc:
            raise RuntimeError(f"FELIX_GOOGLE_COOKIE_KEY: {exc}") from exc
    check_redirect_origins(redirect_origins(settings), "FELIX_GOOGLE_REDIRECT_ORIGINS")
    if not settings.google_callback_path.startswith("/"):
        raise RuntimeError("FELIX_GOOGLE_CALLBACK_PATH must start with '/'")
    if not accounts:
        raise RuntimeError("Google sign-in is on, so FELIX_GOOGLE_ACCOUNTS must list at least one account.")
    _check_pinned_accounts(accounts)
    if not uses_jwt_verifiers(settings) or self_verifier(settings) is None:
        raise RuntimeError(
            "Google sign-in is on, so a minted token must be verifiable: set FELIX_AUTH_MODE=jwt and "
            f"FELIX_JWT_VERIFIERS=self:{SELF_ISSUER}."
        )
    for account in accounts.values():
        verify_login_probe(
            settings,
            account.tenant,
            lambda found=account: mint_google_token(settings, sub="0", account=found),
            label="Google sign-in",
        )
