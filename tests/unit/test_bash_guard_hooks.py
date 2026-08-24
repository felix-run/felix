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
    # The remedy the block message recommends. Blocking it made the advice unfollowable.
    ("git-guard", "git push --force-with-lease origin feat/x", ALLOWED),
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


def _run(hook: str, command: str) -> int:
    payload = json.dumps({"tool_input": {"command": command}})
    return subprocess.run(
        ["bash", str(HOOKS / f"{hook}.sh")],
        input=payload,
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], "CLAUDE_PROJECT_DIR": str(HOOKS.parents[1])},
    ).returncode


@pytest.mark.parametrize(("hook", "command", "want"), CASES, ids=lambda v: str(v)[:48])
def test_the_guard_matches_the_command_and_not_the_text(hook: str, command: str, want: int) -> None:
    got = _run(hook, command)
    verb = "blocked" if got == BLOCKED else "allowed"
    expected = "blocked" if want == BLOCKED else "allowed"
    assert got == want, f"{hook} {verb} `{command}`; expected it {expected}"


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
