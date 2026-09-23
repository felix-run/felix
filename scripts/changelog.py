#!/usr/bin/env python3
"""Assemble `changelog.d/` fragments into `CHANGELOG.md`.

Every pull request used to append to the top of the same block, so any two open at once
conflicted on it. A conflict there is worse than most: the resolution is prose, a merge tool
cannot help, and a botched one silently drops somebody's entry — which happened, six at once,
to a rewrite that should have been a merge.

One file per change means two changes are never in the same file, so there is nothing to
conflict. The cost is this script and one directory.

Deliberately not a dependency. `towncrier` and `scriv` both do this properly and both would be
a build-time dependency plus a config file, for about ninety lines of assembling markdown.
"""

from __future__ import annotations

import argparse
import datetime
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
FRAGMENTS = ROOT / "changelog.d"
CHANGELOG = ROOT / "CHANGELOG.md"

# Keep a Changelog's set, in the order it renders them. A fragment naming anything else is a
# typo, and a typo that silently became its own section is how a release ships an entry under
# a heading nobody reads.
SECTIONS = ("added", "changed", "deprecated", "removed", "fixed", "security")

NAME = re.compile(rf"^({'|'.join(SECTIONS)})-[a-z0-9][a-z0-9-]*\.md$")


def _fragments() -> list[pathlib.Path]:
    if not FRAGMENTS.is_dir():
        return []
    return sorted(p for p in FRAGMENTS.iterdir() if p.suffix == ".md" and p.name != "README.md")


def check() -> int:
    """Every fragment is named for a real section. Silent on an empty directory."""
    problems = []
    for path in _fragments():
        if not NAME.match(path.name):
            problems.append(
                f"{path.name}: expected <section>-<slug>.md where section is one of "
                f"{', '.join(SECTIONS)} and slug is lowercase alphanumeric with dashes"
            )
        elif not path.read_text(encoding="utf-8").strip():
            problems.append(f"{path.name}: empty; an entry nobody can read is worse than none")
    for problem in problems:
        print(f"changelog fragment: {problem}", file=sys.stderr)
    return 1 if problems else 0


def render() -> str:
    """The sections the pending fragments would produce, in Keep a Changelog order."""
    by_section: dict[str, list[str]] = {}
    for path in _fragments():
        match = NAME.match(path.name)
        if match is None:
            continue
        body = path.read_text(encoding="utf-8").strip()
        # Written without the bullet so a fragment reads as prose on its own; indented
        # continuation lines keep a multi-paragraph entry inside its own bullet.
        first, *rest = body.split("\n")
        entry = f"- {first}"
        if rest:
            entry += "\n" + "\n".join(f"  {line}" if line.strip() else "" for line in rest)
        by_section.setdefault(match.group(1), []).append(entry)

    out = []
    for section in SECTIONS:
        entries = by_section.get(section)
        if entries:
            out.append(f"### {section.capitalize()}\n\n" + "\n\n".join(entries))
    return "\n\n".join(out)


def release(version: str) -> int:
    """Fold the fragments into `[Unreleased]`, close it out, and delete them.

    The fragments are merged with whatever `[Unreleased]` already holds rather than replacing
    it, because entries written before this directory existed are still there and are still
    going out in this release.
    """
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        print(f"not a version: {version!r}", file=sys.stderr)
        return 1
    if check() != 0:
        return 1

    text = CHANGELOG.read_text(encoding="utf-8")
    marker = "## [Unreleased]"
    if marker not in text:
        print("CHANGELOG.md has no [Unreleased] section", file=sys.stderr)
        return 1
    start = text.index(marker)
    after = text.index("\n## [", start + len(marker))
    existing = text[start + len(marker) : after].strip("\n")

    pending = render()
    body = "\n\n".join(part for part in (pending, existing) if part)
    if not body:
        print("nothing to release: no fragments and an empty [Unreleased]", file=sys.stderr)
        return 1

    today = datetime.date.today().isoformat()
    closed = f"{marker}\n\n## [{version}] — {today}\n\n{body}\n"
    text = text[:start] + closed + text[after:]

    link = f"[{version}]: https://github.com/felix-run/felix/releases/tag/v{version}\n"
    if link not in text:
        text = text.rstrip("\n") + "\n" + link
    CHANGELOG.write_text(text, encoding="utf-8")

    for path in _fragments():
        path.unlink()
    print(f"released {version}: {(len(pending.splitlines()) and 'fragments folded in') or 'no fragments'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="validate fragment names and bodies")
    group.add_argument("--preview", action="store_true", help="print the sections they would produce")
    group.add_argument("--release", metavar="X.Y.Z", help="fold into CHANGELOG.md and delete them")
    args = parser.parse_args()

    if args.check:
        return check()
    if args.preview:
        rendered = render()
        print(rendered if rendered else "(no pending fragments)")
        return 0
    return release(args.release)


if __name__ == "__main__":
    raise SystemExit(main())
