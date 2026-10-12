"""A git worktree per child task (`felix.repos.worktrees`), against real repositories.

What is pinned: a child's worktree is a working tree of the parent thread's repository on a branch
of its own, served as the child thread's workspace; two children and the parent do not see each
other's edits, but the parent's repository holds every child's commits; nothing the repository
configures runs; and a worktree goes with its thread's checkout -- or with its parent's -- leaving
its branch.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import pytest
from felix.config import Settings
from felix.repos import checkouts, worktrees
from felix.tools.workspace_scope import bound_workspace_thread

from tests.support.git_fixture import IDENTITY, git, git_worktree, ready_local_checkout

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

TENANT = "acme"
PARENT = "acme:lead"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return Settings(workspace_root=str(workspace), repo_checkout_root=str(tmp_path / "checkouts"))


def _branches(repo: Path) -> list[str]:
    return git(repo, "branch", "--list", "felix/task/*", "--format=%(refname:short)").split()


def _parent_checkout(settings: Settings, thread: str = PARENT, **state: object) -> Path:
    return ready_local_checkout(settings, TENANT, thread, **state)


async def test_a_child_works_in_its_own_branch_of_the_parents_repository(settings: Settings) -> None:
    repo = _parent_checkout(settings)
    state = await worktrees.open_worktree(settings, TENANT, PARENT, "acme:lead:task:a")
    path = checkouts.thread_workspace(settings, TENANT, "acme:lead:task:a")
    assert path == (checkouts.thread_dir(settings, TENANT, "acme:lead:task:a") / "repo").resolve()
    assert (path / "README.md").read_text() == "hello\n", "it starts from the parent's commit"
    assert state["branch"].startswith(worktrees.BRANCH_PREFIX)
    assert git_worktree(path, "branch", "--show-current").strip() == state["branch"]

    (path / "child.txt").write_text("from the child\n")
    git_worktree(path, "add", "child.txt")
    git_worktree(path, "commit", "-q", "-m", "child work", extra=IDENTITY)
    assert not (repo / "child.txt").exists(), "the parent's working tree is untouched"
    assert "child work" in git(repo, "log", "--oneline", state["branch"]), "but its repository has the commit"


async def test_two_children_do_not_see_each_others_edits(settings: Settings) -> None:
    _parent_checkout(settings)
    a = await worktrees.open_worktree(settings, TENANT, PARENT, "acme:lead:task:a")
    b = await worktrees.open_worktree(settings, TENANT, PARENT, "acme:lead:task:b")
    assert a["branch"] != b["branch"]
    path_a = checkouts.thread_workspace(settings, TENANT, "acme:lead:task:a")
    path_b = checkouts.thread_workspace(settings, TENANT, "acme:lead:task:b")
    (path_a / "README.md").write_text("a's edit\n")
    assert (path_b / "README.md").read_text() == "hello\n"


async def test_opening_again_returns_the_same_worktree(settings: Settings) -> None:
    _parent_checkout(settings)
    first = await worktrees.open_worktree(settings, TENANT, PARENT, "acme:lead:task:a")
    assert await worktrees.open_worktree(settings, TENANT, PARENT, "acme:lead:task:a") == first


@pytest.mark.parametrize("setup", ["none", "hosted", "cloning"])
async def test_a_parent_without_a_usable_local_repository_gives_no_worktree(
    settings: Settings, setup: str
) -> None:
    if setup == "hosted":
        _parent_checkout(settings, hosted=True)
    elif setup == "cloning":
        directory = checkouts.thread_dir(settings, TENANT, PARENT)
        directory.mkdir(parents=True)
        checkouts._write_state(directory, {"state": checkouts.CLONING, "repo": "o/r"})
    with pytest.raises(worktrees.WorktreeRefused) as refused:
        await worktrees.open_worktree(settings, TENANT, PARENT, "acme:lead:task:a")
    assert refused.value.code == worktrees.NO_REPOSITORY
    assert checkouts.read_checkout(settings, TENANT, "acme:lead:task:a") is None


async def test_a_repository_that_names_a_filter_driver_is_not_checked_out(
    settings: Settings, tmp_path: Path
) -> None:
    """A smudge filter would run a program, in this process, on the worktree's checkout."""
    repo = _parent_checkout(settings)
    marker = tmp_path / "ran"
    git(repo, "config", "filter.evil.smudge", f"touch {marker}; cat")
    (repo / ".gitattributes").write_text("* filter=evil\n")
    git(repo, "add", ".gitattributes")
    git(repo, "commit", "-q", "-m", "attrs", extra=IDENTITY)
    with pytest.raises(worktrees.WorktreeRefused) as refused:
        await worktrees.open_worktree(settings, TENANT, PARENT, "acme:lead:task:a")
    assert refused.value.code == worktrees.UNSAFE_REPOSITORY
    assert not marker.exists()
    assert not checkouts.thread_dir(settings, TENANT, "acme:lead:task:a").exists()


