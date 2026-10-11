"""A repository's own `.git` cannot make an allowlisted git command run a program.

`.git/config` and `.git/hooks` sit in the workspace, which the agent writes; the shell tool then
runs `git commit` from an allowlist. (No pager case: git pages only to a terminal, and the tool's
output is a pipe.) Each case plants one way git runs a program, shows a plain
git does run it (the control), and that a git started by `exec_argv` -- the one exec path, shared
by the in-process tool and the shell runner -- does not.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from felix.tools.shell import _child_env, exec_argv

from tests.support.git_fixture import git

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

IDENTITY = ["-c", "user.name=t", "-c", "user.email=t@example.com"]


def _repo(tmp_path: Path) -> tuple[Path, Path]:
    repo, marker = tmp_path / "repo", tmp_path / "ran"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, *IDENTITY, "commit", "-q", "--allow-empty", "-m", "base")
    return repo, marker


def _script(path: Path, marker: Path) -> Path:
    path.write_text(f"#!/bin/sh\ntouch {marker}\nexit 0\n")
    path.chmod(0o755)
    return path


def _plant_hook(repo: Path, marker: Path) -> None:
    _script(repo / ".git" / "hooks" / "pre-commit", marker)


def _plant_config(key: str, value_from: str):
    def plant(repo: Path, marker: Path) -> None:
        program = _script(repo.parent / "program.sh", marker)
        git(repo, "config", key, value_from.format(program=program))

    return plant


CASES = {
    "pre-commit hook": (_plant_hook, [*IDENTITY, "commit", "--allow-empty", "-m", "x"]),
    "core.hooksPath": (
        _plant_config("core.hooksPath", "{program}.d"),
        [*IDENTITY, "commit", "--allow-empty", "-m", "x"],
    ),
    "core.fsmonitor": (_plant_config("core.fsmonitor", "{program}"), ["status"]),
    "core.editor": (_plant_config("core.editor", "{program}"), [*IDENTITY, "commit", "--allow-empty"]),
    # `-S` forces signing whatever `commit.gpgSign` says, and is the model's to pass.
    "gpg.program, with -S": (
        _plant_config("gpg.program", "{program}"),
        [*IDENTITY, "commit", "-S", "--allow-empty", "-m", "x"],
    ),
}


def _plant(case: str, repo: Path, marker: Path) -> list[str]:
    plant, args = CASES[case]
    plant(repo, marker)
    if case == "core.hooksPath":
        hooks = Path(f"{repo.parent / 'program.sh'}.d")
        hooks.mkdir()
        _script(hooks / "pre-commit", marker)
    return ["git", *args]


@pytest.mark.parametrize("case", list(CASES))
def test_a_plain_git_runs_what_the_repository_names(tmp_path: Path, case: str) -> None:
    """The control: each planted setting does make git run the program."""
    repo, marker = _repo(tmp_path)
    argv = _plant(case, repo, marker)
    # `core.editor` outranks `EDITOR`/`VISUAL`, so an operator's own editor cannot mask it.
    git(repo, *argv[1:], check=False)
    assert marker.exists(), f"{case}: the control did not run the program, so the test proves nothing"


@pytest.mark.parametrize("case", list(CASES))
async def test_git_through_the_shell_tool_runs_none_of_it(tmp_path: Path, case: str) -> None:
    repo, marker = _repo(tmp_path)
    argv = _plant(case, repo, marker)
    result = await exec_argv(argv, cwd=repo, root=tmp_path, stdin=None, timeout_s=30)
    assert not marker.exists(), f"{case}: ran the repository's program ({result['stderr'][-300:]})"


def test_a_git_a_script_starts_is_disarmed_too() -> None:
    """Through the environment, not argv: `make test` running git gets the same overrides."""
    env = _child_env()
    keys = {
        env[f"GIT_CONFIG_KEY_{n}"]: env[f"GIT_CONFIG_VALUE_{n}"] for n in range(int(env["GIT_CONFIG_COUNT"]))
    }
    assert keys["core.hooksPath"] == "/dev/null" and keys["core.fsmonitor"] == "false"
    assert env["GIT_EDITOR"] == "true"
