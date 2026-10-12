"""A git worktree per child task: a child works on its own branch of the parent thread's repository.

A thread with a repository checkout (`repos/checkouts.py`) can hand jobs to child agents (`task`,
`tools/delegation.py`). Run in the parent's checkout, two children working at once edit the same
files, and a background child -- on a thread of its own -- has no checkout at all. Here each child
gets a `git worktree` of the parent's repository instead: its own working tree, on its own branch
(`felix/task/<key>`), sharing the parent's objects, so what it commits is in the parent's
repository the moment it commits it.

- **Where it lives.** The child's thread directory under the checkout root, exactly where a clone
  would be (`repo/`, with a `checkout.json` saying `worktree_of`), so `thread_workspace()` serves it
  to every tool the child runs without knowing it is a worktree. A child's own children branch from
  the child's worktree.
- **Nothing in the repository runs.** git runs with the harness's own prelude (hooks, fsmonitor,
  pager off, no transport at all, no lazy fetch) in an environment built from nothing and fenced at
  the checkout root. A filter driver would run on checkout and on `status`, so the repository's
  configuration -- includes resolved, read inside the new worktree too, where a conditional include
  can first apply -- must name none: anything else, including configuration that cannot be read,
  refuses the worktree, and the checkout happens only after that check.
- **Bounded.** At most `MAX_WORKTREES_PER_THREAD` live worktrees branch from one thread; a
  foreground child that leaves nothing behind has its worktree and branch removed at once.
- **Local checkouts only.** Under the hosted workspace backend each thread's repository is in its
  own sandbox, and a worktree cannot span two.
- **Lifetime.** A worktree goes when its thread's checkout does (removal, or
  FELIX_REPO_CHECKOUT_TTL_DAYS unused -- a child in use keeps its parent's checkout in use too),
  taking the working tree but never the branch. Removing a parent's checkout leaves its children's
  worktrees with nothing behind them: they, and theirs, are marked failed and their files removed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from felix.repos.checkouts import (
    FAILED,
    READY,
    REPO_DIR,
    USED_FILE,
    _key,
    _read_state,
    _write_state,
    checkout_root,
    thread_dir,
)

if TYPE_CHECKING:
    from felix.config import Settings

logger = logging.getLogger("felix.repos.worktrees")

BRANCH_PREFIX = "felix/task/"
MAX_WORKTREES_PER_THREAD = 8
_GIT_LIMIT = 64 * 1024

# Why a worktree was refused: codes the caller maps to what the model can do about it.
NO_REPOSITORY = "no_repository"
UNSAFE_REPOSITORY = "unsafe_repository"
TOO_MANY = "too_many_worktrees"
CHECKOUT_EXISTS = "checkout_exists"
FAILED_TO_CREATE = "worktree_failed"

_UNSAFE = "the repository's configuration may name a filter driver, which a checkout would run"


class WorktreeRefused(Exception):
    """A child task that cannot have a worktree, with a code and a fixed message for the model."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class WorktreeReport:
    branch: str
    base: str
    # None when it could not be read: never taken for "none".
    commits: int | None
    # None when it could not be checked safely (`status` would have run a filter).
    dirty: bool | None


RepoStatus = Literal["none", "hosted", "ready", "unavailable"]


def repository_status(
    settings: Settings, tenant_id: str, thread_id: str
) -> tuple[RepoStatus, dict[str, Any] | None]:
    """What a thread's children would branch from: `("none", None)` without a checkout,
    `("hosted", state)` for one in a sandbox, `("ready", state)` for a usable local one, else
    `("unavailable", state)` (cloning, failed, expired)."""
    state = _read_state(thread_dir(settings, tenant_id, thread_id))
    if state is None:
        return "none", None
    if state.get("state") != READY:
        return "unavailable", state
    return ("hosted" if state.get("hosted") else "ready"), state


# `_git`'s codes for an answer it did not read whole: output past `_GIT_LIMIT`, or no answer in time.
GIT_TRUNCATED = -1
GIT_TIMED_OUT = -2


async def _git(settings: Settings, cwd: Path, *args: str) -> tuple[int, str]:
    """git in `cwd` through the publish path's runner (prelude, empty environment, killed past the
    cap or when cancelled), fenced further: no transport and no lazy fetch, and no repository
    discovered above the checkout root. A caller deciding on the output refuses on either code
    above rather than reading half of it."""
    from felix.tools.github_publish import _git_exec

    env = {
        "GIT_CEILING_DIRECTORIES": str(checkout_root(settings)),
        "GIT_NO_LAZY_FETCH": "1",
        # Overrides every `protocol.*` key, including a repository's own `protocol.ext.allow`,
        # which `-c protocol.allow=never` would not.
        "GIT_ALLOW_PROTOCOL": "none",
    }
    try:
        result = await _git_exec(
            cwd, "-c", "protocol.allow=never", *args, stdin=None, limit=_GIT_LIMIT, env=env
        )
    except TimeoutError:
        return GIT_TIMED_OUT, ""
    if result.err:
        logger.debug("git %s: %s", args[0], result.err[-300:])
    if result.truncated:
        return GIT_TRUNCATED, ""
    return result.code, result.out.decode("utf-8", errors="replace")


