"""Run git against a fixture repo, and only against a fixture repo.

`git -C <dir>` does **not** win over an exported `GIT_DIR` or `GIT_WORK_TREE` — the environment
does. So a fixture that builds a throwaway repo with a bare `git -C tmpdir init && add -A &&
commit` writes into whatever `GIT_DIR` points at, using `tmpdir` as the work tree. Under an
ambient `GIT_DIR=/path/to/felix/.git` that is not a misread: it moves refs in the real
repository. It happened here — a review run with `GIT_DIR` exported committed two fixture
commits into this repo and left `refs/heads/<branch>` and `refs/remotes/origin/main` pointing
at them. No file was touched, so `git status` was the only symptom, and it looked like the
whole tree had been deleted.

The hooks under test defend themselves against exactly this (`env -u GIT_DIR -u GIT_WORK_TREE
git "$@"` in `pr-quality-gate.sh` and `structural-test-proof.sh`). The tests that drive them
have to do the same, or the test harness is the least hermetic thing in the room.

`commit.gpgsign=false` is here for a different reason: a contributor with global commit signing
would otherwise get a `CalledProcessError` out of fixture setup, with a message about gpg and
nothing about the hook under test.

`tests/unit/test_invariants.py:test_every_git_call_in_tests_is_environment_scrubbed` requires
every git subprocess call under `tests/` to come through here.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def git(repo: Path | str, *args: str, check: bool = True) -> str:
    """Run `git <args>` against `repo`, immune to an ambient git environment."""
    # Positively, not by unsetting a list. Naming variables to remove is a denylist, and this
    # repo's own rule is that trust is an allowlist — `GIT_OBJECT_DIRECTORY`,
    # `GIT_ALTERNATE_OBJECT_DIRECTORIES` and friends would still be inherited. Setting
    # `GIT_DIR` and `GIT_WORK_TREE` to this repo makes the answer independent of what else is
    # in the environment, and `env -i`-style scrubbing is handled once in tests/conftest.py.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_DIR"] = str(Path(repo) / ".git")
    env["GIT_WORK_TREE"] = str(repo)
    done = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
    )
    if check and done.returncode != 0:
        # `check=True` alone raises CalledProcessError, whose str() is "returned non-zero exit
        # status 128" with stderr only on the exception object — so centralising every git call
        # behind this function would have hidden the reason for all of them, which is the
        # failure mode the gpgsign line above exists to avoid.
        raise RuntimeError(f"git {' '.join(args)} failed in {repo}:\n{done.stderr.strip()}")
    return done.stdout.strip()
