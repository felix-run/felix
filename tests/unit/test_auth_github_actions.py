"""GitHub Actions login: a workflow's OIDC ID token → a self-issued token the API accepts.

The issuer's key set is served by the same transport-level fake the device-flow tests use, and
every ID token here is signed by a key that fake publishes — or deliberately by one it does
not. A minted token is checked with `verify_jwt`, the call the auth middleware makes.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import httpx
import pytest
from felix.auth import github_actions
from felix.auth.github import (
    GITHUB_ACTIONS_LOGIN_PATH,
    GITHUB_LOGIN_PATHS,
    GitHubLoginError,
    parse_org_tenants,
    public_login_paths,
)
from felix.auth.github_actions import exchange_actions_token
from felix.auth.jwt import parse_verifiers, verify_jwt
from felix.config import Settings
from joserfc import jwk

from tests.support.github_fake import (
    ACTIONS_AUDIENCE,
    ACTIONS_KEY,
    DEPLOY_REPO_ID,
    ORG_IDS,
    FakeGitHub,
    actions_id_token,
)

_KEY = jwk.RSAKey.generate_key(2048)
_ACTIONS: dict[str, Any] = {
    "repositories": {"deploy": DEPLOY_REPO_ID},
    "refs": ["refs/heads/main"],
    "scopes": ["manifests:write"],
}


def _entry(tenant: str = "acme", org_scopes: list[str] | None = None, **actions: Any) -> dict[str, Any]:
    return {
        "id": ORG_IDS["acme"],
        "tenant": tenant,
        "scopes": org_scopes if org_scopes is not None else ["audit:read"],
        "actions": {**_ACTIONS, **actions},
    }


def _orgs(**actions: Any) -> str:
    return json.dumps({"acme": _entry(**actions)})


def _settings(**kw: Any) -> Settings:
    base: dict[str, Any] = {
        "database_url": "memory://github-actions",
        "object_store": "memory",
        "environment": "development",
        "auth_mode": "jwt",
        "jwt_verifiers": "self:felix-self",
        "jwks_public": _KEY.as_pem(private=False).decode(),
        "jwks_private": _KEY.as_pem(private=True).decode(),
        # Explicit, so a developer's .env cannot switch the device flow on underneath a test.
        "github_client_id": "",
        "github_oidc_audience": ACTIONS_AUDIENCE,
        "github_org_tenants": _orgs(),
    }
    base.update(kw)
    return Settings(**base)


@pytest.fixture(autouse=True)
def _fresh_key_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github_actions, "_keys", github_actions._KeyCache())


async def _exchange(
    settings: Settings, token: str, fake: FakeGitHub | None = None, tenant: str | None = None
) -> github_actions.ActionsLogin:
    async with (fake or FakeGitHub()).client() as client:
        return await exchange_actions_token(settings, token, tenant=tenant, client=client)


async def _refused(
    settings: Settings, token: str, fake: FakeGitHub | None = None, tenant: str | None = None
) -> GitHubLoginError:
    with pytest.raises(GitHubLoginError) as info:
        await _exchange(settings, token, fake, tenant)
    return info.value


def _verified(settings: Settings, token: str) -> Any:
    result = verify_jwt(
        token, parse_verifiers(settings.jwt_verifiers), jwks_public=settings.jwks_public, settings=settings
    )
    assert result.ok, result
    return result


# --- the happy path ---------------------------------------------------------------------


async def test_a_granted_workflow_gets_a_token_the_api_accepts() -> None:
    settings = _settings(github_oidc_ttl_seconds=300)
    login = await _exchange(settings, actions_id_token())
    result = _verified(settings, login.token.access_token)
    assert result.principal.tenant_id == "acme"
    # The `actions` scopes, not the org's human ones.
    assert result.principal.scopes == frozenset({"manifests:write"})
    # Built from the repository id and the workflow file, not GitHub's customisable `sub`.
    assert result.payload["sub"] == (
        f"github-actions:{DEPLOY_REPO_ID}:acme/deploy/.github/workflows/ship.yml@refs/heads/main"
    )
    assert result.payload["idp"] == "github-actions"
    # The actor triggered the run; they did not authenticate. `github_login` names someone who did.
    assert result.payload["github_actor"] == "octo"
    assert "github_login" not in result.payload
    assert login.token.github_login == ""
    assert result.payload["exp"] - result.payload["iat"] == 300
    assert login.run.audit() == {
        "repository": "acme/deploy",
        "repository_id": str(DEPLOY_REPO_ID),
        "ref": "refs/heads/main",
        "job_workflow_ref": "acme/deploy/.github/workflows/ship.yml@refs/heads/main",
        "environment": "",
        "event_name": "push",
        "sha": "0" * 40,
        "run_id": "777",
        "run_attempt": "1",
        "actor": "octo",
        "triggering_actor": "octo",
    }


async def test_a_verifier_audience_is_minted_into_the_token() -> None:
    """Without it, every Actions token would 401 at the middleware after a 200 here."""
    settings = _settings(jwt_verifiers="self:felix-self;aud=felix-api")
    login = await _exchange(settings, actions_id_token())
    assert _verified(settings, login.token.access_token).payload["aud"] == "felix-api"


async def test_ref_patterns_are_globs() -> None:
    settings = _settings(github_org_tenants=_orgs(refs=["refs/tags/v*"]))
    login = await _exchange(settings, actions_id_token(ref="refs/tags/v1.2.0"))
    assert login.run.ref == "refs/tags/v1.2.0"
    refused = await _refused(settings, actions_id_token())  # refs/heads/main
    assert (refused.code, refused.status) == ("workflow_not_granted", 403)


async def test_a_workflow_pattern_matches_the_file_that_ran_not_its_caller() -> None:
    """For a reusable workflow, `workflow_ref` is the caller; `job_workflow_ref` is what ran."""
    settings = _settings(
        github_org_tenants=_orgs(refs=[], workflows=["acme/deploy/.github/workflows/ship.yml@*"])
    )
    assert (await _exchange(settings, actions_id_token())).token.tenant == "acme"
    other = "acme/deploy/.github/workflows/lint.yml@refs/heads/main"
    refused = await _refused(
        settings,
        actions_id_token(job_workflow_ref=other, workflow_ref="acme/deploy/.github/workflows/ship.yml@x"),
    )
    assert refused.code == "workflow_not_granted"


async def test_an_environment_narrowing_needs_the_run_to_be_in_it() -> None:
    settings = _settings(github_org_tenants=_orgs(refs=[], environments=["production"]))
    assert (
        await _exchange(settings, actions_id_token(environment="production"))
    ).run.environment == "production"
    assert (await _refused(settings, actions_id_token())).code == "workflow_not_granted"


async def test_every_narrowing_set_must_match() -> None:
    settings = _settings(github_org_tenants=_orgs(environments=["production"]))  # and refs: main
    assert (
        await _refused(settings, actions_id_token(environment="production", ref="refs/heads/x"))
    ).code == ("workflow_not_granted")


# --- who may not ------------------------------------------------------------------------


async def test_an_unlisted_repository_of_the_org_is_refused() -> None:
    """Write access to one repository is enough to run a workflow there; the org is too wide."""
    refused = await _refused(_settings(), actions_id_token(repository="acme/sandbox", repository_id="9002"))
    assert (refused.code, refused.status) == ("workflow_not_granted", 403)
    assert "acme/sandbox@refs/heads/main" in str(refused)


async def test_a_recreated_repository_with_a_listed_name_is_refused() -> None:
    """`deploy` deleted and created again by any member who may create repositories."""
    refused = await _refused(_settings(), actions_id_token(repository_id="424242"))
    assert refused.code == "workflow_not_granted"


async def test_the_owner_is_matched_by_id_not_name() -> None:
    """`acme` released and re-registered by someone else: same name, different id."""
    refused = await _refused(_settings(), actions_id_token(repository_owner_id="999999"))
    assert refused.code == "workflow_not_granted"


async def test_an_org_without_an_actions_block_grants_its_workflows_nothing() -> None:
    orgs = {"acme": {"id": ORG_IDS["acme"], "tenant": "acme", "scopes": ["manifests:write"]}}
    refused = await _refused(_settings(github_org_tenants=json.dumps(orgs)), actions_id_token())
    assert refused.code == "workflow_not_granted"


@pytest.mark.parametrize("event", sorted(github_actions.UNTRUSTED_EVENTS))
async def test_an_event_acting_on_outside_input_is_refused_on_an_allowed_ref(event: str) -> None:
    """A fork's `pull_request_target` runs on refs/heads/main; the ref says nothing about its code."""
    refused = await _refused(_settings(), actions_id_token(event_name=event))
    assert refused.code == "workflow_not_granted"


