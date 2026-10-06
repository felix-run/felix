"""A thread's repository checkout (`felix.repos.checkouts`), cloned for real.

A bare repository is served over git's dumb HTTP protocol from a local thread, which records the
`Authorization` header git sends, so these tests see what production does: the token reaches the
server, and afterwards it is nowhere — not in `.git/config`, the remote URL, the state file or any
file in the checkout. The rest holds the checkout to its promises: the workspace tools work in it
and nowhere else, a thread without one keeps the shared workspace, and a checkout that is cloning,
failed or expired stops the tools rather than sending them back to the shared workspace.
"""

from __future__ import annotations

import base64
import os
import time
from pathlib import Path
from typing import Any

import pytest
from felix.config import Settings
from felix.context import AuthContext, RequestContext, run_with_context
from felix.repos import checkouts
from felix.tools.workspace import workspace_root

from tests.git_server import _git, _Server

TOKEN = "ghu_person_token_0123456789"
REPO = {"full_name": "acme/widgets", "default_branch": "main", "size": 12, "private": True}


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    shared = tmp_path / "workspace"
    shared.mkdir()
    (shared / "shared.txt").write_text("the operator's workspace\n")
    return Settings(
        database_url="memory://checkouts",
        data_dir=str(tmp_path / "data"),
        workspace_root=str(shared),
    )


async def _open(
    settings: Settings, thread: str = "acme:t1", repo: dict[str, Any] | None = None
) -> dict[str, Any]:
    state = await checkouts.open_checkout(
        settings, "acme", thread, repo=repo or REPO, github_user_id=42, opened_by="github:42", token=TOKEN
    )
    await checkouts.wait_for_clones()
    return state


def _in_thread(settings: Settings, thread: str | None) -> Path:
    ctx = RequestContext(
        settings=settings, auth=AuthContext(tenant_id="acme", principal_sub="github:42"), thread_id=thread
    )
    with run_with_context(ctx):
        return workspace_root()


async def test_a_clone_sends_the_token_to_the_server_and_keeps_it_nowhere(
    git_server: _Server, settings: Settings
) -> None:
    first = await _open(settings)
    assert first["state"] == "cloning"
    state = checkouts.read_checkout(settings, "acme", "acme:t1")
    assert state is not None and state["state"] == "ready", state

    expected = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
    assert f"basic {expected}" in git_server.headers

    directory = checkouts.thread_dir(settings, "acme", "acme:t1")
    repo = directory / "repo"
    assert (repo / "app.py").read_text() == "print(1)\n"
    assert _git(repo, "remote", "get-url", "origin").strip() == f"{git_server.base}/acme/widgets.git"
    for path in directory.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert TOKEN.encode() not in data and expected.encode() not in data, path


async def test_the_tools_work_in_the_checkout_and_other_threads_keep_the_shared_workspace(
    git_server: Any, settings: Settings
) -> None:
    await _open(settings)
    assert (
        _in_thread(settings, "acme:t1")
        == checkouts.thread_dir(settings, "acme", "acme:t1").resolve() / "repo"
    )
    assert _in_thread(settings, "acme:t2") == Path(settings.workspace_root).resolve()
    assert _in_thread(settings, None) == Path(settings.workspace_root).resolve()


async def test_a_second_repository_is_refused_and_the_same_one_is_a_no_op(
    git_server: Any, settings: Settings
) -> None:
    await _open(settings)
    again = await checkouts.open_checkout(
        settings, "acme", "acme:t1", repo=REPO, github_user_id=42, opened_by="github:42", token=TOKEN
    )
    assert again["state"] == "ready"
    with pytest.raises(checkouts.CheckoutRefused) as refused:
        await checkouts.open_checkout(
            settings,
            "acme",
            "acme:t1",
            repo={**REPO, "full_name": "acme/other"},
            github_user_id=42,
            opened_by="github:42",
            token=TOKEN,
        )
    assert refused.value.code == "thread_has_repository"


async def test_a_repository_over_the_cap_is_refused_before_cloning(
    git_server: _Server, settings: Settings
) -> None:
    big = {**REPO, "size": (settings.repo_clone_max_mb + 1) * 1024}
    with pytest.raises(checkouts.CheckoutRefused) as refused:
        await _open(settings, repo=big)
    assert refused.value.code == "repository_too_large"
    assert git_server.headers == []
    assert checkouts.read_checkout(settings, "acme", "acme:t1") is None


