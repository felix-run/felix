"""A child task in a thread with a repository works in a git worktree of it, through the real stack.

The route, the compile, the `task` tool and the child's own file tools, with a scripted model whose
one queue serves the parent and its children in call order. What is pinned is what a client is
promised: a background child -- always -- and a foreground child under `spec.delegation.worktree`
write into a working tree of their own, never the thread's checkout; the parent is told which
branch; and a thread without a repository delegates exactly as before.
"""

from __future__ import annotations

import contextlib
import os
import shutil
from pathlib import Path
from typing import Any

import pytest
from felix.manifests.loader import parse_manifest
from felix_ai.providers.scripted import ScriptedTurn
from felix_ai.types import ToolCall

from tests.support.git_fixture import git, ready_local_checkout

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

TENANT = "default"
THREAD = f"{TENANT}:e2e-wt"


def _agent(name: str, **spec: Any) -> Any:
    base: dict[str, Any] = {"pattern": "react", "auth": {"inbound": {"allow_anonymous": True}}}
    base.update(spec)
    return parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": name}, "spec": base}
    )


CODER = _agent("e2e-coder", system_prompt={"inline": "You code."}, tools=["write_file", "read_file"])


def _lead(**delegation: Any) -> Any:
    return _agent(
        "e2e-lead",
        delegation={"agents": [{"name": "e2e-coder", "description": "Writes code."}], **delegation},
    )


def _task(call_id: str, prompt: str, **extra: Any) -> ToolCall:
    return ToolCall(id=call_id, name="task", args={"agent": "e2e-coder", "prompt": prompt, **extra})


def _write(call_id: str, path: str, content: str) -> ScriptedTurn:
    return ScriptedTurn(
        tool_calls=[ToolCall(id=call_id, name="write_file", args={"path": path, "content": content})]
    )


def _env(tmp_path: Path) -> dict[str, str]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return {"FELIX_WORKSPACE_ROOT": str(workspace), "FELIX_REPO_CHECKOUT_ROOT": str(tmp_path / "checkouts")}


def _repository(settings: Any) -> Path:
    return ready_local_checkout(settings, TENANT, THREAD)


async def _chat(app: Any, text: str = "build it") -> None:
    resp = await app.client.post(
        "/chat",
        json={"manifest": "e2e-lead", "thread_id": "e2e-wt", "messages": [{"role": "user", "content": text}]},
    )
    assert resp.status_code == 200, resp.text


def _text(messages: list[Any]) -> str:
    return "\n".join(str(getattr(m, "content", "")) for m in messages)


def _branches(repo: Path) -> list[str]:
    """The task branches, by name: `--format` rather than parsing `*` and `+` markers."""
    out = git(repo, "branch", "--list", "felix/task/*", "--format=%(refname:short)")
    return [b for b in out.splitlines() if b]


def _worktree_of(repo: Path, branch: str) -> Path:
    listing = git(repo, "worktree", "list", "--porcelain").split("\n\n")
    [block] = [b for b in listing if f"branch refs/heads/{branch}" in b]
    return Path(block.splitlines()[0].removeprefix("worktree "))


async def test_a_foreground_child_under_worktree_writes_into_its_own_branch(
    boot: Any, tmp_path: Path
) -> None:
    script = [
        ScriptedTurn(tool_calls=[_task("c1", "Add a file.")]),
        _write("w1", "child.txt", "from the child\n"),
        ScriptedTurn(content="Added child.txt."),
        ScriptedTurn(content="Done."),
    ]
    async with boot(
        script, env=_env(tmp_path), manifests={"e2e-lead": _lead(worktree=True), "e2e-coder": CODER}
    ) as app:
        repo = _repository(app.settings)
        await _chat(app)
        told = _text(app.spy.prompts[-1])
    [branch] = _branches(repo)
    assert not (repo / "child.txt").exists(), "the thread's checkout is untouched"
    assert (_worktree_of(repo, branch) / "child.txt").exists(), "the child wrote into its worktree"
    assert (_worktree_of(repo, branch) / "child.txt").read_text() == "from the child\n"
    assert f"branch {branch}:" in told, "the parent is told the branch"
    assert ": 0 commit(s)" in told
    assert "uncommitted changes" in told, "the parent is told the child's edit is not committed"


async def test_two_children_in_one_turn_get_a_branch_each(boot: Any, tmp_path: Path) -> None:
    script = [
        ScriptedTurn(tool_calls=[_task("c1", "First."), _task("c2", "Second.")]),
        _write("w1", "same.txt", "first\n"),
        ScriptedTurn(content="one"),
        _write("w2", "same.txt", "second\n"),
        ScriptedTurn(content="two"),
        ScriptedTurn(content="Both done."),
    ]
    async with boot(
        script, env=_env(tmp_path), manifests={"e2e-lead": _lead(worktree=True), "e2e-coder": CODER}
    ) as app:
        repo = _repository(app.settings)
        await _chat(app)
    branches = _branches(repo)
    assert len(branches) == 2
    contents = sorted((_worktree_of(repo, b) / "same.txt").read_text() for b in branches)
    assert contents == ["first\n", "second\n"], "neither child overwrote the other"
    assert not (repo / "same.txt").exists()


