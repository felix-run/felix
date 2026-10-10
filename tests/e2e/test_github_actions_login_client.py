"""`felix_client.github_actions_login` against the real route, with the Actions runner faked.

One `httpx.AsyncClient` carries both legs, as in production: the runner's token endpoint (a fake
mounted at its host) and the booted app (its ASGI transport mounted at the Felix host). So the
request the client builds for the runner — the audience, the bearer — is the one asserted, and
the ID token it gets back goes through the middleware and route in `apps/api`.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from felix.auth import github, github_actions
from felix_client import LoginError, github_actions_login
from joserfc import jwk

from tests.support.github_fake import ACTIONS_AUDIENCE, DEPLOY_REPO_ID, ORG_IDS, FakeGitHub, actions_id_token

_KEY = jwk.RSAKey.generate_key(2048)
_BASE = "http://felix.test"
_RUNNER = "https://runner.test/_apis/token?api-version=2.0"
_RUNNER_ENV = {"ACTIONS_ID_TOKEN_REQUEST_URL": _RUNNER, "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "runner-secret"}
_ACTIONS = {"repositories": {"deploy": DEPLOY_REPO_ID}, "refs": ["refs/heads/main"], "scopes": ["jobs:read"]}


def _orgs(**extra: Any) -> str:
    entry = {"id": ORG_IDS["acme"], "tenant": "acme", "scopes": [], "actions": _ACTIONS}
    return json.dumps({"acme": entry, **extra})


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
        "FELIX_GITHUB_ORG_TENANTS": _orgs(),
    }
    env.update(overrides)
    return env


@pytest.fixture(autouse=True)
def fake_github(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    fake = FakeGitHub()
    monkeypatch.setattr(github, "github_http_client", lambda settings: fake.client())
    monkeypatch.setattr(github_actions, "_keys", github_actions._KeyCache())
    return fake


class Runner:
    """The runner's ID-token endpoint: signs for whatever audience it is asked."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"message": "no"})
        return httpx.Response(200, json={"value": actions_id_token(aud=request.url.params["audience"])})


@asynccontextmanager
async def _both(app: Any, runner: Runner) -> AsyncIterator[httpx.AsyncClient]:
    mounts = {
        "https://runner.test": httpx.MockTransport(runner),
        "http://felix.test": app.client._transport,
        "https://felix.test": app.client._transport,
    }
    async with httpx.AsyncClient(mounts=mounts) as client:
        yield client


async def test_a_job_logs_in_with_no_stored_secret_and_the_token_works(boot: Any) -> None:
    runner = Runner()
    async with boot([], env=_env()) as app, _both(app, runner) as client:
        token = await github_actions_login(
            _BASE, audience=ACTIONS_AUDIENCE, client=client, allow_insecure=True, environ=_RUNNER_ENV
        )
        opened = await app.client.get("/jobs", headers={"Authorization": f"Bearer {token.access_token}"})
    assert (token.tenant, token.scopes, token.base_url) == ("acme", ("jobs:read",), _BASE)
    assert opened.status_code == 200
    asked = runner.requests[0]
    # The runner's own query is kept, the audience added, and its bearer is the runner's token.
    assert asked.url.params["api-version"] == "2.0"
    assert asked.url.params["audience"] == ACTIONS_AUDIENCE
    assert asked.headers["authorization"].lower() == "bearer runner-secret"


async def test_the_audience_defaults_to_the_server_url_and_the_token_works(boot: Any) -> None:
    """`FELIX_GITHUB_OIDC_AUDIENCE` is documented to be the server's URL, so no flag is needed."""
    runner = Runner()
    env = _env(FELIX_GITHUB_OIDC_AUDIENCE="https://felix.test")
    async with boot([], env=env) as app, _both(app, runner) as client:
        # The trailing slash is the URL's, not the audience's: `aud` is matched exactly.
        token = await github_actions_login("https://felix.test/", client=client, environ=_RUNNER_ENV)
    assert runner.requests[0].url.params["audience"] == "https://felix.test"
    assert token.tenant == "acme"


async def test_an_audience_the_server_does_not_expect_is_its_refusal(boot: Any) -> None:
    async with boot([], env=_env()) as app, _both(app, Runner()) as client:
        with pytest.raises(LoginError) as info:
            await github_actions_login(_BASE, client=client, allow_insecure=True, environ=_RUNNER_ENV)
    assert (info.value.code, info.value.status) == ("invalid_id_token", 401)


