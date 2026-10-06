"""Browser sign-in by redirect, over real HTTP: authorize → GitHub → callback → exchange.

Boots `create_application()` under `auth_mode=jwt` with GitHub faked at `github_http_client`,
and plays the browser: it follows the redirect to GitHub by hand (approving issues a code bound
to the PKCE challenge), returns through the callback carrying whatever cookies the harness set,
and collects the token. What this pins that nothing else does: the token never appears in a
URL, the handoff is single-use, a callback this browser did not start is refused, the only
redirect target is an allowed origin, and the connection a sign-in stores is usable, private
and revocable over the API.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from felix.auth import github, github_connections
from joserfc import jwk

from tests.github_fake import FakeGitHub, app_grant, org

_KEY = jwk.RSAKey.generate_key(2048)
APP = "http://localhost:5181"


def _env(**overrides: str) -> dict[str, str]:
    env = {
        "FELIX_ENVIRONMENT": "development",
        "FELIX_AUTH_MODE": "jwt",
        "FELIX_JWT_VERIFIERS": "self:felix-self",
        "FELIX_JWKS_PUBLIC": _KEY.as_pem(private=False).decode(),
        "FELIX_JWKS_PRIVATE": _KEY.as_pem(private=True).decode(),
        "FELIX_ALLOWED_TENANTS": "acme,globex",
        "FELIX_GITHUB_CLIENT_ID": "Iv23.e2e",
        "FELIX_GITHUB_CLIENT_SECRET": "app-secret",
        "FELIX_GITHUB_TOKEN_KEY": base64.b64encode(os.urandom(32)).decode(),
        "FELIX_GITHUB_REDIRECT_ORIGINS": APP,
        "FELIX_GITHUB_ORG_TENANTS": json.dumps({"acme": org("acme", "acme", ["jobs:read"])}),
    }
    env.update(overrides)
    return env


@pytest.fixture
def fake_github(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    fake = FakeGitHub()
    monkeypatch.setattr(github, "github_http_client", lambda settings: fake.client())
    github_connections.reset_github_connections_for_tests()
    return fake


async def _to_github(client: Any, return_to: str = f"{APP}/t/abc") -> dict[str, str]:
    started = await client.get("/auth/github/authorize", params={"return_to": return_to})
    assert started.status_code == 302, started.text
    location = urlsplit(started.headers["location"])
    assert (location.scheme, location.netloc, location.path) == (
        "https",
        "github.com",
        "/login/oauth/authorize",
    )
    return {k: v[0] for k, v in parse_qs(location.query).items()}


async def _sign_in(client: Any, fake: FakeGitHub, answer: dict[str, Any] | None = None) -> Any:
    query = await _to_github(client)
    code = fake.issue_web_code(query["code_challenge"], answer)
    return await client.get("/auth/github/callback", params={"code": code, "state": query["state"]})


async def test_a_member_signs_in_by_redirect_and_the_token_never_rides_a_url(
    boot: Any, fake_github: FakeGitHub
) -> None:
    async with boot([], env=_env()) as app:
        assert (await app.client.get("/auth/methods")).json()["github_redirect"] is True

        query = await _to_github(app.client)
        assert query["redirect_uri"] == f"{APP}/api/auth/github/callback"
        assert query["code_challenge_method"] == "S256"
        assert "scope" not in query  # a GitHub App's access is its permissions

        code = fake_github.issue_web_code(query["code_challenge"])
        back = await app.client.get("/auth/github/callback", params={"code": code, "state": query["state"]})
        assert back.status_code == 302
        assert back.headers["location"] == f"{APP}/t/abc#felix_login=ok"
        for header in back.headers.get_list("set-cookie"):
            assert "HttpOnly" in header
        assert "gho_" not in back.headers["location"] and "eyJ" not in back.headers["location"]

        collected = await app.client.post("/auth/github/exchange")
        assert collected.status_code == 200, collected.text
        out = collected.json()
        assert (out["tenant"], out["scopes"], out["github_login"]) == ("acme", ["jobs:read"], "octo")
        bearer = {"Authorization": f"Bearer {out['access_token']}"}
        assert (await app.client.get("/jobs", headers=bearer)).status_code == 200

        # Single use: a reload or a replay finds nothing waiting.
        again = await app.client.post("/auth/github/exchange")
        assert again.status_code == 400
        assert again.json()["error"] == "expired_token"

        # The sign-in kept the person's GitHub connection, and says so without a token in sight.
        state = await app.client.get("/github/connection", headers=bearer)
        assert state.status_code == 200
        body = state.json()
        assert body["connected"] is True
        assert body["connection"]["github_login"] == "octo"
        assert "gho_" not in state.text and "ghr_" not in state.text


async def test_a_callback_this_browser_did_not_start_is_refused(boot: Any, fake_github: FakeGitHub) -> None:
    async with boot([], env=_env()) as app:
        query = await _to_github(app.client)
        code = fake_github.issue_web_code(query["code_challenge"])
        forged = await app.client.get(
            "/auth/github/callback", params={"code": code, "state": "someone-elses"}
        )
        assert forged.status_code == 302
        assert forged.headers["location"].endswith("#felix_login_error=state_mismatch")
        # And with no flow cookie at all there is nowhere trusted to send anyone.
        app.client.cookies.clear()
        cold = await app.client.get("/auth/github/callback", params={"code": code, "state": query["state"]})
        assert cold.status_code == 400
        assert cold.json()["error"] == "sign_in_expired"
        assert (await app.client.post("/auth/github/exchange")).status_code == 400


@pytest.mark.parametrize(
    "return_to",
    [
        "https://evil.example.net/",
        "http://localhost:5182/",
        f"{APP}//evil.example.net/",
        "/relative",
        "javascript:alert(1)",
    ],
)
async def test_the_only_return_target_is_an_allowed_origin(
    boot: Any, fake_github: FakeGitHub, return_to: str
) -> None:
    async with boot([], env=_env()) as app:
        refused = await app.client.get("/auth/github/authorize", params={"return_to": return_to})
        assert refused.status_code == 400
        assert "location" not in refused.headers


async def test_a_refusal_returns_to_the_app_with_its_code(boot: Any, fake_github: FakeGitHub) -> None:
    fake_github.memberships = {}
    async with boot([], env=_env()) as app:
        back = await _sign_in(app.client, fake_github)
        assert back.status_code == 302
        # The account GitHub signed in rides along, so the app can say which one was refused.
        assert back.headers["location"] == f"{APP}/t/abc#felix_login_error=not_a_member&login=octo"
        assert (await app.client.post("/auth/github/exchange")).status_code == 400


async def test_an_uninvited_account_returns_not_invited(boot: Any, fake_github: FakeGitHub) -> None:
    fake_github.memberships = {}
    signup = {
        "FELIX_GITHUB_SIGNUP": "invite",
        "FELIX_GITHUB_SIGNUP_LOGINS": "someone-else",
        "FELIX_GITHUB_SIGNUP_SCOPES": "jobs:read",
    }
    async with boot([], env=_env(**signup)) as app:
        back = await _sign_in(app.client, fake_github)
        assert back.headers["location"] == f"{APP}/t/abc#felix_login_error=not_invited&login=octo"


async def test_cancelling_on_github_returns_access_denied(boot: Any, fake_github: FakeGitHub) -> None:
    async with boot([], env=_env()) as app:
        query = await _to_github(app.client)
        back = await app.client.get(
            "/auth/github/callback", params={"error": "access_denied", "state": query["state"]}
        )
        assert back.headers["location"].endswith("#felix_login_error=access_denied")


async def test_the_redirect_routes_are_not_served_while_redirect_sign_in_is_off(
    boot: Any, fake_github: FakeGitHub
) -> None:
    async with boot([], env=_env(FELIX_GITHUB_CLIENT_SECRET="")) as app:
        assert (await app.client.get("/auth/methods")).json()["github_redirect"] is False
        # Off is not public: the middleware refuses before the route can say 404.
        assert (
            await app.client.get("/auth/github/authorize", params={"return_to": f"{APP}/"})
        ).status_code in {401, 404}


async def test_a_person_revokes_their_own_connection_and_github_hears_of_it(
    boot: Any, fake_github: FakeGitHub
) -> None:
    async with boot([], env=_env()) as app:
        await _sign_in(app.client, fake_github, app_grant("gho_person", "ghr_person"))
        token = (await app.client.post("/auth/github/exchange")).json()["access_token"]
        bearer = {"Authorization": f"Bearer {token}"}

        assert (await app.client.delete("/github/connection", headers=bearer)).status_code == 204
        assert fake_github.revoked_grants == ["gho_person"]
        state = (await app.client.get("/github/connection", headers=bearer)).json()
        assert state == {"connected": False, "connection": None}


async def test_the_operator_view_needs_github_admin(boot: Any, fake_github: FakeGitHub) -> None:
    orgs = {"acme": org("acme", "acme", ["jobs:read"])}
    async with boot([], env=_env(FELIX_GITHUB_ORG_TENANTS=json.dumps(orgs))) as app:
        await _sign_in(app.client, fake_github)
        token = (await app.client.post("/auth/github/exchange")).json()["access_token"]
        bearer = {"Authorization": f"Bearer {token}"}
        assert (await app.client.get("/github/connections", headers=bearer)).status_code == 403
        assert (await app.client.delete("/github/connections/4242", headers=bearer)).status_code == 403

    admin = {"acme": org("acme", "acme", ["github:admin"])}
    async with boot([], env=_env(FELIX_GITHUB_ORG_TENANTS=json.dumps(admin))) as app:
        await _sign_in(app.client, fake_github)
        token = (await app.client.post("/auth/github/exchange")).json()["access_token"]
        bearer = {"Authorization": f"Bearer {token}"}
        listed = await app.client.get("/github/connections", headers=bearer)
        assert listed.status_code == 200
        assert [c["github_login"] for c in listed.json()["items"]] == ["octo"]
        assert "gho_" not in listed.text
        assert (await app.client.delete("/github/connections/4242", headers=bearer)).status_code == 204
        assert (await app.client.delete("/github/connections/4242", headers=bearer)).status_code == 404


async def test_a_device_login_through_the_app_keeps_the_connection_too(
    boot: Any, fake_github: FakeGitHub
) -> None:
    """`felix login` against the App: the same device flow, now returning a refresh token."""
    fake_github.polls = [app_grant("gho_x", "ghr_device")]
    async with boot([], env=_env()) as app:
        started = await app.client.post("/auth/github/device")
        granted = await app.client.post(
            "/auth/github/token", json={"device_code": started.json()["device_code"]}
        )
        assert granted.status_code == 200, granted.text
        bearer = {"Authorization": f"Bearer {granted.json()['access_token']}"}
        assert (await app.client.get("/github/connection", headers=bearer)).json()["connected"] is True


async def test_a_principal_that_is_not_a_github_sign_in_has_no_connection_to_see(
    boot: Any, fake_github: FakeGitHub
) -> None:
    from felix.auth.jwt import mint_token

    async with boot([], env=_env()) as app:
        operator = mint_token(app.settings, sub="ops", tenant_id="acme", scopes=["jobs:read"], ttl_seconds=60)
        seen = await app.client.get("/github/connection", headers={"Authorization": f"Bearer {operator}"})
        assert seen.status_code == 403
        assert seen.json()["detail"] == "not_a_github_sign_in"