async def test_without_worktree_a_foreground_child_works_in_the_threads_checkout(
    boot: Any, tmp_path: Path
) -> None:
    script = [
        ScriptedTurn(tool_calls=[_task("c1", "Add a file.")]),
        _write("w1", "child.txt", "shared\n"),
        ScriptedTurn(content="Added."),
        ScriptedTurn(content="Done."),
    ]
    async with boot(script, env=_env(tmp_path), manifests={"e2e-lead": _lead(), "e2e-coder": CODER}) as app:
        repo = _repository(app.settings)
        await _chat(app)
    assert (repo / "child.txt").read_text() == "shared\n"
    assert _branches(repo) == []


async def test_a_background_child_always_works_in_a_worktree(boot: Any, tmp_path: Path) -> None:
    from felix.durability import fibers as F

    script = [
        ScriptedTurn(tool_calls=[_task("c1", "Add a file.", background=True)]),
        ScriptedTurn(content="Started."),
    ]
    manifests = {"e2e-lead": _lead(background=True), "e2e-coder": CODER}
    async with boot(script, env=_env(tmp_path), manifests=manifests) as app:
        repo = _repository(app.settings)
        await _chat(app)
        started = _text(app.spy.prompts[1])
        [branch] = _branches(repo)
        assert f"own git worktree of this thread's repository, on branch {branch}" in started
        app.spy.push(_write("w1", "bg.txt", "from the background\n"), ScriptedTurn(content="Added bg.txt."))
        await F.resume_due_fibers(app.settings)
    assert (_worktree_of(repo, branch) / "bg.txt").exists(), "the child wrote into its worktree"
    assert (_worktree_of(repo, branch) / "bg.txt").read_text() == "from the background\n"
    assert not (repo / "bg.txt").exists()


async def test_a_thread_without_a_repository_delegates_as_before(boot: Any, tmp_path: Path) -> None:
    script = [
        ScriptedTurn(tool_calls=[_task("c1", "Add a file.")]),
        _write("w1", "child.txt", "plain\n"),
        ScriptedTurn(content="Added."),
        ScriptedTurn(content="Done."),
    ]
    env = _env(tmp_path)
    async with boot(script, env=env, manifests={"e2e-lead": _lead(worktree=True), "e2e-coder": CODER}) as app:
        await _chat(app)
        told = _text(app.spy.prompts[-1])
    assert "worktree" not in told
    assert list(Path(env["FELIX_WORKSPACE_ROOT"]).rglob("child.txt")), "written to the shared workspace"
    assert not os.path.exists(env["FELIX_REPO_CHECKOUT_ROOT"]) or not any(
        Path(env["FELIX_REPO_CHECKOUT_ROOT"]).iterdir()
    )


async def test_a_foreground_child_that_changed_nothing_leaves_no_worktree(boot: Any, tmp_path: Path) -> None:
    script = [
        ScriptedTurn(tool_calls=[_task("c1", "Look around.")]),
        ScriptedTurn(content="Nothing to change."),
        ScriptedTurn(content="Done."),
    ]
    async with boot(
        script, env=_env(tmp_path), manifests={"e2e-lead": _lead(worktree=True), "e2e-coder": CODER}
    ) as app:
        repo = _repository(app.settings)
        await _chat(app)
        told = _text(app.spy.prompts[-1])
    assert "changed nothing in its worktree, which was removed" in told
    assert _branches(repo) == []
    assert git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1


async def test_after_a_child_fails_the_parent_works_in_its_own_checkout_again(
    boot: Any, tmp_path: Path
) -> None:
    lead = _agent(
        "e2e-lead",
        tools=["write_file"],
        delegation={"agents": [{"name": "e2e-coder", "description": "Writes code."}], "worktree": True},
    )
    script = [
        ScriptedTurn(tool_calls=[_task("c1", "Add a file.")]),
        ScriptedTurn(error=RuntimeError("boom")),
        _write("w1", "parent.txt", "from the parent\n"),
        ScriptedTurn(content="Done myself."),
    ]
    async with boot(script, env=_env(tmp_path), manifests={"e2e-lead": lead, "e2e-coder": CODER}) as app:
        repo = _repository(app.settings)
        await _chat(app)
        told = _text(app.spy.prompts[2])
    assert "changed nothing in its worktree, which was removed" in told, "it had one"
    assert (repo / "parent.txt").read_text() == "from the parent\n"
    assert _branches(repo) == [], "the failed child's untouched worktree went too"


async def test_a_repository_that_cannot_give_a_worktree_starts_no_child(boot: Any, tmp_path: Path) -> None:
    """A filter driver in the repository: the task is refused, never run in the thread's checkout."""
    script = [
        ScriptedTurn(tool_calls=[_task("c1", "Add a file.")]),
        ScriptedTurn(content="Could not delegate."),
    ]
    async with boot(
        script, env=_env(tmp_path), manifests={"e2e-lead": _lead(worktree=True), "e2e-coder": CODER}
    ) as app:
        repo = _repository(app.settings)
        git(repo, "config", "filter.evil.smudge", "cat")
        await _chat(app)
        told = _text(app.spy.prompts[-1])
        calls = len(app.spy.prompts)
    assert "could not have a worktree" in told
    assert calls == 2, "the child never ran"
    assert _branches(repo) == []