async def _names_no_filter(settings: Settings, cwd: Path) -> bool:
    """Whether the repository's configuration, as seen from `cwd` with includes resolved, names no
    filter driver. Only git's "no match" (exit 1, nothing printed) counts: a match, an error or an
    answer too long to read all mean it may."""
    code, out = await _git(settings, cwd, "config", "--includes", "--get-regexp", r"^filter\.")
    return code == 1 and not out.strip()


def _live_children(settings: Settings, parent_key: str) -> list[Path]:
    root = checkout_root(settings)
    if not root.is_dir():
        return []
    out = []
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or directory.is_symlink():
            continue
        state = _read_state(directory)
        if state and state.get("parent_key") == parent_key and state.get("state") == READY:
            out.append(directory)
    return out


async def open_worktree(
    settings: Settings, tenant_id: str, parent_thread: str, child_thread: str
) -> dict[str, Any]:
    """The child thread's worktree of the parent's repository, made if missing. Returns its
    checkout state; raises `WorktreeRefused` when the parent has no usable local repository, has
    `MAX_WORKTREES_PER_THREAD` already, its configuration may name a filter driver, or git refuses."""
    status, parent = repository_status(settings, tenant_id, parent_thread)
    if status != "ready" or parent is None:
        raise WorktreeRefused(NO_REPOSITORY, "this thread has no local repository checkout to branch from")
    parent_dir = thread_dir(settings, tenant_id, parent_thread)
    repo = parent_dir / REPO_DIR
    if repo.is_symlink() or not repo.is_dir():
        raise WorktreeRefused(NO_REPOSITORY, "this thread's repository checkout is missing")
    # In use from here on, so the sweep does not take the repository out from under the worktree.
    with contextlib.suppress(OSError):
        (parent_dir / USED_FILE).touch()
    child_dir = thread_dir(settings, tenant_id, child_thread)
    existing = _read_state(child_dir)
    if existing is not None:
        if existing.get("worktree_of") == parent_thread and existing.get("state") == READY:
            return existing
        raise WorktreeRefused(CHECKOUT_EXISTS, "the child task's thread already has a checkout of its own")
    if len(await asyncio.to_thread(_live_children, settings, parent_dir.name)) >= MAX_WORKTREES_PER_THREAD:
        raise WorktreeRefused(
            TOO_MANY,
            f"this thread already has {MAX_WORKTREES_PER_THREAD} task worktrees holding work; until "
            "one expires, do this task in the foreground without a worktree, or yourself",
        )
    code, out = await _git(settings, repo, "rev-parse", "--verify", "HEAD^{commit}")
    if code != 0 or not out.strip():
        raise WorktreeRefused(FAILED_TO_CREATE, "the repository has no commit to branch from")
    base = out.strip()
    branch = BRANCH_PREFIX + _key(tenant_id, child_thread)[:16]
    path = child_dir / REPO_DIR
    child_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "state": READY,
        "repo": parent.get("repo", ""),
        "branch": branch,
        "base_commit": base,
        "worktree_of": parent_thread,
        "parent_key": parent_dir.name,
        "created_at": int(time.time() * 1000),
    }
    try:
        await _check_out(settings, repo, path, branch, base)
        _write_state(child_dir, state)
        with contextlib.suppress(OSError):
            (child_dir / USED_FILE).touch()
    except BaseException:
        # Whatever got as far as git's own records goes too: no worktree, branch or directory
        # left that no state names.
        await _undo(settings, repo, path, branch)
        await asyncio.to_thread(shutil.rmtree, child_dir, True)
        raise
    return state


async def _check_out(settings: Settings, repo: Path, path: Path, branch: str, base: str) -> None:
    """Register the worktree without a checkout, check its configuration from inside it -- a
    conditional include (`includeIf "onbranch:..."`, `"gitdir:..."`) can first apply there -- and
    only then write its files."""
    code, _ = await _git(
        settings, repo, "worktree", "add", "--quiet", "--no-checkout", "-b", branch, str(path), base
    )
    if code != 0:
        raise WorktreeRefused(FAILED_TO_CREATE, "git could not create a worktree for the child task")
    if not await _names_no_filter(settings, path):
        raise WorktreeRefused(UNSAFE_REPOSITORY, _UNSAFE)
    code, _ = await _git(settings, path, "reset", "--hard", "--quiet", "--no-recurse-submodules")
    if code != 0:
        raise WorktreeRefused(FAILED_TO_CREATE, "git could not check out the child task's worktree")


async def _undo(settings: Settings, repo: Path, path: Path, branch: str) -> None:
    steps = (("worktree", "remove", "--force", str(path)), ("worktree", "prune"), ("branch", "-D", branch))
    for args in steps:
        with contextlib.suppress(Exception):
            await _git(settings, repo, *args)