async def test_a_thread_with_a_checkout_of_its_own_is_not_given_a_worktree(settings: Settings) -> None:
    _parent_checkout(settings)
    _parent_checkout(settings, thread="acme:lead:task:a")
    with pytest.raises(worktrees.WorktreeRefused) as refused:
        await worktrees.open_worktree(settings, TENANT, PARENT, "acme:lead:task:a")
    assert refused.value.code == "checkout_exists"


async def test_the_report_says_how_many_commits_and_whether_work_was_left_uncommitted(
    settings: Settings,
) -> None:
    _parent_checkout(settings)
    state = await worktrees.open_worktree(settings, TENANT, PARENT, "acme:lead:task:a")
    path = checkouts.thread_workspace(settings, TENANT, "acme:lead:task:a")
    clean = await worktrees.inspect_worktree(settings, TENANT, "acme:lead:task:a")
    assert clean is not None and (clean.commits, clean.dirty) == (0, False)
    assert clean.base == state["base_commit"]
    for n in (1, 2):
        (path / f"f{n}").write_text(str(n))
        git_worktree(path, "add", f"f{n}")
        git_worktree(path, "commit", "-q", "-m", f"c{n}", extra=IDENTITY)
    (path / "loose").write_text("not committed")
    report = await worktrees.inspect_worktree(settings, TENANT, "acme:lead:task:a")
    assert report is not None and (report.commits, report.dirty) == (2, True)
    sentence = worktrees.describe(report)
    assert f"branch {state['branch']}: 2 commit(s)" in sentence and "uncommitted changes" in sentence
    assert await worktrees.inspect_worktree(settings, TENANT, PARENT) is None, "a parent is no worktree"


def test_a_report_that_could_not_check_for_uncommitted_work_says_so() -> None:
    report = worktrees.WorktreeReport(branch="felix/task/x", base="a" * 40, commits=1, dirty=None)
    assert "could not be checked safely" in worktrees.describe(report)


async def test_an_untouched_worktree_is_discarded_with_its_branch_and_a_used_one_is_kept(
    settings: Settings,
) -> None:
    repo = _parent_checkout(settings)
    a = await worktrees.open_worktree(settings, TENANT, PARENT, "acme:lead:task:a")
    b = await worktrees.open_worktree(settings, TENANT, PARENT, "acme:lead:task:b")
    path_b = checkouts.thread_workspace(settings, TENANT, "acme:lead:task:b")
    (path_b / "loose").write_text("uncommitted")
    for thread in ("acme:lead:task:a", "acme:lead:task:b"):
        report = await worktrees.inspect_worktree(settings, TENANT, thread)
        assert report is not None
        await worktrees.discard_if_untouched(settings, TENANT, thread, report)
    assert _branches(repo) == [b["branch"]], f"{a['branch']} went with its worktree"
    assert (path_b / "loose").exists()


async def test_removing_a_childs_checkout_removes_its_worktree_and_keeps_its_branch(
    settings: Settings,
) -> None:
    repo = _parent_checkout(settings)
    state = await worktrees.open_worktree(settings, TENANT, PARENT, "acme:lead:task:a")
    path = checkouts.thread_workspace(settings, TENANT, "acme:lead:task:a")
    (path / "x").write_text("x")
    git_worktree(path, "add", "x")
    git_worktree(path, "commit", "-q", "-m", "kept", extra=IDENTITY)
    assert await checkouts.remove_checkout(settings, TENANT, "acme:lead:task:a")
    assert str(path) not in git(repo, "worktree", "list", "--porcelain")
    assert "kept" in git(repo, "log", "--oneline", state["branch"])


