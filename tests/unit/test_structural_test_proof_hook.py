"""The structural-test-proof hook, driven the way Claude Code drives it.

`scripts/validate-toolkit.py` checks that the hook parses and is executable. Neither says
anything about what it decides, and for an advisory hook the two failure directions are both
silent: one that never fires is indistinguishable from a clean tree, and one that fires on
every test file becomes ambient noise that gets ignored — the same outcome by a longer route.

So both directions are asserted here. The hook exists because tree-scanning tests go vacuous
without announcing it; if this hook goes vacuous, nothing else notices.

Nothing below transcribes the hook's trigger pattern. An earlier version did, and the
resulting test passed with every arm of the real pattern typo'd into uselessness — it was
comparing the repo against its own copy, never running the hook. The pattern is now read out
of the hook itself, and each arm is exercised by driving the real script.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from tests.git_fixture import git

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / ".claude" / "hooks" / "structural-test-proof.sh"

_HAS_JQ = subprocess.run(["which", "jq"], capture_output=True).returncode == 0
# A silently skipped file looks exactly like a passing one — the reason CI sets
# FELIX_REQUIRE_OPTIONAL_EXTRAS=1 for the extras gates. The same flag applies here: locally a
# missing jq is a fair skip, in CI it means these tests stopped running and nobody was told.
if not _HAS_JQ and os.environ.get("FELIX_REQUIRE_OPTIONAL_EXTRAS") == "1":
    raise RuntimeError("jq is required in CI: without it this whole file skips and reads as a pass")

pytestmark = pytest.mark.skipif(
    not _HAS_JQ, reason="the hook no-ops without jq, so there is nothing to assert"
)

# One corpus idiom per arm of the hook's trigger. A repo where `.glob(` stopped being
# recognised would go quiet on `test_entrypoint_wiring.py`, which uses it.
# One corpus idiom per arm of the hook's trigger, each written the way this repo writes it:
# a named receiver, `ROOT.rglob(...)`, not `Path(".").rglob(...)`. That distinction is not
# cosmetic — it is the bug this file failed to catch. The old prefix class required a
# non-alphanumeric character before `.rglob(`, which the `)` of `Path(".")` supplies and the
# `T` of `ROOT` does not, so both glob arms were dead against real code while these fixtures
# reported them healthy.
CORPUS_IDIOMS = {
    "rglob": 'from pathlib import Path\n\nROOT = Path(".")\n\n\ndef test_alpha() -> None:\n    assert list(ROOT.rglob("*.py"))\n',
    "glob": 'from pathlib import Path\n\nROOT = Path(".")\n\n\ndef test_alpha() -> None:\n    assert list(ROOT.glob("*.py"))\n',
    "os.walk": 'import os\n\n\ndef test_alpha() -> None:\n    assert list(os.walk("."))\n',
    "ast.parse": 'import ast\n\n\ndef test_alpha() -> None:\n    assert ast.parse("x = 1")\n',
    "ast.walk": 'import ast\n\n\ndef test_alpha() -> None:\n    assert list(ast.walk(ast.parse("x = 1")))\n',
}
SCANNING = CORPUS_IDIOMS["rglob"]
BEHAVIORAL = "def test_alpha() -> None:\n    assert 1 + 1 == 2\n"


def _hook_trigger() -> str:
    """The scanning-test pattern, read out of the hook rather than copied into this file."""
    for line in HOOK.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^grep -qE '(.*)' \"\$path\"", line.strip())
        if match:
            return match.group(1)
    pytest.fail("no `grep -qE '<pattern>' \"$path\"` line in the hook — has its trigger moved?")


def _git(repo: Path, *args: str) -> str:
    return git(repo, *args)


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


def _note(repo: Path, filename: str = "test_thing.py", env: dict[str, str] | None = None) -> str:
    """What the hook emitted for that file, or an empty string when it stayed quiet."""
    payload = json.dumps({"tool_input": {"file_path": str(repo / "tests" / "unit" / filename)}})
    done = subprocess.run(
        ["bash", str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        cwd=str(repo),
        env={"PATH": os.environ["PATH"], "CLAUDE_PROJECT_DIR": str(repo), **(env or {})},
    )
    assert done.returncode == 0, f"an advisory hook must never block: {done.stderr}"
    if not done.stdout.strip():
        return ""
    return json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"]


@pytest.mark.parametrize("idiom", sorted(CORPUS_IDIOMS))
def test_every_corpus_idiom_trips_the_trigger(tmp_path: Path, idiom: str) -> None:
    """Each arm of the hook's pattern, driven through the real script.

    Two arms were unexercised: `.glob(` and `os.walk(` could both be typo'd out of the
    pattern with every test still green — and `.glob(` is live in this repo.
    """
    repo = _repo(tmp_path / "r", committed=None, working=CORPUS_IDIOMS[idiom])
    assert "test_alpha" in _note(repo), f"the hook did not fire on a test using {idiom}"


def _scans_the_tree(path: Path) -> bool:
    """Does this file actually walk the tree? Decided by AST, independently of the hook.

    A second opinion has to be arrived at differently or it is not a second opinion. Naming
    two files and grepping them was the previous version, and both happened to match on the
    `ast.` arms — so the two glob arms could be, and were, dead with this green. Reading calls
    rather than text also keeps a docstring that merely mentions `rglob("SKILL.md")` from
    counting as a scan, which grep cannot do.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        attr, value = node.func.attr, node.func.value
        if attr in {"rglob", "glob"}:
            return True
        if attr in {"walk", "parse"} and isinstance(value, ast.Name) and value.id in {"ast", "os"}:
            return True
    return False