async def test_a_workflow_granted_two_tenants_retries_with_one(boot: Any) -> None:
    ops = {"id": ORG_IDS["acme"], "tenant": "ops", "scopes": [], "actions": _ACTIONS}
    env = _env(FELIX_GITHUB_ORG_TENANTS=_orgs(**{"acme-ops": ops}))
    async with boot([], env=env) as app, _both(app, Runner()) as client:
        kwargs: dict[str, Any] = {
            "audience": ACTIONS_AUDIENCE,
            "client": client,
            "allow_insecure": True,
            "environ": _RUNNER_ENV,
        }
        with pytest.raises(LoginError) as info:
            await github_actions_login(_BASE, **kwargs)
        chosen = await github_actions_login(_BASE, tenant="ops", **kwargs)
    assert (info.value.code, info.value.status, info.value.tenants) == (
        "tenant_ambiguous",
        409,
        ("acme", "ops"),
    )
    assert chosen.tenant == "ops"


@pytest.mark.parametrize(
    "environ",
    [
        pytest.param({}, id="neither"),
        pytest.param({"ACTIONS_ID_TOKEN_REQUEST_URL": _RUNNER}, id="url-only"),
        pytest.param({"ACTIONS_ID_TOKEN_REQUEST_TOKEN": "t"}, id="token-only"),
    ],
)
async def test_outside_actions_it_says_what_is_missing(environ: dict[str, str]) -> None:
    with pytest.raises(LoginError) as info:
        await github_actions_login("https://felix.example", environ=environ)
    assert info.value.code == "not_in_github_actions"
    assert "id-token: write" in str(info.value)


async def test_a_runner_that_will_not_issue_a_token_is_named(boot: Any) -> None:
    async with boot([], env=_env()) as app, _both(app, Runner(status=403)) as client:
        with pytest.raises(LoginError) as info:
            await github_actions_login(
                _BASE, audience=ACTIONS_AUDIENCE, client=client, allow_insecure=True, environ=_RUNNER_ENV
            )
    assert (info.value.code, info.value.status) == ("id_token_unavailable", 403)


async def test_plain_http_to_a_remote_server_is_refused_before_asking_the_runner() -> None:
    runner = Runner()
    async with httpx.AsyncClient(transport=httpx.MockTransport(runner)) as client:
        with pytest.raises(LoginError) as info:
            await github_actions_login("http://felix.example", client=client, environ=_RUNNER_ENV)
    assert info.value.code == "insecure_url"
    assert runner.requests == []


async def _runner_failure(handler: Any, environ: dict[str, str] | None = None) -> LoginError:
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(LoginError) as info:
            await github_actions_login("https://felix.example", client=client, environ=environ or _RUNNER_ENV)
    return info.value


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(httpx.Response(200, json={}), id="no-value"),
        pytest.param(httpx.Response(200, json={"value": ""}), id="empty-value"),
        pytest.param(httpx.Response(200, json={"value": 123}), id="non-string"),
        pytest.param(httpx.Response(200, json=["x"]), id="list"),
        pytest.param(httpx.Response(200, text="<html>proxy</html>"), id="html"),
    ],
)
async def test_a_runner_answer_without_a_token_is_named(answer: httpx.Response) -> None:
    failure = await _runner_failure(lambda request: answer)
    assert (failure.code, failure.status) == ("id_token_unavailable", 200)


async def test_an_unreachable_runner_is_a_login_error() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    failure = await _runner_failure(down)
    assert (failure.code, failure.status) == ("id_token_unavailable", 0)


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("http://runner.test/token?api-version=2.0", id="http"),
        pytest.param("http://[::1", id="malformed"),
    ],
)
async def test_a_runner_url_that_is_not_https_gets_no_bearer(url: str) -> None:
    runner = Runner()
    environ = {**_RUNNER_ENV, "ACTIONS_ID_TOKEN_REQUEST_URL": url}
    failure = await _runner_failure(runner, environ)
    assert failure.code == "id_token_unavailable"
    assert runner.requests == []


async def test_a_redirect_is_not_followed_even_by_a_client_that_would() -> None:
    """A 307 would resend the ID token, in the body, to wherever `Location` points."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.host == "runner.test":
            return Runner()(request)
        return httpx.Response(307, headers={"location": "http://evil.test/collect"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        with pytest.raises(LoginError) as info:
            await github_actions_login(
                "https://felix.example", audience=ACTIONS_AUDIENCE, client=client, environ=_RUNNER_ENV
            )
    assert info.value.code == "http_307"
    assert not any("evil.test" in url for url in seen)