async def inspect_worktree(settings: Settings, tenant_id: str, child_thread: str) -> WorktreeReport | None:
    """What a child left in its worktree, or None when its thread has none: its commits past
    where it started, and whether it left changes uncommitted -- not checked (None) when the
    repository's configuration might name a filter, which `status` would run."""
    directory = thread_dir(settings, tenant_id, child_thread)
    state = _read_state(directory)
    if not state or not state.get("worktree_of") or state.get("state") != READY:
        return None
    path = directory / REPO_DIR
    branch, base = str(state.get("branch", "")), str(state.get("base_commit", ""))
    code, out = await _git(settings, path, "rev-list", "--count", f"{base}..HEAD")
    commits = int(out.strip()) if code == 0 and out.strip().isdigit() else None
    dirty: bool | None = None
    if await _names_no_filter(settings, path):
        code, out = await _git(settings, path, "status", "--porcelain", "--untracked-files=normal")
        # Past the output cap there is plenty uncommitted; any other failure says nothing.
        dirty = True if code == GIT_TRUNCATED else (bool(out.strip()) if code == 0 else None)
    return WorktreeReport(branch=branch, base=base, commits=commits, dirty=dirty)


def describe(report: WorktreeReport) -> str:
    """The report as one sentence for the parent agent."""
    counted = "an unknown number of" if report.commits is None else str(report.commits)
    sentence = (
        f"It worked in its own git worktree on branch {report.branch}: {counted} commit(s) "
        f"on top of {report.base[:12]}"
    )
    if report.dirty:
        sentence += "; it also left uncommitted changes there, which this thread cannot see"
    elif report.dirty is None:
        sentence += "; whether it left uncommitted changes could not be checked safely"
    return sentence + "."


async def discard_if_untouched(
    settings: Settings, tenant_id: str, child_thread: str, report: WorktreeReport
) -> bool:
    """A finished foreground child's worktree that holds nothing -- no commit, nothing uncommitted --
    removed with its branch, so a run that delegates often does not leave a tree per call."""
    if report.commits != 0 or report.dirty is not False:
        return False
    from felix.repos.checkouts import remove_checkout

    if await asyncio.to_thread(_live_children, settings, thread_dir(settings, tenant_id, child_thread).name):
        # Its own children's worktrees hang off the same repository and may still be working.
        return False

    await remove_checkout(settings, tenant_id, child_thread, delete_branch=True)
    return True


async def release_if_untouched(
    settings: Settings, tenant_id: str, child_thread: str
) -> tuple[WorktreeReport | None, bool]:
    """A child's run is over: what it left in its worktree (None when its thread has none), and
    whether the worktree went because that was nothing (`discard_if_untouched`)."""
    report = await inspect_worktree(settings, tenant_id, child_thread)
    if report is None:
        return None, False
    return report, await discard_if_untouched(settings, tenant_id, child_thread, report)


async def remove_worktree(
    settings: Settings, directory: Path, state: dict[str, Any], *, delete_branch: bool = False
) -> None:
    """A child's worktree out of its parent's repository, then its files; and its own children's.
    The branch stays unless `delete_branch` (a worktree that never held anything)."""
    parent_repo = checkout_root(settings) / str(state.get("parent_key", "")) / REPO_DIR
    path = directory / REPO_DIR
    await orphan_worktrees(settings, directory.name)
    if state.get("parent_key") and parent_repo.is_dir() and not parent_repo.is_symlink():
        code, _ = await _git(settings, parent_repo, "worktree", "remove", "--force", str(path))
        if code != 0:
            logger.warning("git worktree remove failed for a task worktree; pruning instead")
        await _git(settings, parent_repo, "worktree", "prune")
        if delete_branch and state.get("branch"):
            await _git(settings, parent_repo, "branch", "-D", str(state["branch"]))
    await asyncio.to_thread(shutil.rmtree, path, True)


async def orphan_worktrees(settings: Settings, parent_key: str) -> int:
    """Before a repository a worktree hangs off is removed: each child worktree of it -- found by
    the harness's own state, never by parsing git's output -- and each of theirs, is marked failed
    and its files removed, since its git data goes with the parent. Returns how many."""
    orphaned = 0
    for directory in _live_children(settings, parent_key):
        state = _read_state(directory) or {}
        orphaned += await orphan_worktrees(settings, directory.name)
        await asyncio.to_thread(shutil.rmtree, directory / REPO_DIR, True)
        _write_state(
            directory, {**state, "state": FAILED, "error": "the repository it was a worktree of was removed"}
        )
        orphaned += 1
    return orphaned


__all__ = [
    "BRANCH_PREFIX",
    "CHECKOUT_EXISTS",
    "FAILED_TO_CREATE",
    "MAX_WORKTREES_PER_THREAD",
    "NO_REPOSITORY",
    "TOO_MANY",
    "UNSAFE_REPOSITORY",
    "WorktreeRefused",
    "WorktreeReport",
    "describe",
    "discard_if_untouched",
    "inspect_worktree",
    "open_worktree",
    "orphan_worktrees",
    "release_if_untouched",
    "remove_worktree",
    "repository_status",
]
