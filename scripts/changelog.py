#!/usr/bin/env python3
"""The changelog, written from pull-request descriptions at release time.

Every pull request used to edit `CHANGELOG.md` under `## [Unreleased]`. All of them inserted at
the same few lines, so each merge left the others conflicting on GitHub (which ignores the
`.gitattributes` union merge), and a rebase across a release cut could file an entry under a
version that had already shipped. A file per change (`changelog.d/`) fixed the conflicts and was
dropped for the clutter.

So the entry lives in the pull request's description instead, under a `## Changelog` heading:

    ## Changelog

    ### Fixed

    - **What changed, in bold.** Why, and what an operator sees now.

The category headings are Keep a Changelog's: Added, Changed, Deprecated, Removed, Fixed,
Security. A change with nothing to tell an operator says `none` and why:

    ## Changelog

    none: test-only refactor

Three commands, standard library only (the release workflow runs this without `uv sync`), with
`gh` for the GitHub API:

    changelog.py check [--body-file F]       validate one description (stdin by default)
    changelog.py collect [--since vX.Y.Z]    print what has merged since the last release
    changelog.py cut X.Y.Z [--date D]        write the version section into CHANGELOG.md

`cut` reads the merge commits on `origin/main` since the previous tag, fetches each pull request's
description, and replaces `## [Unreleased]` with the new version section -- keeping anything
still written there by hand, ahead of the collected entries in each category.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import subprocess
import sys
from dataclasses import dataclass

CATEGORIES = ("Added", "Changed", "Deprecated", "Removed", "Fixed", "Security")
CHANGELOG = pathlib.Path(__file__).resolve().parent.parent / "CHANGELOG.md"
REPO_URL = "https://github.com/felix-run/felix"

_SECTION = re.compile(r"^##\s+Changelog\s*$", re.M | re.I)
_NEXT_H2 = re.compile(r"^##\s(?!#)", re.M)
# The attribution line Claude Code appends to every pull request it opens. It sits under the
# last heading of a description, so a `## Changelog` written last swallowed it and failed as
# "text outside a `- ` entry" (#462). Matched exactly, so other stray text still fails.
_TRAILER = re.compile(r"^🤖 Generated with \[Claude Code\]", re.M)
_H3 = re.compile(r"^###\s+(.+?)\s*$")
_NONE = re.compile(r"^none\b\s*[:\-—–]?\s*(.*)$", re.I)
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_MERGE = re.compile(r"^Merge pull request #(\d+)\b")
_SQUASH = re.compile(r"\(#(\d+)\)\s*$")


class ChangelogError(ValueError):
    """A description whose changelog section cannot be used, said for its author."""


@dataclass(frozen=True)
class Entry:
    category: str
    text: str  # one bullet, continuation lines included, no trailing newline


def parse(body: str) -> list[Entry]:
    """The entries a description's `## Changelog` section declares; `[]` for an explicit `none`.

    Raises `ChangelogError` when there is no section, an unknown heading, text outside a
    heading, or a `none` without a reason.
    """
    body = _COMMENT.sub("", body.replace("\r\n", "\n"))
    match = _SECTION.search(body)
    if match is None:
        raise ChangelogError("no `## Changelog` section; add one, or `none: <reason>` under it")
    rest = body[match.end() :]
    ends = [m.start() for m in (_NEXT_H2.search(rest), _TRAILER.search(rest)) if m]
    section = rest[: min(ends, default=len(rest))].strip()
    if not section:
        raise ChangelogError("the `## Changelog` section is empty; write an entry or `none: <reason>`")
    first = section.splitlines()[0].strip()
    if (none := _NONE.match(first)) is not None and not first.startswith("#"):
        if not none.group(1).strip():
            raise ChangelogError("`none` needs a reason: `none: <why nothing here is user-visible>`")
        return []

    entries: list[Entry] = []
    category: str | None = None
    bullet: list[str] = []

    def flush() -> None:
        if bullet:
            assert category is not None
            entries.append(Entry(category, "\n".join(bullet).rstrip()))
            bullet.clear()

    for line in section.splitlines():
        heading = _H3.match(line)
        if heading:
            flush()
            name = heading.group(1).strip().title()
            if name not in CATEGORIES:
                raise ChangelogError(f"`### {heading.group(1)}` is not one of: {', '.join(CATEGORIES)}")
            category = name
        elif line.startswith("- "):
            if category is None:
                raise ChangelogError("an entry before any `### <category>` heading")
            flush()
            bullet.append(line.rstrip())
        elif bullet and (line.startswith(" ") or not line.strip()):
            bullet.append(line.rstrip())
        elif line.strip():
            raise ChangelogError(f"text outside a `- ` entry: {line.strip()[:60]!r}")
    flush()
    if not entries:
        raise ChangelogError("the section has headings but no `- ` entries")
    return entries


def _cite(entry: Entry, number: int) -> Entry:
    return Entry(entry.category, f"{entry.text} (#{number})")


# --- talking to git and GitHub -------------------------------------------------------------


def _run(*argv: str) -> str:
    return subprocess.run(argv, check=True, capture_output=True, text=True).stdout


def previous_tag() -> str:
    return _run("git", "describe", "--tags", "--abbrev=0", "--match", "v*", "origin/main").strip()


def merged_numbers(since: str) -> list[int]:
    """The pull requests merged into `origin/main` after `since`, oldest first."""
    numbers: list[int] = []
    for subject in reversed(
        _run("git", "log", "--first-parent", "--format=%s", f"{since}..origin/main").splitlines()
    ):
        found = _MERGE.match(subject) or _SQUASH.search(subject)
        if found:
            numbers.append(int(found.group(1)))
    return numbers


def description(number: int) -> str:
    out = _run("gh", "pr", "view", str(number), "--json", "body")
    return str(json.loads(out).get("body") or "")


def collect(since: str) -> tuple[list[Entry], list[str]]:
    """Every entry merged since `since`, cited, and a problem line for each unusable description."""
    entries: list[Entry] = []
    problems: list[str] = []
    for number in merged_numbers(since):
        try:
            entries.extend(_cite(e, number) for e in parse(description(number)))
        except ChangelogError as exc:
            problems.append(f"#{number}: {exc}")
    return entries, problems


# --- writing the file ----------------------------------------------------------------------


def _unreleased(text: str) -> tuple[int, int, list[Entry]]:
    """Where `## [Unreleased]` sits, and the entries still written there by hand."""
    start = text.index("## [Unreleased]")
    after = text.index("\n", start) + 1
    nxt = re.compile(r"^## \[", re.M).search(text, after)
    end = nxt.start() if nxt else len(text)
    body = text[after:end]
    entries: list[Entry] = []
    if body.strip():
        entries = parse("## Changelog\n" + body)
    return start, end, entries


def render(version: str, date: str, entries: list[Entry]) -> str:
    lines = [f"## [{version}] — {date}", ""]
    for category in CATEGORIES:
        chosen = [e.text for e in entries if e.category == category]
        if chosen:
            lines += [f"### {category}", "", *("\n".join([text, ""]) for text in chosen)]
    return "\n".join(lines).rstrip() + "\n\n"


def cut(text: str, version: str, date: str, collected: list[Entry]) -> str:
    """`text` with `## [Unreleased]` closed out as `version`, and a fresh empty one above it."""
    if re.search(rf"^## \[{re.escape(version)}\]", text, re.M):
        raise ChangelogError(f"CHANGELOG.md already has a section for {version}")
    start, end, by_hand = _unreleased(text)
    entries = [*by_hand, *collected]
    if not entries:
        raise ChangelogError("nothing to release: no entries by hand and none in merged descriptions")
    section = render(version, date, entries)
    out = text[:start] + "## [Unreleased]\n\n" + section + text[end:]
    return out.rstrip("\n") + f"\n[{version}]: {REPO_URL}/releases/tag/v{version}\n"


# --- the commands --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="validate one pull-request description")
    check.add_argument("--body-file", type=pathlib.Path)
    coll = sub.add_parser("collect", help="print what has merged since the last release")
    coll.add_argument("--since")
    cutp = sub.add_parser("cut", help="write the version section into CHANGELOG.md")
    cutp.add_argument("version")
    cutp.add_argument("--since")
    cutp.add_argument("--date", default=dt.date.today().isoformat())
    args = parser.parse_args(argv)

    try:
        if args.command == "check":
            body = args.body_file.read_text() if args.body_file else sys.stdin.read()
            entries = parse(body)
            print(
                f"changelog: {len(entries)} entr{'y' if len(entries) == 1 else 'ies'}"
                if entries
                else "changelog: none"
            )
            return 0
        since = args.since or previous_tag()
        entries, problems = collect(since)
        for problem in problems:
            print(f"warning: {problem}", file=sys.stderr)
        if args.command == "collect":
            print(render("Unreleased", f"since {since}", entries), end="")
            return 0
        CHANGELOG.write_text(cut(CHANGELOG.read_text(), args.version, args.date, entries))
        print(
            f"CHANGELOG.md: {args.version} written from {len(entries)} entries since {since}; "
            "read it before committing"
        )
        return 0
    except ChangelogError as exc:
        print(f"changelog: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
