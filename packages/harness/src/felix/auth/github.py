"""GitHub login: the OAuth device flow, org membership, and a self-issued Felix JWT.

GitHub OAuth apps issue no OIDC ID token, so there is nothing for a verifier to point at.
Felix runs the device flow on the caller's behalf instead, checks which configured org the
user is an *active* member of, and mints its own token for that org's tenant. The GitHub
access token is used for two reads and dropped; it never reaches the caller.

`FELIX_GITHUB_ORG_TENANTS` is the whole user model: `{"<org>": {"tenant": "<id>",
"scopes": [...]}}`. There is no user table. An org entry's optional `actions` block lets that
org's GitHub Actions workflows trade their OIDC token for one too (`felix.auth.github_actions`).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from felix.auth.github_actions import ActionsGrant
    from felix.auth.jwt import VerifierConfig
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
    """What membership of one org is worth: a tenant and the scopes minted into it.

    `org_id` is the identity; `org` is only where to ask. A name is released when an org is
    renamed or deleted and anyone can register it, so matching on the name alone would hand
    the new org's members this tenant with nothing in configuration looking wrong.
    """

    org: str
    org_id: int
    tenant: str
    scopes: tuple[str, ...]
    actions: ActionsGrant | None = None  # `felix.auth.github_actions`


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
    # The GitHub user who authenticated; empty for a workflow, which is not one.
    github_login: str = ""


class LoginErrorCode(StrEnum):
    """Every `code` a login can fail with — closed, so the API's error vocabulary is ours.

    GitHub's own error strings never become a code: one it adds tomorrow would otherwise be a
    new public Felix code nobody reviewed. An unlisted one is `github_config_error`, with
    GitHub's string kept in the message.
    """

    AUTHORIZATION_PENDING = "authorization_pending"
    SLOW_DOWN = "slow_down"
    EXPIRED_TOKEN = "expired_token"
    INVALID_DEVICE_CODE = "invalid_device_code"
    ACCESS_DENIED = "access_denied"
    NOT_A_MEMBER = "not_a_member"
    ORG_ACCESS_RESTRICTED = "org_access_restricted"
    TENANT_AMBIGUOUS = "tenant_ambiguous"
    TENANT_NOT_GRANTED = "tenant_not_granted"
    INVALID_ID_TOKEN = "invalid_id_token"
    WORKFLOW_NOT_GRANTED = "workflow_not_granted"
    GITHUB_UNAVAILABLE = "github_unavailable"
    GITHUB_CONFIG_ERROR = "github_config_error"
    RATE_LIMITED = "rate_limited"


# The one code -> HTTP status table. `authorization_pending`/`slow_down` are not failures —
# the user has not approved yet — but leave the same way, with `interval` saying how long to wait.
LOGIN_ERROR_STATUS: dict[LoginErrorCode, int] = {
    LoginErrorCode.AUTHORIZATION_PENDING: 428,
    LoginErrorCode.SLOW_DOWN: 428,
    LoginErrorCode.EXPIRED_TOKEN: 400,
    LoginErrorCode.INVALID_DEVICE_CODE: 400,
    LoginErrorCode.ACCESS_DENIED: 403,
    LoginErrorCode.NOT_A_MEMBER: 403,
    LoginErrorCode.ORG_ACCESS_RESTRICTED: 403,
    LoginErrorCode.TENANT_NOT_GRANTED: 403,
    LoginErrorCode.TENANT_AMBIGUOUS: 409,
    LoginErrorCode.INVALID_ID_TOKEN: 401,
    LoginErrorCode.WORKFLOW_NOT_GRANTED: 403,
    LoginErrorCode.GITHUB_UNAVAILABLE: 502,
    # The operator's to fix in the OAuth app (wrong client id, device flow disabled).
    LoginErrorCode.GITHUB_CONFIG_ERROR: 503,
    LoginErrorCode.RATE_LIMITED: 429,
}

# GitHub's device-flow token errors that mean something to the caller. Anything else is
# GITHUB_CONFIG_ERROR.
_GITHUB_POLL_ERRORS: dict[str, LoginErrorCode] = {
    "authorization_pending": LoginErrorCode.AUTHORIZATION_PENDING,
    "slow_down": LoginErrorCode.SLOW_DOWN,
    "expired_token": LoginErrorCode.EXPIRED_TOKEN,
    "incorrect_device_code": LoginErrorCode.INVALID_DEVICE_CODE,
    "access_denied": LoginErrorCode.ACCESS_DENIED,
}


class GitHubLoginError(Exception):
    """A login that did not produce a token: a closed `code`, its status, and what to do next."""

    def __init__(
        self,
        code: LoginErrorCode,
        message: str,
        *,
        interval: int | None = None,
        tenants: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = LOGIN_ERROR_STATUS[code]
        self.interval = interval
        self.tenants = tenants


# The callers of these are anonymous, so what went wrong upstream — an egress proxy's name, a
# DNS error, GitHub's description of the OAuth app's settings — goes to the log, and the answer
# says only whose problem it is.
UNAVAILABLE_MESSAGE = "GitHub could not be reached or answered unexpectedly; try again shortly"
CONFIG_ERROR_MESSAGE = "GitHub login is misconfigured on this server; the cause is in its log"


def unavailable(detail: str) -> GitHubLoginError:
    logger.warning("github login: %s", detail)
    return GitHubLoginError(LoginErrorCode.GITHUB_UNAVAILABLE, UNAVAILABLE_MESSAGE)


def _config_error(body: dict[str, Any]) -> GitHubLoginError:
    logger.error("github login: GitHub refused the OAuth app: %s", _github_error_text(body))
    return GitHubLoginError(LoginErrorCode.GITHUB_CONFIG_ERROR, CONFIG_ERROR_MESSAGE)


# Mounted here, and public only at these two paths while login is configured: an exact set
# rather than the prefix, so a plugin router that happens to mount under it stays behind auth.
GITHUB_LOGIN_PREFIX = "/auth/github"
GITHUB_LOGIN_PATHS = frozenset({f"{GITHUB_LOGIN_PREFIX}/device", f"{GITHUB_LOGIN_PREFIX}/token"})


def is_enabled(settings: Settings) -> bool:
    return bool(settings.github_client_id.strip())


GITHUB_ACTIONS_LOGIN_PATH = f"{GITHUB_LOGIN_PREFIX}/actions"


def actions_enabled(settings: Settings) -> bool:
    """The Actions OIDC exchange is on while its audience is set; it needs no OAuth app."""
    return bool(settings.github_oidc_audience.strip())


def public_login_paths(settings: Settings) -> frozenset[str]:
    """The paths that need no credential because they are how a caller gets one."""
    paths = GITHUB_LOGIN_PATHS if is_enabled(settings) else frozenset()
    if actions_enabled(settings):
        paths |= {GITHUB_ACTIONS_LOGIN_PATH}
    return paths


@lru_cache(maxsize=4)
def parse_org_tenants(raw: str) -> Mapping[str, OrgGrant]:
    """FELIX_GITHUB_ORG_TENANTS, keyed by lowercased org. Raises ValueError on any shape error.

    GitHub org names are case-insensitive, and the membership response spells the org the
    way GitHub stores it, so matching is done on the lowercased name.
    """
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError as exc:
        raise ValueError(f"not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError('expected a JSON object {"<org>": {"id": ..., "tenant": ..., "scopes": [...]}}')
    grants: dict[str, OrgGrant] = {}
    for org, entry in data.items():
        if not isinstance(org, str) or not _ORG_RE.match(org):
            raise ValueError(f"{org!r} is not a GitHub org name")
        if org.lower() in grants:
            raise ValueError(f"org {org!r} is listed twice (org names are case-insensitive)")
        if not isinstance(entry, dict):
            raise ValueError(f"{org}: expected an object with `id`, `tenant` and `scopes`")
        if unknown := set(entry) - {"id", "tenant", "scopes", "actions"}:
            raise ValueError(f"{org}: unknown keys {sorted(unknown)}")
        org_id = entry.get("id")
        if not isinstance(org_id, int) or isinstance(org_id, bool) or org_id <= 0:
            raise ValueError(
                f"{org}: `id` must be the org's numeric GitHub id (gh api orgs/{org} --jq .id); "
                "the name alone can be re-registered by someone else"
            )
        tenant = entry.get("tenant")
        if not isinstance(tenant, str) or not tenant:
            raise ValueError(f"{org}: `tenant` must be a non-empty string")
        scopes = scope_list(entry.get("scopes", []), org)
        actions = None
        if "actions" in entry:
            from felix.auth.github_actions import parse_actions_grant

            actions = parse_actions_grant(entry["actions"], org)
        grants[org.lower()] = OrgGrant(org=org, org_id=org_id, tenant=tenant, scopes=scopes, actions=actions)
    # Cached and shared, so read-only: a caller mutating it would change every later lookup.
    return MappingProxyType(grants)


def scope_list(scopes: Any, where: str) -> tuple[str, ...]:
    if not isinstance(scopes, list) or not all(isinstance(s, str) and s for s in scopes):
        raise ValueError(f"{where}: `scopes` must be a list of non-empty strings")
    # Scopes are joined with spaces into the token's `scope` claim and split on
    # whitespace when verified, so a space inside one becomes two scopes.
    if bad := [s for s in scopes if s.split() != [s]]:
        raise ValueError(f"{where}: scopes may not contain whitespace: {bad}")
    return tuple(dict.fromkeys(scopes))


def github_http_client(settings: Settings) -> httpx.AsyncClient:
    """The client every GitHub call uses when none is passed; tests replace this one seam."""
    from felix.security.egress import safe_async_client

    return safe_async_client(timeout=settings.github_timeout_seconds)


@asynccontextmanager
async def client_scope(
    client: httpx.AsyncClient | None, settings: Settings
) -> AsyncIterator[httpx.AsyncClient]:
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
        raise unavailable(f"{what} answered with non-object JSON")
    return body


async def _post_form(client: httpx.AsyncClient, url: str, data: dict[str, str]) -> dict[str, Any]:
    try:
        resp = await client.post(url, data=data, headers={"accept": "application/json"})
    except httpx.HTTPError as exc:
        raise unavailable(f"GitHub unreachable: {exc}") from exc
    if resp.status_code >= 500:
        raise unavailable(f"GitHub answered {resp.status_code}")
    return _json_object(resp, url)


async def start_device_flow(settings: Settings, *, client: httpx.AsyncClient | None = None) -> DeviceCode:
    """Ask GitHub for a device code. The user enters `user_code` at `verification_uri`."""
    async with client_scope(client, settings) as http:
        body = await _post_form(
            http,
            f"{GITHUB_URL}/login/device/code",
            {"client_id": settings.github_client_id.strip(), "scope": DEVICE_SCOPE},
        )
    if "error" in body:
        # `device_flow_disabled` and `unauthorized_client` are the operator's to fix, in
        # the OAuth app's settings; the caller can do nothing about them.
        raise _config_error(body)
    try:
        return DeviceCode(
            device_code=str(body["device_code"]),
            user_code=str(body["user_code"]),
            verification_uri=str(body["verification_uri"]),
            expires_in=int(body["expires_in"]),
            interval=int(body["interval"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise unavailable("GitHub's device code was incomplete") from exc


def _github_error_text(body: dict[str, Any]) -> str:
    """GitHub's own error, for the message — never for the code."""
    error = str(body["error"])
    description = body.get("error_description")
    return f"{error}: {description}" if description else error


