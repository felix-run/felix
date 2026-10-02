"""GitHub login: device flow → org membership → a self-issued token the API accepts.

GitHub is faked at the transport (`httpx.MockTransport`), so every request this module
builds — URLs, form fields, headers — is the one production sends. A minted token is
checked with `verify_jwt` over the configured verifiers, the call the auth middleware makes.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
import pytest
from felix.auth import github
from felix.auth.github import GitHubLoginError, exchange_device_code, parse_org_tenants, start_device_flow
from felix.auth.jwt import mint_token, parse_verifiers, verify_jwt
from felix.config import Settings
from joserfc import jwk

from tests.github_fake import FakeGitHub
from tests.github_fake import org as _org

_KEY = jwk.RSAKey.generate_key(2048)
_PRIVATE = _KEY.as_pem(private=True).decode()
_PUBLIC = _KEY.as_pem(private=False).decode()
_ORGS = {"acme": _org("acme", "acme", ["manifests:write", "audit:read"])}


def _settings(**kw: Any) -> Settings:
    base: dict[str, Any] = {
        "database_url": "memory://github",
        "object_store": "memory",
        "environment": "development",
        "auth_mode": "jwt",
        "jwt_verifiers": "self:felix-self",
        "jwks_public": _PUBLIC,
        "jwks_private": _PRIVATE,
        "github_client_id": "Iv1.test",
        "github_org_tenants": json.dumps(_ORGS),
    }
    base.update(kw)
    return Settings(**base)


async def _exchange(settings: Settings, fake: FakeGitHub, tenant: str | None = None) -> github.LoginToken:
    async with fake.client() as client:
        return await exchange_device_code(settings, "dev-1", tenant=tenant, client=client)


async def _refused(settings: Settings, fake: FakeGitHub, tenant: str | None = None) -> GitHubLoginError:
    with pytest.raises(GitHubLoginError) as info:
        await _exchange(settings, fake, tenant)
    return info.value


def _accepted(settings: Settings, token: str) -> dict[str, Any]:
    result = verify_jwt(
        token, parse_verifiers(settings.jwt_verifiers), jwks_public=settings.jwks_public, settings=settings
    )
    assert result.ok, result
    return {"principal": result.principal, "claims": result.payload}


# --- the org map ------------------------------------------------------------------------


def test_org_map_is_keyed_case_insensitively_and_keeps_the_configured_spelling() -> None:
    grants = parse_org_tenants(
        json.dumps({"Acme-Corp": {"id": 9, "tenant": "acme", "scopes": ["a", "b", "a"]}})
    )
    assert list(grants) == ["acme-corp"]
    assert grants["acme-corp"].org == "Acme-Corp"
    assert grants["acme-corp"].org_id == 9
    assert grants["acme-corp"].scopes == ("a", "b")


def test_the_cached_org_map_cannot_be_changed_by_a_caller() -> None:
    grants = parse_org_tenants(json.dumps(_ORGS))
    with pytest.raises(TypeError):
        grants["evil"] = grants["acme"]  # type: ignore[index]


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        pytest.param('["acme"]', "expected a JSON object", id="not-an-object"),
        pytest.param("{", "not valid JSON", id="not-json"),
        pytest.param(
            '{"acme/../admin": {"id": 1, "tenant": "t"}}', "not a GitHub org name", id="path-in-org"
        ),
        pytest.param('{"-acme": {"id": 1, "tenant": "t"}}', "not a GitHub org name", id="leading-hyphen"),
        pytest.param('{"ac--me": {"id": 1, "tenant": "t"}}', "not a GitHub org name", id="double-hyphen"),
        pytest.param(
            '{"a": {"id": 1, "tenant": "t"}, "A": {"id": 1, "tenant": "t"}}',
            "listed twice",
            id="duplicate-by-case",
        ),
        pytest.param('{"acme": {"tenant": "t"}}', "numeric GitHub id", id="missing-id"),
        pytest.param('{"acme": {"id": "101", "tenant": "t"}}', "numeric GitHub id", id="string-id"),
        pytest.param('{"acme": {"id": true, "tenant": "t"}}', "numeric GitHub id", id="boolean-id"),
        pytest.param('{"acme": {"id": 1, "tenant": ""}}', "`tenant` must be", id="empty-tenant"),
        pytest.param('{"acme": {"id": 1, "tenant": "t", "role": "x"}}', "unknown keys", id="unknown-key"),
        pytest.param(
            '{"acme": {"id": 1, "tenant": "t", "scopes": ["a b"]}}', "whitespace", id="space-in-scope"
        ),
        pytest.param(
            '{"acme": {"id": 1, "tenant": "t", "scopes": "a"}}', "list of non-empty", id="scopes-not-list"
        ),
    ],
)
def test_org_map_rejects_malformed_entries(raw: str, message: str) -> None:
    with pytest.raises(ValueError, match=re.escape(message)):
        parse_org_tenants(raw)


# --- the device flow --------------------------------------------------------------------


async def test_start_asks_github_for_a_device_code_with_read_org() -> None:
    fake = FakeGitHub()
    async with fake.client() as client:
        code = await start_device_flow(_settings(), client=client)
    assert (code.user_code, code.device_code, code.interval) == ("ABCD-1234", "dev-1", 5)
    sent = dict(httpx.QueryParams(fake.requests[0].content.decode()))
    assert sent == {"client_id": "Iv1.test", "scope": "read:org"}


@pytest.mark.parametrize(
    ("body", "code"),
    [
        pytest.param({"error": "device_flow_disabled"}, "github_config_error", id="flow-disabled"),
        pytest.param({"error": "something_new"}, "github_config_error", id="unknown-github-error"),
        pytest.param({"device_code": "d", "user_code": "u"}, "github_unavailable", id="incomplete"),
    ],
)
async def test_a_failed_start_says_whose_problem_it_is(body: dict[str, Any], code: str) -> None:
    fake = FakeGitHub(device_code_body=body)
    async with fake.client() as client:
        with pytest.raises(GitHubLoginError) as info:
            await start_device_flow(_settings(), client=client)
    assert info.value.code == code
    assert info.value.status == {"github_config_error": 503, "github_unavailable": 502}[code]


@pytest.mark.parametrize(
    ("error", "code", "status"),
    [
        ("authorization_pending", "authorization_pending", 428),
        ("slow_down", "slow_down", 428),
        ("expired_token", "expired_token", 400),
        ("incorrect_device_code", "invalid_device_code", 400),
        ("access_denied", "access_denied", 403),
        # GitHub's vocabulary never becomes ours: an unlisted error is the operator's.
        ("device_flow_disabled", "github_config_error", 503),
        ("a_code_github_adds_later", "github_config_error", 503),
    ],
)
async def test_an_unapproved_poll_says_what_to_do_next(error: str, code: str, status: int) -> None:
    fake = FakeGitHub(polls=[{"error": error, "interval": 10}])
    exc = await _refused(_settings(), fake)
    assert (exc.code, exc.status, exc.interval) == (code, status, 10)
    assert error in str(exc)  # GitHub's own string survives, in the message
    # Nothing past the poll is attempted until GitHub has approved.
    assert [r.url.host for r in fake.requests] == ["github.com"]


async def test_an_approved_poll_mints_a_token_the_api_accepts() -> None:
    settings = _settings()
    fake = FakeGitHub()
    minted = await _exchange(settings, fake)

    seen = _accepted(settings, minted.access_token)
    assert seen["principal"].tenant_id == "acme"
    assert seen["principal"].subject == "github:4242"
    assert seen["principal"].scopes == frozenset({"manifests:write", "audit:read"})
    assert seen["claims"]["idp"] == "github"
    assert seen["claims"]["github_login"] == "octo"
    assert seen["claims"]["exp"] - seen["claims"]["iat"] == settings.github_login_ttl_seconds
    # GitHub's token is used and dropped; it is in neither the token nor what wraps it.
    assert "gho_x" not in repr(minted)
    assert "gho_x" not in json.dumps(seen["claims"])
    assert fake.requests[-1].url.path == "/user/memberships/orgs/acme"


async def test_the_subject_is_the_numeric_id_not_the_renameable_login() -> None:
    minted = await _exchange(_settings(), FakeGitHub(user={"id": 7, "login": "someone-else"}))
    assert minted.subject == "github:7"


async def test_a_boolean_user_id_is_not_an_id() -> None:
    exc = await _refused(_settings(), FakeGitHub(user={"id": True, "login": "octo"}))
    assert exc.code == "github_unavailable"


async def test_membership_is_read_at_the_configured_spelling_of_the_org() -> None:
    settings = _settings(github_org_tenants=json.dumps({"Acme": _org("acme", "acme")}))
    fake = FakeGitHub(memberships={"Acme": (200, "active")})
    minted = await _exchange(settings, fake)
    assert minted.tenant == "acme"


# --- membership -------------------------------------------------------------------------


async def test_a_pending_invitation_is_not_membership() -> None:
    exc = await _refused(_settings(), FakeGitHub(memberships={"acme": (200, "pending")}))
    assert (exc.code, exc.status) == ("not_a_member", 403)


async def test_a_non_member_is_refused() -> None:
    exc = await _refused(_settings(), FakeGitHub(memberships={}))
    assert (exc.code, exc.status) == ("not_a_member", 403)


async def test_an_org_hiding_membership_is_the_cause_but_not_named_to_the_caller(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Any GitHub user can finish the device flow, so the configured org names stay in the log."""
    exc = await _refused(_settings(), FakeGitHub(memberships={"acme": (403, "")}))
    assert (exc.code, exc.status) == ("org_access_restricted", 403)
    assert "acme" not in str(exc)
    assert "acme" in caplog.text


