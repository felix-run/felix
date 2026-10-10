"""Per-person repos over real HTTP: list what you can reach, open one in a thread, publish as you.

Boots `create_application()` under `auth_mode=jwt` with GitHub faked at `github_http_client`
and a real bare repository served over dumb HTTP for the clone. A person signs in with the
device flow through the App (which keeps their connection), lists their repositories, opens one
in a thread, and the thread's workspace is then that checkout. What this pins that the unit tests
cannot: the routes act as the caller and only the caller, a checkout is per thread over the API,
and nothing a route returns carries a GitHub token.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

import pytest
from felix.auth import github
from felix.repos import checkouts
from joserfc import jwk

from tests.support.github_fake import FakeGitHub, app_grant, org, repo

_KEY = jwk.RSAKey.generate_key(2048)


def _env(tmp: Any, **overrides: str) -> dict[str, str]:
    env = {
        "FELIX_ENVIRONMENT": "development",
        "FELIX_AUTH_MODE": "jwt",
        "FELIX_JWT_VERIFIERS": "self:felix-self",
        "FELIX_JWKS_PUBLIC": _KEY.as_pem(private=False).decode(),
        "FELIX_JWKS_PRIVATE": _KEY.as_pem(private=True).decode(),
        "FELIX_ALLOWED_TENANTS": "acme",
        "FELIX_GITHUB_CLIENT_ID": "Iv23.e2e",
        "FELIX_GITHUB_CLIENT_SECRET": "app-secret",
        "FELIX_GITHUB_TOKEN_KEY": base64.b64encode(os.urandom(32)).decode(),
        "FELIX_GITHUB_APP_SLUG": "felix-e2e",
        "FELIX_GITHUB_ORG_TENANTS": json.dumps({"acme": org("acme", "acme", ["jobs:read"])}),
        "FELIX_DATA_DIR": str(tmp / "data"),
        "FELIX_WORKSPACE_ROOT": str(tmp / "workspace"),
    }
    (tmp / "workspace").mkdir(exist_ok=True)
    env.update(overrides)
    return env


@pytest.fixture
def fake_github(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    fake = FakeGitHub(polls=[app_grant("gho_x", "ghr_1")])
    fake.installations = [
        {"id": 7, "account": {"login": "acme", "type": "Organization"}, "repository_selection": "selected"}
    ]
    fake.installation_repos = {
        7: [repo("acme/widgets"), repo("acme/docs", push=False), repo("acme/huge", size=900 * 1024)]
    }
    monkeypatch.setattr(github, "github_http_client", lambda settings: fake.client())
    return fake


async def _sign_in(client: Any) -> dict[str, str]:
    started = await client.post("/auth/github/device")
    granted = await client.post("/auth/github/token", json={"device_code": started.json()["device_code"]})
    assert granted.status_code == 200, granted.text
    return {"Authorization": f"Bearer {granted.json()['access_token']}"}


async def test_a_person_lists_opens_and_works_in_their_repository(
    boot: Any,
    fake_github: FakeGitHub,
    git_server: Any,
    tmp_path: Any,
) -> None:
    async with boot([], env=_env(tmp_path)) as app:
        bearer = await _sign_in(app.client)

        listed = await app.client.get("/github/repos", headers=bearer)
        assert listed.status_code == 200, listed.text
        body = listed.json()
        assert [r["full_name"] for r in body["repositories"]] == ["acme/docs", "acme/huge", "acme/widgets"]
        assert {r["full_name"]: r["can_write"] for r in body["repositories"]}["acme/docs"] is False
        assert body["install_url"] == "https://github.com/apps/felix-e2e/installations/new"
        assert "gho_" not in listed.text
        filtered = await app.client.get("/github/repos", params={"q": "WID"}, headers=bearer)
        assert [r["full_name"] for r in filtered.json()["repositories"]] == ["acme/widgets"]

        opened = await app.client.post(
            "/chat/sessions/t1/workspace/repo", json={"full_name": "acme/widgets"}, headers=bearer
        )
        assert opened.status_code == 202, opened.text
        assert opened.json()["state"] == "cloning"
        await checkouts.wait_for_clones()

        status = await app.client.get("/chat/sessions/t1/workspace/repo", headers=bearer)
        assert status.status_code == 200
        assert {k: status.json()[k] for k in ("state", "repo", "branch", "ahead", "dirty")} == {
            "state": "ready",
            "repo": "acme/widgets",
            "branch": "main",
            "ahead": 0,
            "dirty": False,
        }
        # The person's App token reached the git server; none of the responses carried one.
        assert any(h.startswith("basic ") for h in git_server.headers)
        assert "gho_" not in status.text

        # Another thread has no repository of its own.
        other = await app.client.get("/chat/sessions/t2/workspace/repo", headers=bearer)
        assert other.status_code == 404

        files = await app.client.get("/chat/sessions/t1/workspace/repo/files", headers=bearer)
        assert files.status_code == 200, files.text
        assert files.json()["state"] == "ready" and files.json()["truncated"] is False
        assert [(f["path"], f["status"]) for f in files.json()["files"]] == [
            ("README.md", "clean"),
            ("app.py", "clean"),
        ]
        bad = await app.client.get(
            "/chat/sessions/t1/workspace/repo/files", params={"prefix": "../.."}, headers=bearer
        )
        assert bad.status_code == 400 and bad.json()["error"] == "invalid_prefix"
        none = await app.client.get("/chat/sessions/t2/workspace/repo/files", headers=bearer)
        assert none.status_code == 404

        removed = await app.client.delete("/chat/sessions/t1/workspace/repo", headers=bearer)
        assert removed.status_code == 204
        assert (await app.client.get("/chat/sessions/t1/workspace/repo", headers=bearer)).status_code == 404


async def test_a_repository_the_app_cannot_reach_for_you_is_refused(
    boot: Any, fake_github: FakeGitHub, tmp_path: Any
) -> None:
    async with boot([], env=_env(tmp_path)) as app:
        bearer = await _sign_in(app.client)
        refused = await app.client.post(
            "/chat/sessions/t1/workspace/repo", json={"full_name": "someone/else"}, headers=bearer
        )
        assert refused.status_code == 404
        assert refused.json()["error"] == "repository_unreachable"


async def test_a_repository_over_the_cap_is_refused_with_413(
    boot: Any, fake_github: FakeGitHub, tmp_path: Any
) -> None:
    async with boot([], env=_env(tmp_path)) as app:
        bearer = await _sign_in(app.client)
        refused = await app.client.post(
            "/chat/sessions/t1/workspace/repo", json={"full_name": "acme/huge"}, headers=bearer
        )
        assert refused.status_code == 413
        assert refused.json()["error"] == "repository_too_large"


async def test_without_a_github_connection_there_is_nothing_to_list(
    boot: Any, fake_github: FakeGitHub, tmp_path: Any
) -> None:
    # An OAuth-app style grant (no refresh token): signing in works, nothing is kept.
    fake_github.polls = [{"access_token": "gho_x"}]
    async with boot([], env=_env(tmp_path)) as app:
        bearer = await _sign_in(app.client)
        listed = await app.client.get("/github/repos", headers=bearer)
        assert listed.status_code == 409
        assert listed.json()["error"] == "github_not_connected"


async def test_an_operator_token_is_not_a_person_with_repositories(
    boot: Any, fake_github: FakeGitHub, tmp_path: Any
) -> None:
    from felix.auth.jwt import mint_token

    async with boot([], env=_env(tmp_path)) as app:
        operator = mint_token(app.settings, sub="ops", tenant_id="acme", scopes=["jobs:read"], ttl_seconds=60)
        seen = await app.client.get("/github/repos", headers={"Authorization": f"Bearer {operator}"})
        assert seen.status_code == 403


async def test_a_github_failure_answers_in_fixed_words_not_the_exception_s(
    boot: Any, fake_github: FakeGitHub, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exception's text can name what is the operator's business only — here an egress proxy.
    The answer says whose problem it is; the detail goes to the log."""
    import httpx

    async with boot([], env=_env(tmp_path)) as app:
        bearer = await _sign_in(app.client)

        def unreachable(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("egress-proxy.internal.example:3128 refused the tunnel")

        monkeypatch.setattr(
            github,
            "github_http_client",
            lambda s: httpx.AsyncClient(transport=httpx.MockTransport(unreachable)),
        )
        for answer in (
            await app.client.get("/github/repos", headers=bearer),
            await app.client.post(
                "/chat/sessions/t1/workspace/repo", json={"full_name": "acme/widgets"}, headers=bearer
            ),
        ):
            assert answer.status_code == 502
            assert answer.json() == {
                "error": "github_unavailable",
                "message": "GitHub could not be reached or answered unexpectedly; try again shortly",
            }
            assert "egress-proxy" not in answer.text


async def test_under_the_hosted_backend_the_repository_lives_in_the_threads_sandbox(
    boot: Any,
    fake_github: FakeGitHub,
    git_server: Any,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from felix.tools import workspace_hosted
    from felix.tools.workspace_scope import thread_key

    from tests.support.workspace_gateway_fake import TOKEN, URL, FakeGateway

    gateway = FakeGateway(root=tmp_path / "sandboxes", clone_base=git_server.base)
    monkeypatch.setattr(workspace_hosted, "gateway_client", lambda settings: gateway.client())
    env = _env(
        tmp_path,
        FELIX_WORKSPACE_BACKEND="hosted",
        FELIX_WORKSPACE_GATEWAY_URL=URL,
        FELIX_WORKSPACE_GATEWAY_TOKEN=TOKEN,
    )
    async with boot([], env=env) as app:
        bearer = await _sign_in(app.client)
        opened = await app.client.post(
            "/chat/sessions/t1/workspace/repo", json={"full_name": "acme/widgets"}, headers=bearer
        )
        assert opened.status_code == 202, opened.text
        await checkouts.wait_for_clones()

        status = await app.client.get("/chat/sessions/t1/workspace/repo", headers=bearer)
        assert {k: status.json()[k] for k in ("state", "branch", "ahead", "dirty")} == {
            "state": "ready",
            "branch": "main",
            "ahead": 0,
            "dirty": False,
        }, status.text
        sandbox = gateway.sandbox(f"acme/{thread_key('acme', 'acme:t1')}")
        assert (sandbox / "app.py").is_file()
        # The App token went to the gateway for the clone; no response carried it.
        assert len(gateway.clone_tokens) == 1
        files = await app.client.get("/chat/sessions/t1/workspace/repo/files", headers=bearer)
        assert [(f["path"], f["status"]) for f in files.json()["files"]] == [
            ("README.md", "clean"),
            ("app.py", "clean"),
        ]
        assert gateway.clone_tokens[0] not in status.text + files.text

        # A sandbox that cannot be cleared keeps the checkout and says so.
        gateway.down = True
        refused = await app.client.delete("/chat/sessions/t1/workspace/repo", headers=bearer)
        assert refused.status_code == 503 and refused.json()["error"] == "workspace_unavailable"
        gateway.down = False
        removed = await app.client.delete("/chat/sessions/t1/workspace/repo", headers=bearer)
        assert removed.status_code == 204
        assert not sandbox.exists()