async def poll_device_flow(
    settings: Settings, device_code: str, *, client: httpx.AsyncClient | None = None
) -> str:
    """One poll. Returns GitHub's access token, or raises — `authorization_pending` included."""
    async with client_scope(client, settings) as http:
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
        code = _GITHUB_POLL_ERRORS.get(str(body["error"]))
        if code is None:
            raise _config_error(body)
        interval = body.get("interval")
        raise GitHubLoginError(
            code,
            _github_error_text(body),
            interval=interval if isinstance(interval, int) and not isinstance(interval, bool) else None,
        )
    token = body.get("access_token")
    if not isinstance(token, str) or not token:
        raise unavailable("GitHub returned no access token")
    return token


async def _api_get(client: httpx.AsyncClient, path: str, gh_token: str) -> httpx.Response:
    try:
        return await client.get(
            f"{GITHUB_API_URL}{path}", headers={**_API_HEADERS, "authorization": f"Bearer {gh_token}"}
        )
    except httpx.HTTPError as exc:
        raise unavailable(f"GitHub unreachable: {exc}") from exc


async def fetch_user(gh_token: str, *, client: httpx.AsyncClient) -> GitHubUser:
    resp = await _api_get(client, "/user", gh_token)
    if resp.status_code != 200:
        raise unavailable(f"GET /user answered {resp.status_code}")
    body = _json_object(resp, "GET /user")
    user_id, login = body.get("id"), body.get("login")
    # `bool` is an `int`; a `true` id would otherwise mint `github:True`.
    if not isinstance(user_id, int) or isinstance(user_id, bool) or not isinstance(login, str):
        raise unavailable("GET /user returned no id")
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
            if _is_active_member_of(_json_object(resp, "membership read"), grant):
                grants.append(grant)
        elif resp.status_code == 403:
            # The org has OAuth app access restrictions and has not approved this app.
            restricted.append(grant.org)
        elif resp.status_code != 404:
            raise unavailable(f"membership read answered {resp.status_code}")
    return grants, restricted


