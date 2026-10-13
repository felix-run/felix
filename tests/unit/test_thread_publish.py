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

from tests.support.factories import make_settings

SPEC = GithubPublishSpec(auth="person", branch_prefix="felix/")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    (tmp_path / "workspace").mkdir()
    return make_settings(
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


async def _run(
    settings: Settings, thread: str | None = "acme:t1", *, auth: AuthContext | None = None
) -> tuple[Any, list[Any]]:
    seen: list[Any] = []

    async def fake_execute(self: Any, args: Any, ctx: Any = None) -> str:
        seen.append((self._spec.repo, self._spec.base, self._token))
        return "published"

    executor = github_publish._ThreadPublishExecutor(SPEC)
    ctx = RequestContext(
        settings=settings,
        auth=auth or AuthContext(tenant_id="acme", principal_sub="github:42"),
        thread_id=thread,
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


# --- whose run may publish ---------------------------------------------------------------------


async def test_another_member_driving_the_thread_cannot_publish_as_its_opener(settings: Settings) -> None:
    """The thread is shared; the token is the opener's. Bob driving alice's thread used to publish
    commits to alice's repository with alice's GitHub account."""
    _ready_checkout(settings)
    await _connect(settings)
    out, seen = await _run(settings, auth=AuthContext(tenant_id="acme", principal_sub="github:43"))
    assert "[publish refused]" in str(out) and "only github:42" in str(out), out
    assert seen == []


@pytest.mark.parametrize(
    ("on_behalf_of", "publishes"),
    [("github:42", True), ("github:43", False), ("", False)],
    ids=["opener-s-run", "someone-else-s-run", "no-one-s-run"],
)
async def test_a_machine_run_publishes_only_on_the_opener_s_behalf(
    settings: Settings, on_behalf_of: str, publishes: bool
) -> None:
    """A resumed fiber (`fiber`) or a job (`cron`) acts for whoever started it: that person, not
    the machine, is who must be the opener."""
    _ready_checkout(settings)
    await _connect(settings)
    for machine in ("fiber", "cron"):
        auth = AuthContext(tenant_id="acme", principal_sub=machine, on_behalf_of=on_behalf_of)
        out, seen = await _run(settings, auth=auth)
        assert (seen == [("acme/widgets", "main", "ghu_person")]) is publishes, (machine, out)


async def test_a_checkout_that_names_no_opener_publishes_nothing(settings: Settings) -> None:
    """Fail closed for a state that does not say whose it is, and say how to fix it."""
    directory = checkouts.thread_dir(settings, "acme", "acme:t1")
    (directory / "repo").mkdir(parents=True)
    checkouts._write_state(
        directory, {"state": "ready", "repo": "acme/widgets", "base": "main", "github_user_id": 42}
    )
    await _connect(settings)
    out, seen = await _run(settings)
    assert "does not record who opened it" in str(out) and "open the repository again" in str(out), out
    assert seen == []


async def test_an_opener_that_is_not_the_token_s_account_publishes_nothing(settings: Settings) -> None:
    """The token is minted for `github_user_id`; an `opened_by` naming someone else would let one
    person's check pass for another's credential."""
    directory = checkouts.thread_dir(settings, "acme", "acme:t1")
    (directory / "repo").mkdir(parents=True)
    checkouts._write_state(
        directory,
        {
            "state": "ready",
            "repo": "acme/widgets",
            "base": "main",
            "github_user_id": 42,
            "opened_by": "github:43",
        },
    )
    await _connect(settings)
    out, seen = await _run(settings, auth=AuthContext(tenant_id="acme", principal_sub="github:43"))
    assert "does not record who opened it" in str(out), out
    assert seen == []


# --- whose checkout a thread holds ----------------------------------------------------------------


def _worktree(settings: Settings, thread: str, parent_key: str) -> None:
    directory = checkouts.thread_dir(settings, "acme", thread)
    directory.mkdir(parents=True)
    checkouts._write_state(
        directory, {"state": "ready", "worktree_of": "acme:t1", "parent_key": parent_key, "branch": "felix/c"}
    )


def test_a_child_worktree_answers_for_the_clone_it_was_made_from(settings: Settings) -> None:
    _ready_checkout(settings)
    _worktree(settings, "acme:child", checkouts.thread_dir(settings, "acme", "acme:t1").name)
    assert checkouts.checkout_opener(settings, "acme", "acme:child") == "github:42"


@pytest.mark.parametrize(
    "parent_key", ["../elsewhere", "", "0" * 40], ids=["not-a-key", "empty", "missing-parent"]
)
def test_a_child_worktree_with_no_findable_parent_is_nobody_s(settings: Settings, parent_key: str) -> None:
    _ready_checkout(settings)
    _worktree(settings, "acme:child", parent_key)
    assert checkouts.checkout_opener(settings, "acme", "acme:child") is checkouts.NOBODY
    assert not checkouts.may_reach_checkout(
        settings.model_copy(update={"auth_mode": "api_key"}),
        "acme",
        "acme:child",
        principal="github:42",
        scopes=[],
    )


def test_a_thread_with_no_checkout_has_no_opener(settings: Settings) -> None:
    assert checkouts.checkout_opener(settings, "acme", "acme:t1") is None
