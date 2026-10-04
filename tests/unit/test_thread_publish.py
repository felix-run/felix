"""`publish_commits` with `auth: person`: the thread's repository, as the person who opened it.

Resolved per call, so these drive `_ThreadPublishExecutor` inside a RequestContext for a thread
and check what it hands to the ordinary publish executor: the checkout's repository and base,
and an access token minted from the opener's stored connection. The publish itself — Git Data API
calls, fast-forward rules — is `test_publish_commits.py`'s, unchanged.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any

import pytest
from felix.auth import github_connections
from felix.auth.github import GitHubGrant
from felix.config import Settings
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.manifests.schema import GithubPublishSpec
from felix.repos import checkouts
from felix.tools import github_publish

SPEC = GithubPublishSpec(auth="person", branch_prefix="felix/")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    (tmp_path / "workspace").mkdir()
    return Settings(
        database_url="memory://thread-publish",
        data_dir=str(tmp_path / "data"),
        workspace_root=str(tmp_path / "workspace"),
        github_client_id="Iv23.app",
        github_client_secret="app-secret",
        github_token_key=base64.b64encode(os.urandom(32)).decode(),
    )


def _ready_checkout(settings: Settings, repo: str = "acme/widgets", base: str = "main") -> None:
    directory = checkouts.thread_dir(settings, "acme", "acme:t1")
    (directory / "repo").mkdir(parents=True)
    checkouts._write_state(
        directory,
        {"state": "ready", "repo": repo, "base": base, "github_user_id": 42, "opened_by": "github:42"},
    )


async def _connect(settings: Settings) -> None:
    await github_connections.save_connection(
        settings,
        "acme",
        github_user_id=42,
        github_login="octo",
        grant=GitHubGrant(access_token="ghu_person", refresh_token="ghr_1", expires_in=28_800),
    )


async def _run(settings: Settings, thread: str | None = "acme:t1") -> tuple[Any, list[Any]]:
    seen: list[Any] = []

    async def fake_execute(self: Any, args: Any, ctx: Any = None) -> str:
        seen.append((self._spec.repo, self._spec.base, self._token))
        return "published"

    executor = github_publish._ThreadPublishExecutor(SPEC)
    ctx = RequestContext(
        settings=settings, auth=AuthContext(tenant_id="acme", principal_sub="github:42"), thread_id=thread
    )
    original = github_publish._PublishExecutor.execute
    github_publish._PublishExecutor.execute = fake_execute  # type: ignore[method-assign]
    try:
        async with async_run_with_context(ctx):
            out = await executor.execute({"branch": "felix/x", "head_sha": "0" * 40})
    finally:
        github_publish._PublishExecutor.execute = original  # type: ignore[method-assign]
    return out, seen


async def test_it_publishes_to_the_thread_s_repository_as_its_opener(settings: Settings) -> None:
    _ready_checkout(settings, repo="acme/widgets", base="develop")
    await _connect(settings)
    out, seen = await _run(settings)
    assert out == "published"
    assert seen == [("acme/widgets", "develop", "ghu_person")]


async def test_a_thread_without_a_repository_has_nothing_to_publish_to(settings: Settings) -> None:
    out, seen = await _run(settings)
    assert "this thread has no repository" in str(out)
    assert seen == []


async def test_a_lapsed_connection_says_to_reconnect_rather_than_failing_vaguely(settings: Settings) -> None:
    _ready_checkout(settings)  # no stored connection for github:42
    out, seen = await _run(settings)
    assert "[github disconnected]" in str(out) and "reconnect GitHub" in str(out)
    assert seen == []


async def test_a_checkout_that_is_not_ready_publishes_nothing(settings: Settings) -> None:
    directory = checkouts.thread_dir(settings, "acme", "acme:t1")
    directory.mkdir(parents=True)
    checkouts._write_state(
        directory, {"state": "cloning", "repo": "acme/widgets", "base": "main", "github_user_id": 42}
    )
    await _connect(settings)
    out, seen = await _run(settings)
    assert "is cloning" in str(out)
    assert seen == []


def test_person_auth_takes_no_repo_and_a_secret_needs_one() -> None:
    with pytest.raises(ValueError, match="must be omitted"):
        GithubPublishSpec(auth="person", repo="acme/widgets", branch_prefix="felix/")
    with pytest.raises(ValueError, match="is required"):
        GithubPublishSpec(auth="secret:GH", branch_prefix="felix/")
    with pytest.raises(ValueError, match="secret ref"):
        GithubPublishSpec(auth="ghp_literal_token", repo="acme/widgets", branch_prefix="felix/")