def _is_active_member_of(membership: dict[str, Any], grant: OrgGrant) -> bool:
    # A `pending` membership is an invitation not yet accepted. It is not membership.
    if membership.get("state") != "active":
        return False
    org = membership.get("organization")
    org_id = org.get("id") if isinstance(org, dict) else None
    if org_id != grant.org_id:
        logger.error(
            "github org %r now has id %r, not the configured %d: renamed or re-registered; "
            "refusing its members until FELIX_GITHUB_ORG_TENANTS is updated",
            grant.org,
            org_id,
            grant.org_id,
        )
        return False
    return True


def merge_by_tenant(grants: list[OrgGrant]) -> dict[str, OrgGrant]:
    """One grant per tenant, the scopes of every grant landing there unioned in first-seen order.

    Two orgs (or two Actions grants) mapped to one tenant are one tenant: the caller is not made
    to pick between two halves of the same grant. The merged grant keeps the first org, and no
    `actions` block — its scopes are already the ones being granted.
    """
    by_tenant: dict[str, OrgGrant] = {}
    for g in grants:
        prior = by_tenant.get(g.tenant)
        scopes = tuple(dict.fromkeys((*prior.scopes, *g.scopes))) if prior else g.scopes
        first = prior or g
        by_tenant[g.tenant] = OrgGrant(org=first.org, org_id=first.org_id, tenant=g.tenant, scopes=scopes)
    return by_tenant


