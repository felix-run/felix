"""The advisory hooks and the toolkit validator, judged the way a session meets them.

`test_bash_guard_hooks.py` and `test_file_guard_hooks.py` cover the guards that block. These
are the hooks that only speak — and a hook that only speaks fails by going quiet, which
nobody notices. An audit found four of them silent in every git worktree (they stripped
`CLAUDE_PROJECT_DIR` off the path, leaving `.claude/worktrees/<name>/` on the front), the
subagent log recording "subagent" on every one of its 3,546 lines, the doc-drift gate
blocking a read-only session over another branch's edits, and the failure hint telling a
zsh typo to run `make fmt`.

Every test asserts both directions: a hook that always speaks passes the first half of each
and fails the second, and one that crashed passes neither.
"""

from __future__ import annotations

import ast
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable

import pytest

from tests.git_fixture import git

ROOT = pathlib.Path(__file__).resolve().parents[2]
HOOKS = ROOT / ".claude" / "hooks"
BLOCKED, ALLOWED = 2, 0

_HAS_JQ = shutil.which("jq") is not None
if not _HAS_JQ and os.environ.get("FELIX_REQUIRE_OPTIONAL_EXTRAS") == "1":
    raise RuntimeError("jq is required in CI: without it this whole file skips and reads as a pass")

needs_jq = pytest.mark.skipif(not _HAS_JQ, reason="the hooks no-op without jq")


def _hook(
    name: str, payload: dict[str, object], *, project: pathlib.Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    base = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ.get("HOME", "/tmp"),
        "CLAUDE_PROJECT_DIR": str(project),
    }
    return subprocess.run(
        ["bash", str(HOOKS / name)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env={**base, **(env or {})},
        timeout=60,
    )


def _context(proc: subprocess.CompletedProcess[str]) -> str:
    """The `additionalContext` a PostToolUse hook injected, or "" when it said nothing."""
    if not proc.stdout.strip():
        return ""
    return json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]


