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
BOT_LOGINS = frozenset({"felix-run-bot", "felix-run-bot[bot]"})

# Paths a Felix-authored pull request may not touch. Globs over the repo-relative path; a
# trailing `/**` matches the directory and everything under it. Kept as one list a person
# can read top to bottom — it is the list, not the mechanism, that the spec promises.
PROTECTED_PATHS: tuple[str, ...] = (
    ".github/**",
    ".claude/**",
    "CODEOWNERS",
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
_FENCED = re.compile(r"```.*?```", re.S)


def _outside_fences(body: str) -> str:
    """The body with fenced blocks removed — GitHub links no keyword inside one, and the
    `Gates run` section is exactly where a bot pastes command output."""
    return _FENCED.sub("", body or "")


def closes(body: str) -> list[int]:
    """Every `Closes #N` line outside a code fence. The one place the pattern is read."""
    return [int(n) for n in _CLOSES.findall(_outside_fences(body))]


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
    rest = (body or "")[m.end() :].split("\n###", 1)[0]
    fence = _FENCE.search(rest)
    block = fence.group(1) if fence else rest
    out: list[str] = []
    for line in block.splitlines():
        if line.strip().lower().startswith("_no response_"):
            continue  # the form's placeholder for an empty field
        path = _first_path(line)
        if path:
            out.append(path)
    return out


_PATH_TOKEN = re.compile(r"[A-Za-z0-9_./*?\[\]-]+")


def _first_path(line: str) -> str:
    """The path a `Files expected to change` line names, ignoring what follows it.

    People — and Felix — write `- \`path\` — why` or `path (new file)`. The first bot PR failed
    the surface check on every file because each whole line, prose included, was read as a
    glob. A path is the first backticked span when there is one, else the first path-shaped
    token; the rest of the line is commentary.
    """
    text = line.strip().lstrip("-*").strip()
    if not text:
        return ""
    tick = re.search(r"`([^`]+)`", text)
    if tick:
        text = tick.group(1).strip()
    m = _PATH_TOKEN.match(text)
    return m.group(0) if m else ""


@dataclass
class Contract:
    closes: int | None = None
    # Read by nothing here yet: the scoreboard joins a PR to its run on this key.
    thread: str | None = None
    missing: list[str] = field(default_factory=list)


def parse_contract(body: str) -> Contract:
    """The sections `.github/PULL_REQUEST_TEMPLATE.md` requires of a Felix-authored PR."""
    c = Contract()
    found = closes(body)
    if len(found) == 1:
        c.closes = found[0]
    elif not found:
        c.missing.append("a `Closes #N` line (exactly one felix:go issue)")
    else:
        c.missing.append(f"a single `Closes #N`: it names {len(found)} issues")
    thread = _THREAD.search(body or "")
    if thread:
        c.thread = thread.group(1)
    else:
        c.missing.append("a `Felix-Thread: <thread_id>` trailer")
    if not _GATES_HEADING.search(body or ""):
        c.missing.append("a `## Gates run` section")
    if not _NOT_VERIFIED_HEADING.search(body or ""):
        c.missing.append("a `## Not verified` section")
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
    and `ticket_surface` are None when the workflow could not read the ticket. With no
    `Closes` that is the contract check's finding; with one, it is a finding of its own —
    a ticket that cannot be read cannot have been authorised.
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
    if contract.closes is not None:
        if ticket_labels is None:
            problems.append(f"#{contract.closes} could not be read, so nothing shows a person authorised it")
        elif "felix:go" not in ticket_labels:
            problems.append(f"#{contract.closes} does not carry felix:go — a person has not authorised it")
    if ticket_surface:
        stray = outside_surface(changed, ticket_surface)
        if stray:
            problems.append("changes paths the ticket did not name: " + ", ".join(stray))
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--print-closes",
        metavar="BODY",
        help="print the one issue number the body closes (nothing when absent or ambiguous) and exit",
    )
    ap.add_argument("--author")
    ap.add_argument("--branch")
    ap.add_argument("--changed", help="path to a file with one changed path per line")
    ap.add_argument("--body", help="path to a file holding the PR body")
    ap.add_argument(
        "--ticket", help="path to a JSON file {labels: [...], body: str} for the closed issue, or absent"
    )
    args = ap.parse_args(argv)
    if args.print_closes:
        with open(args.print_closes, encoding="utf-8") as fh:
            found = closes(fh.read())
        print(found[0] if len(found) == 1 else "")
        return 0
    if not (args.author and args.branch and args.changed and args.body):
        ap.error("--author, --branch, --changed and --body are required to judge")
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
