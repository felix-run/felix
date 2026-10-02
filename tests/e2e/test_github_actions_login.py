"""GitHub Actions login over real HTTP: a workflow's ID token → Felix token → a scoped route.

Boots the zero-argument `create_application()` under `auth_mode=jwt`, with the Actions issuer's
key set served by the fake at the `github_http_client` seam. What this proves that the unit
tests cannot: the exchange is reachable without a credential only while its audience is set,
and the token it returns is one the auth middleware then accepts, tenant and scopes intact.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from felix.auth import github, github_actions
from joserfc import jwk

from tests.github_fake import ACTIONS_AUDIENCE, DEPLOY_REPO_ID, ORG_IDS, FakeGitHub, actions_id_token

_KEY = jwk.RSAKey.generate_key(2048)
_ORGS = {
    "acme": {
        "id": ORG_IDS["acme"],
        "tenant": "acme",
        "scopes": ["audit:read"],
        "actions": {
            "repositories": {"deploy": DEPLOY_REPO_ID},
            "refs": ["refs/heads/main"],
            "scopes": ["jobs:read"],
        },
    }
}


def _env(**overrides: str) -> dict[str, str]:
    env = {
        "FELIX_ENVIRONMENT": "development",
        "FELIX_AUTH_MODE": "jwt",
        "FELIX_JWT_VERIFIERS": "self:felix-self",
        "FELIX_JWKS_PUBLIC": _KEY.as_pem(private=False).decode(),
        "FELIX_JWKS_PRIVATE": _KEY.as_pem(private=True).decode(),
        "FELIX_ALLOWED_TENANTS": "acme,ops",
        "FELIX_GITHUB_CLIENT_ID": "",
        "FELIX_GITHUB_OIDC_AUDIENCE": ACTIONS_AUDIENCE,
        "FELIX_GITHUB_ORG_TENANTS": json.dumps(_ORGS),
    }
    env.update(overrides)
    return env


@pytest.fixture(autouse=True)
def fake_github(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    fake = FakeGitHub()
    monkeypatch.setattr(github, "github_http_client", lambda settings: fake.client())
    monkeypatch.setattr(github_actions, "_keys", github_actions._KeyCache())
    return fake


async def test_a_granted_workflow_logs_in_and_the_token_opens_its_tenant(
    boot: Any, caplog: pytest.LogCaptureFixture
) -> None:
    from felix.audit import store as audit_store
    from felix.flush import flush_all

    caplog.set_level(logging.DEBUG)
    id_token = actions_id_token()
    async with boot([], env=_env()) as app:
        granted = await app.client.post("/auth/github/actions", json={"id_token": id_token})
        assert granted.status_code == 200, granted.text
        out = granted.json()
        assert (out["token_type"], out["tenant"], out["scopes"]) == ("Bearer", "acme", ["jobs:read"])
        assert out["expires_in"] == 900

        bearer = {"Authorization": f"Bearer {out['access_token']}"}
        assert (await app.client.get("/jobs", headers=bearer)).status_code == 200
        # The `actions` scopes, not the org's: audit:read is the humans' grant.
        assert (await app.client.get("/audit", headers=bearer)).status_code == 403

        await flush_all(app.settings)
        events, _ = await audit_store.list_events(app.settings, "acme", event_type="github_actions_login")

    assert len(events) == 1
    workflow = "acme/deploy/.github/workflows/ship.yml@refs/heads/main"
    assert events[0]["principal_subj"] == f"github-actions:{DEPLOY_REPO_ID}:{workflow}"
    payload = events[0]["payload_json"]
    assert payload["repository"] == "acme/deploy"
    assert payload["job_workflow_ref"] == workflow
    assert (payload["ref"], payload["event_name"], payload["run_id"], payload["run_attempt"]) == (
        "refs/heads/main",
        "push",
        "777",
        "1",
    )
    assert (payload["actor"], payload["triggering_actor"]) == ("octo", "octo")
    assert payload["scopes"] == ["jobs:read"]
    # The ID token is a bearer credential for its lifetime: in neither the audit row nor the log.
    assert id_token not in json.dumps(events)
    assert id_token not in caplog.text


async def test_a_refused_workflow_gets_the_error_shape(boot: Any) -> None:
    async with boot([], env=_env()) as app:
        refused = await app.client.post(
            "/auth/github/actions",
            json={"id_token": actions_id_token(repository="acme/sandbox", repository_id="9002")},
        )
        invalid = await app.client.post("/auth/github/actions", json={"id_token": "not.a.jwt"})
    assert refused.status_code == 403
    assert refused.json()["error"] == "workflow_not_granted"
    assert "acme/sandbox@refs/heads/main" in refused.json()["message"]
    assert invalid.status_code == 401
    assert invalid.json() == {
        "error": "invalid_id_token",
        "message": "the GitHub Actions ID token was not accepted",
    }


async def test_a_workflow_granted_two_tenants_names_one(boot: Any) -> None:
    ops = {**_ORGS["acme"], "tenant": "ops"}
    env = _env(FELIX_GITHUB_ORG_TENANTS=json.dumps({**_ORGS, "acme-ops": ops}))
    id_token = actions_id_token()
    async with boot([], env=env) as app:
        ambiguous = await app.client.post("/auth/github/actions", json={"id_token": id_token})
        chosen = await app.client.post("/auth/github/actions", json={"id_token": id_token, "tenant": "ops"})
    assert ambiguous.status_code == 409
    assert ambiguous.json()["tenants"] == ["acme", "ops"]
    assert chosen.status_code == 200, chosen.text
    assert chosen.json()["tenant"] == "ops"


async def test_an_actions_token_is_not_a_felix_credential(boot: Any) -> None:
    """Only the exchange accepts it: presented as a bearer it is just a token no verifier knows."""
    async with boot([], env=_env()) as app:
        direct = await app.client.get("/jobs", headers={"Authorization": f"Bearer {actions_id_token()}"})
    assert direct.status_code == 401


async def test_with_no_audience_the_exchange_is_not_public(boot: Any) -> None:
    async with boot([], env=_env(FELIX_GITHUB_OIDC_AUDIENCE="", FELIX_GITHUB_ORG_TENANTS="")) as app:
        closed = await app.client.post("/auth/github/actions", json={"id_token": actions_id_token()})
    assert closed.status_code == 401


async def test_with_no_audience_and_no_auth_the_exchange_is_absent(boot: Any) -> None:
    # Explicit, so a developer's .env cannot turn the exchange on underneath this test.
    async with boot([], env={"FELIX_GITHUB_OIDC_AUDIENCE": "", "FELIX_GITHUB_ORG_TENANTS": ""}) as app:
        absent = await app.client.post("/auth/github/actions", json={"id_token": actions_id_token()})
    assert absent.status_code == 404
