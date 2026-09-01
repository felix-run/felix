"""The PR quality gate, driven the way Claude Code drives it.

`scripts/validate-toolkit.py` checks that every hook parses under `bash -n` and is
executable. Neither catches a hook that runs fine and decides the wrong thing, which is
what this one did: the guard meant to skip other checkouts compared the repo root
against itself, because it resolved "which repo" from the *hook's* cwd rather than the
command's. The early exit could never fire, so a docs PR in a sibling repo was judged
against this project's Python — and would have passed the moment this project's HEAD
had a marker. A gate that reads as satisfied when nothing was reviewed is worse than no
gate, and no amount of syntax checking would have said so.

So the hook gets asserted on behaviour: real temp repos, real stdin, and the decision
the hook returns.

The gate is advisory — it emits `additionalContext` and exits 0 rather than blocking.
The hard stop cost more than it bought: every amended commit re-armed it mid-flow. That
makes "warned" and "silent" two different outcomes at the same exit code, so the tests
assert the decision rather than the code, and a gate that silently stopped noticing
would fail here exactly as a blocking one would.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[2] / ".claude" / "hooks" / "pr-quality-gate.sh"
CREATE = "gh pr create --title t --body b"

_HAS_JQ = subprocess.run(["which", "jq"], capture_output=True).returncode == 0
# A silently skipped file looks exactly like a passing one — the reason CI sets
# FELIX_REQUIRE_OPTIONAL_EXTRAS=1 for the extras gates. The same flag applies here: locally a
# missing jq is a fair skip, in CI it means these tests stopped running and nobody was told.
if not _HAS_JQ and os.environ.get("FELIX_REQUIRE_OPTIONAL_EXTRAS") == "1":
    raise RuntimeError("jq is required in CI: without it this whole file skips and reads as a pass")

pytestmark = pytest.mark.skipif(
    not _HAS_JQ, reason="the hook no-ops without jq, so there is nothing to assert"
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


# What the hook decided, rather than how it exited.
FLAGGED = "flagged"  # the review note was emitted
QUIET = "quiet"  # the hook had nothing to say


def _run(command: str, *, project: Path, cwd: Path, env: dict[str, str] | None = None) -> str:
    payload = json.dumps({"tool_input": {"command": command}, "cwd": str(cwd)})
    done = subprocess.run(
        ["bash", str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env={
            "PATH": os.environ["PATH"],
            "CLAUDE_PROJECT_DIR": str(project),
            **(env or {}),
        },
    )
    assert done.returncode == 0, f"an advisory hook must never block: {done.stderr}"
    return FLAGGED if "quality-reviewer" in done.stdout else QUIET


def test_it_blocks_an_unreviewed_python_change(tmp_path: Path) -> None:
    project = _repo(tmp_path / "project", "packages/harness/src/felix/x.py")
    assert _run(CREATE, project=project, cwd=project) == FLAGGED


def test_a_marker_for_this_exact_sha_satisfies_it(tmp_path: Path) -> None:
    project = _repo(tmp_path / "project", "packages/harness/src/felix/x.py")
    markers = project / ".claude" / "logs" / "quality-review"
    markers.mkdir(parents=True)
    (markers / _git(project, "rev-parse", "HEAD")).touch()
    assert _run(CREATE, project=project, cwd=project) == QUIET


def test_a_marker_for_a_different_sha_does_not(tmp_path: Path) -> None:
    """The keying is the whole point — a review of code you are no longer shipping."""
    project = _repo(tmp_path / "project", "packages/harness/src/felix/x.py")
    markers = project / ".claude" / "logs" / "quality-review"
    markers.mkdir(parents=True)
    (markers / ("0" * 40)).touch()
    assert _run(CREATE, project=project, cwd=project) == FLAGGED


def test_a_docs_only_change_passes_straight_through(tmp_path: Path) -> None:
    project = _repo(tmp_path / "project", "docs/README.md")
    assert _run(CREATE, project=project, cwd=project) == QUIET


# --- the bug ------------------------------------------------------------------------


def test_a_pr_in_a_sibling_checkout_is_not_judged_by_this_project(tmp_path: Path) -> None:
    """The regression. `cd <sibling>` then open a PR, from a session rooted here.

    Before the fix this returned 2: the sibling's docs PR was blocked by unreviewed
    Python in a repo it has nothing to do with. The same comparison would have let the
    sibling through on this project's marker, which is the direction that matters.
    """
    project = _repo(tmp_path / "project", "packages/harness/src/felix/x.py")
    sibling = _repo(tmp_path / "sibling", "docs/page.mdx")
    assert _run(f"cd {sibling}; {CREATE}", project=project, cwd=project) == QUIET


@pytest.mark.parametrize(
    "form",
    ["cd {p}; {c}", "cd {p} && {c}", 'cd "{p}" ; {c}', "cd '{p}' ; {c}", "  cd   {p}  ; {c}"],
)
def test_the_cd_is_recognised_in_the_forms_a_command_actually_takes(tmp_path: Path, form: str) -> None:
    project = _repo(tmp_path / "project", "packages/harness/src/felix/x.py")
    sibling = _repo(tmp_path / "sibling", "docs/page.mdx")
    assert _run(form.format(p=sibling, c=CREATE), project=project, cwd=project) == QUIET


def test_a_cd_into_this_project_still_gates(tmp_path: Path) -> None:
    """The `cd` must not become an escape hatch — naming this repo re-enters the gate."""
    project = _repo(tmp_path / "project", "packages/harness/src/felix/x.py")
    assert _run(f"cd {project}; {CREATE}", project=project, cwd=project) == FLAGGED


def test_a_cd_to_somewhere_that_is_not_a_repo_does_not_gate(tmp_path: Path) -> None:
    project = _repo(tmp_path / "project", "packages/harness/src/felix/x.py")
    elsewhere = tmp_path / "plain"
    elsewhere.mkdir()
    assert _run(f"cd {elsewhere}; {CREATE}", project=project, cwd=project) == QUIET


def test_an_unrelated_command_is_never_touched(tmp_path: Path) -> None:
    project = _repo(tmp_path / "project", "packages/harness/src/felix/x.py")
    assert _run("git status", project=project, cwd=project) == QUIET


# --- the ways the redirect goes wrong -----------------------------------------------
#
# The first fix pointed the gate at the repo a leading `cd` names. These are the shapes
# where "a leading `cd`" turned out not to mean what the code did. Both of the first two
# are things a session writes without trying; neither needs an adversary.


def test_a_cd_in_the_pr_body_does_not_redirect_the_gate(tmp_path: Path) -> None:
    """The one that would have bitten silently.

    `^` in sed anchors per *line*, so a `cd` anywhere in a multi-line command redirected
    the gate — and a reproduction step in a PR body is exactly that. The control then
    fires or does not depending on whether a path named in your prose happens to exist
    on the machine, which is worse than being plainly broken.
    """
    project = _repo(tmp_path / "project", "packages/harness/src/felix/x.py")
    elsewhere = _repo(tmp_path / "sibling", "docs/page.mdx")
    command = f'''gh pr create --body "$(cat <<'EOF'
To reproduce:
cd {elsewhere}
make check
EOF
)"'''
    assert _run(command, project=project, cwd=project) == FLAGGED


def test_the_hook_does_not_resolve_a_path_the_shell_would_not_enter(tmp_path: Path) -> None:
    """`cd "'/path'"` — bash fails that cd and stays put; the hook peeled both quote
    layers and followed it. The bypass is the disagreement, not the quoting."""
    project = _repo(tmp_path / "project", "packages/harness/src/felix/x.py")
    sibling = _repo(tmp_path / "sibling", "docs/page.mdx")
    assert _run(f"""cd "'{sibling}'" ; {CREATE}""", project=project, cwd=project) == FLAGGED


def test_a_worktree_of_this_project_is_still_gated(tmp_path: Path) -> None:
    """Identity by toplevel skipped every worktree, which is a normal way to work here —
    a fail-open on a legitimate workflow rather than an exotic one."""
    project = _repo(tmp_path / "project", "packages/harness/src/felix/x.py")
    tree = tmp_path / "wt"
    _git(project, "worktree", "add", "-q", str(tree), "HEAD")
    assert _run(CREATE, project=project, cwd=tree) == FLAGGED
    assert _run(f"cd {tree}; {CREATE}", project=project, cwd=project) == FLAGGED


def test_a_worktree_is_gated_on_its_own_head(tmp_path: Path) -> None:
    """The marker is shared but keyed by sha, so a reviewed worktree passes on its own
    commit and does not borrow the main checkout's."""
    project = _repo(tmp_path / "project", "packages/harness/src/felix/x.py")
    tree = tmp_path / "wt"
    _git(project, "worktree", "add", "-q", str(tree), "HEAD")
    (tree / "packages" / "harness" / "src" / "felix" / "y.py").write_text("y = 2\n")
    _git(tree, "add", "-A")
    _git(tree, "commit", "-qm", "worktree change")

    markers = project / ".claude" / "logs" / "quality-review"
    markers.mkdir(parents=True)
    (markers / _git(project, "rev-parse", "HEAD")).touch()
    assert _run(CREATE, project=project, cwd=tree) == FLAGGED, "worktree borrowed the main sha's marker"

    (markers / _git(tree, "rev-parse", "HEAD")).touch()
    assert _run(CREATE, project=project, cwd=tree) == QUIET


