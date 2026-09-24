"""`scripts/felix_boundary.py` — the gate a Felix-authored pull request cannot edit its way past.

The workflow runs the base branch's copy on `pull_request_target`, so the list below is the
control; these tests are what goes red when it shrinks, and when the two ways the gate could
fail *open* — an author it does not recognise, a ticket it could not read — stop being refused.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests._scripts import load_script

boundary = load_script("felix_boundary")

BOT = "felix-run-bot"
# Literal, not `boundary.BOT_LOGINS`: a login dropped from the control must go red here.
BOT_LOGINS = ["felix-run-bot", "felix-run-bot[bot]"]
GOOD_BODY = """## Summary

- did the thing

Closes #42

## Gates run

```
./scripts/test.sh -q → 12 passed
```

## Not verified

none

Felix-Thread: default:t:abc
"""

# One representative path per PROTECTED_PATHS entry, in the same order. The structural test
# below holds each to exactly one pattern, so a redundant entry is itself a failure.
ONE_PATH_PER_ENTRY = [
    ".github/workflows/felix-boundary.yml",
    ".claude/hooks/git-guard.sh",
    "CODEOWNERS",
    "uv.lock",
    "migrations/versions/0017_x.py",
    "deploy/docker/compose.yml",
    "manifests/contributor.yaml",
    "manifests/triage.yaml",
    "docs/SELF.md",
    "scripts/felix_boundary.py",
    "tests/unit/test_contributor_manifest.py",
    "packages/harness/src/felix/manifests/builder.py",
    "packages/harness/src/felix/governance/pii.py",
    "packages/harness/src/felix/auth/mgmt.py",
    "packages/harness/src/felix/security/shell_policy.py",
    "packages/harness/src/felix/tools/shell.py",
]


def _judge(**kw):
    base = dict(
        author=BOT,
        branch="felix/42-thing",
        changed=["packages/harness/src/felix/eval/runner.py"],
        body=GOOD_BODY,
        ticket_labels=["felix:task", "felix:go"],
        ticket_surface=["packages/harness/src/felix/eval/**"],
    )
    base.update(kw)
    return boundary.judge(**base)


def test_a_conforming_bot_pr_passes() -> None:
    assert _judge() == []


def test_a_person_is_never_judged() -> None:
    assert _judge(author="blakebauman", changed=[".github/workflows/ci.yml"], body="", branch="fix/x") == []


@pytest.mark.parametrize("login", BOT_LOGINS)
def test_every_bot_login_is_judged(login: str) -> None:
    """The user and the GitHub App spellings. Dropping either from BOT_LOGINS fails open."""
    problems = _judge(author=login, branch="main")
    assert any(p.startswith("branch ") for p in problems), problems


@pytest.mark.parametrize("path", ONE_PATH_PER_ENTRY)
def test_every_protected_path_is_refused(path: str) -> None:
    problems = _judge(changed=[path], ticket_surface=[])
    assert any("touches the boundary" in p and path in p for p in problems), problems


def test_each_protected_entry_is_the_only_one_its_path_matches() -> None:
    """Every entry is exercised by exactly one path here, and that path by exactly one
    entry — so an entry another already covers, or one no test reaches, both go red."""
    entries = list(boundary.PROTECTED_PATHS)
    assert len(ONE_PATH_PER_ENTRY) == len(entries), "add a representative path for the new entry"
    for pattern, path in zip(entries, ONE_PATH_PER_ENTRY, strict=True):
        matching = [e for e in entries if boundary._match(e, path)]
        assert matching == [pattern], f"{path!r} is matched by {matching}, expected only {pattern!r}"


def test_a_sibling_of_a_protected_path_is_not_refused() -> None:
    assert boundary.protected(["packages/harness/src/felix/tools/workspace.py", "docs/ROADMAP.md"]) == []
    assert boundary.protected(["manifests/quick.yaml", "tests/unit/test_shell_tool.py"]) == []
    # A prefix that shares letters is not the directory: `deploy/**` does not cover `deployments/`.
    assert boundary.protected(["deployments/x", "packages/harness/src/felix/security_notes.md"]) == []


def test_the_branch_must_be_under_felix() -> None:
    problems = _judge(branch="feat/thing")
    assert any(p.startswith("branch 'feat/thing' is not under 'felix/'") for p in problems), problems


def test_the_body_must_carry_the_contract() -> None:
    problems = _judge(body="## Summary\n\n- did the thing\n")
    joined = "\n".join(problems)
    assert "Closes #N" in joined
    assert "Felix-Thread" in joined
    assert "Gates run" in joined
    assert "Not verified" in joined
    problems = _judge(body=GOOD_BODY.replace("Closes #42", "Closes #42\nCloses #43"))
    assert any("names 2 issues" in p for p in problems), problems


def test_a_closes_inside_a_code_fence_does_not_count() -> None:
    """GitHub links no keyword inside a fence, and the Gates section is where output is pasted."""
    assert boundary.closes("## Gates run\n\n```\nCloses #42\n```\n") == []
    assert boundary.closes(GOOD_BODY) == [42]
    fenced_extra = GOOD_BODY.replace(
        "./scripts/test.sh -q → 12 passed", "Closes #99\n./scripts/test.sh -q → 12 passed"
    )
    assert boundary.closes(fenced_extra) == [42]


@pytest.mark.parametrize(
    ("heading", "ok"),
    [("## Gates run", True), ("## GATES RUN", True), ("### Gates run", False), ("Gates run", False)],
)
def test_the_gates_heading_is_level_two_in_any_case(heading: str, ok: bool) -> None:
    body = GOOD_BODY.replace("## Gates run", heading)
    assert (not any("Gates run" in p for p in _judge(body=body))) is ok


def test_the_ticket_must_carry_felix_go() -> None:
    problems = _judge(ticket_labels=["felix:task", "felix:ready"])
    assert any("felix:go" in p for p in problems), problems


def test_a_ticket_that_could_not_be_read_is_refused() -> None:
    """`Closes #999999` with no ticket to show for it used to pass the authorisation check."""
    problems = _judge(ticket_labels=None, ticket_surface=None)
    assert any("could not be read" in p for p in problems), problems


def test_paths_outside_the_ticket_surface_are_refused() -> None:
    problems = _judge(changed=["packages/harness/src/felix/eval/runner.py", "README.md"])
    assert any("README.md" in p for p in problems), problems
    assert _judge(ticket_surface=[]) == [], "a ticket that named no files constrains nothing here"
    # `eval/**` licenses `eval/`, not `evals/` — the fail-open direction of a prefix test.
    stray = boundary.outside_surface(
        ["packages/harness/src/felix/evals/x.py"], ["packages/harness/src/felix/eval/**"]
    )
    assert stray == ["packages/harness/src/felix/evals/x.py"]


def test_surface_is_read_from_the_issue_form() -> None:
    body = (
        "### Evidence\n\nROADMAP.md:1@abc\n\n### Files expected to change\n\n"
        "```text\npackages/harness/src/felix/eval/runner.py\n- `tests/unit/test_eval_x.py`\n```\n\n"
        "### Acceptance test\n\n```text\n./scripts/test.sh\n```\n"
    )
    assert boundary.surface_from_issue_body(body) == [
        "packages/harness/src/felix/eval/runner.py",
        "tests/unit/test_eval_x.py",
    ]
    assert boundary.surface_from_issue_body("### Files expected to change\n\n_No response_\n") == []
    assert boundary.surface_from_issue_body("no such section") == []


def test_a_surface_line_may_carry_prose_after_the_path() -> None:
    """The first bot PR (#290) failed the surface check on all three files: its ticket wrote
    `- \`path\` — reason` per line and the whole line was read as a glob."""
    body = (
        "### Files expected to change\n\n"
        "- `packages/harness/src/felix/waiters.py` — add TTL-based eviction or size cap\n"
        "- `tests/unit/test_waiters.py` or new test file — verify the bound\n"
        "changelog.d/fixed-thing.md (new file)\n"
        "packages/**/x.py: every module\n"
    )
    assert boundary.surface_from_issue_body(body) == [
        "packages/harness/src/felix/waiters.py",
        "tests/unit/test_waiters.py",
        "changelog.d/fixed-thing.md",
        "packages/**/x.py",
    ]


def test_an_unfenced_surface_does_not_borrow_the_next_sections_fence() -> None:
    """A person editing the issue by hand drops the fence; the acceptance command must not
    become the surface."""
    body = "### Files expected to change\n\npackages/x.py\n\n### Acceptance test\n\n```text\n./scripts/test.sh\n```\n"
    assert boundary.surface_from_issue_body(body) == ["packages/x.py"]


def test_main_judges_a_bot_pr_through_the_github_ticket_shape(tmp_path: Path, capsys) -> None:
    """The seam between GitHub's JSON and `judge`: labels are dicts, the surface is in the body."""
    (tmp_path / "changed").write_text("packages/harness/src/felix/eval/runner.py\n")
    (tmp_path / "body").write_text(GOOD_BODY)
    ticket = {
        "labels": [{"name": "felix:task"}, {"name": "felix:go"}],
        "body": "### Files expected to change\n\n```text\npackages/harness/src/felix/eval/runner.py\n```\n",
    }
    (tmp_path / "ticket.json").write_text(json.dumps(ticket))
    args = [
        "--author",
        BOT,
        "--branch",
        "felix/42-x",
        "--changed",
        str(tmp_path / "changed"),
        "--body",
        str(tmp_path / "body"),
    ]
    assert boundary.main([*args, "--ticket", str(tmp_path / "ticket.json")]) == 0
    assert "ok (felix-run-bot" in capsys.readouterr().out
    # Without the ticket the same PR is refused: a `Closes` nobody could read.
    assert boundary.main(args) == 1
    assert "could not be read" in capsys.readouterr().out


def test_main_exits_nonzero_with_annotations(tmp_path: Path, capsys) -> None:
    (tmp_path / "changed").write_text(".github/workflows/ci.yml\n")
    (tmp_path / "body").write_text("nothing")
    rc = boundary.main(
        [
            "--author",
            BOT,
            "--branch",
            "felix/1-x",
            "--changed",
            str(tmp_path / "changed"),
            "--body",
            str(tmp_path / "body"),
        ]
    )
    out = capsys.readouterr().out
    assert rc == 1
    assert "::error::felix-boundary: touches the boundary" in out
    rc = boundary.main(
        [
            "--author",
            "blakebauman",
            "--branch",
            "x",
            "--changed",
            str(tmp_path / "changed"),
            "--body",
            str(tmp_path / "body"),
        ]
    )
    assert rc == 0


def test_the_workflow_judges_with_the_base_branch_tip() -> None:
    """`base.sha` is the base as of the PR's last synchronize; a fix to the script on main
    never reached an open PR that way. The checkout must name the branch."""
    workflow = (Path(__file__).resolve().parents[2] / ".github/workflows/felix-boundary.yml").read_text()
    assert "ref: ${{ github.event.pull_request.base.ref }}" in workflow
    assert "pull_request.base.sha" not in workflow
    assert "pull_request.head" not in workflow.split("Read the pull request")[0], "never check out the head"


def test_print_closes_is_what_the_workflow_fetches_with(tmp_path: Path, capsys) -> None:
    """One regex: the workflow asks the script which ticket to fetch."""
    (tmp_path / "body").write_text(GOOD_BODY)
    assert boundary.main(["--print-closes", str(tmp_path / "body")]) == 0
    assert capsys.readouterr().out.strip() == "42"
    (tmp_path / "body2").write_text("Closes #1\nCloses #2\n")
    assert boundary.main(["--print-closes", str(tmp_path / "body2")]) == 0
    assert capsys.readouterr().out.strip() == ""
    workflow = (Path(__file__).resolve().parents[2] / ".github/workflows/felix-boundary.yml").read_text()
    assert "--print-closes body.md" in workflow
    assert "re.findall" not in workflow, "the workflow must not carry its own copy of the Closes pattern"