async def test_removing_the_parents_checkout_marks_its_childrens_worktrees_failed(settings: Settings) -> None:
    _parent_checkout(settings)
    await worktrees.open_worktree(settings, TENANT, PARENT, "acme:lead:task:a")
    assert await checkouts.remove_checkout(settings, TENANT, PARENT)
    state = checkouts.read_checkout(settings, TENANT, "acme:lead:task:a")
    assert state is not None and state["state"] == checkouts.FAILED
    assert not (checkouts.thread_dir(settings, TENANT, "acme:lead:task:a") / "repo").exists()
    with pytest.raises(ValueError, match="workspace_root"):
        checkouts.thread_workspace(settings, TENANT, "acme:lead:task:a")


async def test_an_unused_childs_worktree_is_swept_out_of_its_parents_repository(settings: Settings) -> None:
    repo = _parent_checkout(settings)
    await worktrees.open_worktree(settings, TENANT, PARENT, "acme:lead:task:a")
    child_dir = checkouts.thread_dir(settings, TENANT, "acme:lead:task:a")
    old = 1_000_000.0
    os.utime(child_dir / checkouts.USED_FILE, (old, old))
    removed = await checkouts.sweep_expired(settings, now=old + settings.repo_checkout_ttl_days * 86_400 + 1)
    assert removed == 1
    assert checkouts.read_checkout(settings, TENANT, "acme:lead:task:a")["state"] == checkouts.EXPIRED
    assert str(child_dir / "repo") not in git(repo, "worktree", "list", "--porcelain")
    assert checkouts.read_checkout(settings, TENANT, PARENT)["state"] == checkouts.READY


async def test_a_bound_workspace_thread_is_where_the_tools_work(settings: Settings) -> None:
    """A foreground child keeps the parent's request; the workspace it works in is its own."""
    from felix.context import AuthContext, RequestContext, async_run_with_context
    from felix.tools.workspace import workspace_root
    from felix.tools.workspace_backend import current_workspace_scope

    repo = _parent_checkout(settings)
    await worktrees.open_worktree(settings, TENANT, PARENT, "acme:lead:task:a")
    child = checkouts.thread_workspace(settings, TENANT, "acme:lead:task:a")
    ctx = RequestContext(settings=settings, auth=AuthContext(tenant_id=TENANT), thread_id=PARENT)
    async with async_run_with_context(ctx):
        assert workspace_root() == repo.resolve()
        with bound_workspace_thread("acme:lead:task:a"):
            assert workspace_root() == child
            assert current_workspace_scope()[1].thread_id == "acme:lead:task:a"
        assert workspace_root() == repo.resolve()


def _req(settings: Settings, thread: str | None = PARENT) -> Any:
    from felix.context import AuthContext, RequestContext

    return RequestContext(settings=settings, auth=AuthContext(tenant_id=TENANT), thread_id=thread)


async def test_a_background_child_under_hosted_is_told_it_has_no_repository(settings: Settings) -> None:
    """A worktree cannot span two sandboxes: the start message says so rather than nothing."""
    from felix.tools import task_workspace

    _parent_checkout(settings, hosted=True)
    ws = await task_workspace.prepare(_req(settings), "acme:lead:task:a", wanted=True)
    assert ws == task_workspace.ChildWorkspace(hosted=True)
    assert "no copy of this thread's repository" in task_workspace.background_note(ws)
    plain = await task_workspace.prepare(_req(settings, "acme:other"), "acme:other:task:a", wanted=True)
    assert plain == task_workspace.ChildWorkspace()
    assert task_workspace.background_note(plain) == ""


@pytest.mark.parametrize(
    ("state", "says"), [("cloning", "still cloning"), ("expired", "open the repository again")]
)
async def test_a_parent_whose_repository_is_not_ready_starts_no_child(
    settings: Settings, state: str, says: str
) -> None:
    from felix.tools import task_workspace
    from felix.tools.types import tool_output_content

    directory = checkouts.thread_dir(settings, TENANT, PARENT)
    directory.mkdir(parents=True)
    checkouts._write_state(directory, {"state": state, "repo": "o/r"})
    refusal = await task_workspace.prepare(_req(settings), "acme:lead:task:a", wanted=False)
    assert not isinstance(refusal, task_workspace.ChildWorkspace)
    assert says in tool_output_content(refusal)


