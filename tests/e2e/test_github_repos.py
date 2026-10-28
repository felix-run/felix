"""Per-person repos over real HTTP: list what you can reach, open one in a thread, publish as you.

Boots `create_application()` under `auth_mode=jwt` with GitHub faked at `github_http_client`
and a real bare repository served over dumb HTTP for the clone. A person signs in with the
device flow through the App (which keeps their connection), lists their repositories, opens one
in a thread, and the thread's workspace is then that checkout; and reads that repository's issues
and pull requests to start from one. What this pins that the unit tests
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

from tests.support.github_fake import FakeGitHub, app_grant, issue, org, repo

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
            await app.client.get("/github/repos/acme/widgets/items", headers=bearer),
            await app.client.get("/github/repos/acme/widgets/items/1", headers=bearer),
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


def _items_fixture(fake: FakeGitHub) -> None:
    fake.issues["acme/widgets"] = [
        issue(1, "Crash on start", updated_at="2026-10-01T00:00:00Z", body="Steps:\n1. run it"),
        issue(2, "Add dark mode", pull=True, updated_at="2026-10-03T00:00:00Z", body="Closes #1"),
        issue(3, "Old bug", state="closed", updated_at="2026-10-04T00:00:00Z"),
        issue(4, "Typo in README", author=None, updated_at="2026-10-02T00:00:00Z", body=None),
    ]


async def test_a_person_lists_a_repository_s_issues_and_pull_requests(
    boot: Any, fake_github: FakeGitHub, tmp_path: Any
) -> None:
    _items_fixture(fake_github)
    async with boot([], env=_env(tmp_path)) as app:
        bearer = await _sign_in(app.client)

        listed = await app.client.get("/github/repos/acme/widgets/items", headers=bearer)
        assert listed.status_code == 200, listed.text
        body = listed.json()
        assert body["repository"] == "acme/widgets" and body["truncated"] is False
        # Open only by default, most recently updated first, both kinds from the one listing.
        assert [(i["number"], i["kind"]) for i in body["items"]] == [(2, "pull"), (4, "issue"), (1, "issue")]
        assert body["items"][0] == {
            "number": 2,
            "kind": "pull",
            "title": "Add dark mode",
            "state": "open",
            # Built from the repository and number, not GitHub's `html_url`.
            "html_url": "https://github.com/acme/widgets/pull/2",
            "author": "octo",
            "updated_at": "2026-10-03T00:00:00Z",
        }
        assert body["items"][1]["author"] == "" and body["items"][1]["html_url"].endswith("/issues/4")
        assert "evil.example" not in listed.text and "gho_" not in listed.text

        async def numbers(**params: str) -> list[int]:
            got = await app.client.get("/github/repos/acme/widgets/items", params=params, headers=bearer)
            assert got.status_code == 200, got.text
            return [i["number"] for i in got.json()["items"]]

        assert await numbers(kind="pull") == [2]
        assert await numbers(kind="issue") == [4, 1]
        assert await numbers(q="CRASH") == [1]
        assert await numbers(state="closed") == [3]
        assert await numbers(state="all", kind="issue") == [3, 4, 1]

        bad = await app.client.get("/github/repos/acme/widgets/items", params={"kind": "x"}, headers=bearer)
        assert bad.status_code == 422


async def test_the_item_listing_is_capped_and_says_so(
    boot: Any, fake_github: FakeGitHub, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from felix.repos import github_api

    fake_github.issues["acme/widgets"] = [
        issue(n, f"Issue {n}", updated_at=f"2026-10-01T00:{n // 60:02d}:{n % 60:02d}Z") for n in range(1, 41)
    ]
    async with boot([], env=_env(tmp_path)) as app:
        bearer = await _sign_in(app.client)
        listed = await app.client.get("/github/repos/acme/widgets/items", headers=bearer)
        body = listed.json()
        assert len(body["items"]) == 30 and body["truncated"] is True
        assert body["items"][0]["number"] == 40 and body["items"][-1]["number"] == 11

        narrowed = await app.client.get(
            "/github/repos/acme/widgets/items", params={"q": "issue 1"}, headers=bearer
        )
        assert narrowed.json()["truncated"] is False
        assert [i["number"] for i in narrowed.json()["items"]] == [19, 18, 17, 16, 15, 14, 13, 12, 11, 10, 1]

        # Past the pages one listing walks, a sparse match is cut short, and said to be.
        fake_github.issues["acme/widgets"] = [
            issue(n, f"Issue {n}", pull=n == 1, updated_at=f"2026-10-01T00:{n // 60:02d}:{n % 60:02d}Z")
            for n in range(1, 151)
        ]
        monkeypatch.setattr(github_api, "MAX_ITEM_PAGES", 1)
        sparse = await app.client.get(
            "/github/repos/acme/widgets/items", params={"kind": "pull"}, headers=bearer
        )
        assert sparse.json() == {"repository": "acme/widgets", "items": [], "truncated": True}


async def test_a_person_reads_one_item_with_its_body(
    boot: Any, fake_github: FakeGitHub, tmp_path: Any
) -> None:
    from felix.repos import github_api

    _items_fixture(fake_github)
    long_body = "x" * (github_api.MAX_BODY_CHARS + 5)
    # Third-party text: returned as data, whatever it says.
    fake_github.issues["acme/widgets"].append(issue(5, "Long", body=long_body))
    async with boot([], env=_env(tmp_path)) as app:
        bearer = await _sign_in(app.client)

        one = await app.client.get("/github/repos/acme/widgets/items/1", headers=bearer)
        assert one.status_code == 200, one.text
        assert one.json() == {
            "number": 1,
            "kind": "issue",
            "title": "Crash on start",
            "state": "open",
            "html_url": "https://github.com/acme/widgets/issues/1",
            "author": "octo",
            "updated_at": "2026-10-01T00:00:00Z",
            "body": "Steps:\n1. run it",
            "body_truncated": False,
        }
        pull = (await app.client.get("/github/repos/acme/widgets/items/2", headers=bearer)).json()
        assert "evil.example" not in one.text
        assert pull["kind"] == "pull" and pull["html_url"] == "https://github.com/acme/widgets/pull/2"
        # A closed item is still readable by number, and a null body is empty text.
        empty = (await app.client.get("/github/repos/acme/widgets/items/4", headers=bearer)).json()
        assert empty["body"] == "" and empty["body_truncated"] is False

        cut = (await app.client.get("/github/repos/acme/widgets/items/5", headers=bearer)).json()
        assert cut["body"] == long_body[: github_api.MAX_BODY_CHARS] and cut["body_truncated"] is True

        missing = await app.client.get("/github/repos/acme/widgets/items/99", headers=bearer)
        assert missing.status_code == 404 and missing.json()["error"] == "item_not_found"
        for bad in ("0", "-1", "abc"):
            assert (
                await app.client.get(f"/github/repos/acme/widgets/items/{bad}", headers=bearer)
            ).status_code == 422
        assert all("gho_" not in a.text for a in (one, missing))


async def test_a_repository_with_issues_turned_off_still_lists_its_pull_requests(
    boot: Any, fake_github: FakeGitHub, tmp_path: Any
) -> None:
    _items_fixture(fake_github)
    fake_github.issues_disabled.add("acme/widgets")
    async with boot([], env=_env(tmp_path)) as app:
        bearer = await _sign_in(app.client)
        listed = await app.client.get("/github/repos/acme/widgets/items", headers=bearer)
        assert listed.status_code == 200, listed.text
        assert [(i["number"], i["kind"]) for i in listed.json()["items"]] == [(2, "pull")]
        issues = await app.client.get(
            "/github/repos/acme/widgets/items", params={"kind": "issue"}, headers=bearer
        )
        assert issues.json()["items"] == []

        pull = await app.client.get("/github/repos/acme/widgets/items/2", headers=bearer)
        assert (
            pull.status_code == 200 and pull.json()["kind"] == "pull" and pull.json()["body"] == "Closes #1"
        )
        gone = await app.client.get("/github/repos/acme/widgets/items/1", headers=bearer)
        assert gone.status_code == 404 and gone.json()["error"] == "item_not_found"


async def test_items_of_a_repository_the_app_cannot_reach_for_you_are_refused(
    boot: Any, fake_github: FakeGitHub, tmp_path: Any
) -> None:
    fake_github.issues["someone/else"] = [issue(1, "Not yours")]
    async with boot([], env=_env(tmp_path)) as app:
        bearer = await _sign_in(app.client)
        for path in (
            "/github/repos/someone/else/items",
            "/github/repos/someone/else/items/1",
            # Dot segments are not a repository: never resolved upward into another GitHub endpoint.
            "/github/repos/%2E%2E/user/items",
        ):
            refused = await app.client.get(path, headers=bearer)
            assert refused.status_code == 404, (path, refused.text)
            assert refused.json()["error"] == "repository_unreachable"
        assert not any(r.url.path.endswith("/user/issues") for r in fake_github.requests)


async def test_without_a_github_connection_there_are_no_items(
    boot: Any, fake_github: FakeGitHub, tmp_path: Any
) -> None:
    fake_github.polls = [{"access_token": "gho_x"}]
    async with boot([], env=_env(tmp_path)) as app:
        bearer = await _sign_in(app.client)
        for path in ("/github/repos/acme/widgets/items", "/github/repos/acme/widgets/items/1"):
            refused = await app.client.get(path, headers=bearer)
            assert refused.status_code == 409 and refused.json()["error"] == "github_not_connected"

        from felix.auth.jwt import mint_token

        operator = mint_token(app.settings, sub="ops", tenant_id="acme", scopes=["jobs:read"], ttl_seconds=60)
        seen = await app.client.get(
            "/github/repos/acme/widgets/items", headers={"Authorization": f"Bearer {operator}"}
        )
        assert seen.status_code == 403


async def test_a_github_failure_reading_items_answers_in_fixed_words(
    boot: Any, fake_github: FakeGitHub, tmp_path: Any
) -> None:
    _items_fixture(fake_github)
    async with boot([], env=_env(tmp_path)) as app:
        bearer = await _sign_in(app.client)
        # The repository is reachable; GitHub then fails the issues read itself.
        fake_github.items_status = 500
        for path in ("/github/repos/acme/widgets/items", "/github/repos/acme/widgets/items/1"):
            answer = await app.client.get(path, headers=bearer)
            assert answer.status_code == 502, answer.text
            assert answer.json() == {
                "error": "github_unavailable",
                "message": "GitHub could not be reached or answered unexpectedly; try again shortly",
            }
