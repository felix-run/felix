"""`scripts/felix_boundary.py` — the gate a Felix-authored pull request cannot edit its way past.

The workflow runs the base branch's copy on `pull_request_target`, so the list below is the
control; these tests are what goes red when it shrinks.
"""

from __future__ import annotations

import pytest

from tests._scripts import load_script

boundary = load_script("felix_boundary")

BOT = "felix-bot"
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


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/felix-boundary.yml",
        ".github/CODEOWNERS",
        ".claude/hooks/git-guard.sh",
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
    ],
)
def test_every_protected_path_is_refused(path: str) -> None:
    """One case per entry the spec lists. Removing an entry from PROTECTED_PATHS goes red here."""
    problems = _judge(changed=[path], ticket_surface=[])
    assert any("touches the boundary" in p and path in p for p in problems), problems


def test_a_sibling_of_a_protected_path_is_not_refused() -> None:
    assert boundary.protected(["packages/harness/src/felix/tools/workspace.py", "docs/ROADMAP.md"]) == []
    assert boundary.protected(["manifests/quick.yaml", "tests/unit/test_shell_tool.py"]) == []


def test_the_branch_must_be_under_felix() -> None:
    assert any("felix/" in p for p in _judge(branch="feat/thing"))


def test_the_body_must_carry_the_contract() -> None:
    problems = _judge(body="## Summary\n\n- did the thing\n")
    joined = "\n".join(problems)
    assert "Closes #N" in joined
    assert "Felix-Thread" in joined
    assert "Gates run" in joined
    assert "Not verified" in joined
    assert _judge(body=GOOD_BODY.replace("Closes #42", "Closes #42\nCloses #43"))


def test_the_ticket_must_carry_felix_go() -> None:
    problems = _judge(ticket_labels=["felix:task", "felix:ready"])
    assert any("felix:go" in p for p in problems), problems


def test_paths_outside_the_ticket_surface_are_refused() -> None:
    problems = _judge(changed=["packages/harness/src/felix/eval/runner.py", "README.md"])
    assert any("README.md" in p for p in problems), problems
    assert _judge(ticket_surface=[]) == [], "a ticket that named no files constrains nothing here"


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


def test_main_exits_nonzero_with_annotations(tmp_path, capsys) -> None:
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