async def test_a_thread_has_at_most_so_many_worktrees(settings: Settings, monkeypatch: Any) -> None:
    monkeypatch.setattr(worktrees, "MAX_WORKTREES_PER_THREAD", 2)
    repo = _parent_checkout(settings)
    await worktrees.open_worktree(settings, TENANT, PARENT, "acme:lead:task:a")
    await worktrees.open_worktree(settings, TENANT, PARENT, "acme:lead:task:b")
    with pytest.raises(worktrees.WorktreeRefused) as refused:
        await worktrees.open_worktree(settings, TENANT, PARENT, "acme:lead:task:c")
    assert refused.value.code == worktrees.TOO_MANY
    assert len(_branches(repo)) == 2
    await checkouts.remove_checkout(settings, TENANT, "acme:lead:task:a")
    await worktrees.open_worktree(settings, TENANT, PARENT, "acme:lead:task:c")


async def test_a_conditional_include_that_names_a_filter_on_the_task_branch_is_caught(
    settings: Settings, tmp_path: Path
) -> None:
    """`includeIf "onbranch:felix/task/**"` is invisible from the parent's branch: checked from
    inside the new worktree, before any file is written, it refuses and leaves nothing behind."""
    repo = _parent_checkout(settings)
    marker = tmp_path / "ran"
    evil = tmp_path / "evil.cfg"
    evil.write_text(f'[filter "evil"]\n\tsmudge = touch {marker}; cat\n')
    git(repo, "config", "includeIf.onbranch:felix/task/**.path", str(evil))
    (repo / ".gitattributes").write_text("* filter=evil\n")
    git(repo, "add", ".gitattributes")
    git(repo, "commit", "-q", "-m", "attrs", extra=IDENTITY)
    with pytest.raises(worktrees.WorktreeRefused) as refused:
        await worktrees.open_worktree(settings, TENANT, PARENT, "acme:lead:task:a")
    assert refused.value.code == worktrees.UNSAFE_REPOSITORY
    assert not marker.exists()
    assert not checkouts.thread_dir(settings, TENANT, "acme:lead:task:a").exists()
    assert _branches(repo) == []
    assert git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1


async def test_a_filter_check_that_errors_refuses(settings: Settings, monkeypatch: Any) -> None:
    """Only git's "no match" clears a repository: an error is not an answer."""
    _parent_checkout(settings)
    real = worktrees._git

    async def failing(settings: Settings, cwd: Path, *args: str) -> tuple[int, str]:
        return (128, "") if args[:1] == ("config",) else await real(settings, cwd, *args)

    monkeypatch.setattr(worktrees, "_git", failing)
    with pytest.raises(worktrees.WorktreeRefused) as refused:
        await worktrees.open_worktree(settings, TENANT, PARENT, "acme:lead:task:a")
    assert refused.value.code == worktrees.UNSAFE_REPOSITORY


async def test_plan_mode_makes_no_worktree(settings: Settings) -> None:
    from felix.governance import permission_mode
    from felix.tools import task_workspace

    _parent_checkout(settings)
    req = _req(settings)
    await permission_mode.set_thread_mode(req, "plan", via="test")
    assert (
        await task_workspace.prepare(req, "acme:lead:task:a", wanted=True) == task_workspace.ChildWorkspace()
    )
    assert checkouts.read_checkout(settings, TENANT, "acme:lead:task:a") is None


async def test_a_misconfigured_checkout_root_leaves_delegation_as_it_was(tmp_path: Path) -> None:
    from felix.tools import task_workspace

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = Settings(workspace_root=str(workspace), repo_checkout_root=str(workspace / "checkouts"))
    ws = await task_workspace.prepare(_req(settings), "acme:lead:task:a", wanted=True)
    assert ws == task_workspace.ChildWorkspace()


