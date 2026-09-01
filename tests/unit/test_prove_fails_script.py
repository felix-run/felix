"""`scripts/prove-fails.sh`, driven end to end.

The script shipped as the only new control on its branch with no test at all — a tool for
deciding whether a test can fail, which nothing checked could fail. Two of its three verdicts
are claims about *the test being examined* rather than about the code, so a script that
mislabels them is worse than no script: VACUOUS on a sound test sends someone to rewrite it,
and PROVEN on a comparison that never happened is how a scanning test got blessed.

Everything here builds a throwaway git repo and runs the real script against it. Nothing
touches this repository — see `tests/git_fixture.py` for why that needs saying.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.git_fixture import git

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "prove-fails.sh"

_HAS_GIT = shutil.which("git") is not None
if not _HAS_GIT and os.environ.get("FELIX_REQUIRE_OPTIONAL_EXTRAS") == "1":
    raise RuntimeError("git is required in CI: without it this whole file skips and reads as a pass")

pytestmark = pytest.mark.skipif(not _HAS_GIT, reason="the script is a git worktree wrapper")

# A miniature of this workspace: one package on a src root, a test that exercises it through
# an import, and the runner the script shells out to.
# `sys.executable`, not `python`: the fixture runs with a minimal environment and the bare
# name is not on PATH in a uv-managed venv.
TEST_SH = """#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
exec {interpreter} -m pytest "${{@:--q}}"
"""


def _workspace(root: Path, *, before: str, after: str, test_body: str) -> Path:
    """A repo whose `pkg/thing.py` was `before` at HEAD and is `after` in the working tree."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "scripts").mkdir()
    (root / "scripts" / "test.sh").write_text(TEST_SH.format(interpreter=sys.executable))
    (root / "scripts" / "test.sh").chmod(0o755)
    # `probepkg`, not `felix`: the real `felix` is a regular package on the outer venv's path,
    # and a regular package found later beats a namespace portion found earlier — so a fixture
    # package sharing the name is shadowed no matter what PYTHONPATH says.
    pkg = root / "packages" / "harness" / "src" / "probepkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (root / "tests").mkdir()

    source = pkg / "thing.py"
    source.write_text(before)
    (root / "tests" / "test_thing.py").write_text(test_body)

    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "before")

    source.write_text(after)
    return root