async def test_a_reregistered_org_name_grants_nothing() -> None:
    """The org was renamed or deleted and someone else took the name: its id changed."""
    exc = await _refused(_settings(), FakeGitHub(org_ids={"acme": 999}))
    assert (exc.code, exc.status) == ("not_a_member", 403)


_TWO_TENANTS = {
    "acme": _org("acme", "acme", ["audit:read"]),
    "globex": _org("globex", "globex", ["manifests:write"]),
}
_BOTH = {"acme": (200, "active"), "globex": (200, "active")}


async def test_two_tenants_without_a_choice_is_ambiguous() -> None:
    settings = _settings(github_org_tenants=json.dumps(_TWO_TENANTS))
    exc = await _refused(settings, FakeGitHub(memberships=_BOTH))
    assert (exc.code, exc.status, exc.tenants) == ("tenant_ambiguous", 409, ("acme", "globex"))


async def test_a_chosen_tenant_gets_only_that_orgs_scopes() -> None:
    settings = _settings(github_org_tenants=json.dumps(_TWO_TENANTS))
    minted = await _exchange(settings, FakeGitHub(memberships=_BOTH), tenant="globex")
    seen = _accepted(settings, minted.access_token)
    assert seen["principal"].tenant_id == "globex"
    assert seen["principal"].scopes == frozenset({"manifests:write"})


