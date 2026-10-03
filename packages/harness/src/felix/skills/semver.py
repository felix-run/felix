"""`major.minor.patch` bumping and comparison for skill versions.

Deliberately lenient, matching Skillist's `skill-format`: a non-numeric segment counts as
0 and a prerelease or build suffix is ignored, because versions are regex-gated before they
get here. Ported from Skillist's `skill-format` package (MIT); see NOTICE.
"""

from __future__ import annotations

from typing import Literal

SemverBump = Literal["major", "minor", "patch"]


def _segment(text: str) -> int:
    try:
        return int(text.strip() or 0)
    except ValueError:
        return 0


def _parts(version: str) -> list[int]:
    return [_segment(part) for part in version.split(".")]


def bump_semver(version: str, bump: SemverBump = "patch") -> str:
    parts = [*_parts(version), 0, 0, 0]
    major, minor, patch = parts[0], parts[1], parts[2]
    if bump == "major":
        return f"{major + 1}.0.0"
    if bump == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def compare_semver(a: str, b: str) -> Literal[-1, 0, 1]:
    """-1, 0 or 1 as ``a`` is older, equal or newer, ignoring any prerelease/build suffix."""
    pa = [*_parts(a.split("+")[0].split("-")[0]), 0, 0, 0]
    pb = [*_parts(b.split("+")[0].split("-")[0]), 0, 0, 0]
    for x, y in zip(pa[:3], pb[:3], strict=True):
        if x < y:
            return -1
        if x > y:
            return 1
    return 0


def resolve_next_semver(
    current: str | None,
    *,
    semver: str | None = None,
    bump: SemverBump | None = None,
) -> str:
    """An explicit ``semver`` wins; else bump ``current``; else the first version, 0.1.0."""
    if semver:
        return semver
    if current:
        return bump_semver(current, bump or "patch")
    return "0.1.0"


__all__ = ["SemverBump", "bump_semver", "compare_semver", "resolve_next_semver"]
