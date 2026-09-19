#!/usr/bin/env python3
"""The one gate the loop cannot reach: what a Felix-authored pull request may not do.

Run by `.github/workflows/felix-boundary.yml` on `pull_request_target`, so the *base* branch's
copy of this file judges the pull request — a change to this file or to the workflow inside a
Felix-authored PR is itself a boundary violation, judged by the copy it tried to replace. The
workflow checks out nothing from the head; it reads the changed paths and the body through the
API and hands them here. Pure functions, so `tests/unit/test_felix_boundary.py` can go red the
day a protected path is removed or the body parser accepts a PR without a ticket.

`docs/SELF.md` is the spec this enforces. For a person's pull request every check here passes;
the boundary is Felix's, not the maintainer's.
"""

import argparse
import fnmatch
import json
import re
import sys
from dataclasses import dataclass, field

# Who the loop acts as on GitHub. A second bot is a second entry here, judged by a person.
BOT_LOGINS = frozenset({"felix-bot", "felix-bot[bot]"})

# Paths a Felix-authored pull request may not touch. Globs over the repo-relative path; a
# trailing `/**` matches the directory and everything under it. Kept as one list a person
# can read top to bottom — it is the list, not the mechanism, that the spec promises.
PROTECTED_PATHS: tuple[str, ...] = (
    ".github/**",
    ".claude/**",
    "CODEOWNERS",
    ".github/CODEOWNERS",
    "uv.lock",
    "migrations/**",
    "deploy/**",
    "manifests/contributor.yaml",
    "manifests/triage.yaml",
    "docs/SELF.md",
    "scripts/felix_boundary.py",
    "tests/unit/test_*_manifest.py",
    "packages/harness/src/felix/manifests/builder.py",
    "packages/harness/src/felix/governance/**",
    "packages/harness/src/felix/auth/**",
    "packages/harness/src/felix/security/**",
    "packages/harness/src/felix/tools/shell.py",
)

# Branches the loop may author from.
BRANCH_PREFIX = "felix/"

_CLOSES = re.compile(r"(?im)^\s*closes\s+#(\d+)\s*$")
_THREAD = re.compile(r"(?im)^\s*Felix-Thread:\s*(\S+)\s*$")
_GATES_HEADING = re.compile(r"(?im)^##\s+Gates run\s*$")
_NOT_VERIFIED_HEADING = re.compile(r"(?im)^##\s+Not verified\s*$")


def _match(pattern: str, path: str) -> bool:
    if pattern.endswith("/**"):
        root = pattern[: -len("/**")]
        return path == root or path.startswith(root + "/")
    return fnmatch.fnmatchcase(path, pattern)


def protected(paths: list[str]) -> list[str]:
    """The changed paths that fall inside the boundary."""
    return sorted(p for p in paths if any(_match(pat, p) for pat in PROTECTED_PATHS))


def outside_surface(paths: list[str], surface: list[str]) -> list[str]:
    """Changed paths the ticket's `files expected to change` did not name.

    The ticket's list is treated as globs too, so `tests/unit/test_x.py` and `tests/**` both
    work. An empty surface means the ticket named nothing, and then nothing is outside it —
    the readiness check, not this one, is what refuses a ticket with no surface.
    """
    if not surface:
        return []
    return sorted(p for p in paths if not any(_match(s, p) for s in surface))


_SURFACE_HEADING = re.compile(r"(?im)^###\s+Files expected to change\s*$")
_FENCE = re.compile(r"```[^\n]*\n(.*?)```", re.S)


def surface_from_issue_body(body: str) -> list[str]:
    """The `Files expected to change` list from a `felix_task` issue body.

    GitHub renders the form's textarea as `### <label>` followed by a fenced block (the field
    is `render: text`). Lines inside the fence are the paths; a bullet or a backtick around one
    is tolerated, because a person editing the issue by hand will add both.
    """
    m = _SURFACE_HEADING.search(body or "")
    if not m:
        return []
    rest = (body or "")[m.end() :]
    fence = _FENCE.search(rest)
    block = fence.group(1) if fence else rest.split("\n###", 1)[0]
    out: list[str] = []
    for line in block.splitlines():
        item = line.strip().lstrip("-*").strip().strip("`").strip()
        if item and not item.lower().startswith("_no response_"):
            out.append(item)
    return out