async def test_a_childs_own_child_branches_from_the_childs_worktree(settings: Settings) -> None:
    from felix.tools import task_workspace

    repo = _parent_checkout(settings)
    req = _req(settings)
    child = await task_workspace.prepare(req, "acme:lead:task:a", wanted=True)
    assert isinstance(child, task_workspace.ChildWorkspace) and child.thread
    path = checkouts.thread_workspace(settings, TENANT, child.thread)
    (path / "c").write_text("c")
    git_worktree(path, "add", "c")
    git_worktree(path, "commit", "-q", "-m", "child", extra=IDENTITY)
    head = git_worktree(path, "rev-parse", "HEAD").strip()
    with bound_workspace_thread(child.thread):
        grandchild = await task_workspace.prepare(req, "acme:lead:task:a:task:b", wanted=True)
    assert isinstance(grandchild, task_workspace.ChildWorkspace) and grandchild.thread
    state = checkouts.read_checkout(settings, TENANT, grandchild.thread)
    assert state is not None and state["base_commit"] == head and state["worktree_of"] == child.thread
    assert git(repo, "rev-parse", "main").strip() != head

    assert await checkouts.remove_checkout(settings, TENANT, child.thread)
    orphan = checkouts.read_checkout(settings, TENANT, grandchild.thread)
    assert orphan is not None and orphan["state"] == checkouts.FAILED


async def test_discarding_a_child_that_never_started_removes_its_branch(settings: Settings) -> None:
    from felix.tools import task_workspace

    repo = _parent_checkout(settings)
    req = _req(settings)
    ws = await task_workspace.prepare(req, "acme:lead:task:a", wanted=True)
    assert isinstance(ws, task_workspace.ChildWorkspace) and ws.branch
    await task_workspace.discard(req, ws)
    assert _branches(repo) == []
    assert checkouts.read_checkout(settings, TENANT, "acme:lead:task:a") is None


async def test_using_a_childs_worktree_keeps_its_parent_from_expiring(settings: Settings) -> None:
    _parent_checkout(settings)
    await worktrees.open_worktree(settings, TENANT, PARENT, "acme:lead:task:a")
    used = checkouts.thread_dir(settings, TENANT, PARENT) / checkouts.USED_FILE
    used.touch()
    os.utime(used, (1_000_000.0, 1_000_000.0))
    checkouts.thread_workspace(settings, TENANT, "acme:lead:task:a")
    assert used.stat().st_mtime > 1_000_000.0


async def test_a_child_cannot_publish_from_its_worktree(settings: Settings) -> None:
    from felix.context import async_run_with_context
    from felix.manifests.schema import GithubPublishSpec
    from felix.tools import github_publish

    _parent_checkout(settings)
    await worktrees.open_worktree(settings, TENANT, PARENT, "acme:lead:task:a")
    executor = github_publish._ThreadPublishExecutor(GithubPublishSpec(auth="person", branch_prefix="felix/"))
    async with async_run_with_context(_req(settings)):
        with bound_workspace_thread("acme:lead:task:a"):
            resolved = await executor._resolve()
    assert isinstance(resolved, str)
    assert resolved.startswith("[publish refused] this is a child task's worktree"), resolved


async def test_a_worktree_whose_own_children_still_work_is_not_discarded(settings: Settings) -> None:
    """Untouched itself, but its child's worktree hangs off the same repository and may be running."""
    _parent_checkout(settings)
    await worktrees.open_worktree(settings, TENANT, PARENT, "acme:lead:task:a")
    await worktrees.open_worktree(settings, TENANT, "acme:lead:task:a", "acme:lead:task:a:task:b")
    report = await worktrees.inspect_worktree(settings, TENANT, "acme:lead:task:a")
    assert report is not None and (report.commits, report.dirty) == (0, False)
    assert not await worktrees.discard_if_untouched(settings, TENANT, "acme:lead:task:a", report)
    grandchild = checkouts.read_checkout(settings, TENANT, "acme:lead:task:a:task:b")
    assert grandchild is not None and grandchild["state"] == checkouts.READY


async def test_using_a_grandchilds_worktree_keeps_every_checkout_above_it(settings: Settings) -> None:
    _parent_checkout(settings)
    await worktrees.open_worktree(settings, TENANT, PARENT, "acme:lead:task:a")
    await worktrees.open_worktree(settings, TENANT, "acme:lead:task:a", "acme:lead:task:a:task:b")
    used = [
        checkouts.thread_dir(settings, TENANT, t) / checkouts.USED_FILE for t in (PARENT, "acme:lead:task:a")
    ]
    for path in used:
        os.utime(path, (1_000_000.0, 1_000_000.0))
    checkouts.thread_workspace(settings, TENANT, "acme:lead:task:a:task:b")
    assert all(path.stat().st_mtime > 1_000_000.0 for path in used)