def _run(repo: Path, *args: str) -> tuple[int, str]:
    # The *installed copy*, never `SCRIPT` itself: the script does `cd "$(dirname "$0")/.."`
    # to find its repo, so running the original from a fixture cwd points it at this
    # repository and makes it add a worktree here.
    done = subprocess.run(
        ["bash", str(repo / "scripts" / "prove-fails.sh"), *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", str(repo))},
    )
    return done.returncode, done.stdout + done.stderr


# The script resolves its own repo root from its location, so it has to be copied in.
def _install(repo: Path) -> None:
    (repo / "scripts" / "prove-fails.sh").write_text(SCRIPT.read_text(encoding="utf-8"))
    (repo / "scripts" / "prove-fails.sh").chmod(0o755)


IMPORT_TEST = """\
from probepkg.thing import answer


def test_answer():
    assert answer() == 42
"""


def test_a_test_that_pins_the_change_is_proven(tmp_path: Path) -> None:
    """The fix is in the working tree; the old source is what the test must fail against."""
    repo = _workspace(
        tmp_path / "r",
        before="def answer():\n    return 0\n",
        after="def answer():\n    return 42\n",
        test_body=IMPORT_TEST,
    )
    _install(repo)
    code, out = _run(repo, "tests/test_thing.py")
    assert "PROVEN" in out, out
    assert code == 0


def test_a_test_that_passes_on_the_old_source_is_vacuous(tmp_path: Path) -> None:
    """The assertion holds before and after, so it pins nothing about the change."""
    repo = _workspace(
        tmp_path / "r",
        before="def answer():\n    return 42\n",
        after="def answer():\n    return 42  # reformatted\n",
        test_body=IMPORT_TEST,
    )
    _install(repo)
    code, out = _run(repo, "tests/test_thing.py")
    assert "VACUOUS" in out, out
    assert code == 1, "a vacuous verdict must not exit 0 — it is a finding, not a pass"


def test_an_error_is_reported_as_broken_not_as_a_failure(tmp_path: Path) -> None:
    """The distinction the whole script exists for.

    The test references a symbol that does not exist at the base, so it errors rather than
    fails. That says nothing about whether it would catch the bug, and calling it PROVEN would
    be the exact confusion this repo has written down twice.
    """
    repo = _workspace(
        tmp_path / "r",
        before="def answer():\n    return 0\n",
        after="def answer():\n    return 42\n\n\ndef added_later():\n    return 1\n",
        # The failure is inside a fixture, so pytest reports "1 error" and exits 1 — the same
        # exit code as a real failure. Discriminating those two is the branch under test; an
        # import error at collection exits 2 and takes the catch-all instead, which is why an
        # earlier version of this test passed with that discrimination removed entirely.
        test_body=(
            "import pytest\n"
            "from probepkg.thing import answer\n\n\n"
            "@pytest.fixture\n"
            "def later():\n"
            "    from probepkg.thing import added_later\n\n"
            "    return added_later()\n\n\n"
            "def test_answer(later):\n"
            "    assert answer() == 42 and later == 1\n"
        ),
    )
    _install(repo)
    code, out = _run(repo, "tests/test_thing.py")
    assert "BROKEN" in out, out
    assert "PROVEN" not in out
    assert code == 1


def test_a_test_that_reads_the_tree_is_warned_about(tmp_path: Path) -> None:
    """The trap: the script cannot revert what a file-reading test sees, and must say so.

    Without this warning the same scenario returns a confident verdict about a comparison that
    never happened — which is how a scanning test got blessed on the branch that added this.
    """
    repo = _workspace(
        tmp_path / "r",
        before="def answer():\n    return 0\n",
        after="def answer():\n    return 42\n",
        test_body=(
            "from pathlib import Path\n\n"
            "ROOT = Path(__file__).resolve().parents[1]\n\n\n"
            "def test_source_text():\n"
            '    assert "42" in (ROOT / "packages/harness/src/probepkg/thing.py").read_text()\n'
        ),
    )
    _install(repo)
    _, out = _run(repo, "tests/test_thing.py")
    assert "reads files from the tree" in out, out


def test_an_import_driven_test_is_not_warned_about(tmp_path: Path) -> None:
    """A guard that fires on the common case is the same as no guard.

    `read_text(`/`open(` were briefly in the trigger and matched 14 test files here, none of
    which read the tree — thirteen on `store.open(thread_id)` alone.
    """
    repo = _workspace(
        tmp_path / "r",
        before="def answer():\n    return 0\n",
        after="def answer():\n    return 42\n",
        # `store.open(thread_id)` is the exact call that produced thirteen of the fourteen
        # false positives — the session layer's central method. `read_text(` on a tmp_path
        # file the test wrote produced another.
        test_body=IMPORT_TEST.replace(
            "def test_answer():",
            "def test_answer(tmp_path):\n"
            "    store = tmp_path\n"
            "    (store / 'f').write_text('x')\n"
            "    assert (store / 'f').read_text() == 'x'\n"
            "    _ = getattr(store, 'open', None)",
        ),
    )
    _install(repo)
    _, out = _run(repo, "tests/test_thing.py")
    assert "reads files from the tree" not in out, out


def test_it_leaves_no_worktree_behind(tmp_path: Path) -> None:
    """The script adds a detached worktree per run and removes it in a trap."""
    repo = _workspace(
        tmp_path / "r",
        before="def answer():\n    return 0\n",
        after="def answer():\n    return 42\n",
        test_body=IMPORT_TEST,
    )
    _install(repo)
    _run(repo, "tests/test_thing.py")
    listed = git(repo, "worktree", "list")
    assert listed.count("\n") == 0, f"a worktree survived the run: {listed}"


def test_an_unknown_distribution_is_refused(tmp_path: Path) -> None:
    repo = _workspace(
        tmp_path / "r",
        before="def answer():\n    return 0\n",
        after="def answer():\n    return 42\n",
        test_body=IMPORT_TEST,
    )
    _install(repo)
    code, out = _run(repo, "--only", "nope", "tests/test_thing.py")
    assert code == 64
    assert "unknown distribution" in out


def test_an_assertion_mentioning_errors_is_not_mistaken_for_one(tmp_path: Path) -> None:
    """A genuine failure whose message contains the word "errors".

    The verdict is decided by grepping pytest's output for an error count. Grepping the last
    twenty lines rather than the summary line means an assertion message like "found 2 errors
    in the manifest" — ordinary prose in a repo full of validators — gets read as a collection
    error, and a test that is real evidence is reported as BROKEN.
    """
    repo = _workspace(
        tmp_path / "r",
        before="def answer():\n    return 0\n",
        after="def answer():\n    return 42\n",
        test_body=(
            "from probepkg.thing import answer\n\n\n"
            "def test_answer():\n"
            '    assert answer() == 42, "found 2 errors in the manifest"\n'
        ),
    )
    _install(repo)
    code, out = _run(repo, "tests/test_thing.py")
    assert "PROVEN" in out, out
    assert "BROKEN" not in out
    assert code == 0
