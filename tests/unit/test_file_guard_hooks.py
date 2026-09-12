"""The Write/Edit hooks, judged in the tree a session actually works in.

`tests/unit/test_bash_guard_hooks.py` covers the `Bash` guards. These are the ones that
take a `file_path` and decide something from its repo-relative name — and every one of
them derived that name by stripping `CLAUDE_PROJECT_DIR` off the front, which is wrong
whenever the session is in a git worktree. The file then lives at
`<project>/.claude/worktrees/<name>/<rel>`, the strip leaves the worktree prefix attached,
and an anchored pattern like `.env` stops matching.

For `protect-files` that meant failing **open**: `.env`, `uv.lock` and applied migrations
were all freely editable inside a worktree, silently. A blocking guard that stops blocking
is worse than one that was never written, because its silence reads as approval — and
worktrees are how this repo is routinely worked in.

Both directions are asserted throughout: a guard that blocks everything would pass the
first half of every test here and fail the second.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess

import pytest

from tests.git_fixture import git

HOOKS = pathlib.Path(__file__).resolve().parents[2] / ".claude" / "hooks"
BLOCKED, ALLOWED = 2, 0


def _module(lines: int) -> str:
    """A syntactically valid module of a known length, for the size budget."""
    return "def f():\n" + "".join(f"    x{i} = {i}\n" for i in range(lines))


_HAS_JQ = subprocess.run(["which", "jq"], capture_output=True).returncode == 0
if not _HAS_JQ and os.environ.get("FELIX_REQUIRE_OPTIONAL_EXTRAS") == "1":
    raise RuntimeError("jq is required in CI: without it this whole file skips and reads as a pass")

pytestmark = pytest.mark.skipif(not _HAS_JQ, reason="the guards no-op without jq")


@pytest.fixture(scope="module")
def repo(tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    """A throwaway project with a worktree under `.claude/worktrees/`, as here.

    Real git rather than a fake tree: `hook_repo_root` asks git where a path lives, so a
    directory that merely looks like a worktree would prove nothing.
    """
    root = tmp_path_factory.mktemp("project")
    run = lambda *a: git(root, *a)  # env-scrubbed; see tests/git_fixture.py
    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    (root / "migrations" / "versions").mkdir(parents=True)
    (root / "migrations" / "versions" / "0001_baseline.py").write_text("# applied\n")
    (root / "README.md").write_text("# project\n")
    # Committed before the worktree exists, so the worktree's HEAD carries it. Committing
    # from *inside* a linked worktree is the thing to avoid here: `tests/git_fixture.py`
    # pins `GIT_DIR` to `<repo>/.git`, which in a linked worktree is a file rather than a
    # directory — and an invariant (rightly) refuses a raw `subprocess.run(["git", …])`
    # that would dodge that scrubbing.
    (root / "big.py").write_text(_module(700))
    run("add", "-A")
    run("commit", "-qm", "seed")
    # The migration arm asks whether the revision matches `origin/main` — "published" is
    # defined against the remote, not against local history — so the fixture needs that
    # ref or the guard silently falls through and the test proves nothing.
    run("update-ref", "refs/remotes/origin/main", "HEAD")
    (root / ".claude").mkdir(exist_ok=True)
    (root / ".claude" / "hooks").symlink_to(HOOKS)
    git(root, "worktree", "add", "-q", "-b", "feat/x", str(root / ".claude" / "worktrees" / "wt"))
    return root


def _protect(path: pathlib.Path, project: pathlib.Path) -> int:
    return subprocess.run(
        ["bash", str(HOOKS / "protect-files.sh")],
        input=json.dumps({"tool_input": {"file_path": str(path)}}),
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], "CLAUDE_PROJECT_DIR": str(project)},
    ).returncode


# (relative path, expected verdict, what it is)
PROTECTED = [
    pytest.param(".env", BLOCKED, id="dotenv"),
    pytest.param("uv.lock", BLOCKED, id="lockfile"),
    pytest.param("secrets/key.pem", BLOCKED, id="secrets-dir"),
    pytest.param(".venv/lib/x.py", BLOCKED, id="virtualenv"),
    pytest.param("README.md", ALLOWED, id="ordinary-doc"),
    pytest.param("packages/harness/src/felix/config.py", ALLOWED, id="ordinary-source"),
]


@pytest.mark.parametrize(("rel", "want"), PROTECTED)
def test_protect_files_judges_a_worktree_path_by_its_repo_relative_name(
    repo: pathlib.Path, rel: str, want: int
) -> None:
    """The failure that prompted this file, and the cases that must stay editable.

    Before the fix every BLOCKED row here returned 0 — the guard was absent inside a
    worktree and said nothing about it.
    """
    got = _protect(repo / ".claude" / "worktrees" / "wt" / rel, repo)
    assert got == want, f"worktree/{rel}: got {got}, wanted {want}"


@pytest.mark.parametrize(("rel", "want"), PROTECTED)
def test_protect_files_still_judges_the_main_checkout(repo: pathlib.Path, rel: str, want: int) -> None:
    """The half that already worked, so the fix cannot be "block everywhere"."""
    got = _protect(repo / rel, repo)
    assert got == want, f"{rel}: got {got}, wanted {want}"


def test_an_applied_migration_is_protected_in_a_worktree_too(repo: pathlib.Path) -> None:
    """This arm asks *git* whether the revision is published, so it needs the right repo.

    Pointed at the project root with a worktree-prefixed path, `ls-files --error-unmatch`
    simply failed and the guard fell through — an applied revision was editable.
    """
    wt = repo / ".claude" / "worktrees" / "wt"
    assert _protect(wt / "migrations" / "versions" / "0001_baseline.py", repo) == BLOCKED


def test_a_new_migration_is_not_protected(repo: pathlib.Path) -> None:
    """The point of the guard is "edit history, no; add a revision, yes"."""
    wt = repo / ".claude" / "worktrees" / "wt"
    new = wt / "migrations" / "versions" / "0002_new.py"
    new.write_text("# unreleased\n")
    assert _protect(new, repo) == ALLOWED


def test_a_path_outside_any_repository_is_left_alone(repo: pathlib.Path, tmp_path: pathlib.Path) -> None:
    """`hook_repo_rel` falls back to the path unchanged rather than inventing a name."""
    stray = tmp_path / "elsewhere" / "notes.md"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_text("x\n")
    assert _protect(stray, repo) == ALLOWED


def test_the_ratchet_compares_a_worktree_file_against_its_own_history(repo: pathlib.Path) -> None:
    """A ratchet that cannot find the previous version reports every file as new.

    `git show HEAD:<rel>` ran in the project root with a worktree-prefixed path, found
    nothing, and `previous` became None — which both bypasses the "did this edit make it
    worse" guard and prints "new file". Observed on `felix_cli/main.py`: "module is 696
    lines (new file)" for a module months old, after a twelve-line edit. A ratchet that
    reports absolute size is a nag, and a nag gets muted.

    Both directions, because silence alone would also be produced by a hook that crashed.
    """
    target = repo / ".claude" / "worktrees" / "wt" / "big.py"

    def ratchet() -> str:
        return subprocess.run(
            ["bash", str(HOOKS / "quality-ratchet.sh")],
            input=json.dumps({"tool_input": {"file_path": str(target)}}),
            capture_output=True,
            text=True,
            env={"PATH": os.environ["PATH"], "CLAUDE_PROJECT_DIR": str(repo)},
        ).stdout

    # Over budget and unchanged since HEAD: the ratchet's whole point is that this is silent.
    assert ratchet() == "", "the ratchet nagged about size this edit did not cause"

    # Now genuinely made worse. It must speak, and it must say what the file *was* —
    # "new file" is the tell that it lost the history and is reporting absolute size.
    target.write_text(_module(900))
    spoke = ratchet()
    assert "big.py" in spoke, f"the ratchet stayed silent on a real regression: {spoke!r}"
    assert "new file" not in spoke, f"the ratchet lost the file's history: {spoke!r}"
    assert "was 701" in spoke, f"the ratchet did not compare against HEAD: {spoke!r}"


def test_a_migration_in_a_sibling_repository_is_judged_by_that_repository(
    repo: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """Editing another checkout from a session rooted here is an ordinary shape.

    The published-migration arm asks git whether the revision matches `origin/main`. Asked
    of the *project* root about a file that lives elsewhere, the answer describes the wrong
    repository — and in the direction that hurts: this project's `0001_baseline.py` is
    published, so writing a brand-new revision in the sibling was refused as "already
    published history".

    Distinguishes the `repo` half of the fix, which a worktree cannot: a worktree shares
    its main checkout's history, so both answers agree there.
    """
    sibling = tmp_path / "sibling"
    (sibling / "migrations" / "versions").mkdir(parents=True)
    run = lambda *a: git(sibling, *a)
    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    (sibling / "README.md").write_text("# sibling\n")
    run("add", "-A")
    run("commit", "-qm", "seed")
    run("update-ref", "refs/remotes/origin/main", "HEAD")

    # Same relative path this project has published, brand new over there.
    new = sibling / "migrations" / "versions" / "0001_baseline.py"
    new.write_text("# a new revision in another repo\n")

    assert _protect(new, repo) == ALLOWED, (
        "the guard judged a sibling repo's new migration by this project's published history"
    )