@pytest.fixture(scope="module")
def repo(tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    """A project with a linked worktree under `.claude/worktrees/`, as this repo is worked in."""
    root = tmp_path_factory.mktemp("project")
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    (root / "manifests").mkdir()
    (root / "manifests" / "quick.yaml").write_text("apiVersion: felix/v1\n")
    (root / "pyproject.toml").write_text("[tool.ruff]\nline-length = 110\n")
    (root / "README.md").write_text("# project\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "seed")
    git(root, "worktree", "add", "-q", "-b", "feat/x", str(root / ".claude" / "worktrees" / "wt"))
    (root / ".claude" / "worktrees" / "wt" / ".venv").mkdir()
    # At the root too: without it, a hook that fell back to the project root for a file in no
    # repository would still stop at `[ -d .venv ]`, and the test for that fallback could
    # not fail.
    (root / ".venv").mkdir()
    return root


@pytest.fixture
def fake_uv(tmp_path: pathlib.Path) -> tuple[dict[str, str], pathlib.Path]:
    """A `uv` that records where it ran and with what, and fails like a bad manifest does.

    The real one would import the whole harness per call; what these hooks owe is *which*
    tree they ask and *what* they pass, and that is exactly what this records.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "uv.log"
    uv = bindir / "uv"
    uv.write_text(
        f'#!/bin/bash\nprintf "%s\\t%s\\n" "$PWD" "$*" >> "{log}"\n'
        'if [ "${FAKE_UV_OK:-}" = 1 ]; then echo ok; exit 0; fi\n'
        'echo "invalid manifest"\nexit 1\n'
    )
    uv.chmod(uv.stat().st_mode | stat.S_IEXEC)
    return {"PATH": f"{bindir}:{os.environ['PATH']}"}, log


# --- the worktree-blind hooks ------------------------------------------------------------


@needs_jq
@pytest.mark.parametrize(
    ("rel", "expect"),
    [
        ("apps/api/src/felix_api/routes/memory.py", "management-api.mdx"),
        ("apps/api/src/felix_api/routes/chat.py", "rest-api.mdx"),
        ("packages/ai/src/felix_ai/catalog.py", "model-client.mdx"),
        # Surfaces an audit found mapped to no page: a second CLI module, the client package,
        # the agent card, the completion webhook payload and the error envelope.
        ("apps/cli/src/felix_cli/skills.py", "skill-import.mdx"),
        ("packages/client/src/felix_client/client.py", "getting-started.mdx"),
        # Each with a token only its own message carries, so two mappings collapsing into one
        # generic line, or swapping, goes red.
        ("packages/harness/src/felix/a2a/card.py", "agent-card.json"),
        ("packages/harness/src/felix/durability/webhooks.py", "Completion webhooks"),
        ("apps/api/src/felix_api/errors.py", "error envelope"),
        ("apps/api/src/felix_api/middleware.py", "error envelope"),
        # The CLI glob, not only its `skills.py` special case.
        ("apps/cli/src/felix_cli/main.py", "deploy.mdx"),
    ],
)
def test_doc_sync_reminder_speaks_inside_a_worktree(repo: pathlib.Path, rel: str, expect: str) -> None:
    """`memory.py` also proves the map: it was one of five routes no copy of the map had."""
    for tree in (repo, repo / ".claude" / "worktrees" / "wt"):
        said = _context(
            _hook("doc-sync-reminder.sh", {"tool_input": {"file_path": str(tree / rel)}}, project=repo)
        )
        assert expect in said, f"{tree.name}/{rel}: {said!r}"
    quiet = _hook(
        "doc-sync-reminder.sh", {"tool_input": {"file_path": str(repo / "notes.txt")}}, project=repo
    )
    assert _context(quiet) == ""


@needs_jq
def test_settings_sync_reminder_speaks_inside_a_worktree(repo: pathlib.Path) -> None:
    wt = repo / ".claude" / "worktrees" / "wt"
    config = _hook(
        "settings-sync-reminder.sh",
        {"tool_input": {"file_path": str(wt / "packages/harness/src/felix/config.py")}},
        project=repo,
    )
    assert ".env.example" in _context(config)
    model = _hook(
        "settings-sync-reminder.sh",
        {"tool_input": {"file_path": str(wt / "packages/harness/src/felix/decisions.py")}},
        project=repo,
    )
    assert "test_decision_provider.py" in _context(model)
    unrelated = _hook(
        "settings-sync-reminder.sh", {"tool_input": {"file_path": str(wt / "README.md")}}, project=repo
    )
    assert _context(unrelated) == ""


@needs_jq
@pytest.mark.parametrize(
    ("rel", "expect"),
    [
        # The wire contract is checked in; a route edit that does not regenerate it fails CI.
        ("apps/api/src/felix_api/routes/chat.py", ("make contract",)),
        ("apps/api/src/felix_api/routes/_sse.py", ("make contract",)),
        ("packages/harness/src/felix/manifests/schema.py", ("make schema",)),
        # Generated or release-written files: the point is "do not hand-edit this", which a
        # make target alone does not say -- the generated branch names both targets.
        ("schemas/openapi.json", ("generated", "make contract")),
        ("schemas/sse-events.json", ("generated", "make contract")),
        ("schemas/manifest.schema.json", ("generated", "make schema")),
        ("CHANGELOG.md", ("release step", "## Changelog")),
        ("packages/harness/src/felix/jobs/scheduler.py", ("felix-scheduler",)),
    ],
)
def test_settings_sync_reminder_names_the_regeneration_step(
    repo: pathlib.Path, rel: str, expect: tuple[str, ...]
) -> None:
    said = _context(
        _hook("settings-sync-reminder.sh", {"tool_input": {"file_path": str(repo / rel)}}, project=repo)
    )
    for token in expect:
        assert token in said, f"{rel}: {token!r} not in {said!r}"


@needs_jq
def test_the_route_reminder_stays_inside_the_routes_package(repo: pathlib.Path) -> None:
    """`app.py` sits beside `routes/`; a glob widened to the API package would catch it."""
    said = _context(
        _hook(
            "settings-sync-reminder.sh",
            {"tool_input": {"file_path": str(repo / "apps/api/src/felix_api/app.py")}},
            project=repo,
        )
    )
    assert "make contract" not in said, said


@needs_jq
def test_manifest_validate_checks_the_worktree_copy_without_dns(
    repo: pathlib.Path, fake_uv: tuple[dict[str, str], pathlib.Path]
) -> None:
    """Silent in a worktree before; and had it matched, it would have validated the main
    checkout's copy. It also resolved every egress host in DNS on every edit."""
    env, log = fake_uv
    wt = repo / ".claude" / "worktrees" / "wt"
    said = _context(
        _hook(
            "manifest-validate.sh",
            {"tool_input": {"file_path": str(wt / "manifests/quick.yaml")}},
            project=repo,
            env=env,
        )
    )
    assert "manifests/quick.yaml" in said, said
    ran_in, args = log.read_text().splitlines()[-1].split("\t")
    assert pathlib.Path(ran_in).resolve() == wt.resolve(), f"validated from {ran_in}, not the worktree"
    assert "--no-resolve-egress" in args


@needs_jq
def test_manifest_validate_is_quiet_on_a_valid_manifest_and_on_other_yaml(
    repo: pathlib.Path, fake_uv: tuple[dict[str, str], pathlib.Path]
) -> None:
    """The other direction: a hook that reported on every YAML edit would pass the test above."""
    env, log = fake_uv
    wt = repo / ".claude" / "worktrees" / "wt"
    manifest = {"tool_input": {"file_path": str(wt / "manifests/quick.yaml")}}
    assert (
        _context(_hook("manifest-validate.sh", manifest, project=repo, env={**env, "FAKE_UV_OK": "1"})) == ""
    )
    assert log.exists(), "a valid manifest was not validated at all"

    log.unlink()
    (wt / "other.yaml").write_text("a: 1\n")
    other = {"tool_input": {"file_path": str(wt / "other.yaml")}}
    assert _context(_hook("manifest-validate.sh", other, project=repo, env=env)) == ""
    assert not log.exists(), "a YAML file outside manifests/ was sent to the validator"


@needs_jq
def test_ruff_format_leaves_files_outside_any_repository_alone(
    repo: pathlib.Path, tmp_path: pathlib.Path, fake_uv: tuple[dict[str, str], pathlib.Path]
) -> None:
    """It formatted `~/.claude/plans/*.md` and memory files: ruff rewrites Markdown code blocks."""
    env, log = fake_uv
    plan = tmp_path / "plans" / "plan.md"
    plan.parent.mkdir()
    plan.write_text("# plan\n")
    _hook("ruff-format.sh", {"tool_input": {"file_path": str(plan)}}, project=repo, env=env)
    assert not log.exists(), f"ruff ran on a file in no repository: {log.read_text()}"

    wt = repo / ".claude" / "worktrees" / "wt"
    (wt / "mod.py").write_text("x=1\n")
    _hook("ruff-format.sh", {"tool_input": {"file_path": str(wt / "mod.py")}}, project=repo, env=env)
    calls = log.read_text().splitlines()
    assert calls, "ruff-format stopped running on a repo file"
    assert all(pathlib.Path(c.split("\t")[0]).resolve() == wt.resolve() for c in calls)
    assert all("--no-sync" in c for c in calls), "a formatter must not trigger a resync"
    assert any("ruff check" in c for c in calls), "a .py file was formatted but not linted"


@needs_jq
def test_ruff_format_formats_markdown_but_does_not_lint_it(
    repo: pathlib.Path, fake_uv: tuple[dict[str, str], pathlib.Path]
) -> None:
    env, log = fake_uv
    doc = repo / ".claude" / "worktrees" / "wt" / "notes.md"
    doc.write_text("# notes\n")
    _hook("ruff-format.sh", {"tool_input": {"file_path": str(doc)}}, project=repo, env=env)
    calls = log.read_text().splitlines()
    assert any("ruff format" in c for c in calls), "an in-repo .md file was not formatted"
    assert not any("ruff check" in c for c in calls), "Markdown was linted as Python"


@needs_jq
def test_ruff_format_skips_a_repository_that_does_not_use_ruff(
    tmp_path: pathlib.Path, fake_uv: tuple[dict[str, str], pathlib.Path]
) -> None:
    """A sibling checkout with its own conventions is not this repo's to reformat."""
    env, log = fake_uv
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q", "-b", "main")
    (other / ".venv").mkdir()
    (other / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    (other / "m.py").write_text("x=1\n")
    _hook("ruff-format.sh", {"tool_input": {"file_path": str(other / "m.py")}}, project=other, env=env)
    assert not log.exists(), f"ruff ran in a repository without [tool.ruff]: {log.read_text()}"


# --- protect-files: the spellings it missed -------------------------------------------------


@needs_jq
@pytest.mark.parametrize(
    ("rel", "want"),
    [
        (".env.staging", BLOCKED),
        ("apps/x/.env", BLOCKED),
        (".env.example", ALLOWED),
        ("apps/x/.env.example", ALLOWED),
    ],
)
def test_protect_files_covers_every_env_spelling(repo: pathlib.Path, rel: str, want: int) -> None:
    got = _hook("protect-files.sh", {"tool_input": {"file_path": str(repo / rel)}}, project=repo).returncode
    assert got == want, f"{rel}: got {got}, wanted {want}"


@needs_jq
def test_protect_files_reads_a_notebook_edit_target(repo: pathlib.Path) -> None:
    """NotebookEdit names its target `notebook_path`; reading only `file_path` let it through."""
    blocked = _hook("protect-files.sh", {"tool_input": {"notebook_path": str(repo / ".env")}}, project=repo)
    assert blocked.returncode == BLOCKED
    fine = _hook("protect-files.sh", {"tool_input": {"notebook_path": str(repo / "nb.ipynb")}}, project=repo)
    assert fine.returncode == ALLOWED


def test_protect_files_fails_closed_without_jq(repo: pathlib.Path, tmp_path: pathlib.Path) -> None:
    """Without jq the target read as empty and every edit was allowed — the wrong way to fail."""
    bindir = tmp_path / "nojq"
    bindir.mkdir()
    (bindir / "cat").symlink_to(shutil.which("cat") or "/bin/cat")
    proc = subprocess.run(
        ["/bin/bash", str(HOOKS / "protect-files.sh")],
        input=json.dumps({"tool_input": {"file_path": str(repo / ".env")}}),
        capture_output=True,
        text=True,
        env={"PATH": str(bindir), "CLAUDE_PROJECT_DIR": str(repo)},
    )
    assert proc.returncode == BLOCKED, proc.stderr
    assert "jq" in proc.stderr


# --- subagent-log ---------------------------------------------------------------------------


@needs_jq
def test_subagent_log_records_the_agent_type(tmp_path: pathlib.Path) -> None:
    payload = {"session_id": "s1", "agent_type": "felix-test-engineer", "agent_id": "a42"}
    _hook("subagent-log.sh", payload, project=tmp_path)
    line = (tmp_path / ".claude" / "logs" / "subagents.log").read_text().strip().split("\t")
    assert line[1:] == ["s1", "felix-test-engineer", "a42"], line


@needs_jq
@pytest.mark.parametrize(
    ("names", "want"),
    [
        ({"agent_type": ""}, "unnamed"),
        # An empty type falls through to the next field that has a value.
        ({"agent_type": "", "agent_name": "felix-engineer"}, "felix-engineer"),
        ({}, "unnamed"),
    ],
)
def test_subagent_log_never_writes_an_empty_agent_column(
    tmp_path: pathlib.Path, names: dict[str, str], want: str
) -> None:
    """An empty column is indistinguishable from a lost one when the log is audited."""
    _hook("subagent-log.sh", {"session_id": "s1", "agent_id": "a43", **names}, project=tmp_path)
    line = (tmp_path / ".claude" / "logs" / "subagents.log").read_text().strip().split("\t")
    assert line[1:] == ["s1", want, "a43"], line


# --- doc-drift-stop: this session's changes, not the tree's ----------------------------------


def _drift_repo(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "drift"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    for rel in (
        "packages/harness/src/felix/config.py",
        "packages/harness/src/felix/manifests/builder.py",
        "CHANGELOG.md",
    ):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("seed\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "seed")
    return root


def _start(root: pathlib.Path, sid: str, tmpdir: pathlib.Path) -> None:
    """SessionStart with no docker on PATH: its `compose ps` is irrelevant here, and a daemon
    still starting up would stall the test to its timeout."""
    bindir = tmpdir / "bin"
    bindir.mkdir(exist_ok=True)
    for tool in ("bash", "cat", "jq", "git", "dirname", "sort", "shasum", "cut", "grep", "wc", "tr", "perl"):
        found = shutil.which(tool)
        if found and not (bindir / tool).exists():
            (bindir / tool).symlink_to(found)
    env = {"TMPDIR": str(tmpdir), "PATH": str(bindir)}
    _hook("session-start.sh", {"session_id": sid, "cwd": str(root)}, project=root, env=env)


def _stop(root: pathlib.Path, sid: str, tmpdir: pathlib.Path) -> str:
    proc = _hook(
        "doc-drift-stop.sh", {"session_id": sid, "cwd": str(root)}, project=root, env={"TMPDIR": str(tmpdir)}
    )
    return json.loads(proc.stdout)["reason"] if proc.stdout.strip() else ""


@needs_jq
def test_doc_drift_ignores_what_the_tree_carried_before_the_session(tmp_path: pathlib.Path) -> None:
    """Reproduced live: a read-only planning turn was blocked over another branch's edits."""
    root = _drift_repo(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    (root / "packages/harness/src/felix/config.py").write_text("someone else's edit\n")
    _start(root, "s1", state)  # SessionStart takes the baseline

    assert _stop(root, "s1", state) == "", "blocked over an edit that predates the session"

    (root / "packages/harness/src/felix/manifests/builder.py").write_text("this session's edit\n")
    reason = _stop(root, "s1", state)
    assert "builder.py" in reason, "a real surface change went unremarked"
    assert "config.py" not in reason, "the pre-existing edit was reported as this session's"


@needs_jq
def test_doc_drift_counts_a_pre_existing_edit_the_session_changes_again(tmp_path: pathlib.Path) -> None:
    """The baseline is path *and* content, so touching an already-dirty file is this session's."""
    root = _drift_repo(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    config = root / "packages/harness/src/felix/config.py"
    config.write_text("someone else's edit\n")
    _start(root, "s3", state)
    config.write_text("someone else's edit\nand this session's\n")
    assert "config.py" in _stop(root, "s3", state)
    # Once per drift-set: the same set does not block the next turn again...
    assert _stop(root, "s3", state) == ""
    # ...but a different set does.
    (root / "packages/harness/src/felix/manifests/builder.py").write_text("more\n")
    assert "builder.py" in _stop(root, "s3", state)


@needs_jq
def test_a_second_session_start_keeps_the_first_baseline(tmp_path: pathlib.Path) -> None:
    """SessionStart fires again on resume and compact. A fresh snapshot then would absorb the
    session's own work into the baseline, and the gate would never fire on it."""
    root = _drift_repo(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    _start(root, "s4", state)
    (root / "packages/harness/src/felix/manifests/builder.py").write_text("this session's\n")
    _start(root, "s4", state)  # resume
    assert "builder.py" in _stop(root, "s4", state)


@needs_jq
def test_doc_drift_is_not_satisfied_by_editing_the_changelog(tmp_path: pathlib.Path) -> None:
    """A PR's entry belongs in its description now; the release writes CHANGELOG.md, and an
    edit to it is the habit this hook must not reward."""
    root = _drift_repo(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    _start(root, "s2", state)
    (root / "packages/harness/src/felix/config.py").write_text("changed\n")
    (root / "CHANGELOG.md").write_text("seed\n- entry\n")
    reason = _stop(root, "s2", state)
    assert "config.py" in reason and "## Changelog in the PR description" in reason


@needs_jq
def test_doc_drift_is_satisfied_by_a_docs_change(tmp_path: pathlib.Path) -> None:
    root = _drift_repo(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    _start(root, "s2", state)
    (root / "packages/harness/src/felix/config.py").write_text("changed\n")
    (root / "README.md").write_text("documented\n")
    assert _stop(root, "s2", state) == ""


@needs_jq
def test_doc_drift_without_a_baseline_measures_against_head(tmp_path: pathlib.Path) -> None:
    """The documented fallback: a tree the session never snapshotted (a worktree it made)."""
    root = _drift_repo(tmp_path)
    (root / "packages/harness/src/felix/config.py").write_text("changed\n")
    assert "config.py" in _stop(root, "never-started", tmp_path)


# --- test-failure-hint: precise enough to be believed -----------------------------------------


def _hint(command: str, output: str) -> str:
    proc = subprocess.run(
        ["bash", str(HOOKS / "test-failure-hint.sh")],
        input=json.dumps({"tool_input": {"command": command}, "tool_response": {"stderr": output}}),
        capture_output=True,
        text=True,
    )
    return json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"] if proc.stdout.strip() else ""


@needs_jq
@pytest.mark.parametrize(
    ("command", "output", "expect"),
    [
        ("=====", "zsh: = not found; bad format string", ""),
        ("curl localhost:8080/health", "curl: (7) Failed to connect: Connection refused", ""),
        ("x", "psycopg.OperationalError: connection to server at 127.0.0.1 port 5432 failed", "Postgres"),
        ("x", "redis.exceptions.ConnectionError: Connection refused 6379", "Postgres/Valkey"),
        ("ruff format --check .", "Would reformat: a.py\n1 file would be reformatted", "make fmt"),
        # validate-manifest spells it in lower case; build_agent capitalises it.
        ("uv run felix validate-manifest m.yaml", "invalid m.yaml: unknown pattern 'reactt'", "spec.pattern"),
        ("x", "ValueError: Unknown pattern 'reactt' for manifest 'm'", "spec.pattern"),
    ],
)
def test_failure_hints_fire_only_on_their_own_failure(command: str, output: str, expect: str) -> None:
    said = _hint(command, output)
    assert (expect in said) if expect else said == "", said


# --- the validator's drift checks -----------------------------------------------------------


def test_the_toolkit_in_this_tree_validates() -> None:
    """In the main suite, not only in CI's `toolkit` job — that job is path-filtered to
    `.claude/`, so renaming a module a skill cites would otherwise pass every check that ran."""
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "validate-toolkit.py")], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr


def test_the_validator_catches_a_stale_citation(tmp_path: pathlib.Path) -> None:
    claude = tmp_path / ".claude"
    (claude / "hooks").mkdir(parents=True)
    hook = claude / "hooks" / "ok.sh"
    hook.write_text("#!/bin/bash\nexit 0\n")
    hook.chmod(0o755)
    (claude / "settings.json").write_text("{}")
    (claude / "agents").mkdir()
    (claude / "agents" / "a.md").write_text("---\nname: a\ndescription: d\nskills:\n  - gone\n---\nbody\n")
    (claude / "skills" / "s").mkdir(parents=True)
    (claude / "skills" / "s" / "SKILL.md").write_text(
        "---\nname: s\ndescription: d\n---\n"
        "See `packages/real.py:present` and `packages/real.py:absent`, `packages/missing.py`,\n"
        "`packages/<name>.py` (a pattern), `make check`, `make nope`, and references/x.md.\n"
        # The import-path spelling has no top-level prefix; the check skipped it at first.
        "Also `felix_ai/gone.py` and `lib/real.sh`.\n"
    )
    (tmp_path / "packages").mkdir()
    (tmp_path / "packages" / "real.py").write_text("def present():\n    pass\n")
    (claude / "hooks" / "lib").mkdir()
    (claude / "hooks" / "lib" / "real.sh").write_text("true\n")
    (tmp_path / "Makefile").write_text("check:\n\ttrue\n")

    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "validate-toolkit.py"), str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    found = proc.stderr
    assert "preloads skill 'gone'" in found
    assert "`packages/missing.py`" in found
    assert "'absent' is not defined" in found
    assert "`make nope`" in found
    assert "references/x.md" in found
    assert "`felix_ai/gone.py`" in found
    # ...and nothing it should have accepted.
    assert "`packages/real.py:present`" not in found
    assert "<name>" not in found
    assert "`make check`" not in found
    assert "lib/real.sh" not in found


# --- validate-toolkit: prose lists checked against the code they restate ----------------------
#
# Each case plants one disagreement in a copy of the real tree and asserts the validator names
# it. The copy holds only what the enumeration check reads, so it fails other checks (cited
# paths it did not copy); the assertion is on what the plant *adds* over the unplanted copy.

_ENUM_INPUTS = [
    "CLAUDE.md",
    "README.md",
    "Makefile",
    "schemas/manifest.schema.json",
    "tests/unit/test_invariants.py",
    "apps/api/src/felix_api/app.py",
    "apps/api/src/felix_api/routes",
    "packages/harness/src/felix/session/strategies.py",
    "apps/cli/src/felix_cli/main.py",
    "skills/felix-architecture/SKILL.md",
]


def _enum_tree(tmp_path: pathlib.Path) -> pathlib.Path:
    tree = tmp_path / "tree"
    shutil.copytree(
        ROOT / ".claude",
        tree / ".claude",
        ignore=shutil.ignore_patterns("worktrees", "logs", "settings.local.json"),
    )
    for rel in _ENUM_INPUTS:
        source, target = ROOT / rel, tree / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(source, target)
    return tree


def _validator_problems(tree: pathlib.Path) -> set[str]:
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "validate-toolkit.py"), str(tree)],
        capture_output=True,
        text=True,
    )
    assert "Traceback" not in proc.stderr, proc.stderr
    return {
        line.strip().removeprefix("- ") for line in proc.stderr.splitlines() if line.strip().startswith("- ")
    }


def _enum_baseline(tree: pathlib.Path) -> set[str]:
    before = _validator_problems(tree)
    assert not [p for p in before if "toolkit:enum" in p or " list " in p], before
    return before


def _plant(tree: pathlib.Path, rel: str, old: str, new: str) -> None:
    path = tree / rel
    text = path.read_text()
    assert old in text, f"the plant's anchor is gone from {rel}: {old!r}"
    path.write_text(text.replace(old, new, 1))


def _append(tree: pathlib.Path, rel: str, code: str) -> None:
    path = tree / rel
    path.write_text(path.read_text() + code)


def _edit_schema(tree: pathlib.Path, edit: Callable[[dict], None]) -> None:
    path = tree / "schemas/manifest.schema.json"
    schema = json.loads(path.read_text())
    edit(schema["$defs"])
    path.write_text(json.dumps(schema, indent=2))


def _marker_files(key: str) -> set[str]:
    """Every file the validator expects to carry `key` -- each must notice a change in the code."""
    source = (ROOT / "scripts" / "validate-toolkit.py").read_text()
    table = ast.literal_eval(
        re.search(r"EXPECTED_MARKERS: dict\[str, set\[str\]\] = (\{.*?\n\})", source, re.S)[1]
    )
    return {rel for rel, keys in table.items() if key in keys}


@pytest.mark.parametrize(
    ("rel", "old", "new", "expect"),
    [
        (
            ".claude/rules/felix-invariants.md",
            "judges → approvals",
            "approvals → judges",
            "where the code has 'judges'",
        ),
        ("CLAUDE.md", " → workspace scope", "", "has 9 elements; the code has 10"),
        (
            ".claude/hooks/compact-reminder.sh",
            "judges -> approvals",
            "approvals -> judges",
            "where the code has 'judges'",
        ),
        (
            ".claude/skills/governance-pipeline/SKILL.md",
            "→ workspace scope\n",
            "→ workspace scope\n → tool budget\n",
            "has 11 elements; the code has 10",
        ),
        (
            ".claude/skills/api-surface/SKILL.md",
            "body limit → rate limit",
            "rate limit → body limit",
            "where the code has 'body limit'",
        ),
        # The last element replaced while its word survives later in the paragraph
        # (`auth/mgmt.py`): a search for "auth" anywhere in it called this a match.
        ("CLAUDE.md", "`AuthMiddleware`", "`CsrfMiddleware`", "where the code has 'auth'"),
        (
            ".claude/skills/api-surface/SKILL.md",
            "| `mcp.py` |",
            "| `mcp.py`, `ghost.py` |",
            "names 'ghost', which the code does not define",
        ),
        (
            ".claude/skills/manifest-authoring/SKILL.md",
            "| `pattern` |",
            "| `phantom` | nothing |\n| `pattern` |",
            "names 'phantom', which the code does not define",
        ),
        # Dropped from the list while the paragraph names it again after the list ends.
        (
            "README.md",
            "`compacting` (token-threshold), ",
            "",
            "session-strategies list does not name 'compacting'",
        ),
        (
            ".claude/skills/manifest-authoring/references/spec-fields.md",
            "| `windowed:N` |",
            "| `ghost:N` | x |\n| `windowed:N` |",
            "names 'ghost', which the code does not define",
        ),
        (
            ".claude/skills/model-layer/SKILL.md",
            "`content_screening.decider`",
            "`content_screening`",
            "does not name 'content_screening.decider'",
        ),
        (
            ".claude/skills/model-layer/SKILL.md",
            "`reflect.decider`",
            "`reflect.decider`, `reflect.critic.decider`",
            "names 'reflect.critic.decider', which the code does not define",
        ),
        ("CLAUDE.md", " | doctor |", " |", "cli-commands list does not name 'doctor'"),
        (
            "CLAUDE.md",
            "<!-- toolkit:enum middleware-order -->\n",
            "",
            "lost its `toolkit:enum middleware-order` marker",
        ),
    ],
    ids=[
        "rules-wrapper-reordered",
        "claude-md-wrapper-dropped",
        "compact-reminder-wrapper-reordered",
        "governance-pipeline-wrapper-extra",
        "api-surface-middleware-reordered",
        "claude-md-last-middleware-replaced",
        "api-surface-route-that-does-not-exist",
        "manifest-authoring-field-that-does-not-exist",
        "readme-strategy-dropped-but-mentioned-later",
        "spec-fields-strategy-that-does-not-exist",
        "model-layer-decider-consumer-dropped",
        "model-layer-decider-consumer-that-does-not-exist",
        "claude-md-cli-command-dropped",
        "claude-md-marker-removed",
    ],
)
def test_the_validator_catches_prose_that_disagrees_with_the_code(
    tmp_path: pathlib.Path, rel: str, old: str, new: str, expect: str
) -> None:
    tree = _enum_tree(tmp_path)
    before = _enum_baseline(tree)
    _plant(tree, rel, old, new)
    added = _validator_problems(tree) - before
    assert any(p.startswith(f"{rel}: ") and expect in p for p in added), (
        f"{expect!r} not among {sorted(added)}"
    )


def _optional_decider(defs: dict) -> None:
    defs["SkillSuggestionSpec"]["properties"]["decider"] = {"anyOf": [{"type": "boolean"}, {"type": "null"}]}


_CODE_PLANTS: dict[str, tuple[Callable[[pathlib.Path], None], str, str]] = {
    # name: (plant, key, the problem every file carrying that key must now report)
    "adds-a-wrapper": (
        lambda tree: _plant(
            tree,
            "tests/unit/test_invariants.py",
            "EXPECTED_WRAPPER_ORDER = [\n",
            'EXPECTED_WRAPPER_ORDER = [\n    "apply_tool_budget",\n',
        ),
        "wrapper-order",
        "the code has 11",
    ),
    "adds-a-middleware": (
        lambda tree: _append(tree, "apps/api/src/felix_api/app.py", "\napp.add_middleware(GzipMiddleware)\n"),
        "middleware-order",
        "the code has 6",
    ),
    "adds-a-strategy": (
        lambda tree: _plant(
            tree, "packages/harness/src/felix/session/strategies.py", "frozenset({", 'frozenset({"fancy", '
        ),
        "session-strategies",
        "does not name 'fancy'",
    ),
    "adds-a-command": (
        lambda tree: _append(
            tree, "apps/cli/src/felix_cli/main.py", '\n@app.command("frobnicate")\ndef _f() -> None: ...\n'
        ),
        "cli-commands",
        "does not name 'frobnicate'",
    ),
    "adds-a-command-named-after-its-function": (
        lambda tree: _append(
            tree, "apps/cli/src/felix_cli/main.py", "\n@app.command()\ndef do_thing() -> None: ...\n"
        ),
        "cli-commands",
        "does not name 'do-thing'",
    ),
    "adds-a-sub-app": (
        lambda tree: _append(
            tree, "apps/cli/src/felix_cli/main.py", '\napp.add_typer(object(), name="frob")\n'
        ),
        "cli-commands",
        "does not name 'frob'",
    ),
    "adds-a-spec-field": (
        lambda tree: _edit_schema(
            tree, lambda defs: defs["Spec"]["properties"].update(brand_new={"type": "string"})
        ),
        "spec-fields",
        "does not name 'brand_new'",
    ),
    "adds-a-decider-consumer": (
        lambda tree: _edit_schema(
            tree, lambda defs: defs["SkillSuggestionSpec"]["properties"].update(decider={"type": "boolean"})
        ),
        "decider-consumers",
        "does not name 'skill_suggestion.decider'",
    ),
    # `bool | None` is `anyOf: [boolean, null]`; read as a type of None, it was skipped.
    "adds-an-optional-decider-consumer": (
        lambda tree: _edit_schema(tree, _optional_decider),
        "decider-consumers",
        "does not name 'skill_suggestion.decider'",
    ),
    # Newer syntax than the runner's python3 must be reported, not end the run in a traceback.
    "stops-parsing": (
        lambda tree: _append(tree, "apps/api/src/felix_api/app.py", "\ndef (:\n"),
        "middleware-order",
        "cannot check the middleware-order list",
    ),
}


@pytest.mark.parametrize("name", list(_CODE_PLANTS))
def test_every_copy_of_a_list_notices_when_the_code_changes(tmp_path: pathlib.Path, name: str) -> None:
    """Every file carrying the key must report it: one copy noticing is not the check working,
    since a marker that governs the wrong paragraph can still contain every name."""
    plant, key, expect = _CODE_PLANTS[name]
    tree = _enum_tree(tmp_path)
    before = _enum_baseline(tree)
    plant(tree)
    added = _validator_problems(tree) - before
    reporting = {p.split(": ", 1)[0] for p in added if expect in p}
    assert reporting == _marker_files(key), sorted(added)


def test_a_new_route_module_must_join_the_api_surface_table(tmp_path: pathlib.Path) -> None:
    tree = _enum_tree(tmp_path)
    before = _enum_baseline(tree)
    (tree / "apps/api/src/felix_api/routes/new_surface.py").write_text("")
    added = _validator_problems(tree) - before
    assert (
        ".claude/skills/api-surface/SKILL.md: the route-modules list does not name 'new_surface'" in added
    ), added


# Ownership: every subpackage, and every module over the size floor, must be claimed by a
# skill's `metadata.covers` or listed in the validator's UNOWNED. These run the tree's own copy
# of the validator so a plant can edit UNOWNED as well as the skills.

_OWNED_ROOTS = [
    "packages/harness/src/felix",
    "packages/ai/src/felix_ai",
    "packages/client/src/felix_client",
    "apps/api/src/felix_api",
    "apps/cli/src/felix_cli",
    "apps/worker/src/felix_worker",
]
_OWNERSHIP_WORDS = ("has no owner", "covers ", "UNOWNED")
OWNED_FLOOR = 300  # OWNED_MIN_LINES in the validator


def _owned_tree(tmp_path: pathlib.Path) -> pathlib.Path:
    tree = tmp_path / "tree"
    shutil.copytree(
        ROOT / ".claude",
        tree / ".claude",
        ignore=shutil.ignore_patterns("worktrees", "logs", "settings.local.json"),
    )
    for rel in _OWNED_ROOTS:
        shutil.copytree(ROOT / rel, tree / rel, ignore=shutil.ignore_patterns("__pycache__"))
    (tree / "scripts").mkdir()
    shutil.copy2(ROOT / "scripts" / "validate-toolkit.py", tree / "scripts" / "validate-toolkit.py")
    return tree


def _ownership_problems(tree: pathlib.Path) -> set[str]:
    proc = subprocess.run(
        [sys.executable, str(tree / "scripts" / "validate-toolkit.py"), str(tree)],
        capture_output=True,
        text=True,
    )
    assert "Traceback" not in proc.stderr, proc.stderr
    problems = {line.strip().removeprefix("- ") for line in proc.stderr.splitlines()}
    return {p for p in problems if any(word in p for word in _OWNERSHIP_WORDS)}


def _unowned_entry(tree: pathlib.Path, entry: str) -> None:
    _plant(
        tree,
        "scripts/validate-toolkit.py",
        "UNOWNED: dict[str, str] = {",
        f"UNOWNED: dict[str, str] = {{{entry!r}: 'x', ",
    )


def _a_module_of(lines: int) -> Callable[[pathlib.Path], None]:
    return lambda tree: (tree / "packages/harness/src/felix/sizeable.py").write_text("x = 1\n" * lines)


def _a_subpackage(rel: str) -> Callable[[pathlib.Path], None]:
    def plant(tree: pathlib.Path) -> None:
        (tree / rel).mkdir()
        (tree / rel / "__init__.py").write_text("")

    return plant


_OWNERSHIP_PLANTS: dict[str, tuple[Callable[[pathlib.Path], None], str]] = {
    # name: (plant, the problem it must add)
    "new-subpackage": (_a_subpackage("packages/harness/src/felix/newthing"), "felix/newthing/ has no owner"),
    "module-at-the-floor": (_a_module_of(OWNED_FLOOR), "felix/sizeable.py has no owner"),
    "skill-drops-a-cover": (
        lambda tree: _plant(
            tree, ".claude/skills/durable-execution/SKILL.md", "covers: felix/durability/, ", "covers: "
        ),
        "felix/durability/ has no owner",
    ),
    "cover-that-names-nothing": (
        lambda tree: _plant(
            tree, ".claude/skills/model-layer/SKILL.md", "covers: ", "covers: felix/ghost/, "
        ),
        "covers 'felix/ghost/', which names nothing",
    ),
    "unowned-entry-now-covered": (
        lambda tree: _unowned_entry(tree, "felix/tools/"),
        "UNOWNED lists felix/tools/, which tools-runtime now covers",
    ),
    "unowned-entry-that-is-gone": (
        lambda tree: _unowned_entry(tree, "felix/retired/"),
        "UNOWNED lists felix/retired/, which is no longer a subpackage",
    ),
}


@pytest.mark.parametrize("name", list(_OWNERSHIP_PLANTS))
def test_code_without_an_owning_skill_fails_the_validator(tmp_path: pathlib.Path, name: str) -> None:
    plant, expect = _OWNERSHIP_PLANTS[name]
    tree = _owned_tree(tmp_path)
    assert not _ownership_problems(tree)
    plant(tree)
    added = _ownership_problems(tree)
    assert any(p.startswith(expect) or f": {expect}" in p for p in added), sorted(added)


@pytest.mark.parametrize(
    "plant",
    [_a_subpackage("apps/api/src/felix_api/newsurface"), _a_module_of(OWNED_FLOOR - 1)],
    ids=["under-a-covered-directory", "below-the-size-floor"],
)
def test_code_already_covered_or_too_small_to_need_an_owner_passes(
    tmp_path: pathlib.Path, plant: Callable[[pathlib.Path], None]
) -> None:
    tree = _owned_tree(tmp_path)
    plant(tree)
    assert not _ownership_problems(tree)