def choose_grant(grants: list[OrgGrant], restricted: list[str], tenant: str | None) -> OrgGrant:
    """The one tenant this login lands in, with the scopes of every matching org merged."""
    by_tenant = merge_by_tenant(grants)
    if not by_tenant:
        if restricted:
            # Named in the log, not the answer: GitHub may 403 a non-member too, and the
            # caller is anyone who finished the device flow.
            logger.warning("github orgs hiding membership from this OAuth app: %s", ", ".join(restricted))
            raise GitHubLoginError(
                LoginErrorCode.ORG_ACCESS_RESTRICTED,
                "a configured org restricts OAuth app access; an org owner must approve this app "
                "before membership is visible",
            )
        raise GitHubLoginError(LoginErrorCode.NOT_A_MEMBER, "not an active member of any configured org")
    if tenant is not None:
        if tenant not in by_tenant:
            raise GitHubLoginError(
                LoginErrorCode.TENANT_NOT_GRANTED, f"membership grants no tenant {tenant!r}"
            )
        return by_tenant[tenant]
    if len(by_tenant) > 1:
        raise GitHubLoginError(
            LoginErrorCode.TENANT_AMBIGUOUS,
            # GitHub has already exchanged this device code, and it is single-use: the
            # caller's only way forward is a new flow that names its tenant up front.
            "membership grants more than one tenant; this login is spent, so start a new one "
            "passing `tenant`",
            tenants=tuple(sorted(by_tenant)),
        )
    return next(iter(by_tenant.values()))


def mint_self_token(
    settings: Settings,
    *,
    subject: str,
    grant: OrgGrant,
    ttl: int,
    extra: Mapping[str, Any],
    github_login: str = "",
) -> LoginToken:
    """The one way a GitHub login — device flow or Actions — becomes a Felix token.

    One function so that a rule about self-issued tokens (a claim, the verifier's `aud`) is made
    once, and so the boot probe, which mints through here, checks it for both paths.
    """
    from felix.auth.jwt import mint_token

    claims = dict(extra)
    if (verifier := self_verifier(settings)) is not None and verifier.audience:
        claims["aud"] = verifier.audience
    token = mint_token(
        settings,
        sub=subject,
        tenant_id=grant.tenant,
        scopes=list(grant.scopes),
        ttl_seconds=ttl,
        extra_claims=claims,
    )
    return LoginToken(
        access_token=token,
        expires_in=ttl,
        tenant=grant.tenant,
        scopes=grant.scopes,
        subject=subject,
        github_login=github_login,
    )


