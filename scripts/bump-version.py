#!/usr/bin/env python3
"""The one version number, in every file that carries it.

Felix ships one version across every workspace member's `pyproject.toml` and `__init__.py`
and the Helm chart's `version`, `appVersion` and `image.tag`. Releasing meant editing them
from memory; `v0.2.1` shipped with the chart's `image.tag` pointing at the previous image.
This script is the list.

    python scripts/bump-version.py --check            # every location agrees; print it
    python scripts/bump-version.py --check 0.3.0      # ...and it is this one (the release workflow)
    python scripts/bump-version.py 0.3.0              # set it everywhere, then re-lock

`tests/unit/test_version_single_source.py` asserts the list here matches the files in the
tree, so a new workspace member cannot be forgotten silently.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")

# (path, field, regex with one group around the version)
LOCATIONS: tuple[tuple[str, str, str], ...] = (
    ("pyproject.toml", "version", r'^version = "([^"]+)"$'),
    ("packages/ai/pyproject.toml", "version", r'^version = "([^"]+)"$'),
    ("packages/harness/pyproject.toml", "version", r'^version = "([^"]+)"$'),
    ("packages/cli/pyproject.toml", "version", r'^version = "([^"]+)"$'),
    ("apps/api/pyproject.toml", "version", r'^version = "([^"]+)"$'),
    ("apps/worker/pyproject.toml", "version", r'^version = "([^"]+)"$'),
    ("packages/harness/src/felix/__init__.py", "__version__", r'^__version__ = "([^"]+)"$'),
    ("packages/cli/src/felix_cli/__init__.py", "__version__", r'^__version__ = "([^"]+)"$'),
    ("apps/api/src/felix_api/__init__.py", "__version__", r'^__version__ = "([^"]+)"$'),
    ("apps/worker/src/felix_worker/__init__.py", "__version__", r'^__version__ = "([^"]+)"$'),
    ("deploy/helm/felix/Chart.yaml", "version", r"^version: (\S+)$"),
    ("deploy/helm/felix/Chart.yaml", "appVersion", r'^appVersion: "([^"]+)"$'),
    ("deploy/helm/felix/values.yaml", "image.tag", r'^  tag: "([^"]+)"$'),
)


def versions_by_location() -> dict[str, str]:
    """`"path field" -> version` for every location; a missing line raises."""
    found: dict[str, str] = {}
    for rel, field, pattern in LOCATIONS:
        text = (ROOT / rel).read_text(encoding="utf-8")
        match = re.search(pattern, text, re.MULTILINE)
        if match is None:
            raise SystemExit(
                f"{rel}: no `{field}` line matches {pattern!r} — the list in this script is stale"
            )
        found[f"{rel} {field}"] = match.group(1)
    return found


def _require_semver(value: str) -> None:
    if not SEMVER.match(value):
        raise SystemExit(f"{value!r} is not a semantic version")


def check(expected: str | None) -> str:
    found = versions_by_location()
    versions = sorted(set(found.values()))
    if len(versions) != 1:
        lines = "\n".join(f"  {v:10} {where}" for where, v in sorted(found.items(), key=lambda kv: kv[1]))
        raise SystemExit(f"version disagrees across {len(found)} locations:\n{lines}")
    version = versions[0]
    if expected is not None:
        # The release workflow derives `expected` from a git tag name, which permits shell
        # metacharacters; refuse anything that is not a version before it goes further.
        _require_semver(expected)
        if version != expected:
            raise SystemExit(f"tree is at {version}, expected {expected}")
    return version


def bump(new: str) -> None:
    _require_semver(new)
    for rel, field, pattern in LOCATIONS:
        path = ROOT / rel
        text = path.read_text(encoding="utf-8")
        updated, n = re.subn(
            pattern, lambda m: m.group(0).replace(m.group(1), new), text, count=1, flags=re.MULTILINE
        )
        if n != 1:
            raise SystemExit(f"{rel}: no `{field}` line matches {pattern!r}")
        path.write_text(updated, encoding="utf-8")
    # The lock records the workspace members' versions; `uv lock --check` fails otherwise.
    subprocess.run(["uv", "lock"], cwd=ROOT, check=True)


def main(argv: list[str]) -> int:
    if argv[:1] == ["--check"]:
        print(check(argv[1] if len(argv) > 1 else None))
        return 0
    if len(argv) == 1 and not argv[0].startswith("-"):
        bump(argv[0])
        print(f"bumped {len(LOCATIONS)} locations to {argv[0]}")
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