# --- where the hook and bash still parted company ------------------------------------


@pytest.mark.parametrize("first", ["sibling", "plain"])
def test_a_second_cd_is_not_a_way_back_in_ungated(tmp_path: Path, first: str) -> None:
    """`cd <elsewhere> && cd <project> && …` — the hook took the first `cd` and stopped
    while bash kept going, so the PR opened here with the gate pointed elsewhere."""
    project = _repo(tmp_path / "project", "packages/harness/src/felix/x.py")
    sibling = _repo(tmp_path / "sibling", "docs/page.mdx")
    plain = tmp_path / "plain"
    plain.mkdir()
    start = sibling if first == "sibling" else plain
    assert _run(f"cd {start} && cd {project} && {CREATE}", project=project, cwd=project) == FLAGGED


def test_navigating_into_a_subdirectory_still_gates(tmp_path: Path) -> None:
    """The shape that broke the first attempt at fixing the one above.

    `cd <project> && cd <project>/sub` is ordinary navigation, not an evasion. Refusing
    to resolve whenever there was more than one `cd` was described as conservative and
    was not: it fell back to the payload cwd, which skips the gate outright whenever the
    session cwd is outside the project — as it is in any session configured with
    additional working directories. The cwd here is deliberately the sibling.
    """
    project = _repo(tmp_path / "project", "packages/harness/src/felix/x.py")
    sibling = _repo(tmp_path / "sibling", "docs/page.mdx")
    sub = project / "packages"
    assert _run(f"cd {project} && cd {sub} && {CREATE}", project=project, cwd=sibling) == FLAGGED


