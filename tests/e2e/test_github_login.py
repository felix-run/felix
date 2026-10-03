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
        # Who logged in, so a client can say so without decoding the token.
        assert out["github_login"] == "octo"

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


async def test_ambiguous_membership_spends_the_flow_and_a_new_one_with_a_tenant_lands_there(
    boot: Any, fake_github: FakeGitHub
) -> None:
    """GitHub device codes are single-use: the 409 is the end of that flow, not a question."""
    from felix.audit import store as audit_store
    from felix.flush import flush_all

    orgs = {"acme": org("acme", "acme", ["jobs:read"]), "globex": org("globex", "globex", ["jobs:read"])}
    fake_github.memberships = {"acme": (200, "active"), "globex": (200, "active")}
    fake_github.polls = [{"access_token": "gho_x"}, {"access_token": "gho_x"}]
    async with boot([], env=_env(FELIX_GITHUB_ORG_TENANTS=json.dumps(orgs))) as app:
        started = await app.client.post("/auth/github/device")
        device_code = started.json()["device_code"]
        ambiguous = await app.client.post("/auth/github/token", json={"device_code": device_code})
        assert ambiguous.status_code == 409
        assert ambiguous.json()["error"] == "tenant_ambiguous"
        assert ambiguous.json()["tenants"] == ["acme", "globex"]

        retried = await app.client.post(
            "/auth/github/token", json={"device_code": device_code, "tenant": "globex"}
        )
        assert retried.status_code == 400
        assert retried.json()["error"] == "invalid_device_code"

        chosen = await _login(app.client, tenant="globex")
        assert chosen.status_code == 200, chosen.text
        assert chosen.json()["tenant"] == "globex"

        await flush_all(app.settings)
        in_globex, _ = await audit_store.list_events(app.settings, "globex", event_type="github_login")
        in_acme, _ = await audit_store.list_events(app.settings, "acme", event_type="github_login")
    assert len(in_globex) == 1
    assert in_acme == []


async def test_choosing_a_tenant_membership_does_not_grant_is_refused(
    boot: Any, fake_github: FakeGitHub
) -> None:
    fake_github.polls = [{"access_token": "gho_x"}]
    async with boot([], env=_env()) as app:
        refused = await _login(app.client, tenant="globex")
    assert refused.status_code == 403
    assert refused.json()["error"] == "tenant_not_granted"


async def test_a_non_member_gets_no_token(boot: Any, fake_github: FakeGitHub) -> None:
    fake_github.memberships = {}
    fake_github.polls = [{"access_token": "gho_x"}]
    async with boot([], env=_env()) as app:
        refused = await _login(app.client)
    assert refused.status_code == 403
    assert refused.json()["error"] == "not_a_member"
    assert "access_token" not in refused.json()


async def test_a_github_outage_is_a_502_naming_nothing_upstream(boot: Any, fake_github: FakeGitHub) -> None:
    from felix.auth.github import UNAVAILABLE_MESSAGE

    fake_github.api_status = 500
    fake_github.polls = [{"access_token": "gho_x"}]
    async with boot([], env=_env()) as app:
        failed = await _login(app.client)
    assert failed.status_code == 502
    assert failed.json() == {"error": "github_unavailable", "message": UNAVAILABLE_MESSAGE}
    assert "retry-after" not in failed.headers


def _from(address: str) -> dict[str, str]:
    return {"x-forwarded-for": address}


_BEHIND_PROXY = {
    "FELIX_TRUSTED_CLIENT_IP_HEADER": "x-forwarded-for",
    "FELIX_GITHUB_DEVICE_STARTS_PER_HOUR": "2",
}


async def test_starting_flows_has_a_bucket_per_client(boot: Any, fake_github: FakeGitHub) -> None:
    async with boot([], env=_env(**_BEHIND_PROXY)) as app:
        statuses = [
            (await app.client.post("/auth/github/device", headers=_from("203.0.113.7"))).status_code
            for _ in range(2)
        ]
        limited = await app.client.post("/auth/github/device", headers=_from("203.0.113.7"))
        # Another address is not locked out by the first one spending its bucket.
        other = await app.client.post("/auth/github/device", headers=_from("198.51.100.9"))
    assert statuses == [200, 200]
    assert limited.status_code == 429
    assert limited.json()["error"] == "rate_limited"
    assert limited.headers["retry-after"] == "3600"
    assert other.status_code == 200


async def test_one_ipv6_subscriber_is_one_client(boot: Any, fake_github: FakeGitHub) -> None:
    """Rotating through a /64 used to buy a fresh bucket per address."""
    async with boot([], env=_env(**_BEHIND_PROXY)) as app:
        statuses = [
            (await app.client.post("/auth/github/device", headers=_from(f"2001:db8:1:2::{n}"))).status_code
            for n in range(1, 4)
        ]
        neighbour = await app.client.post("/auth/github/device", headers=_from("2001:db8:1:3::1"))
    assert statuses == [200, 200, 429]
    assert neighbour.status_code == 200


