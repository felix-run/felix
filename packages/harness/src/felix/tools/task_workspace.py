"""Where a child task works: its own git worktree of the thread's repository, or where it always has.

`tools/delegation.py` asks this before a child runs and after it finishes; `repos/worktrees.py` does
the git. The rules, in one place:

- A thread with no repository: the child works where it would have, as before any of this.
- A thread whose repository is still cloning, failed or expired: no child starts. Neither does one
  whose repository could not give it a worktree (a filter driver, too many worktrees already) --
  never a child working somewhere other than the repository it was asked about.
- A local repository: a background child always gets a worktree, a foreground one under
  `spec.delegation.worktree` -- except in plan mode, where a foreground child is held to read-only
  tools and has nothing to keep apart. A child's own children branch from the child's worktree.
- A hosted repository: a worktree cannot span two sandboxes; a background child is told it has no
  copy of the repository, and a foreground child shares the thread's.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from felix.repos import worktrees
from felix.repos.checkouts import CLONING
from felix.tools.errors import tool_error_output
from felix.tools.workspace_scope import workspace_thread

if TYPE_CHECKING:
    from felix.context import RequestContext
    from felix.tools.types import ToolOutput

logger = logging.getLogger("felix.tools.task_workspace")

# What the model can do about each refusal: nothing until the repository is ready or the
# configuration changes, or wait for a slot.
_REFUSAL_CODE = {
    worktrees.UNSAFE_REPOSITORY: "permission_denied",
    worktrees.CHECKOUT_EXISTS: "permission_denied",
    worktrees.TOO_MANY: "rate_limited",
}


@dataclass(frozen=True, slots=True)
class ChildWorkspace:
    """Where one child task works. `thread` is set when it has a worktree (the child thread whose
    checkout it is); `hosted` when the repository is in a sandbox the child cannot share."""

    thread: str | None = None
    branch: str | None = None
    hosted: bool = False


async def prepare(req: RequestContext, child_thread: str, *, wanted: bool) -> ChildWorkspace | ToolOutput:
    """The child's workspace, or the tool error that keeps it from starting. `wanted` is whether
    this child should have a worktree when its thread has a local repository."""
    from felix.governance.permission_mode import in_plan_mode

    parent = workspace_thread(req)
    if not parent:
        return ChildWorkspace()
    try:
        status, state = worktrees.repository_status(req.settings, req.auth.tenant_id, parent)
    except ValueError:
        # A checkout root the operator misconfigured: the repository routes say so; a thread that
        # never opened a repository should not lose its delegation over it.
        logger.warning("task_workspace_unresolved: the checkout root is misconfigured", exc_info=True)
        return ChildWorkspace()
    if status == "none":
        return ChildWorkspace()
    if status == "hosted":
        return ChildWorkspace(hosted=True)
    if status == "unavailable":
        repo = (state or {}).get("repo", "the repository")
        why = (
            "is still cloning; no task can work in it yet"
            if (state or {}).get("state") == CLONING
            else "is gone (it failed or expired); open the repository again, or remove it, first"
        )
        return tool_error_output("transport_unavailable", f"[task] this thread's checkout of {repo} {why}")
    if not wanted or await in_plan_mode(req):
        return ChildWorkspace()
    try:
        made = await worktrees.open_worktree(req.settings, req.auth.tenant_id, parent, child_thread)
    except worktrees.WorktreeRefused as exc:
        logger.info("task_worktree_refused code=%s", exc.code)
        return tool_error_output(
            _REFUSAL_CODE.get(exc.code, "transport_unavailable"),
            f"[task] the task could not have a worktree: {exc}",
        )
    except OSError, ValueError:
        logger.warning("task_worktree_failed", exc_info=True)
        return tool_error_output("transport_unavailable", "[task] the task could not have a worktree")
    return ChildWorkspace(thread=child_thread, branch=str(made.get("branch") or ""))


async def finish_foreground(req: RequestContext, workspace: ChildWorkspace) -> str | None:
    """What the parent is told about a foreground child's worktree once the child is done; a
    worktree it left nothing in is removed with its branch."""
    if workspace.thread is None:
        return None
    try:
        report, removed = await worktrees.release_if_untouched(
            req.settings, req.auth.tenant_id, workspace.thread
        )
        if report is None:
            return None
        if removed:
            return "[task] It changed nothing in its worktree, which was removed."
        return f"[task] {worktrees.describe(report)}"
    except Exception:
        logger.warning("task_worktree_summary_failed", exc_info=True)
        return f"[task] It worked in its own git worktree on branch {workspace.branch}."


def background_note(workspace: ChildWorkspace) -> str:
    """Where a background child works, for the start message."""
    if workspace.thread:
        return (
            f" It works in its own git worktree of this thread's repository, on branch "
            f"{workspace.branch}; what it commits is in this repository."
        )
    if workspace.hosted:
        return (
            " It has no copy of this thread's repository: under the hosted workspace, a background "
            "task works in a sandbox of its own."
        )
    return ""


async def discard(req: RequestContext, workspace: ChildWorkspace) -> None:
    """A worktree made for a child that never started: removed with its branch."""
    if workspace.thread is None:
        return
    from felix.repos.checkouts import remove_checkout

    try:
        await remove_checkout(req.settings, req.auth.tenant_id, workspace.thread, delete_branch=True)
    except Exception:
        logger.warning("discarding an unused task worktree failed", exc_info=True)


__all__ = [
    "ChildWorkspace",
    "background_note",
    "discard",
    "finish_foreground",
    "prepare",
]
