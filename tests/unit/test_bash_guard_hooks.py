"""What the PreToolUse(Bash) guards block, as a table.

Each guard began by matching a substring of the whole command, and each one fired on
text that was never going to execute. In a single session that produced seven false
blocks: a `rm -f` in a later command read as a force-push because an earlier one said
`git stash push`; `git commit -m 'do not push --force here'` blocked on the wording of
the hook's own advice; `grep -rn pytest .claude/` blocked; and the test guard refusing
to let its own source file be read, because the filename contains the word it watches.

A guard that cries wolf gets worked around, which is worse than one that is merely
absent — the workaround is a habit of rephrasing commands to slip past it. So the false
negatives and the false *positives* are both asserted here, and the allow rows outnumber
the deny rows on purpose.

`git-guard` also blocked `--force-with-lease`, which is what its own message tells you
to use. That one is a contradiction rather than a false positive: no command satisfied
it.

2 is "blocked" in the PreToolUse protocol; 0 is "allowed".
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parents[2] / ".claude" / "hooks"

# The guard watches for this word, so spelling it out in a command below would once
# have blocked this file from being collected at all.
PYTEST = "py" + "test"

pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "jq"], capture_output=True).returncode != 0,
    reason="the guards no-op without jq, so there is nothing to assert",
)

BLOCKED, ALLOWED = 2, 0

# (hook, command, expected exit)
CASES: list[tuple[str, str, int]] = [
    # --- git-guard: the destructive things it exists for ---------------------------
    ("git-guard", "git push --force origin main", BLOCKED),
    ("git-guard", "git push -f origin main", BLOCKED),
    ("git-guard", "git reset --hard origin/main", BLOCKED),
    ("git-guard", "git clean -fdx", BLOCKED),
    ("git-guard", "git clean -f -d -x", BLOCKED),
    ("git-guard", "git commit --no-verify -m x", BLOCKED),
    # The verb survives a runner and the subcommand survives global options.
    ("git-guard", "env git push --force origin main", BLOCKED),
    ("git-guard", "git -C /tmp/x push --force origin main", BLOCKED),
    # --- git-guard: everything it must not touch -----------------------------------
    ("git-guard", "git push origin feat/x", ALLOWED),
    # `--force-with-lease` is branch-sensitive, so it lives in its own test below
    # rather than in this table, which cannot express "allowed here, refused there".
    # "push" from one command, "-f" from another, three segments apart.
    ("git-guard", "git stash push -q file && echo ok; rm -f /tmp/x", ALLOWED),
    ("git-guard", "git status; docker rm -f container", ALLOWED),
    # Flags inside a quoted argument are text, not flags.
    ("git-guard", "git commit -m 'do not push --force here'", ALLOWED),
    ("git-guard", "git commit -m 'stop using --no-verify'", ALLOWED),
    ("git-guard", "git log --grep 'reset --hard'", ALLOWED),
    ("git-guard", "git clean -n", ALLOWED),
    ("git-guard", "git reset HEAD~1", ALLOWED),
    # --- the test-env guard: bare runs ---------------------------------------------
    (f"{PYTEST}-env-guard", f"{PYTEST} tests/unit", BLOCKED),
    (f"{PYTEST}-env-guard", f"uv run {PYTEST} -q", BLOCKED),
    (f"{PYTEST}-env-guard", f"python -m {PYTEST} -q", BLOCKED),
    (f"{PYTEST}-env-guard", f"cd /tmp && {PYTEST}", BLOCKED),
    # --- the test-env guard: the supported entry points and mere mentions ----------
    (f"{PYTEST}-env-guard", "./scripts/test.sh tests/unit -q", ALLOWED),
    (f"{PYTEST}-env-guard", "make check", ALLOWED),
    (f"{PYTEST}-env-guard", f"cat .claude/hooks/{PYTEST}-env-guard.sh", ALLOWED),
    (f"{PYTEST}-env-guard", f"grep -rn {PYTEST} .claude/", ALLOWED),
    (f"{PYTEST}-env-guard", f"git commit -m 'run {PYTEST} via the script'", ALLOWED),
    (f"{PYTEST}-env-guard", f"FELIX_DATABASE_URL=memory://x {PYTEST} -q", ALLOWED),
    # A heredoc body is input, not commands.
    (f"{PYTEST}-env-guard", f"python3 - <<'PY'\n# mentions {PYTEST} here\nPY", ALLOWED),
]


def _repo_on(tmp_path: pathlib.Path, branch: str) -> pathlib.Path:
    """A throwaway repo checked out on `branch`, with the hooks reachable from it.

    `git-guard` asks the project repo which branch it is on, so any test of a
    branch-sensitive rule that points at the real checkout asserts a fact about the
    developer's working state. That is how this suite passed on a feature branch and
    failed on `main` in CI, which is the same defect it exists to catch elsewhere:
    a result that depends on something the test does not control.
    """
    root = tmp_path / f"repo-{branch.replace('/', '-')}"
    root.mkdir(parents=True)
    run = lambda *a: subprocess.run(["git", "-C", str(root), *a], capture_output=True, check=True)
    run("init", "-q", "-b", branch)
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    (root / "seed").write_text("seed\n")
    run("add", "-A")
    run("commit", "-qm", "seed")
    # The hooks resolve `lib/command.sh` relative to their own path, so point at the
    # real ones rather than copying them.
    (root / ".claude").mkdir()
    (root / ".claude" / "hooks").symlink_to(HOOKS)
    return root


def _run(hook: str, command: str, *, project: pathlib.Path | None = None) -> int:
    payload = json.dumps({"tool_input": {"command": command}})
    return subprocess.run(
        ["bash", str(HOOKS / f"{hook}.sh")],
        input=payload,
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ["PATH"],
            "CLAUDE_PROJECT_DIR": str(project or HOOKS.parents[1]),
        },
    ).returncode


@pytest.mark.parametrize(("hook", "command", "want"), CASES, ids=lambda v: str(v)[:48])
def test_the_guard_matches_the_command_and_not_the_text(
    tmp_path: pathlib.Path, hook: str, command: str, want: int
) -> None:
    # Always a feature branch, never whatever the developer happens to be on.
    got = _run(hook, command, project=_repo_on(tmp_path, "feat/x"))
    verb = "blocked" if got == BLOCKED else "allowed"
    expected = "blocked" if want == BLOCKED else "allowed"
    assert got == want, f"{hook} {verb} `{command}`; expected it {expected}"


@pytest.mark.parametrize(
    ("branch", "want"),
    [("feat/x", ALLOWED), ("main", BLOCKED)],
    ids=["feature-branch", "main"],
)
def test_force_with_lease_is_allowed_on_a_branch_and_refused_on_main(
    tmp_path: pathlib.Path, branch: str, want: int
) -> None:
    """The rule the flat table could not express, and the one that broke CI.

    `--force-with-lease` is what the block message recommends, so refusing it outright
    made the advice unfollowable — but "use it on a feature branch, never on main" is
    the whole of that advice, and only half of it was ever asserted.
    """
    got = _run("git-guard", "git push --force-with-lease origin HEAD", project=_repo_on(tmp_path, branch))
    assert got == want, f"on {branch}: got {got}, wanted {want}"


def test_every_bash_guard_is_covered(tmp_path: Path) -> None:
    """A guard added later gets the same scrutiny, or this says so.

    The settings file is the list of what actually runs; the table above is the list of
    what has been thought about. They drift silently otherwise, and the drift is
    invisible precisely because a guard nobody tested is a guard nobody notices until
    it blocks something it should not.
    """
    settings = json.loads((HOOKS.parents[0] / "settings.json").read_text())
    configured = {
        # The configured command is a quoted shell string, so the basename arrives
        # wrapped: `"$CLAUDE_PROJECT_DIR/.claude/hooks/git-guard.sh"`.
        handler["command"].strip('"').rsplit("/", 1)[-1].removesuffix(".sh")
        for group in settings.get("hooks", {}).get("PreToolUse", [])
        if "Bash" in group.get("matcher", "")
        for handler in group.get("hooks", [])
    }
    covered = {hook for hook, _, _ in CASES} | {"pr-quality-gate"}  # its own module
    assert configured <= covered, f"Bash guards with no cases: {sorted(configured - covered)}"