async def test_a_failed_clone_leaves_no_half_checkout_and_stops_the_tools(
    git_server: _Server, settings: Settings
) -> None:
    await _open(settings, repo={**REPO, "full_name": "acme/missing"})
    state = checkouts.read_checkout(settings, "acme", "acme:t1")
    assert state is not None and state["state"] == "failed"
    assert TOKEN not in state["error"]
    # What a client reads back is classified, not git's own text, which names this server's paths.
    assert state["error"] == "GitHub refused the clone for this account"
    assert "/" not in state["error"]
    directory = checkouts.thread_dir(settings, "acme", "acme:t1")
    assert not (directory / "repo").exists()
    assert not any(p.name.startswith(".clone-") for p in directory.iterdir())
    with pytest.raises(ValueError, match=r"workspace_root: .*failed"):
        _in_thread(settings, "acme:t1")


async def test_a_cloning_checkout_stops_the_tools_rather_than_using_the_shared_workspace(
    settings: Settings,
) -> None:
    directory = checkouts.thread_dir(settings, "acme", "acme:t1")
    directory.mkdir(parents=True)
    checkouts._write_state(directory, {"state": "cloning", "repo": "acme/widgets"})
    with pytest.raises(ValueError, match="still cloning"):
        _in_thread(settings, "acme:t1")


async def test_an_unused_checkout_expires_and_says_so(git_server: Any, settings: Settings) -> None:
    await _open(settings)
    directory = checkouts.thread_dir(settings, "acme", "acme:t1")
    old = time.time() - (settings.repo_checkout_ttl_days + 1) * 86_400
    os.utime(directory / checkouts.USED_FILE, (old, old))
    assert checkouts.sweep_expired(settings) == 1
    assert not (directory / "repo").exists()
    with pytest.raises(ValueError, match="removed after 14 days unused"):
        _in_thread(settings, "acme:t1")


async def test_a_checkout_in_use_is_not_swept(git_server: Any, settings: Settings) -> None:
    await _open(settings)
    _in_thread(settings, "acme:t1")  # a tool call marks it used
    assert checkouts.sweep_expired(settings) == 0