async def test_listed_events_are_the_only_ones_admitted() -> None:
    settings = _settings(github_org_tenants=_orgs(events=["workflow_run"]))
    assert (
        await _exchange(settings, actions_id_token(event_name="workflow_run"))
    ).run.event_name == "workflow_run"
    assert (await _refused(settings, actions_id_token(event_name="push"))).code == "workflow_not_granted"


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"aud": "https://github.com/acme"}, id="github-default-audience"),
        pytest.param({"aud": "https://elsewhere.test"}, id="other-audience"),
        pytest.param({"iss": "https://token.actions.example.test"}, id="other-issuer"),
        pytest.param({"exp": int(time.time()) - 3600}, id="expired"),
        pytest.param({"exp": None}, id="no-exp"),
        pytest.param({"iat": None}, id="no-iat"),
        pytest.param({"repository_owner_id": None}, id="no-owner-id"),
        pytest.param({"repository_owner_id": "10x"}, id="non-numeric-owner-id"),
        pytest.param({"repository_id": None}, id="no-repository-id"),
        pytest.param({"repository": None}, id="no-repository"),
        pytest.param({"job_workflow_ref": None}, id="no-job-workflow-ref"),
    ],
)
async def test_a_token_with_the_wrong_claims_is_refused(overrides: dict[str, Any]) -> None:
    refused = await _refused(_settings(), actions_id_token(**overrides))
    assert (refused.code, refused.status) == ("invalid_id_token", 401)
    assert str(refused) == "the GitHub Actions ID token was not accepted"