async def test_harness_git_reaches_no_transport_whatever_the_repository_allows(
    settings: Settings, tmp_path: Path
) -> None:
    """A repository's own `protocol.ext.allow=always` outranks `-c protocol.allow=never`; the
    environment's `GIT_ALLOW_PROTOCOL` outranks both."""
    repo = _parent_checkout(settings)
    marker = tmp_path / "ran"
    git(repo, "config", "protocol.ext.allow", "always")
    # `ls-remote` fails either way (`touch` speaks no protocol): whether the command ran is the test.
    await worktrees._git(settings, repo, "ls-remote", f"ext::sh -c touch% {marker}")
    assert not marker.exists()


async def test_a_worktree_whose_uncommitted_work_could_not_be_checked_is_kept(settings: Settings) -> None:
    """`status` would run a filter named after the worktree was made: not checked, so not "clean"."""
    repo = _parent_checkout(settings)
    state = await worktrees.open_worktree(settings, TENANT, PARENT, "acme:lead:task:a")
    path = checkouts.thread_workspace(settings, TENANT, "acme:lead:task:a")
    (path / "loose").write_text("uncommitted")
    git(repo, "config", "filter.x.smudge", "cat")
    report = await worktrees.inspect_worktree(settings, TENANT, "acme:lead:task:a")
    assert report is not None and report.dirty is None
    assert not await worktrees.discard_if_untouched(settings, TENANT, "acme:lead:task:a", report)
    assert (path / "loose").exists() and _branches(repo) == [state["branch"]]


async def test_a_parent_is_told_the_branch_even_when_the_worktree_cannot_be_inspected(
    settings: Settings, monkeypatch: Any
) -> None:
    from felix.tools import task_workspace

    _parent_checkout(settings)
    req = _req(settings)
    ws = await task_workspace.prepare(req, "acme:lead:task:a", wanted=True)
    assert isinstance(ws, task_workspace.ChildWorkspace) and ws.branch

    async def broken(*_: Any) -> Any:
        raise OSError("disk")

    monkeypatch.setattr(worktrees, "inspect_worktree", broken)
    told = await task_workspace.finish_foreground(req, ws)
    assert told == f"[task] It worked in its own git worktree on branch {ws.branch}."


async def test_opening_a_worktree_marks_the_parent_in_use(settings: Settings) -> None:
    """Or a parent past its TTL could be swept while the worktree is still being made."""
    _parent_checkout(settings)
    used = checkouts.thread_dir(settings, TENANT, PARENT) / checkouts.USED_FILE
    os.utime(used, (1_000_000.0, 1_000_000.0))
    await worktrees.open_worktree(settings, TENANT, PARENT, "acme:lead:task:a")
    assert used.stat().st_mtime > 1_000_000.0


async def test_concurrent_children_each_see_their_own_workspace(settings: Settings) -> None:
    """Parallel `task` calls run as sibling tasks: each binding is its own, however they interleave."""
    import asyncio

    from felix.context import async_run_with_context
    from felix.tools.workspace import workspace_root

    _parent_checkout(settings)
    threads = ("acme:lead:task:a", "acme:lead:task:b")
    for thread in threads:
        await worktrees.open_worktree(settings, TENANT, PARENT, thread)
    bound = {t: asyncio.Event() for t in threads}
    read = {t: asyncio.Event() for t in threads}

    async def child(mine: str, other: str) -> Path:
        with bound_workspace_thread(mine):
            bound[mine].set()
            await bound[other].wait()  # both bindings are live now
            seen = workspace_root()
            read[mine].set()
            await read[other].wait()  # and stay live until both have read
            return seen

    async with async_run_with_context(_req(settings)):
        a, b = await asyncio.gather(child(*threads), child(*reversed(threads)))
    assert a == checkouts.thread_workspace(settings, TENANT, threads[0])
    assert b == checkouts.thread_workspace(settings, TENANT, threads[1])


