"""GitHub login: the OAuth device flow, org membership, and a self-issued Felix JWT.

GitHub OAuth apps issue no OIDC ID token, so there is nothing for a verifier to point at.
Felix runs the device flow on the caller's behalf instead, checks which configured org the
user is an *active* member of, and mints its own token for that org's tenant. The GitHub
access token is used for two reads and dropped; it never reaches the caller.

`FELIX_GITHUB_ORG_TENANTS` is the whole user model: `{"<org>": {"tenant": "<id>",
"scopes": [...]}}`. There is no user table.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from felix.config import Settings

logger = logging.getLogger("felix.auth.github")

GITHUB_URL = "https://github.com"
GITHUB_API_URL = "https://api.github.com"
# `read:org` is what makes a private org membership visible to the membership endpoint.
DEVICE_SCOPE = "read:org"
DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
_API_HEADERS = {"accept": "application/vnd.github+json", "x-github-api-version": "2022-11-28"}

# An org name is interpolated into an API path, so it is held to GitHub's own grammar
# (alphanumerics and single hyphens, at most 39 characters) rather than trusted as text:
# a `/` or `..` would otherwise address a different endpoint with the user's token.
_ORG_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")


@dataclass(frozen=True, slots=True)
class OrgGrant:
    """What membership of one org is worth: a tenant and the scopes minted into it."""

    org: str
    tenant: str
    scopes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DeviceCode:
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


@dataclass(frozen=True, slots=True)
class GitHubUser:
    id: int
    login: str


@dataclass(frozen=True, slots=True)
class LoginToken:
    access_token: str
    expires_in: int
    tenant: str
    scopes: tuple[str, ...]
    subject: str


class GitHubLoginError(Exception):
    """A login that did not produce a token, carrying the status and code a route answers with.

    `authorization_pending` and `slow_down` are not failures — the user has not approved yet —
    but they leave the same way, with `interval` telling the client how long to wait.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        interval: int | None = None,
        tenants: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.interval = interval
        self.tenants = tenants


def is_enabled(settings: Settings) -> bool:
    return bool(settings.github_client_id.strip())