async def test_choosing_a_tenant_membership_does_not_grant_is_refused() -> None:
    settings = _settings(github_org_tenants=json.dumps(_TWO_TENANTS))
    exc = await _refused(settings, FakeGitHub(memberships={"acme": (200, "active")}), tenant="globex")
    assert (exc.code, exc.status) == ("tenant_not_granted", 403)


async def test_two_orgs_on_one_tenant_merge_their_scopes() -> None:
    orgs = {
        "acme": _org("acme", "acme", ["audit:read"]),
        "acme-ops": _org("acme-ops", "acme", ["jobs:write"]),
    }
    settings = _settings(github_org_tenants=json.dumps(orgs))
    minted = await _exchange(
        settings, FakeGitHub(memberships={"acme": (200, "active"), "acme-ops": (200, "active")})
    )
    seen = _accepted(settings, minted.access_token)
    assert seen["principal"].scopes == frozenset({"audit:read", "jobs:write"})


# --- GitHub failing ---------------------------------------------------------------------


async def test_a_github_error_is_a_502_not_a_refusal() -> None:
    exc = await _refused(_settings(), FakeGitHub(api_status=500))
    assert (exc.code, exc.status) == ("github_unavailable", 502)


@pytest.mark.parametrize("status", [500, 401])
async def test_a_failed_membership_read_is_not_read_as_non_membership(status: int) -> None:
    """An outage or a revoked token must not tell a member they are not one."""
    exc = await _refused(_settings(), FakeGitHub(membership_status=status))
    assert (exc.code, exc.status) == ("github_unavailable", 502)


