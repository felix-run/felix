"""GitHub Actions login: trade a workflow's OIDC ID token for a self-issued Felix JWT.

The headless half of GitHub login. A workflow with `permissions: id-token: write` asks GitHub
for an ID token whose `aud` is `FELIX_GITHUB_OIDC_AUDIENCE` and posts it here; Felix checks
GitHub's signature, maps `repository_owner_id` through the same `FELIX_GITHUB_ORG_TENANTS` the
device flow uses, and mints a short-lived token. CI then holds no stored secret at all.

Who may log in is the org entry's `actions` block (`ActionsGrant`), never the org alone, and
never a repository alone either: a valid ID token proves that *some* workflow ran in that
repository, not which one, on what, or why.

Owners and repositories are matched by numeric id: a name can be re-registered. GitHub users and
orgs share one account-id space, so a user-owned repository can never carry a mapped org's id.

An ID token is not single-use. Replaying one within its few minutes of life mints another Felix
token for the same workflow, which is no more than whoever holds the ID token could do anyway.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

import httpx
from joserfc import jwk, jwt
from joserfc.errors import JoseError
from joserfc.jwt import JWTClaimsRegistry

from felix.auth.github import (
    GITHUB_URL,
    GitHubLoginError,
    LoginErrorCode,
    LoginToken,
    OrgGrant,
    client_scope,
    merge_by_tenant,
    mint_self_token,
    parse_org_tenants,
    scope_list,
    unavailable,
)

if TYPE_CHECKING:
    from felix.config import Settings

logger = logging.getLogger("felix.auth.github_actions")

ACTIONS_ISSUER = "https://token.actions.githubusercontent.com"
ACTIONS_JWKS_URL = f"{ACTIONS_ISSUER}/.well-known/jwks"
# GitHub signs Actions ID tokens with RS256 only; accepting more is accepting nothing useful.
ACTIONS_ALGORITHMS = ["RS256"]
KEYS_TTL_S = 15 * 60
# Fetches of GitHub's key set are at least this far apart, however they are triggered: anyone
# can present a token naming a key we do not hold, or arrive while the first fetch is failing.
KEYS_REFETCH_MIN_S = 60
ACTIONS_SUBJECT_PREFIX = "github-actions:"

# GitHub's repository-name grammar, minus the names it reserves (`.`, `..`).
_REPO_RE = re.compile(r"^(?!\.\.?$)[A-Za-z0-9._-]{1,100}$")

# Events whose workflow runs on the base branch yet acts on what someone outside the repository
# sent: a fork's pull request (`pull_request_target`), a comment, another workflow's output. A
# `ref` of refs/heads/main proves nothing about the code such a run executes, so they are refused
# unless an `actions` block lists them in `events`.
UNTRUSTED_EVENTS = frozenset({"pull_request_target", "issue_comment", "workflow_run"})

# Audiences that belong to someone else. GitHub's default `aud` is the owner's URL, carried by
# every ID token a workflow requests without naming one; GCP workload identity's are
# iam.googleapis.com URLs. Tokens with these are routinely handed to other services, so a Felix
# that accepted them could be sent one of those tokens and replay nothing it was meant to get.
_FOREIGN_AUDIENCES = (GITHUB_URL, "https://iam.googleapis.com")


# --- configuration ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ActionsGrant:
    """Which of an org's workflows may trade an Actions OIDC token, and for what scopes.

    Repositories are `{name: id}` inside the org, matched on the id. Listed explicitly because
    the org alone is too wide — an outside collaborator with write on one repository is not an
    org member, yet can push a workflow there.

    A repository alone is too wide as well, so at least one narrowing is required: `refs`
    (`ref`), `workflows` (`job_workflow_ref`, the workflow file that actually ran — for a
    reusable workflow, the callee) or `environments` (`environment`; one with required
    reviewers is GitHub's strongest gate). Each is a list of fnmatch patterns, case-sensitive,
    where `*` also matches `/`. Every one set must match.
    """

    repositories: Mapping[str, int]  # lowercased name -> repository id
    refs: tuple[str, ...]
    workflows: tuple[str, ...]
    environments: tuple[str, ...]
    events: tuple[str, ...]
    scopes: tuple[str, ...]


def _patterns(entry: dict[str, Any], key: str, where: str, *, prefix: str = "") -> tuple[str, ...]:
    value = entry.get(key, [])
    if not isinstance(value, list) or not all(
        isinstance(v, str) and v and v.startswith(prefix) for v in value
    ):
        hint = f" starting {prefix}" if prefix else ""
        raise ValueError(f"{where}: `{key}` must be a list of non-empty patterns{hint}")
    return tuple(dict.fromkeys(value))


def _repositories(repos: Any, org: str, where: str) -> Mapping[str, int]:
    if not isinstance(repos, dict) or not repos:
        raise ValueError(
            f"{where}: `repositories` must map at least one repository name in {org} to its "
            f"numeric id (gh api repos/{org}/<name> --jq .id)"
        )
    out: dict[str, int] = {}
    for name, repo_id in repos.items():
        if not _REPO_RE.match(name):
            raise ValueError(f"{where}: {name!r} is not a repository name (one inside {org}, not owner/name)")
        if not isinstance(repo_id, int) or isinstance(repo_id, bool) or repo_id <= 0:
            raise ValueError(f"{where}: {name}: the value must be the repository's numeric GitHub id")
        if name.lower() in out:
            raise ValueError(f"{where}: {name!r} is listed twice (names are case-insensitive)")
        out[name.lower()] = repo_id
    return MappingProxyType(out)


def parse_actions_grant(entry: Any, org: str) -> ActionsGrant:
    """One org entry's `actions` block. Raises ValueError on any shape error."""
    where = f"{org}.actions"
    keys = {"repositories", "refs", "workflows", "environments", "events", "scopes"}
    if not isinstance(entry, dict):
        raise ValueError(f"{where}: expected an object with {sorted(keys)}")
    if unknown := set(entry) - keys:
        raise ValueError(f"{where}: unknown keys {sorted(unknown)}")
    refs = _patterns(entry, "refs", where, prefix="refs/")
    workflows = _patterns(entry, "workflows", where)
    environments = _patterns(entry, "environments", where)
    if not (refs or workflows or environments):
        raise ValueError(
            f"{where}: set at least one of `refs`, `workflows` or `environments`; a repository "
            "alone lets anyone who can push a branch there mint a token"
        )
    if "scopes" not in entry:
        # No default to the org's own: a deploy job and a person rarely need the same authority,
        # and an inherited grant is the one nobody notices widening.
        raise ValueError(f"{where}: `scopes` is required")
    return ActionsGrant(
        repositories=_repositories(entry.get("repositories"), org, where),
        refs=refs,
        workflows=workflows,
        environments=environments,
        events=_patterns(entry, "events", where),
        scopes=scope_list(entry["scopes"], where),
    )


def validate_actions_config(settings: Settings, grants: Mapping[str, OrgGrant]) -> None:
    """Boot checks for the exchange; the shared ones (minting, verifiers) are the caller's."""
    audience = settings.github_oidc_audience.strip()
    if not audience.lower().startswith("https://") or audience.lower().startswith(_FOREIGN_AUDIENCES):
        raise RuntimeError(
            "FELIX_GITHUB_OIDC_AUDIENCE must be this server's own https URL, unique to this "
            f"deployment, and not a {GITHUB_URL} URL: that is GitHub's default audience, which "
            "tokens meant for other services carry."
        )
    if not any(g.actions for g in grants.values()):
        raise RuntimeError(
            "FELIX_GITHUB_OIDC_AUDIENCE is set, so an org in FELIX_GITHUB_ORG_TENANTS needs an "
            "`actions` block naming the repositories whose workflows may log in."
        )


# --- the issuer's keys ------------------------------------------------------------------


@dataclass
class _KeyCache:
    """GitHub's published signing keys. Global on purpose: they are GitHub's, not a tenant's."""

    keys: Any = None
    fetched_at: float | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def age(self) -> float:
        return float("inf") if self.fetched_at is None else time.monotonic() - self.fetched_at

    async def get(self, http: httpx.AsyncClient, *, refresh: bool = False) -> Any:
        """The key set, fetched when missing, stale or `refresh`ed — and never twice a minute.

        Single-flight: requests arriving while a fetch is out wait for it rather than each
        starting their own.
        """
        async with self.lock:
            wanted = refresh or self.keys is None or self.age() > KEYS_TTL_S
            if wanted and self.age() >= KEYS_REFETCH_MIN_S:
                self.fetched_at = time.monotonic()
                self.keys = await _fetch_keys(http)
            if self.keys is None:
                raise unavailable("Actions JWKS not fetched yet; the last attempt failed")
            return self.keys


_keys = _KeyCache()


async def _fetch_keys(http: httpx.AsyncClient) -> Any:
    try:
        resp = await http.get(ACTIONS_JWKS_URL, headers={"accept": "application/json"})
    except httpx.HTTPError as exc:
        raise unavailable(f"Actions JWKS unreachable: {exc}") from exc
    if resp.status_code != 200:
        raise unavailable(f"Actions JWKS answered {resp.status_code}")
    try:
        return jwk.KeySet.import_key_set(resp.json())
    except Exception as exc:
        raise unavailable("Actions JWKS did not parse") from exc


# --- verifying an ID token --------------------------------------------------------------


def _invalid(detail: str) -> GitHubLoginError:
    # The reason goes to the log; the caller learns only that the token was refused.
    logger.warning("github actions login: %s", detail)
    return GitHubLoginError(LoginErrorCode.INVALID_ID_TOKEN, "the GitHub Actions ID token was not accepted")


async def _decode(id_token: str, audience: str, http: httpx.AsyncClient) -> dict[str, Any]:
    from felix.auth.jwt import JWT_LEEWAY_S

    try:
        token = jwt.decode(id_token, await _keys.get(http), algorithms=ACTIONS_ALGORITHMS)
    except (JoseError, ValueError) as exc:
        # Possibly a key GitHub rotated in since the last fetch; `get` decides whether to ask.
        try:
            token = jwt.decode(id_token, await _keys.get(http, refresh=True), algorithms=ACTIONS_ALGORITHMS)
        except (JoseError, ValueError) as again:
            raise _invalid(f"signature: {again}") from exc
    registry = JWTClaimsRegistry(
        leeway=JWT_LEEWAY_S,
        iss={"essential": True, "value": ACTIONS_ISSUER},
        aud={"essential": True, "value": audience},
        exp={"essential": True},
        iat={"essential": True},
    )
    try:
        registry.validate(token.claims)
    except JoseError as exc:
        raise _invalid(f"claims: {exc}") from exc
    return dict(token.claims)


def _str_claim(claims: Mapping[str, Any], name: str) -> str:
    value = claims.get(name)
    if not isinstance(value, str) or not value:
        raise _invalid(f"missing claim {name!r}")
    return value


def _id_claim(claims: Mapping[str, Any], name: str) -> int:
    value = _str_claim(claims, name)
    if not value.isdigit():
        raise _invalid(f"{name} is not numeric")
    return int(value)


@dataclass(frozen=True, slots=True)
class Workflow:
    """The run an ID token speaks for: the claims a grant is matched on, and the audit row."""

    owner_id: int
    repository_id: int
    repository: str
    ref: str
    job_workflow_ref: str
    environment: str
    event_name: str
    sha: str
    run_id: str
    run_attempt: str
    actor: str
    triggering_actor: str

    @classmethod
    def from_claims(cls, claims: Mapping[str, Any]) -> Workflow:
        def text(name: str) -> str:
            value = claims.get(name)
            return value if isinstance(value, str) else ""

        return cls(
            owner_id=_id_claim(claims, "repository_owner_id"),
            repository_id=_id_claim(claims, "repository_id"),
            repository=_str_claim(claims, "repository"),
            ref=text("ref"),
            job_workflow_ref=_str_claim(claims, "job_workflow_ref"),
            environment=text("environment"),
            event_name=text("event_name"),
            sha=text("sha"),
            run_id=text("run_id"),
            run_attempt=text("run_attempt"),
            actor=text("actor"),
            triggering_actor=text("triggering_actor"),
        )

    @property
    def label(self) -> str:
        return f"{self.repository}@{self.ref}" if self.ref else self.repository

    @property
    def subject(self) -> str:
        # Built from the stable ids, not the token's `sub`: an org can customise the `sub`
        # template, and the default one names neither the workflow file nor the run.
        return f"{ACTIONS_SUBJECT_PREFIX}{self.repository_id}:{self.job_workflow_ref}"

    def audit(self) -> dict[str, str]:
        return {
            "repository": self.repository,
            "repository_id": str(self.repository_id),
            "ref": self.ref,
            "job_workflow_ref": self.job_workflow_ref,
            "environment": self.environment,
            "event_name": self.event_name,
            "sha": self.sha,
            "run_id": self.run_id,
            "run_attempt": self.run_attempt,
            "actor": self.actor,
            "triggering_actor": self.triggering_actor,
        }


def _matches(patterns: tuple[str, ...], value: str) -> bool:
    return not patterns or any(fnmatch.fnmatchcase(value, p) for p in patterns)


def admits(grant: ActionsGrant, run: Workflow) -> bool:
    if run.repository_id not in grant.repositories.values():
        return False
    if grant.events:
        if run.event_name not in grant.events:
            return False
    elif run.event_name in UNTRUSTED_EVENTS:
        return False
    return (
        _matches(grant.refs, run.ref)
        and _matches(grant.workflows, run.job_workflow_ref)
        and _matches(grant.environments, run.environment)
    )


def _matching_grants(settings: Settings, run: Workflow) -> list[OrgGrant]:
    """Every org entry whose `actions` block admits this run, carrying the Actions scopes."""
    return [
        OrgGrant(org=g.org, org_id=g.org_id, tenant=g.tenant, scopes=g.actions.scopes)
        for g in parse_org_tenants(settings.github_org_tenants).values()
        if g.org_id == run.owner_id and g.actions is not None and admits(g.actions, run)
    ]


def _choose(grants: list[OrgGrant], tenant: str | None, run: Workflow) -> OrgGrant:
    by_tenant = merge_by_tenant(grants)
    if not by_tenant:
        raise GitHubLoginError(
            LoginErrorCode.WORKFLOW_NOT_GRANTED, f"{run.label} may not log in to this server"
        )
    if tenant is not None:
        if tenant not in by_tenant:
            raise GitHubLoginError(
                LoginErrorCode.TENANT_NOT_GRANTED, f"{run.label} is granted no tenant {tenant!r}"
            )
        return by_tenant[tenant]
    if len(by_tenant) > 1:
        # Unlike a device code, an ID token is not spent by a refusal: retry it naming one.
        raise GitHubLoginError(
            LoginErrorCode.TENANT_AMBIGUOUS,
            "this workflow is granted more than one tenant; pass `tenant`",
            tenants=tuple(sorted(by_tenant)),
        )
    return next(iter(by_tenant.values()))


def _mint(settings: Settings, run: Workflow, grant: OrgGrant) -> LoginToken:
    # `github_actor`, never `github_login`: that claim names a person who authenticated, and the
    # actor here only triggered the run.
    return mint_self_token(
        settings,
        subject=run.subject,
        grant=grant,
        ttl=settings.github_oidc_ttl_seconds,
        extra={
            "idp": "github-actions",
            "repository": run.repository,
            "ref": run.ref,
            "job_workflow_ref": run.job_workflow_ref,
            "github_actor": run.actor,
        },
    )


_PROBE_RUN = Workflow(
    owner_id=0,
    repository_id=0,
    repository="felix/boot-probe",
    ref="refs/heads/main",
    job_workflow_ref="felix/boot-probe/.github/workflows/probe.yml@refs/heads/main",
    environment="",
    event_name="push",
    sha="",
    run_id="",
    run_attempt="",
    actor="felix-boot-probe",
    triggering_actor="",
)


def mint_probe(settings: Settings, grants: list[OrgGrant]) -> LoginToken:
    """An Actions token for the boot probe: the real mint, TTL and claims, with these scopes."""
    scopes = tuple(dict.fromkeys(s for g in grants if g.actions for s in g.actions.scopes))
    first = grants[0]
    return _mint(settings, _PROBE_RUN, OrgGrant(org=first.org, org_id=0, tenant=first.tenant, scopes=scopes))


@dataclass(frozen=True, slots=True)
class ActionsLogin:
    """The minted token, and the run it was minted for — what the audit row records."""

    token: LoginToken
    run: Workflow


async def exchange_actions_token(
    settings: Settings,
    id_token: str,
    *,
    tenant: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> ActionsLogin:
    """Verify a GitHub Actions ID token and mint a Felix token for the tenant it maps to."""
    async with client_scope(client, settings) as http:
        claims = await _decode(id_token, settings.github_oidc_audience.strip(), http)
    run = Workflow.from_claims(claims)
    try:
        grant = _choose(_matching_grants(settings, run), tenant, run)
    except GitHubLoginError as exc:
        logger.warning(
            "github actions login refused for %s (%s, %s): %s",
            run.label,
            run.job_workflow_ref,
            run.event_name,
            exc.code,
        )
        raise
    minted = _mint(settings, run, grant)
    logger.info(
        "github actions login minted a token for %s (%s) tenant=%s scopes=%s",
        run.label,
        run.job_workflow_ref,
        grant.tenant,
        ",".join(grant.scopes),
    )
    return ActionsLogin(token=minted, run=run)


__all__ = [
    "ACTIONS_ISSUER",
    "ACTIONS_JWKS_URL",
    "UNTRUSTED_EVENTS",
    "ActionsGrant",
    "ActionsLogin",
    "Workflow",
    "admits",
    "exchange_actions_token",
    "mint_probe",
    "parse_actions_grant",
    "validate_actions_config",
]