def test_a_relative_chain_is_followed_the_way_bash_follows_it(tmp_path: Path) -> None:
    """Each `cd` resolves against where the previous one landed, not against the start."""
    project = _repo(tmp_path / "project", "packages/harness/src/felix/x.py")
    assert _run(f"cd {project} && cd packages && {CREATE}", project=project, cwd=tmp_path) == FLAGGED


def test_the_last_cd_wins_when_it_leads_out(tmp_path: Path) -> None:
    """The converse: ending up outside the project must skip, or the fix would just be
    an over-block in the other direction."""
    project = _repo(tmp_path / "project", "packages/harness/src/felix/x.py")
    sibling = _repo(tmp_path / "sibling", "docs/page.mdx")
    assert _run(f"cd {project} && cd {sibling} && {CREATE}", project=project, cwd=project) == QUIET


def test_an_ambient_git_dir_does_not_follow_the_gate_into_another_repo(tmp_path: Path) -> None:
    """`-C` does not override an exported GIT_DIR — the env var wins.

    The whole file assumes `-C` decides which repo is being asked about, so an ambient
    GIT_DIR made every query answer about that repo from anywhere: a PR in an unrelated
    checkout, blocked for unreviewed Python here.
    """
    project = _repo(tmp_path / "project", "packages/harness/src/felix/x.py")
    unrelated = _repo(tmp_path / "unrelated", "docs/page.mdx")
    assert _run(CREATE, project=project, cwd=unrelated, env={"GIT_DIR": str(project / ".git")}) == QUIET