async def test_a_foreground_child_cancelled_mid_run_leaves_no_untouched_worktree(settings: Settings) -> None:
    """A real `Task.cancel()`, not a raised exception: the cleanup's own awaits (git) must still
    finish while the cancellation is delivered."""
    import asyncio

    from felix.context import async_run_with_context
    from felix.limits import effective_limits
    from felix.manifests.loader import parse_manifest
    from felix.tools.delegation import make_task_tool

    repo = _parent_checkout(settings)
    started = asyncio.Event()

    class Blocks:
        async def invoke(self, _: Any) -> Any:
            started.set()
            await asyncio.Event().wait()

    manifest = parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": "coder"},
         "spec": {"auth": {"inbound": {"allow_anonymous": True}}}}
    )  # fmt: skip
    tool = make_task_tool({"coder": (Blocks(), "C", manifest)}, ceiling=effective_limits(None), worktree=True)  # type: ignore[dict-item]

    async def call() -> Any:
        async with async_run_with_context(_req(settings)):
            return await tool.executor.execute({"agent": "coder", "prompt": "x"}, None)

    task = asyncio.create_task(call())
    await asyncio.wait_for(started.wait(), 5)
    assert len(_branches(repo)) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.5)  # a shielded cleanup finishes after the cancellation is delivered
    for _ in range(50):
        if not _branches(repo):
            break
        await asyncio.sleep(0.1)
    assert _branches(repo) == []


async def test_a_foreground_child_under_a_cancel_scope_leaves_no_untouched_worktree(
    settings: Settings,
) -> None:
    """anyio cancels at every await until the scope exits: unshielded, the cleanup's first git call
    would be cancelled too, and the worktree left behind."""
    import asyncio

    import anyio
    from felix.context import async_run_with_context
    from felix.limits import effective_limits
    from felix.manifests.loader import parse_manifest
    from felix.tools.delegation import make_task_tool

    repo = _parent_checkout(settings)
    started = asyncio.Event()

    class Blocks:
        async def invoke(self, _: Any) -> Any:
            started.set()
            await asyncio.Event().wait()

    manifest = parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": "coder"},
         "spec": {"auth": {"inbound": {"allow_anonymous": True}}}}
    )  # fmt: skip
    tool = make_task_tool({"coder": (Blocks(), "C", manifest)}, ceiling=effective_limits(None), worktree=True)  # type: ignore[dict-item]

    async with anyio.create_task_group() as group:

        async def call(scope: anyio.CancelScope) -> None:
            with scope:
                async with async_run_with_context(_req(settings)):
                    await tool.executor.execute({"agent": "coder", "prompt": "x"}, None)

        scope = anyio.CancelScope()
        group.start_soon(call, scope)
        await started.wait()
        assert len(_branches(repo)) == 1
        scope.cancel()
    for _ in range(50):
        if not _branches(repo):
            break
        await asyncio.sleep(0.1)
    assert _branches(repo) == []


async def test_status_output_past_the_cap_counts_as_uncommitted_work(settings: Settings) -> None:
    _parent_checkout(settings)
    await worktrees.open_worktree(settings, TENANT, PARENT, "acme:lead:task:a")
    path = checkouts.thread_workspace(settings, TENANT, "acme:lead:task:a")
    for n in range(worktrees._GIT_LIMIT // 40 + 10):
        (path / f"untracked-file-with-a-long-name-{n:06d}").touch()
    report = await worktrees.inspect_worktree(settings, TENANT, "acme:lead:task:a")
    assert report is not None and report.dirty is True


async def test_a_commit_count_that_could_not_be_read_keeps_the_worktree_and_its_branch(
    settings: Settings, monkeypatch: Any
) -> None:
    """Read as 0, a `rev-list` timeout would have force-deleted a branch holding the child's commits."""
    repo = _parent_checkout(settings)
    state = await worktrees.open_worktree(settings, TENANT, PARENT, "acme:lead:task:a")
    path = checkouts.thread_workspace(settings, TENANT, "acme:lead:task:a")
    (path / "x").write_text("x")
    git_worktree(path, "add", "x")
    git_worktree(path, "commit", "-q", "-m", "work", extra=IDENTITY)
    real = worktrees._git

    async def slow_rev_list(settings: Settings, cwd: Path, *args: str) -> tuple[int, str]:
        return (
            (worktrees.GIT_TIMED_OUT, "") if args[:1] == ("rev-list",) else await real(settings, cwd, *args)
        )

    monkeypatch.setattr(worktrees, "_git", slow_rev_list)
    report, removed = await worktrees.release_if_untouched(settings, TENANT, "acme:lead:task:a")
    assert report is not None and report.commits is None and report.dirty is False
    assert not removed
    assert _branches(repo) == [state["branch"]]
    assert "an unknown number of commit(s)" in worktrees.describe(report)