async def test_the_deployment_cap_holds_across_clients(
    boot: Any, fake_github: FakeGitHub, caplog: pytest.LogCaptureFixture
) -> None:
    """A per-client key cannot protect a quota every client shares: a botnet is many clients."""
    env = _env(**_BEHIND_PROXY, FELIX_GITHUB_DEVICE_STARTS_PER_HOUR_TOTAL="3")
    async with boot([], env=env) as app:
        # 203.0.113.7 spends its own bucket; its refused third start must not count toward
        # the total, or one noisy address could run the deployment cap down alone.
        own = [
            (await app.client.post("/auth/github/device", headers=_from("203.0.113.7"))).status_code
            for _ in range(3)
        ]
        third_client = await app.client.post("/auth/github/device", headers=_from("198.51.100.9"))
        capped = await app.client.post("/auth/github/device", headers=_from("192.0.2.44"))
        capped_again = await app.client.post("/auth/github/device", headers=_from("192.0.2.45"))
    assert own == [200, 200, 429]
    assert third_client.status_code == 200
    assert capped.status_code == capped_again.status_code == 429
    assert capped.json()["error"] == "rate_limited"
    assert capped.headers["retry-after"] == "3600"
    # The operator hears about it once per window, not once per refused start: under attack the
    # attacker would choose the number of WARNING lines.
    warnings = [
        r for r in caplog.records if r.name == "felix_api.auth_github" and r.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    # Only the starts that were allowed reached GitHub.
    assert fake_github.issued == 3


async def test_polling_does_not_spend_the_start_bucket(boot: Any, fake_github: FakeGitHub) -> None:
    fake_github.polls = [{"error": "authorization_pending", "interval": 5}] * 5
    async with boot([], env=_env(**_BEHIND_PROXY)) as app:
        started = await app.client.post("/auth/github/device", headers=_from("203.0.113.7"))
        for _ in range(5):
            body = {"device_code": started.json()["device_code"]}
            await app.client.post("/auth/github/token", json=body, headers=_from("203.0.113.7"))
        second = await app.client.post("/auth/github/device", headers=_from("203.0.113.7"))
    assert second.status_code == 200


async def test_the_hourly_bucket_outlives_the_global_minute(
    boot: Any, fake_github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The in-memory limiter expires keys using the calling request's window; sharing one store
    with the 60 s global bucket let any request a minute later reset the hourly one."""
    import time

    from felix.security import rate_limit

    clock = {"now": time.monotonic()}
    monkeypatch.setattr(rate_limit.time, "monotonic", lambda: clock["now"])
    async with boot([], env=_env(**_BEHIND_PROXY)) as app:
        for _ in range(2):
            await app.client.post("/auth/github/device", headers=_from("203.0.113.7"))
        clock["now"] += 61
        # Any request through the global limiter, from anyone.
        await app.client.get("/jobs", headers=_from("198.51.100.9"))
        again = await app.client.post("/auth/github/device", headers=_from("203.0.113.7"))
    assert again.status_code == 429


async def test_with_login_off_the_routes_are_not_public(boot: Any) -> None:
    async with boot([], env=_env(FELIX_GITHUB_CLIENT_ID="", FELIX_GITHUB_ORG_TENANTS="")) as app:
        assert (await app.client.post("/auth/github/device")).status_code == 401
        assert (await app.client.post("/auth/github/token", json={"device_code": "x"})).status_code == 401


async def test_with_login_off_and_no_auth_the_routes_are_absent(boot: Any) -> None:
    async with boot([]) as app:
        assert (await app.client.post("/auth/github/device")).status_code == 404
        assert (await app.client.post("/auth/github/token", json={"device_code": "x"})).status_code == 404


async def test_only_the_two_login_paths_are_public(boot: Any, fake_github: FakeGitHub) -> None:
    """Exact paths, not a prefix: a plugin route mounted beside them stays behind auth."""
    async with boot([], env=_env()) as app:
        assert (await app.client.get("/auth/github/other")).status_code == 401
        assert (await app.client.post("/auth/github/device/")).status_code == 401
        assert (await app.client.get("/auth/githubx/device")).status_code == 401


_API_KEY_MODE = {
    "FELIX_AUTH_MODE": "api_key",
    "FELIX_AUTH_API_KEYS": json.dumps({"k-e2e": {"tenant_id": "acme", "scopes": ["admin"]}}),
}
_LOGIN_OFF = {"FELIX_GITHUB_CLIENT_ID": "", "FELIX_GITHUB_ORG_TENANTS": ""}


# Login on is `jwt` only: under `none` or `api_key` no verifier would check the minted token,
# so `validate_login_config` refuses to start, and those combinations never answer anything.
@pytest.mark.parametrize(
    ("mode", "login_on", "bearer_required"),
    [
        pytest.param({"FELIX_AUTH_MODE": "none"}, False, False, id="none-login-off"),
        pytest.param(_API_KEY_MODE, False, True, id="api_key-login-off"),
        pytest.param({}, False, True, id="jwt-login-off"),
        pytest.param({}, True, True, id="jwt-login-on"),
    ],
)
async def test_auth_methods_answers_an_anonymous_caller_in_every_mode(
    boot: Any, fake_github: FakeGitHub, mode: dict[str, str], login_on: bool, bearer_required: bool
) -> None:
    """How a client with no credential learns how to get one, so it cannot itself need one."""
    async with boot([], env=_env(**mode, **({} if login_on else _LOGIN_OFF))) as app:
        methods = await app.client.get("/auth/methods")
        if bearer_required:
            # The rest of the surface still wants a credential: only this path was opened.
            assert (await app.client.get("/jobs")).status_code == 401
    assert methods.status_code == 200, methods.text
    # Exactly these two keys: a proxy decides whether to honour a browser's bearer on the second.
    assert methods.json() == {"github_device": login_on, "bearer_required": bearer_required}
    # Asking never starts a flow, so it spends nothing at GitHub or from the hourly start budget.
    assert fake_github.issued == 0


async def test_auth_methods_is_an_exact_public_path(boot: Any) -> None:
    """The `/auth` prefix it is mounted under grants nothing else."""
    async with boot([], env=_env(**_LOGIN_OFF)) as app:
        assert (await app.client.get("/auth/methods")).status_code == 200
        assert (await app.client.get("/auth/methods/")).status_code == 401
        assert (await app.client.get("/auth/other")).status_code == 401