async def test_children_run_in_parallel_each_write_into_a_worktree(boot: Any, tmp_path: Path) -> None:
    """Under `execution.tools: parallel` the children run at once; the scripted queue serves them in
        the order they ask, and a scripted turn never suspends, so one child may take both writes. What is
    pinned here is the stack: nothing lands in the thread's checkout. That concurrent children's
    bindings stay apart is `test_concurrent_children_each_see_their_own_workspace` in the unit suite."""
    lead = _agent(
        "e2e-lead",
        execution={"tools": "parallel"},
        delegation={"agents": [{"name": "e2e-coder", "description": "Writes code."}], "worktree": True},
    )
    script = [
        ScriptedTurn(tool_calls=[_task("c1", "First."), _task("c2", "Second.")]),
        _write("w1", "a.txt", "a\n"),
        _write("w2", "b.txt", "b\n"),
        ScriptedTurn(content="one"),
        ScriptedTurn(content="two"),
        ScriptedTurn(content="Both done."),
    ]
    async with boot(script, env=_env(tmp_path), manifests={"e2e-lead": lead, "e2e-coder": CODER}) as app:
        repo = _repository(app.settings)
        await _chat(app)
    assert not (repo / "a.txt").exists() and not (repo / "b.txt").exists()
    written = sorted(p.name for b in _branches(repo) for p in _worktree_of(repo, b).glob("*.txt"))
    assert written == ["a.txt", "b.txt"]


async def test_a_background_child_refused_a_slot_leaves_no_worktree(boot: Any, tmp_path: Path) -> None:
    script = [
        ScriptedTurn(
            tool_calls=[_task("c1", "First.", background=True), _task("c2", "Second.", background=True)]
        ),
        ScriptedTurn(content="One started."),
    ]
    manifests = {"e2e-lead": _lead(background=True, max_background=1), "e2e-coder": CODER}
    async with boot(script, env=_env(tmp_path), manifests=manifests) as app:
        repo = _repository(app.settings)
        await _chat(app)
        told = _text(app.spy.prompts[1])
    assert "background tasks are already running here" in told
    assert len(_branches(repo)) == 1, "the refused child's worktree and branch went"
    assert git(repo, "worktree", "list", "--porcelain").count("worktree ") == 2


async def test_a_background_child_that_changed_nothing_frees_its_worktree_when_it_ends(
    boot: Any, tmp_path: Path
) -> None:
    from felix.durability import fibers as F

    script = [
        ScriptedTurn(tool_calls=[_task("c1", "Look around.", background=True)]),
        ScriptedTurn(content="Started."),
    ]
    manifests = {"e2e-lead": _lead(background=True), "e2e-coder": CODER}
    async with boot(script, env=_env(tmp_path), manifests=manifests) as app:
        repo = _repository(app.settings)
        await _chat(app)
        assert len(_branches(repo)) == 1
        app.spy.push(ScriptedTurn(content="Nothing to change."))
        for _ in range(10):  # until no step is due: the run has ended
            if not await F.resume_due_fibers(app.settings):
                break
    assert _branches(repo) == []
    assert git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1


@pytest.mark.parametrize("wrote", [False, True])
async def test_a_cancelled_foreground_child_keeps_only_a_worktree_with_work(
    boot: Any, tmp_path: Path, wrote: bool
) -> None:
    """Cancellation is no `Exception`: without its own arm the worktree outlived the run."""
    import asyncio

    script = [ScriptedTurn(tool_calls=[_task("c1", "Add a file.")])]
    if wrote:
        script.append(_write("w1", "child.txt", "half done\n"))
    script.append(ScriptedTurn(error=asyncio.CancelledError()))
    async with boot(
        script, env=_env(tmp_path), manifests={"e2e-lead": _lead(worktree=True), "e2e-coder": CODER}
    ) as app:
        repo = _repository(app.settings)
        with contextlib.suppress(BaseException):
            await _chat(app)
        assert "You code." in _text(app.spy.prompts[1]), "the child ran"
        branches = _branches(repo)
    if wrote:
        [branch] = branches
        assert (_worktree_of(repo, branch) / "child.txt").exists()
    else:
        assert branches == []


async def test_a_background_child_that_failed_frees_an_untouched_worktree(boot: Any, tmp_path: Path) -> None:
    from felix.durability import fibers as F

    script = [
        ScriptedTurn(tool_calls=[_task("c1", "Look around.", background=True)]),
        ScriptedTurn(content="Started."),
    ]
    manifests = {"e2e-lead": _lead(background=True), "e2e-coder": CODER}
    async with boot(script, env=_env(tmp_path), manifests=manifests) as app:
        repo = _repository(app.settings)
        await _chat(app)
        assert len(_branches(repo)) == 1
        app.spy.push(ScriptedTurn(error=RuntimeError("boom")))
        for _ in range(10):
            if not await F.resume_due_fibers(app.settings):
                break
    assert _branches(repo) == []
