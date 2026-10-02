"""GitHub login over real HTTP: device flow → Felix token → a scoped management route.

Boots the zero-argument `create_application()` under `auth_mode=jwt`, with GitHub faked at the
one seam `felix.auth.github` exposes (`github_http_client`). What this proves that the unit
tests cannot: the routes are reachable without a credential only while login is configured,
the token they hand back is one the auth middleware then accepts — tenant and scopes intact —
and the device-flow start has a bucket of its own.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from felix.auth import github
from joserfc import jwk

from tests.github_fake import FakeGitHub, org

_KEY = jwk.RSAKey.generate_key(2048)


def _env(**overrides: str) -> dict[str, str]:
    env = {
        "FELIX_ENVIRONMENT": "development",
        "FELIX_AUTH_MODE": "jwt",
        "FELIX_JWT_VERIFIERS": "self:felix-self",
        "FELIX_JWKS_PUBLIC": _KEY.as_pem(private=False).decode(),
        "FELIX_JWKS_PRIVATE": _KEY.as_pem(private=True).decode(),
        "FELIX_ALLOWED_TENANTS": "acme,globex",
        "FELIX_GITHUB_CLIENT_ID": "Iv1.e2e",
        "FELIX_GITHUB_ORG_TENANTS": json.dumps({"acme": org("acme", "acme", ["jobs:read"])}),
    }
    env.update(overrides)
    return env


@pytest.fixture
def fake_github(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    fake = FakeGitHub(polls=[{"error": "authorization_pending", "interval": 5}, {"access_token": "gho_x"}])
    monkeypatch.setattr(github, "github_http_client", lambda settings: fake.client())
    return fake


async def _login(client: Any, **body: Any) -> Any:
    started = await client.post("/auth/github/device")
    assert started.status_code == 200, started.text
    return await client.post(
        "/auth/github/token", json={"device_code": started.json()["device_code"], **body}
    )


async def test_a_github_member_logs_in_and_the_token_opens_their_tenant(
    boot: Any, fake_github: FakeGitHub, caplog: pytest.LogCaptureFixture
) -> None:
    from felix.audit import store as audit_store
    from felix.flush import flush_all

    caplog.set_level(logging.DEBUG)
    async with boot([], env=_env()) as app:
        started = await app.client.post("/auth/github/device")
        assert started.status_code == 200, started.text
        assert started.json()["user_code"] == "ABCD-1234"
        device_code = started.json()["device_code"]

        pending = await app.client.post("/auth/github/token", json={"device_code": device_code})
        assert pending.status_code == 428
        assert pending.json() == {
            "error": "authorization_pending",
            "message": "authorization_pending",
            "interval": 5,
        }
        assert pending.headers["retry-after"] == "5"

        granted = await app.client.post("/auth/github/token", json={"device_code": device_code})
        assert granted.status_code == 200, granted.text
        out = granted.json()
        assert (out["token_type"], out["tenant"], out["scopes"]) == ("Bearer", "acme", ["jobs:read"])

        bearer = {"Authorization": f"Bearer {out['access_token']}"}
        assert (await app.client.get("/jobs", headers=bearer)).status_code == 200
        # The org's scopes, not more: audit:read was never granted.
        assert (await app.client.get("/audit", headers=bearer)).status_code == 403
        assert (await app.client.get("/jobs")).status_code == 401

        await flush_all(app.settings)
        events, _ = await audit_store.list_events(app.settings, "acme", event_type="github_login")

    assert len(events) == 1
    assert events[0]["principal_subj"] == "github:4242"
    assert events[0]["payload_json"]["github_login"] == "octo"
    # A device code is a bearer secret until redeemed: in neither the audit row nor the log.
    assert device_code not in json.dumps(events)
    assert device_code not in caplog.text


async def test_ambiguous_membership_names_the_tenants_and_a_choice_resolves_it(
    boot: Any, fake_github: FakeGitHub
) -> None:
    orgs = {"acme": org("acme", "acme", ["jobs:read"]), "globex": org("globex", "globex", ["jobs:read"])}
    fake_github.memberships = {"acme": (200, "active"), "globex": (200, "active")}
    fake_github.polls = [{"access_token": "gho_x"}, {"access_token": "gho_x"}]
    async with boot([], env=_env(FELIX_GITHUB_ORG_TENANTS=json.dumps(orgs))) as app:
        ambiguous = await _login(app.client)
        assert ambiguous.status_code == 409
        assert ambiguous.json()["tenants"] == ["acme", "globex"]

        chosen = await _login(app.client, tenant="globex")
        assert chosen.status_code == 200, chosen.text
        assert chosen.json()["tenant"] == "globex"


async def test_a_non_member_gets_no_token(boot: Any, fake_github: FakeGitHub) -> None:
    fake_github.memberships = {}
    fake_github.polls = [{"access_token": "gho_x"}]
    async with boot([], env=_env()) as app:
        refused = await _login(app.client)
    assert refused.status_code == 403
    assert refused.json()["error"] == "not_a_member"
    assert "access_token" not in refused.json()


async def test_starting_flows_has_a_bucket_of_its_own(boot: Any, fake_github: FakeGitHub) -> None:
    async with boot([], env=_env(FELIX_GITHUB_DEVICE_STARTS_PER_HOUR="2")) as app:
        statuses = [(await app.client.post("/auth/github/device")).status_code for _ in range(3)]
    assert statuses == [200, 200, 429]


async def test_with_login_off_the_routes_are_not_public(boot: Any) -> None:
    async with boot([], env=_env(FELIX_GITHUB_CLIENT_ID="", FELIX_GITHUB_ORG_TENANTS="")) as app:
        assert (await app.client.post("/auth/github/device")).status_code == 401
        assert (await app.client.post("/auth/github/token", json={"device_code": "x"})).status_code == 401


async def test_with_login_off_and_no_auth_the_routes_are_absent(boot: Any) -> None:
    async with boot([]) as app:
        assert (await app.client.post("/auth/github/device")).status_code == 404


async def test_the_public_prefix_opens_nothing_beside_the_login_routes(
    boot: Any, fake_github: FakeGitHub
) -> None:
    """`/auth/github/` is public while login is on; a sibling path is still behind auth."""
    async with boot([], env=_env()) as app:
        assert (await app.client.get("/auth/githubx/device")).status_code == 401
        assert (await app.client.get("/auth/other")).status_code == 401