async def test_describe_reports_branch_commits_ahead_and_dirty(git_server: Any, settings: Settings) -> None:
    await _open(settings)
    repo = checkouts.thread_dir(settings, "acme", "acme:t1") / "repo"
    described = await checkouts.describe(settings, "acme", "acme:t1")
    assert described is not None
    assert (described["branch"], described["ahead"], described["dirty"]) == ("main", 0, False)
    (repo / "new.txt").write_text("x\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "three")
    (repo / "wip.txt").write_text("y\n")
    described = await checkouts.describe(settings, "acme", "acme:t1")
    assert described is not None
    assert (described["ahead"], described["dirty"]) == (1, True)


def test_a_checkout_root_inside_the_shared_workspace_is_refused(tmp_path: Path) -> None:
    shared = tmp_path / "workspace"
    shared.mkdir()
    nested = Settings(
        database_url="memory://x", workspace_root=str(shared), repo_checkout_root=str(shared / "checkouts")
    )
    with pytest.raises(ValueError, match="inside FELIX_WORKSPACE_ROOT"):
        checkouts.checkout_root(nested)


async def test_removing_a_checkout_deletes_it(git_server: Any, settings: Settings) -> None:
    await _open(settings)
    assert checkouts.remove_checkout(settings, "acme", "acme:t1") is True
    assert checkouts.read_checkout(settings, "acme", "acme:t1") is None
    assert _in_thread(settings, "acme:t1") == Path(settings.workspace_root).resolve()
    assert checkouts.remove_checkout(settings, "acme", "acme:t1") is False


def test_the_remote_shell_runner_is_refused_for_a_thread_checkout(settings: Settings, tmp_path: Path) -> None:
    from felix.tools.shell import _is_thread_checkout

    assert _is_thread_checkout(Path(settings.workspace_root).resolve(), settings) is False
    assert _is_thread_checkout(tmp_path / "data" / "checkouts" / "x" / "repo", settings) is True


async def test_the_listing_names_every_file_with_its_size_and_status(
    git_server: Any, settings: Settings
) -> None:
    await _open(settings)
    repo = checkouts.thread_dir(settings, "acme", "acme:t1") / "repo"
    (repo / "app.py").write_text("print(2)\n# more\n")
    (repo / "README.md").unlink()
    (repo / "src").mkdir()
    (repo / "src" / "new.py").write_text("x = 1\n")
    (repo / "staged.txt").write_text("s\n")
    _git(repo, "add", "staged.txt")
    (repo / ".gitignore").write_text("*.log\n")
    (repo / "noise.log").write_text("ignored\n")
    listed = await checkouts.list_files(settings, "acme", "acme:t1")
    assert listed is not None and listed["state"] == "ready" and listed["truncated"] is False
    rows = {f["path"]: f for f in listed["files"]}
    assert rows["app.py"] == {"path": "app.py", "kind": "file", "size": 16, "status": "modified"}
    assert rows["README.md"]["kind"] == "missing" and rows["README.md"]["status"] == "deleted"
    assert rows["src/new.py"]["status"] == "untracked"
    assert rows["staged.txt"]["status"] == "added"
    assert "noise.log" not in rows  # ignored files are not the repository's
    assert not any(p.startswith(".git/") for p in rows)
    assert [f["path"] for f in listed["files"]] == sorted(rows)


async def test_a_symlink_is_listed_as_one_and_never_followed(git_server: Any, settings: Settings) -> None:
    await _open(settings)
    repo = checkouts.thread_dir(settings, "acme", "acme:t1") / "repo"
    os.symlink("/etc/passwd", repo / "escape")
    listed = await checkouts.list_files(settings, "acme", "acme:t1")
    assert listed is not None
    row = next(f for f in listed["files"] if f["path"] == "escape")
    assert row["kind"] == "symlink" and row["size"] == len("/etc/passwd")


async def test_a_prefix_narrows_the_listing_and_a_limit_says_it_cut(
    git_server: Any, settings: Settings
) -> None:
    await _open(settings)
    repo = checkouts.thread_dir(settings, "acme", "acme:t1") / "repo"
    (repo / "src").mkdir()
    for name in ("a.py", "b.py", "c.py"):
        (repo / "src" / name).write_text("")
    (repo / "src*").mkdir()
    (repo / "src*" / "literal.py").write_text("")
    listed = await checkouts.list_files(settings, "acme", "acme:t1", prefix="src/")
    assert listed is not None
    assert [f["path"] for f in listed["files"]] == ["src/a.py", "src/b.py", "src/c.py"]
    # A prefix is a directory name, not a glob.
    starred = await checkouts.list_files(settings, "acme", "acme:t1", prefix="src*")
    assert starred is not None and [f["path"] for f in starred["files"]] == ["src*/literal.py"]
    cut = await checkouts.list_files(settings, "acme", "acme:t1", prefix="src", limit=2)
    assert cut is not None and len(cut["files"]) == 2 and cut["truncated"] is True


@pytest.mark.parametrize("prefix", ["..", "../x", "src/../..", "a\\b", "./src", "a//b"])
async def test_a_prefix_outside_the_checkout_is_refused(
    git_server: Any, settings: Settings, prefix: str
) -> None:
    await _open(settings)
    with pytest.raises(checkouts.ListRefused) as refused:
        await checkouts.list_files(settings, "acme", "acme:t1", prefix=prefix)
    assert refused.value.code == "invalid_prefix"


async def test_the_listing_answers_for_a_checkout_that_is_not_ready(settings: Settings) -> None:
    assert await checkouts.list_files(settings, "acme", "acme:none") is None
    directory = checkouts.thread_dir(settings, "acme", "acme:t1")
    directory.mkdir(parents=True)
    checkouts._write_state(directory, {"state": "cloning", "repo": "acme/widgets"})
    with pytest.raises(checkouts.ListRefused) as refused:
        await checkouts.list_files(settings, "acme", "acme:t1")
    assert refused.value.code == "checkout_cloning"
    for state in ("failed", "expired"):
        checkouts._write_state(directory, {"state": state, "repo": "acme/widgets"})
        assert await checkouts.list_files(settings, "acme", "acme:t1") == {
            "state": state,
            "files": [],
            "truncated": False,
        }