async def test_a_token_signed_by_another_key_is_refused() -> None:
    forged = jwk.RSAKey.generate_key(2048, parameters={"kid": "actions-1"})
    refused = await _refused(_settings(), actions_id_token(forged))
    assert refused.code == "invalid_id_token"


async def test_only_rs256_is_accepted_even_from_a_published_key() -> None:
    """Pins the algorithm allowlist: an ES256 key in GitHub's set would verify under a wider one."""
    ec = jwk.ECKey.generate_key("P-256", parameters={"kid": "ec-1"})
    fake = FakeGitHub(actions_keys=[ACTIONS_KEY, ec])
    refused = await _refused(_settings(), actions_id_token(ec, alg="ES256"), fake)
    assert refused.code == "invalid_id_token"


# --- tenants ----------------------------------------------------------------------------


def _two_tenants() -> str:
    # A second entry for the same org id, mapped to another tenant.
    return json.dumps({"acme": _entry(), "acme-ops": _entry(tenant="ops", org_scopes=[])})


async def test_two_tenants_without_a_choice_is_ambiguous_and_the_token_is_not_spent() -> None:
    settings = _settings(github_org_tenants=_two_tenants())
    token = actions_id_token()
    refused = await _refused(settings, token)
    assert (refused.code, refused.status) == ("tenant_ambiguous", 409)
    assert refused.tenants == ("acme", "ops")
    assert (await _exchange(settings, token, tenant="ops")).token.tenant == "ops"


async def test_an_entry_that_does_not_admit_the_run_does_not_make_it_ambiguous() -> None:
    orgs = {"acme": _entry(), "acme-ops": _entry(tenant="ops", refs=["refs/heads/release"])}
    login = await _exchange(_settings(github_org_tenants=json.dumps(orgs)), actions_id_token())
    assert login.token.tenant == "acme"


async def test_two_grants_on_one_tenant_union_only_the_ones_that_admit_the_run() -> None:
    """Narrowing is applied per grant, before the union: main gets both, a branch gets one."""
    orgs = {
        "acme": _entry(refs=["refs/heads/main"], scopes=["manifests:write"]),
        "acme-ci": _entry(refs=["refs/heads/*"], scopes=["jobs:read"]),
    }
    settings = _settings(github_org_tenants=json.dumps(orgs))
    on_main = await _exchange(settings, actions_id_token())
    assert on_main.token.scopes == ("manifests:write", "jobs:read")
    on_branch = await _exchange(settings, actions_id_token(ref="refs/heads/feature"))
    assert on_branch.token.scopes == ("jobs:read",)


async def test_choosing_a_tenant_the_workflow_is_not_granted_is_refused() -> None:
    refused = await _refused(_settings(), actions_id_token(), tenant="globex")
    assert (refused.code, refused.status) == ("tenant_not_granted", 403)