def test_an_ambient_git_work_tree_does_not_open_a_way_out(tmp_path: Path) -> None:
    """The same hazard in the fail-open direction: a PR in this project must still gate
    however the environment is pointed."""
    project = _repo(tmp_path / "project", "packages/harness/src/felix/x.py")
    sibling = _repo(tmp_path / "sibling", "docs/page.mdx")
    for var in ("GIT_DIR", "GIT_WORK_TREE"):
        value = str(sibling / ".git") if var == "GIT_DIR" else str(sibling)
        assert _run(CREATE, project=project, cwd=project, env={var: value}) == FLAGGED, (
            f"{var} pointed the gate out of the project"
        )


# --- the security-reviewer branch ----------------------------------------------------
#
# A security fix carries a risk a feature change does not: closing one hole while opening
# another. Hostname validation added to stop SSRF was interpolated straight into
# `--host-resolver-rules`, whose grammar is a comma-separated list, so a host containing a
# comma would have redirected every other name to the metadata service. A reviewer caught
# it. The ask should not depend on the session noticing the change was security-shaped, so
# the hook decides from the paths — and that decision is asserted here rather than trusted.


def _context(command: str, *, project: Path, cwd: Path) -> str:
    """The note the hook emitted, or an empty string when it stayed quiet."""
    payload = json.dumps({"tool_input": {"command": command}, "cwd": str(cwd)})
    done = subprocess.run(
        ["bash", str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env={"PATH": os.environ["PATH"], "CLAUDE_PROJECT_DIR": str(project)},
    )
    assert done.returncode == 0, f"an advisory hook must never block: {done.stderr}"
    if not done.stdout.strip():
        return ""
    return json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"]


@pytest.mark.parametrize(
    "changed",
    [
        "packages/harness/src/felix/security/ssrf.py",
        "packages/harness/src/felix/auth/middleware.py",
        "packages/harness/src/felix/tools/browser.py",
        "packages/harness/src/felix/governance/inbound.py",
        "apps/api/src/felix_api/routes/internal.py",  # tenant/internal surface
        "packages/harness/src/felix/governance/screening.py",  # `screen` as a prefix
        "packages/harness/src/felix/manifests/policies.py",  # `polic` covers policy + policies
        "packages/harness/src/felix/tools/transports.py",
        "packages/harness/src/felix/db/rls_gucs.py",  # `rls` as a real path component
        "packages/harness/src/felix/approvals.py",
        # The governance wrapper order lives here and `.claude/rules/` calls it load-bearing;
        # the unanchored pattern matched none of this path.
        "packages/harness/src/felix/manifests/builder.py",
    ],
)
def test_a_control_path_change_also_asks_for_the_security_reviewer(tmp_path: Path, changed: str) -> None:
    project = _repo(tmp_path / "project", changed)
    note = _context(CREATE, project=project, cwd=project)
    assert "sit on a control path" in note, "the security reviewer was not asked for"
    assert "felix-security-reviewer" in note
    assert "felix-security-reviewer is not needed" not in note


@pytest.mark.parametrize(
    "changed",
    [
        "packages/harness/src/felix/eval/runner.py",
        # `rls` inside `urls`. Matched as a bare substring, this asked for a security review
        # of a URL helper — and a false positive is how a note gets trained into noise.
        "apps/api/src/felix_api/urls.py",
    ],
)
def test_an_ordinary_change_does_not_ask_for_the_security_reviewer(tmp_path: Path, changed: str) -> None:
    """Asking every time is the same as never asking — the note has to mean something."""
    project = _repo(tmp_path / "project", changed)
    note = _context(CREATE, project=project, cwd=project)
    assert "felix-quality-reviewer" in note, "the note should still be emitted"
    assert "felix-security-reviewer is not needed" in note