def test_the_trigger_matches_every_scanning_test_in_this_repo() -> None:
    """Derived from the tree, not from a list of files someone remembered to update.

    This is the direction that rots, and it rotted: the hook's two glob arms required a
    non-alphanumeric character before `.rglob(`, so `ROOT.rglob(...)` never matched and three
    real scanning tests were invisible. The previous version of this test named two files,
    both of which matched on a different arm, and passed throughout.
    """
    pattern = _hook_trigger()
    scanning = sorted(p for p in (ROOT / "tests" / "unit").glob("test_*.py") if _scans_the_tree(p))
    assert len(scanning) >= 8, f"only {len(scanning)} scanning tests found — has the suite moved?"

    missed = [
        str(p.relative_to(ROOT))
        for p in scanning
        if subprocess.run(["grep", "-qE", pattern, str(p)]).returncode != 0
    ]
    assert missed == [], (
        "the hook's trigger does not match these tests, which do scan the tree — it would "
        f"stay silent on exactly the files it exists for: {missed}"
    )


def test_a_new_case_in_a_scanning_test_is_flagged(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path / "r",
        committed=SCANNING,
        working=SCANNING + '\n\ndef test_beta() -> None:\n    assert list(Path(".").rglob("*.md"))\n',
    )
    note = _note(repo)
    assert "test_beta" in note, "the added case was not named"
    assert "test_alpha" not in note, "an unchanged case was reported as new"
    assert "Introduce a real violation" in note, "the note no longer carries the mutation procedure"


def test_a_brand_new_scanning_file_is_flagged(tmp_path: Path) -> None:
    """Untracked, so every case in it is new — the shape in which most of these arrive."""
    repo = _repo(tmp_path / "r", committed=None, working=SCANNING)
    assert "test_alpha" in _note(repo)


def test_a_behavioral_test_is_left_alone(tmp_path: Path) -> None:
    """Firing on every added test would make this ambient, which is the same as off."""
    repo = _repo(
        tmp_path / "r",
        committed=BEHAVIORAL,
        working=BEHAVIORAL + "\n\ndef test_beta() -> None:\n    assert 2 + 2 == 4\n",
    )
    assert _note(repo) == ""


def test_editing_a_scanning_test_without_adding_a_case_is_quiet(tmp_path: Path) -> None:
    """Renaming a variable or rewording an assertion message is not a new claim."""
    repo = _repo(tmp_path / "r", committed=SCANNING, working=SCANNING.replace("assert list", "assert any"))
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


@pytest.mark.parametrize("filename", ["helpers.py", "notes.md"])
def test_a_file_that_is_not_a_test_is_ignored(tmp_path: Path, filename: str) -> None:
    repo = _repo(tmp_path / "r", committed=None, working=SCANNING, name=filename)
    assert _note(repo, filename) == ""


@pytest.mark.parametrize("var", ["GIT_DIR", "GIT_WORK_TREE"])
def test_an_ambient_git_environment_does_not_silence_the_hook(tmp_path: Path, var: str) -> None:
    """The hook's `env -u GIT_DIR -u GIT_WORK_TREE` wrapper is load-bearing.

    `-C` does not override an exported GIT_DIR, so without the wrapper `git ls-files` answers
    about the other repo, finds nothing, and the hook emits *nothing at all*. Silence is this
    hook's worst outcome — it is indistinguishable from a clean tree. The sibling hook has
    this test; this one took the defense and left the test behind.
    """
    repo = _repo(tmp_path / "r", committed=None, working=SCANNING)
    other = _repo(tmp_path / "other", committed=None, working=BEHAVIORAL)
    value = str(other / ".git") if var == "GIT_DIR" else str(other)
    assert "test_alpha" in _note(repo, env={var: value}), f"{var} silenced the hook"