# --- the issuer's keys ------------------------------------------------------------------


async def test_keys_are_fetched_once_and_reused() -> None:
    fake = FakeGitHub()
    for _ in range(3):
        await _exchange(_settings(), actions_id_token(), fake)
    assert fake.actions_jwks_fetches == 1


async def test_a_rotated_key_is_picked_up_without_a_restart() -> None:
    fake = FakeGitHub()
    settings = _settings()
    await _exchange(settings, actions_id_token(), fake)
    rotated = jwk.RSAKey.generate_key(2048, parameters={"kid": "actions-2"})
    fake.actions_keys = [rotated]
    # Within the refetch floor an unknown key is refused without asking GitHub again: anyone can
    # present a token naming a key we do not hold.
    assert (await _refused(settings, actions_id_token(rotated), fake)).code == "invalid_id_token"
    assert fake.actions_jwks_fetches == 1
    github_actions._keys.fetched_at -= github_actions.KEYS_REFETCH_MIN_S + 1
    assert (await _exchange(settings, actions_id_token(rotated), fake)).token.tenant == "acme"
    assert fake.actions_jwks_fetches == 2


async def test_a_withdrawn_key_stops_verifying_once_the_cache_expires() -> None:
    fake = FakeGitHub()
    settings = _settings()
    await _exchange(settings, actions_id_token(), fake)
    fake.actions_keys = [jwk.RSAKey.generate_key(2048, parameters={"kid": "actions-2"})]
    github_actions._keys.fetched_at -= github_actions.KEYS_TTL_S + 1
    assert (await _refused(settings, actions_id_token(), fake)).code == "invalid_id_token"
    assert fake.actions_jwks_fetches == 2