@dataclass
class Contract:
    closes: int | None = None
    thread: str | None = None
    gates_section: bool = False
    not_verified_section: bool = False
    missing: list[str] = field(default_factory=list)


def parse_contract(body: str) -> Contract:
    """The sections `.github/PULL_REQUEST_TEMPLATE.md` requires of a Felix-authored PR."""
    c = Contract()
    closes = _CLOSES.findall(body or "")
    if len(closes) == 1:
        c.closes = int(closes[0])
    elif not closes:
        c.missing.append("Closes #N (exactly one felix:go issue)")
    else:
        c.missing.append(f"Closes #N names {len(closes)} issues; a Felix PR closes exactly one")
    thread = _THREAD.search(body or "")
    if thread:
        c.thread = thread.group(1)
    else:
        c.missing.append("Felix-Thread: <thread_id> trailer")
    c.gates_section = bool(_GATES_HEADING.search(body or ""))
    if not c.gates_section:
        c.missing.append("## Gates run section")
    c.not_verified_section = bool(_NOT_VERIFIED_HEADING.search(body or ""))
    if not c.not_verified_section:
        c.missing.append("## Not verified section")
    return c


def judge(
    *,
    author: str,
    branch: str,
    changed: list[str],
    body: str,
    ticket_labels: list[str] | None,
    ticket_surface: list[str] | None,
) -> list[str]:
    """Every reason this pull request fails the boundary. Empty means it passes.

    A person's PR never fails here; the list is about what the loop may do. `ticket_labels`
    and `ticket_surface` are None when the workflow could not read the ticket (no `Closes`,
    or the issue does not exist), which is itself reported by the contract check.
    """
    if author not in BOT_LOGINS:
        return []
    problems: list[str] = []
    hits = protected(changed)
    if hits:
        problems.append("touches the boundary: " + ", ".join(hits))
    if not branch.startswith(BRANCH_PREFIX):
        problems.append(f"branch {branch!r} is not under {BRANCH_PREFIX!r}")
    contract = parse_contract(body)
    for m in contract.missing:
        problems.append("PR body lacks " + m)
    if contract.closes is not None and ticket_labels is not None and "felix:go" not in ticket_labels:
        problems.append(f"#{contract.closes} does not carry felix:go — a person has not authorised it")
    if ticket_surface:
        stray = outside_surface(changed, ticket_surface)
        if stray:
            problems.append("changes paths the ticket did not name: " + ", ".join(stray))
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--author", required=True)
    ap.add_argument("--branch", required=True)
    ap.add_argument("--changed", required=True, help="path to a file with one changed path per line")
    ap.add_argument("--body", required=True, help="path to a file holding the PR body")
    ap.add_argument(
        "--ticket", help="path to a JSON file {labels: [...], body: str} for the closed issue, or absent"
    )
    args = ap.parse_args(argv)
    with open(args.changed, encoding="utf-8") as fh:
        changed = [line.strip() for line in fh if line.strip()]
    with open(args.body, encoding="utf-8") as fh:
        body = fh.read()
    labels: list[str] | None = None
    surface: list[str] | None = None
    if args.ticket:
        with open(args.ticket, encoding="utf-8") as fh:
            ticket = json.load(fh)
        labels = [str(lb.get("name") if isinstance(lb, dict) else lb) for lb in ticket.get("labels") or []]
        surface = surface_from_issue_body(str(ticket.get("body") or ""))
    problems = judge(
        author=args.author,
        branch=args.branch,
        changed=changed,
        body=body,
        ticket_labels=labels,
        ticket_surface=surface,
    )
    for p in problems:
        print(f"::error::felix-boundary: {p}")
    if not problems:
        who = "a person" if args.author not in BOT_LOGINS else args.author
        print(f"felix-boundary: ok ({who}, {len(changed)} changed paths)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
