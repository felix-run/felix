"""The PR quality gate, driven the way Claude Code drives it.

`scripts/validate-toolkit.py` checks that every hook parses under `bash -n` and is
executable. Neither catches a hook that runs fine and decides the wrong thing, which is
what this one did: the guard meant to skip other checkouts compared the repo root
against itself, because it resolved "which repo" from the *hook's* cwd rather than the
command's. The early exit could never fire, so a docs PR in a sibling repo was judged
against this project's Python — and would have passed the moment this project's HEAD
had a marker. A gate that reads as satisfied when nothing was reviewed is worse than no
gate, and no amount of syntax checking would have said so.

So the hook gets asserted on behaviour: real temp repos, real stdin, exit codes only.
Exit 2 is "blocked" in the PreToolUse protocol; exit 0 is "allowed".
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[2] / ".claude" / "hooks" / "pr-quality-gate.sh"
CREATE = "gh pr create --title t --body b"

pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "jq"], capture_output=True).returncode != 0,
    reason="the hook no-ops without jq, so there is nothing to assert",
)


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)
    return out.stdout.strip()


def _repo(root: Path, changed: str) -> Path:
    """A repo with `origin/main` behind HEAD by one commit touching `changed`."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "seed").write_text("seed\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "seed")
    # origin/main pinned here, so the diff below is exactly the one commit.
    _git(root, "update-ref", "refs/remotes/origin/main", _git(root, "rev-parse", "HEAD"))
    path = root / changed
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x = 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "change")
    return root


def _run(command: str, *, project: Path, cwd: Path) -> int:
    payload = json.dumps({"tool_input": {"command": command}, "cwd": str(cwd)})
    return subprocess.run(
        ["bash", str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env={"PATH": __import__("os").environ["PATH"], "CLAUDE_PROJECT_DIR": str(project)},
    ).returncode


def test_it_blocks_an_unreviewed_python_change(tmp_path: Path) -> None:
    project = _repo(tmp_path / "project", "packages/harness/src/felix/x.py")
    assert _run(CREATE, project=project, cwd=project) == 2


def test_a_marker_for_this_exact_sha_satisfies_it(tmp_path: Path) -> None:
    project = _repo(tmp_path / "project", "packages/harness/src/felix/x.py")
    markers = project / ".claude" / "logs" / "quality-review"
    markers.mkdir(parents=True)
    (markers / _git(project, "rev-parse", "HEAD")).touch()
    assert _run(CREATE, project=project, cwd=project) == 0


def test_a_marker_for_a_different_sha_does_not(tmp_path: Path) -> None:
    """The keying is the whole point — a review of code you are no longer shipping."""
    project = _repo(tmp_path / "project", "packages/harness/src/felix/x.py")
    markers = project / ".claude" / "logs" / "quality-review"
    markers.mkdir(parents=True)
    (markers / ("0" * 40)).touch()
    assert _run(CREATE, project=project, cwd=project) == 2


def test_a_docs_only_change_passes_straight_through(tmp_path: Path) -> None:
    project = _repo(tmp_path / "project", "docs/README.md")
    assert _run(CREATE, project=project, cwd=project) == 0


# --- the bug ------------------------------------------------------------------------


def test_a_pr_in_a_sibling_checkout_is_not_judged_by_this_project(tmp_path: Path) -> None:
    """The regression. `cd <sibling>` then open a PR, from a session rooted here.

    Before the fix this returned 2: the sibling's docs PR was blocked by unreviewed
    Python in a repo it has nothing to do with. The same comparison would have let the
    sibling through on this project's marker, which is the direction that matters.
    """
    project = _repo(tmp_path / "project", "packages/harness/src/felix/x.py")
    sibling = _repo(tmp_path / "sibling", "docs/page.mdx")
    assert _run(f"cd {sibling}; {CREATE}", project=project, cwd=project) == 0


@pytest.mark.parametrize(
    "form",
    ["cd {p}; {c}", "cd {p} && {c}", 'cd "{p}" ; {c}', "cd '{p}' ; {c}", "  cd   {p}  ; {c}"],
)
def test_the_cd_is_recognised_in_the_forms_a_command_actually_takes(tmp_path: Path, form: str) -> None:
    project = _repo(tmp_path / "project", "packages/harness/src/felix/x.py")
    sibling = _repo(tmp_path / "sibling", "docs/page.mdx")
    assert _run(form.format(p=sibling, c=CREATE), project=project, cwd=project) == 0


def test_a_cd_into_this_project_still_gates(tmp_path: Path) -> None:
    """The `cd` must not become an escape hatch — naming this repo re-enters the gate."""
    project = _repo(tmp_path / "project", "packages/harness/src/felix/x.py")
    assert _run(f"cd {project}; {CREATE}", project=project, cwd=project) == 2


def test_a_cd_to_somewhere_that_is_not_a_repo_does_not_gate(tmp_path: Path) -> None:
    project = _repo(tmp_path / "project", "packages/harness/src/felix/x.py")
    elsewhere = tmp_path / "plain"
    elsewhere.mkdir()
    assert _run(f"cd {elsewhere}; {CREATE}", project=project, cwd=project) == 0


def test_an_unrelated_command_is_never_touched(tmp_path: Path) -> None:
    project = _repo(tmp_path / "project", "packages/harness/src/felix/x.py")
    assert _run("git status", project=project, cwd=project) == 0