def mint_login_token(settings: Settings, user: GitHubUser, grant: OrgGrant) -> LoginToken:
    return mint_self_token(
        settings,
        subject=f"github:{user.id}",
        grant=grant,
        ttl=settings.github_login_ttl_seconds,
        extra={"idp": "github", "github_login": user.login},
        github_login=user.login,
    )


def self_verifier(settings: Settings) -> VerifierConfig | None:
    """The first `self:felix-self` verifier: the one whose `aud` a login token must carry."""
    from felix.auth.jwt import SELF_ISSUER, parse_verifiers

    for cfg in parse_verifiers(settings.jwt_verifiers):
        if cfg.scheme == "self" and cfg.issuer == SELF_ISSUER:
            return cfg
    return None


async def exchange_device_code(
    settings: Settings,
    device_code: str,
    *,
    tenant: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> LoginToken:
    """Poll once and, when GitHub has approved, turn the approval into a Felix token."""
    async with client_scope(client, settings) as http:
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
    device, actions = is_enabled(settings), actions_enabled(settings)
    if not (device or actions):
        if settings.github_org_tenants.strip():
            logger.warning(
                "FELIX_GITHUB_ORG_TENANTS is set but neither FELIX_GITHUB_CLIENT_ID nor "
                "FELIX_GITHUB_OIDC_AUDIENCE is; GitHub login is off"
            )
        return
    try:
        grants = parse_org_tenants(settings.github_org_tenants)
    except ValueError as exc:
        raise RuntimeError(f"FELIX_GITHUB_ORG_TENANTS: {exc}") from exc
    if not grants:
        raise RuntimeError("GitHub login is on, so FELIX_GITHUB_ORG_TENANTS must map at least one org.")
    if actions:
        from felix.auth.github_actions import validate_actions_config

        validate_actions_config(settings, grants)
    elif any(g.actions for g in grants.values()):
        logger.warning(
            "FELIX_GITHUB_ORG_TENANTS has `actions` blocks but FELIX_GITHUB_OIDC_AUDIENCE is empty"
        )
    for grant in grants.values():
        scopes = set(grant.scopes) | set(grant.actions.scopes if grant.actions else ())
        if {"admin", "*"} & scopes:
            logger.warning("FELIX_GITHUB_ORG_TENANTS gives %s admin scope", grant.org)
    for tenant, merged in merge_by_tenant(list(grants.values())).items():
        _probe_tenant(settings, merged)
        actions_grants = [g for g in grants.values() if g.tenant == tenant and g.actions]
        if actions and actions_grants:
            from felix.auth.github_actions import mint_probe

            _verify_probe(settings, tenant, lambda found=actions_grants: mint_probe(settings, found))


def _probe_tenant(settings: Settings, grant: OrgGrant) -> None:
    """Mint through `mint_login_token` — the real login path, every claim and the real TTL —
    so a rule added to either the minting or the verifiers is checked here too."""
    from felix.auth.jwt import SELF_ISSUER, uses_jwt_verifiers

    if not uses_jwt_verifiers(settings):
        raise RuntimeError(
            "GitHub login is on but no JWT verifier is in play, so a minted token would "
            f"never be checked. Set FELIX_AUTH_MODE=jwt and FELIX_JWT_VERIFIERS=self:{SELF_ISSUER}."
        )
    if self_verifier(settings) is None:
        raise RuntimeError(
            f"GitHub login is on, so FELIX_JWT_VERIFIERS needs self:{SELF_ISSUER} "
            "to accept the tokens GitHub login mints."
        )
    _verify_probe(settings, grant.tenant, lambda: mint_login_token(settings, _PROBE_USER, grant))


_PROBE_USER = GitHubUser(id=0, login="felix-boot-probe")


def _verify_probe(settings: Settings, tenant: str, mint: Callable[[], LoginToken]) -> None:
    """Mint one token the way a login would and run it through the middleware's verifiers."""
    from felix.auth.jwt import SELF_ISSUER, parse_verifiers, verify_jwt

    try:
        probe = mint()
    except Exception as exc:
        raise RuntimeError(f"GitHub login cannot mint with FELIX_JWKS_PRIVATE: {exc}") from exc
    verifiers = parse_verifiers(settings.jwt_verifiers)
    result = verify_jwt(probe.access_token, verifiers, jwks_public=settings.jwks_public, settings=settings)
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