async def test_an_approval_without_a_token_is_a_502() -> None:
    exc = await _refused(_settings(), FakeGitHub(polls=[{"token_type": "bearer"}]))
    assert (exc.code, exc.status) == ("github_unavailable", 502)


async def test_an_unreachable_github_is_a_502() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(down)) as client:
        with pytest.raises(GitHubLoginError) as info:
            await exchange_device_code(_settings(), "dev-1", client=client)
    assert (info.value.code, info.value.status) == ("github_unavailable", 502)


async def test_the_default_client_is_the_one_production_uses(monkeypatch: pytest.MonkeyPatch) -> None:
    """No `client=`: the call shape routes will make, through the one replaceable seam."""
    fake = FakeGitHub()
    monkeypatch.setattr(github, "github_http_client", lambda settings: fake.client())
    minted = await exchange_device_code(_settings(), "dev-1")
    assert minted.tenant == "acme"


# --- minting ----------------------------------------------------------------------------


def test_extra_claims_cannot_override_identity_or_authority() -> None:
    for claim in ("tenant_id", "sub", "scope", "iss", "exp"):
        with pytest.raises(ValueError, match=claim):
            mint_token(_settings(), sub="s", tenant_id="t", scopes=[], extra_claims={claim: "x"})


async def test_a_verifier_audience_is_minted_into_the_token() -> None:
    settings = _settings(jwt_verifiers="self:felix-self;aud=felix-api")
    minted = await _exchange(settings, FakeGitHub())
    assert _accepted(settings, minted.access_token)["claims"]["aud"] == "felix-api"


# --- startup validation -----------------------------------------------------------------


def test_a_working_configuration_starts() -> None:
    _settings().validate_runtime()


def test_login_off_needs_nothing_else() -> None:
    _settings(github_client_id="", jwks_private="", auth_mode="none", allow_insecure=True).validate_runtime()


_OTHER = jwk.RSAKey.generate_key(2048).as_pem(private=False).decode()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        pytest.param({"github_org_tenants": "{"}, "FELIX_GITHUB_ORG_TENANTS", id="bad-json"),
        pytest.param({"github_org_tenants": "{}"}, "at least one org", id="empty-map"),
        pytest.param(
            {"github_org_tenants": json.dumps({"acme": _org("acme", "acme corp")})},
            "FELIX_GITHUB_ORG_TENANTS (acme)",
            id="unusable-tenant",
        ),
        pytest.param(
            {"auth_mode": "api_key", "auth_api_keys": "{}"}, "no JWT verifier is in play", id="not-jwt-mode"
        ),
        pytest.param({"jwt_verifiers": "self:https://elsewhere"}, "self:felix-self", id="no-self-verifier"),
        pytest.param({"jwks_private": ""}, "FELIX_JWKS_PRIVATE", id="no-private-key"),
        pytest.param({"jwks_public": _OTHER}, "FELIX_JWKS_PUBLIC pairs", id="mismatched-keys"),
        pytest.param({"allowed_tenants": "globex"}, "not in FELIX_ALLOWED_TENANTS", id="tenant-not-allowed"),
        pytest.param(
            {"github_org_tenants": '{"acme": {"tenant": "acme"}}'}, "numeric GitHub id", id="no-org-id"
        ),
        pytest.param(
            {"jwt_verifiers": "self:felix-self;tenant=fixed:ops,self:felix-self"},
            "pins the tenant",
            id="fixed-verifier-first",
        ),
    ],
)
def test_a_login_that_would_mint_refused_tokens_does_not_start(
    overrides: dict[str, Any], message: str
) -> None:
    with pytest.raises(RuntimeError, match=re.escape(message)):
        _settings(**overrides).validate_runtime()


def test_admin_scope_starts_but_says_so(caplog: pytest.LogCaptureFixture) -> None:
    _settings(github_org_tenants=json.dumps({"acme": _org("acme", "acme", ["admin"])})).validate_runtime()
    assert "admin scope" in caplog.text


def test_an_org_map_without_a_client_id_starts_but_says_login_is_off(
    caplog: pytest.LogCaptureFixture,
) -> None:
    _settings(github_client_id="").validate_runtime()
    assert "login is off" in caplog.text


def test_the_ttl_ceiling_is_a_day_since_nothing_revokes_a_token() -> None:
    with pytest.raises(ValueError):
        _settings(github_login_ttl_seconds=86_401)