@lru_cache(maxsize=4)
def parse_org_tenants(raw: str) -> dict[str, OrgGrant]:
    """FELIX_GITHUB_ORG_TENANTS, keyed by lowercased org. Raises ValueError on any shape error.

    GitHub org names are case-insensitive, and the membership response spells the org the
    way GitHub stores it, so matching is done on the lowercased name.
    """
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError as exc:
        raise ValueError(f"not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError('expected a JSON object {"<org>": {"tenant": ..., "scopes": [...]}}')
    grants: dict[str, OrgGrant] = {}
    for org, entry in data.items():
        if not isinstance(org, str) or not _ORG_RE.match(org):
            raise ValueError(f"{org!r} is not a GitHub org name")
        if org.lower() in grants:
            raise ValueError(f"org {org!r} is listed twice (org names are case-insensitive)")
        if not isinstance(entry, dict):
            raise ValueError(f"{org}: expected an object with `tenant` and `scopes`")
        if unknown := set(entry) - {"tenant", "scopes"}:
            raise ValueError(f"{org}: unknown keys {sorted(unknown)}")
        tenant = entry.get("tenant")
        if not isinstance(tenant, str) or not tenant:
            raise ValueError(f"{org}: `tenant` must be a non-empty string")
        scopes = entry.get("scopes", [])
        if not isinstance(scopes, list) or not all(isinstance(s, str) and s for s in scopes):
            raise ValueError(f"{org}: `scopes` must be a list of non-empty strings")
        # Scopes are joined with spaces into the token's `scope` claim and split on
        # whitespace when verified, so a space inside one becomes two scopes.
        if bad := [s for s in scopes if s.split() != [s]]:
            raise ValueError(f"{org}: scopes may not contain whitespace: {bad}")
        grants[org.lower()] = OrgGrant(org=org, tenant=tenant, scopes=tuple(dict.fromkeys(scopes)))
    return grants


def github_http_client(settings: Settings) -> httpx.AsyncClient:
    """The client every GitHub call uses when none is passed; tests replace this one seam."""
    from felix.security.egress import safe_async_client

    return safe_async_client(timeout=settings.github_timeout_seconds)


@asynccontextmanager
async def _client(client: httpx.AsyncClient | None, settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    """The caller's client as-is, or the default one, opened and closed here."""
    if client is not None:
        yield client
        return
    async with github_http_client(settings) as owned:
        yield owned


def _json_object(resp: httpx.Response, what: str) -> dict[str, Any]:
    try:
        body = resp.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise GitHubLoginError("github_unavailable", f"{what} answered with non-object JSON", status=502)
    return body


async def _post_form(client: httpx.AsyncClient, url: str, data: dict[str, str]) -> dict[str, Any]:
    try:
        resp = await client.post(url, data=data, headers={"accept": "application/json"})
    except httpx.HTTPError as exc:
        raise GitHubLoginError("github_unavailable", f"GitHub unreachable: {exc}", status=502) from exc
    if resp.status_code >= 500:
        raise GitHubLoginError("github_unavailable", f"GitHub answered {resp.status_code}", status=502)
    return _json_object(resp, url)


async def start_device_flow(settings: Settings, *, client: httpx.AsyncClient | None = None) -> DeviceCode:
    """Ask GitHub for a device code. The user enters `user_code` at `verification_uri`."""
    async with _client(client, settings) as http:
        body = await _post_form(
            http,
            f"{GITHUB_URL}/login/device/code",
            {"client_id": settings.github_client_id.strip(), "scope": DEVICE_SCOPE},
        )
    if "error" in body:
        # `device_flow_disabled` and `unauthorized_client` are the operator's to fix, in
        # the OAuth app's settings; the caller can do nothing about them.
        raise GitHubLoginError(
            str(body["error"]),
            str(body.get("error_description") or body["error"]),
            status=503,
        )
    try:
        return DeviceCode(
            device_code=str(body["device_code"]),
            user_code=str(body["user_code"]),
            verification_uri=str(body["verification_uri"]),
            expires_in=int(body["expires_in"]),
            interval=int(body["interval"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise GitHubLoginError(
            "github_unavailable", "GitHub's device code was incomplete", status=502
        ) from exc


# Device-flow token errors, mapped to what the caller should do. Anything unlisted is the
# operator's configuration (a wrong client id, device flow disabled) and answers 503.
_POLL_ERRORS: dict[str, int] = {
    "authorization_pending": 428,
    "slow_down": 428,
    "expired_token": 400,
    "incorrect_device_code": 400,
    "access_denied": 403,
}


async def poll_device_flow(
    settings: Settings, device_code: str, *, client: httpx.AsyncClient | None = None
) -> str:
    """One poll. Returns GitHub's access token, or raises — `authorization_pending` included."""
    async with _client(client, settings) as http:
        body = await _post_form(
            http,
            f"{GITHUB_URL}/login/oauth/access_token",
            {
                "client_id": settings.github_client_id.strip(),
                "device_code": device_code,
                "grant_type": DEVICE_GRANT_TYPE,
            },
        )
    if "error" in body:
        code = str(body["error"])
        interval = body.get("interval")
        raise GitHubLoginError(
            code,
            str(body.get("error_description") or code),
            status=_POLL_ERRORS.get(code, 503),
            interval=int(interval) if isinstance(interval, int) else None,
        )
    token = body.get("access_token")
    if not isinstance(token, str) or not token:
        raise GitHubLoginError("github_unavailable", "GitHub returned no access token", status=502)
    return token


async def _api_get(client: httpx.AsyncClient, path: str, gh_token: str) -> httpx.Response:
    try:
        return await client.get(
            f"{GITHUB_API_URL}{path}", headers={**_API_HEADERS, "authorization": f"Bearer {gh_token}"}
        )
    except httpx.HTTPError as exc:
        raise GitHubLoginError("github_unavailable", f"GitHub unreachable: {exc}", status=502) from exc


async def fetch_user(gh_token: str, *, client: httpx.AsyncClient) -> GitHubUser:
    resp = await _api_get(client, "/user", gh_token)
    if resp.status_code != 200:
        raise GitHubLoginError("github_unavailable", f"GET /user answered {resp.status_code}", status=502)
    body = _json_object(resp, "GET /user")
    user_id, login = body.get("id"), body.get("login")
    # `bool` is an `int`; a `true` id would otherwise mint `github:True`.
    if not isinstance(user_id, int) or isinstance(user_id, bool) or not isinstance(login, str):
        raise GitHubLoginError("github_unavailable", "GET /user returned no id", status=502)
    return GitHubUser(id=user_id, login=login)


async def active_grants(
    settings: Settings, gh_token: str, *, client: httpx.AsyncClient
) -> tuple[list[OrgGrant], list[str]]:
    """The configured orgs this user is an **active** member of, and those that hid it.

    One membership read per configured org, rather than listing the user's orgs: the
    listing omits an org that restricts OAuth app access without saying so, which reads as
    "not a member" and sends the operator looking in the wrong place.
    """
    grants: list[OrgGrant] = []
    restricted: list[str] = []
    for grant in parse_org_tenants(settings.github_org_tenants).values():
        resp = await _api_get(client, f"/user/memberships/orgs/{grant.org}", gh_token)
        if resp.status_code == 200:
            # A `pending` membership is an invitation not yet accepted. It is not membership.
            if _json_object(resp, "membership read").get("state") == "active":
                grants.append(grant)
        elif resp.status_code == 403:
            # The org has OAuth app access restrictions and has not approved this app.
            restricted.append(grant.org)
        elif resp.status_code != 404:
            raise GitHubLoginError(
                "github_unavailable", f"membership read answered {resp.status_code}", status=502
            )
    return grants, restricted


def choose_grant(grants: list[OrgGrant], restricted: list[str], tenant: str | None) -> OrgGrant:
    """The one tenant this login lands in, with the scopes of every matching org merged.

    Two orgs mapped to one tenant are one tenant: their scopes are unioned rather than
    making the user pick between two halves of the same grant.
    """
    by_tenant: dict[str, OrgGrant] = {}
    for g in grants:
        prior = by_tenant.get(g.tenant)
        scopes = tuple(dict.fromkeys((*prior.scopes, *g.scopes))) if prior else g.scopes
        by_tenant[g.tenant] = OrgGrant(org=prior.org if prior else g.org, tenant=g.tenant, scopes=scopes)
    if not by_tenant:
        if restricted:
            raise GitHubLoginError(
                "org_access_restricted",
                f"{', '.join(restricted)} restricts OAuth app access; an org owner must approve "
                "this app before membership is visible",
                status=403,
            )
        raise GitHubLoginError("not_a_member", "not an active member of any configured org", status=403)
    if tenant is not None:
        if tenant not in by_tenant:
            raise GitHubLoginError(
                "tenant_not_granted", f"membership grants no tenant {tenant!r}", status=403
            )
        return by_tenant[tenant]
    if len(by_tenant) > 1:
        raise GitHubLoginError(
            "tenant_ambiguous",
            "membership grants more than one tenant; pass `tenant`",
            status=409,
            tenants=tuple(sorted(by_tenant)),
        )
    return next(iter(by_tenant.values()))


def mint_login_token(settings: Settings, user: GitHubUser, grant: OrgGrant) -> LoginToken:
    from felix.auth.jwt import mint_token

    subject = f"github:{user.id}"
    extra: dict[str, Any] = {"idp": "github", "github_login": user.login}
    if (aud := login_audience(settings)) is not None:
        extra["aud"] = aud
    ttl = settings.github_login_ttl_seconds
    token = mint_token(
        settings,
        sub=subject,
        tenant_id=grant.tenant,
        scopes=list(grant.scopes),
        ttl_seconds=ttl,
        extra_claims=extra,
    )
    return LoginToken(
        access_token=token, expires_in=ttl, tenant=grant.tenant, scopes=grant.scopes, subject=subject
    )


def login_audience(settings: Settings) -> str | None:
    """The `aud` the first `self:felix-self` verifier demands, so a minted token passes it."""
    from felix.auth.jwt import SELF_ISSUER, parse_verifiers

    for cfg in parse_verifiers(settings.jwt_verifiers):
        if cfg.scheme == "self" and cfg.issuer == SELF_ISSUER:
            return cfg.audience
    return None


async def exchange_device_code(
    settings: Settings,
    device_code: str,
    *,
    tenant: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> LoginToken:
    """Poll once and, when GitHub has approved, turn the approval into a Felix token."""
    async with _client(client, settings) as http:
        gh_token = await poll_device_flow(settings, device_code, client=http)
        user = await fetch_user(gh_token, client=http)
        grants, restricted = await active_grants(settings, gh_token, client=http)
    try:
        grant = choose_grant(grants, restricted, tenant)
    except GitHubLoginError as exc:
        logger.warning("github login refused for %s (github:%s): %s", user.login, user.id, exc.code)
        raise
    minted = mint_login_token(settings, user, grant)
    logger.info(
        "github login minted a token for %s (github:%s) tenant=%s scopes=%s",
        user.login,
        user.id,
        grant.tenant,
        ",".join(grant.scopes),
    )
    return minted


def validate_login_config(settings: Settings) -> None:
    """Refuse to start a login that succeeds and hands back a token every request 401s.

    Checked by minting one probe token per mapped tenant and running it through the
    verifiers the auth middleware uses. Every rule short of that — key pair, `iss`, `aud`,
    a `fixed:` verifier that would override the minted tenant, FELIX_ALLOWED_TENANTS — is
    one more way to ship the failure `felix mint-jwt` already guards its own output against.
    """
    if not is_enabled(settings):
        if settings.github_org_tenants.strip():
            logger.warning(
                "FELIX_GITHUB_ORG_TENANTS is set but FELIX_GITHUB_CLIENT_ID is empty; login is off"
            )
        return
    try:
        grants = parse_org_tenants(settings.github_org_tenants)
    except ValueError as exc:
        raise RuntimeError(f"FELIX_GITHUB_ORG_TENANTS: {exc}") from exc
    if not grants:
        raise RuntimeError(
            "FELIX_GITHUB_CLIENT_ID is set, so FELIX_GITHUB_ORG_TENANTS must map at least one org."
        )
    for grant in grants.values():
        if {"admin", "*"} & set(grant.scopes):
            logger.warning("FELIX_GITHUB_ORG_TENANTS gives every member of %s admin scope", grant.org)
    for tenant in sorted({g.tenant for g in grants.values()}):
        _probe_tenant(settings, tenant)


def _probe_tenant(settings: Settings, tenant: str) -> None:
    from felix.auth.jwt import SELF_ISSUER, mint_token, parse_verifiers, uses_jwt_verifiers, verify_jwt

    if not uses_jwt_verifiers(settings):
        raise RuntimeError(
            "FELIX_GITHUB_CLIENT_ID is set but no JWT verifier is in play, so a minted token would "
            f"never be checked. Set FELIX_AUTH_MODE=jwt and FELIX_JWT_VERIFIERS=self:{SELF_ISSUER}."
        )
    verifiers = parse_verifiers(settings.jwt_verifiers)
    if not any(v.scheme == "self" and v.issuer == SELF_ISSUER for v in verifiers):
        raise RuntimeError(
            f"FELIX_GITHUB_CLIENT_ID is set, so FELIX_JWT_VERIFIERS needs self:{SELF_ISSUER} "
            "to accept the tokens GitHub login mints."
        )
    aud = login_audience(settings)
    try:
        probe = mint_token(
            settings,
            sub="github:probe",
            tenant_id=tenant,
            scopes=[],
            ttl_seconds=300,
            extra_claims={"aud": aud} if aud else None,
        )
    except Exception as exc:
        raise RuntimeError(f"GitHub login cannot mint with FELIX_JWKS_PRIVATE: {exc}") from exc
    result = verify_jwt(probe, verifiers, jwks_public=settings.jwks_public, settings=settings)
    if not result.ok:
        hint = (
            f"{tenant!r} is not in FELIX_ALLOWED_TENANTS"
            if result.reason == "tenant_not_allowed"
            else "check that FELIX_JWKS_PUBLIC pairs with FELIX_JWKS_PRIVATE"
        )
        raise RuntimeError(
            f"A GitHub login token for tenant {tenant!r} would be refused ({result.reason}): {hint}."
        )
    if result.principal.tenant_id != tenant:
        raise RuntimeError(
            f"A GitHub login token for tenant {tenant!r} verifies as tenant {result.principal.tenant_id!r}: "
            f"a verifier ahead of self:{SELF_ISSUER} in FELIX_JWT_VERIFIERS pins the tenant. "
            "GitHub login needs tenant=claim."
        )