def _issuer(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _down(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("no route", request=request)


@pytest.mark.parametrize(
    "handler",
    [
        pytest.param(_down, id="unreachable"),
        pytest.param(lambda r: httpx.Response(500, json={}), id="5xx"),
        pytest.param(lambda r: httpx.Response(200, json={"keys": "x"}), id="unparseable"),
    ],
)
async def test_an_unusable_issuer_is_a_502(handler: Any) -> None:
    async with _issuer(handler) as client:
        with pytest.raises(GitHubLoginError) as info:
            await exchange_actions_token(_settings(), actions_id_token(), client=client)
    assert (info.value.code, info.value.status) == ("github_unavailable", 502)


async def test_a_failed_first_fetch_is_not_retried_by_every_request() -> None:
    """Anonymous callers arriving during an outage must not each start an outbound fetch."""
    calls = 0

    def counting(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("no route", request=request)

    async with _issuer(counting) as client:
        for _ in range(3):
            with pytest.raises(GitHubLoginError) as info:
                await exchange_actions_token(_settings(), actions_id_token(), client=client)
            assert info.value.code == "github_unavailable"
    assert calls == 1


# --- configuration ----------------------------------------------------------------------


def test_the_actions_block_parses() -> None:
    raw = _orgs(repositories={"Deploy": 9001, "infra": 9002}, workflows=["w"], events=["push"])
    grant = parse_org_tenants(raw)["acme"]
    assert grant.actions is not None
    assert dict(grant.actions.repositories) == {"deploy": 9001, "infra": 9002}
    assert (grant.actions.refs, grant.actions.workflows, grant.actions.events) == (
        ("refs/heads/main",),
        ("w",),
        ("push",),
    )
    assert grant.actions.scopes == ("manifests:write",)


@pytest.mark.parametrize(
    ("actions", "message"),
    [
        pytest.param({"refs": ["refs/x"], "scopes": []}, "`repositories` must map", id="no-repositories"),
        pytest.param(
            {"repositories": {}, "refs": ["refs/x"], "scopes": []}, "`repositories` must map", id="empty"
        ),
        pytest.param(
            {"repositories": ["deploy"], "refs": ["refs/x"], "scopes": []},
            "`repositories` must map",
            id="list",
        ),
        pytest.param(
            {"repositories": {"acme/deploy": 1}, "refs": ["refs/x"], "scopes": []},
            "not owner/name",
            id="slash",
        ),
        pytest.param(
            {"repositories": {"..": 1}, "refs": ["refs/x"], "scopes": []}, "not a repository", id="dotdot"
        ),
        pytest.param(
            {"repositories": {"d": "1"}, "refs": ["refs/x"], "scopes": []}, "numeric GitHub id", id="str-id"
        ),
        pytest.param(
            {"repositories": {"d": 1, "D": 2}, "refs": ["refs/x"], "scopes": []},
            "listed twice",
            id="dup-name",
        ),
        pytest.param({"repositories": {"d": 1}, "scopes": []}, "set at least one of", id="no-narrowing"),
        pytest.param({"repositories": {"d": 1}, "refs": ["refs/x"]}, "`scopes` is required", id="no-scopes"),
        pytest.param(
            {"repositories": {"d": 1}, "refs": ["main"], "scopes": []}, "starting refs/", id="short-ref"
        ),
        pytest.param(
            {"repositories": {"d": 1}, "refs": ["refs/x"], "scopes": [], "env": "x"},
            "unknown keys",
            id="unknown",
        ),
        pytest.param(
            {"repositories": {"d": 1}, "refs": ["refs/x"], "scopes": ["a b"]},
            "whitespace",
            id="space-in-scope",
        ),
    ],
)
def test_a_malformed_actions_block_is_refused(actions: dict[str, Any], message: str) -> None:
    raw = json.dumps({"acme": {"id": 1, "tenant": "t", "actions": actions}})
    with pytest.raises(ValueError, match=re.escape(message)):
        parse_org_tenants(raw)


def test_a_working_actions_configuration_starts_without_an_oauth_app() -> None:
    _settings().validate_runtime()


def test_a_working_configuration_starts_with_both_logins_on() -> None:
    _settings(github_client_id="Iv1.test").validate_runtime()


@pytest.mark.parametrize("device", [pytest.param("", id="actions-only"), pytest.param("Iv1.test", id="both")])
@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        pytest.param(
            {"github_oidc_audience": "https://github.com/acme"}, "not a https://github.com", id="gh-aud"
        ),
        pytest.param({"github_oidc_audience": "sts.amazonaws.com"}, "own https URL", id="aws-aud"),
        pytest.param(
            {"github_oidc_audience": "https://iam.googleapis.com/projects/1/x"}, "own https URL", id="gcp-aud"
        ),
        pytest.param(
            {"github_org_tenants": json.dumps({"acme": {"id": 1, "tenant": "acme", "scopes": []}})},
            "needs an `actions` block",
            id="no-actions-block",
        ),
        pytest.param({"jwks_private": ""}, "FELIX_JWKS_PRIVATE", id="cannot-mint"),
    ],
)
def test_an_actions_login_that_cannot_work_does_not_start(
    device: str, overrides: dict[str, Any], message: str
) -> None:
    with pytest.raises(RuntimeError, match=re.escape(message)):
        _settings(github_client_id=device, **overrides).validate_runtime()


def test_the_boot_probe_mints_the_actions_token_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """An Actions mint that verifiers would refuse fails boot, not every CI run."""

    def refused(settings: Any, grants: Any) -> Any:
        raise RuntimeError("actions mint broken")

    monkeypatch.setattr(github_actions, "mint_probe", refused)
    with pytest.raises(RuntimeError, match="actions mint broken"):
        _settings().validate_runtime()


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "felix.auth.github" and r.levelno == logging.WARNING
    ]


def test_actions_blocks_without_an_audience_start_but_say_so(caplog: pytest.LogCaptureFixture) -> None:
    _settings(github_client_id="Iv1.test", github_oidc_audience="").validate_runtime()
    assert any("FELIX_GITHUB_OIDC_AUDIENCE is empty" in m for m in _warnings(caplog))


def test_admin_in_actions_scopes_starts_but_says_so(caplog: pytest.LogCaptureFixture) -> None:
    orgs = json.dumps({"acme": _entry(org_scopes=[], scopes=["admin"])})
    _settings(github_org_tenants=orgs).validate_runtime()
    assert any("admin scope" in m for m in _warnings(caplog))


def test_the_exchange_path_is_public_only_while_an_audience_is_set() -> None:
    assert public_login_paths(_settings()) == {GITHUB_ACTIONS_LOGIN_PATH}
    assert public_login_paths(_settings(github_oidc_audience="")) == frozenset()
    # Both on: the device paths stay public beside it.
    both = public_login_paths(_settings(github_client_id="Iv1.test"))
    assert both == GITHUB_LOGIN_PATHS | {GITHUB_ACTIONS_LOGIN_PATH}
