"""The structural-test-proof hook, driven the way Claude Code drives it.

`scripts/validate-toolkit.py` checks that the hook parses and is executable. Neither says
anything about what it decides, and for an advisory hook the two failure directions are both
silent: one that never fires is indistinguishable from a clean tree, and one that fires on
every test file becomes ambient noise that gets ignored — which is the same outcome by a
longer route.

So both directions are asserted here. The hook exists because tree-scanning tests go vacuous
without announcing it; if this hook goes vacuous, nothing else notices.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / ".claude" / "hooks" / "structural-test-proof.sh"

pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "jq"], capture_output=True).returncode != 0,
    reason="the hook no-ops without jq, so there is nothing to assert",
)

SCANNING = """\
import ast
from pathlib import Path


def test_alpha() -> None:
    for path in Path(".").rglob("*.py"):
        ast.parse(path.read_text())
"""

BEHAVIORAL = """\
def test_alpha() -> None:
    assert 1 + 1 == 2
"""


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)
    return out.stdout.strip()


def _repo(root: Path, *, committed: str | None, working: str, name: str = "test_thing.py") -> Path:
    """A repo whose `tests/unit/<name>` is `working`, and was `committed` at HEAD.

    `committed=None` leaves the file untracked, which is how a brand-new test file looks.
    """
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    target = root / "tests" / "unit" / name
    target.parent.mkdir(parents=True, exist_ok=True)

    (root / "seed").write_text("seed\n")
    if committed is not None:
        target.write_text(committed)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "seed")

    target.write_text(working)
    return root


def _note(repo: Path, filename: str = "test_thing.py") -> str:
    """What the hook emitted for that file, or an empty string when it stayed quiet."""
    payload = json.dumps({"tool_input": {"file_path": str(repo / "tests" / "unit" / filename)}})
    done = subprocess.run(
        ["bash", str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        cwd=str(repo),
        env={"PATH": os.environ["PATH"], "CLAUDE_PROJECT_DIR": str(repo)},
    )
    assert done.returncode == 0, f"an advisory hook must never block: {done.stderr}"
    if not done.stdout.strip():
        return ""
    return json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"]


def test_a_new_case_in_a_scanning_test_is_flagged(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path / "r",
        committed=SCANNING,
        working=SCANNING + '\n\ndef test_beta() -> None:\n    assert list(Path(".").rglob("*.md"))\n',
    )
    note = _note(repo)
    assert "test_beta" in note, "the added case was not named"
    assert "test_alpha" not in note, "an unchanged case was reported as new"
    assert "prove-fails.sh" in note


def test_a_brand_new_scanning_file_is_flagged(tmp_path: Path) -> None:
    """Untracked, so every case in it is new — the shape in which most of these arrive."""
    repo = _repo(tmp_path / "r", committed=None, working=SCANNING)
    assert "test_alpha" in _note(repo)


def test_a_behavioral_test_is_left_alone(tmp_path: Path) -> None:
    """Firing on every added test would make this ambient, which is the same as off.

    Behavioral tests assert on a value the system produced, so a broken one usually says so.
    """
    repo = _repo(
        tmp_path / "r",
        committed=BEHAVIORAL,
        working=BEHAVIORAL + "\n\ndef test_beta() -> None:\n    assert 2 + 2 == 4\n",
    )
    assert _note(repo) == ""


def test_editing_a_scanning_test_without_adding_a_case_is_quiet(tmp_path: Path) -> None:
    """Renaming a variable or rewording an assertion message is not a new claim."""
    repo = _repo(
        tmp_path / "r",
        committed=SCANNING,
        working=SCANNING.replace("for path in", "for source_path in").replace(
            "path.read_text", "source_path.read_text"
        ),
    )
    assert _note(repo) == ""


def test_a_file_outside_the_project_repo_is_not_reported_on(tmp_path: Path) -> None:
    """`git ls-files` is what scopes this. A path git does not know is not this project's."""
    repo = _repo(tmp_path / "r", committed=None, working=SCANNING)
    outside = tmp_path / "elsewhere" / "test_thing.py"
    outside.parent.mkdir(parents=True)
    outside.write_text(SCANNING)

    payload = json.dumps({"tool_input": {"file_path": str(outside)}})
    done = subprocess.run(
        ["bash", str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        cwd=str(repo),
        env={"PATH": os.environ["PATH"], "CLAUDE_PROJECT_DIR": str(repo)},
    )
    assert done.returncode == 0
    assert done.stdout.strip() == ""


@pytest.mark.parametrize("filename", ["helpers.py", "conftest.py", "notes.md"])
def test_a_file_that_is_not_a_test_is_ignored(tmp_path: Path, filename: str) -> None:
    repo = _repo(tmp_path / "r", committed=None, working=SCANNING, name=filename)
    assert _note(repo, filename) == ""


def test_the_real_hook_fires_on_this_repos_own_invariants_file() -> None:
    """A guard whose trigger no longer matches the repo it guards is the failure it exists
    to catch, so this asserts against the real files rather than a fixture: adding a case to
    `tests/unit/test_invariants.py` must still be recognised as a scanning test."""
    text = (ROOT / "tests" / "unit" / "test_invariants.py").read_text(encoding="utf-8")
    assert "ast.walk" in text or ".rglob(" in text, (
        "test_invariants.py no longer matches the hook's scanning-test trigger"
    )
