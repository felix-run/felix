"""`felix_client.github_device_login` against the real routes, not a copy of their contract.

The client is handed the booted app's own `AsyncClient` (ASGI transport), so every request it
makes goes through the middleware and the routes in `apps/api`, with GitHub faked at the server's
one seam. `sleep` is recorded rather than awaited, which is how the polling cadence is asserted.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from felix.auth import github
from felix_client import DeviceCode, LoginError, github_device_login
from joserfc import jwk

from tests.github_fake import FakeGitHub, org

_KEY = jwk.RSAKey.generate_key(2048)
_BASE = "http://felix.test"


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
    fake = FakeGitHub()
    monkeypatch.setattr(github, "github_http_client", lambda settings: fake.client())
    return fake


class Sleeps:
    def __init__(self) -> None:
        self.waited: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waited.append(seconds)


async def test_a_login_waits_out_pending_and_slow_down_then_returns_a_working_token(
    boot: Any, fake_github: FakeGitHub
) -> None:
    fake_github.polls = [
        {"error": "authorization_pending", "interval": 5},
        {"error": "slow_down", "interval": 10},
        {"access_token": "gho_x"},
    ]
    shown: list[DeviceCode] = []
    sleeps = Sleeps()
    async with boot([], env=_env()) as app:
        token = await github_device_login(_BASE, on_code=shown.append, client=app.client, sleep=sleeps)
        opened = await app.client.get("/jobs", headers={"Authorization": f"Bearer {token.access_token}"})

    assert [c.user_code for c in shown] == ["ABCD-1234"]
    # The start's interval, kept through `authorization_pending`, raised to what `slow_down` said.
    assert sleeps.waited == [5, 5, 10]
    assert (token.tenant, token.scopes, token.base_url) == ("acme", ("jobs:read",), _BASE)
    assert not token.expired()
    assert opened.status_code == 200


async def test_slow_down_without_an_interval_adds_five_seconds(boot: Any, fake_github: FakeGitHub) -> None:
    fake_github.polls = [{"error": "slow_down"}, {"access_token": "gho_x"}]
    sleeps = Sleeps()
    async with boot([], env=_env()) as app:
        await github_device_login(_BASE, on_code=lambda _: None, client=app.client, sleep=sleeps)
    assert sleeps.waited == [5, 10]


async def test_ambiguous_membership_surfaces_the_tenants_to_choose_from(
    boot: Any, fake_github: FakeGitHub
) -> None:
    orgs = {"acme": org("acme", "acme"), "globex": org("globex", "globex")}
    fake_github.memberships = {"acme": (200, "active"), "globex": (200, "active")}
    async with boot([], env=_env(FELIX_GITHUB_ORG_TENANTS=json.dumps(orgs))) as app:
        with pytest.raises(LoginError) as info:
            await github_device_login(_BASE, on_code=lambda _: None, client=app.client, sleep=Sleeps())
        fake_github.polls = [{"access_token": "gho_x"}]
        chosen = await github_device_login(
            _BASE, tenant="globex", on_code=lambda _: None, client=app.client, sleep=Sleeps()
        )
    assert (info.value.code, info.value.status, info.value.tenants) == (
        "tenant_ambiguous",
        409,
        ("acme", "globex"),
    )
    assert chosen.tenant == "globex"


async def test_a_refused_start_is_a_login_error(boot: Any, fake_github: FakeGitHub) -> None:
    async with boot([], env=_env(FELIX_GITHUB_DEVICE_STARTS_PER_HOUR="1")) as app:
        await github_device_login(_BASE, on_code=lambda _: None, client=app.client, sleep=Sleeps())
        with pytest.raises(LoginError) as info:
            await github_device_login(_BASE, on_code=lambda _: None, client=app.client, sleep=Sleeps())
    assert (info.value.code, info.value.status, info.value.interval) == ("rate_limited", 429, 3600)


async def test_login_off_is_a_login_error_not_a_crash(boot: Any) -> None:
    async with boot([]) as app:
        with pytest.raises(LoginError) as info:
            await github_device_login(_BASE, on_code=lambda _: None, client=app.client, sleep=Sleeps())
    assert info.value.status == 404
