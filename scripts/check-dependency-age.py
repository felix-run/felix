#!/usr/bin/env python3
"""Refuse locked dependency versions published in the last N hours.

The analogue of npm's `min-release-age`: a version pushed to PyPI in the last two days is
inside the window in which a hijacked package is noticed and yanked. `uv lock --check
--exclude-newer <ts>` looked like the switch for this and is not — a timestamp cutoff is a
resolution input, so uv discards the committed lock and re-resolves from scratch, and the
check fails on every PR for reasons unrelated to age. This reads the lock as written and
asks PyPI when each pinned version was uploaded.

    python scripts/check-dependency-age.py            # 48h, every PyPI package in uv.lock
    python scripts/check-dependency-age.py --hours 24
    python scripts/check-dependency-age.py --allow some-package   # a deliberate urgent bump

Stdlib only. Workspace members and non-PyPI sources (git, path, url) are skipped: the
question is about what PyPI served, not about code in this tree.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
PYPI = "https://pypi.org/pypi/{name}/{version}/json"
PYPI_HOST = "pypi.org"


def _is_pypi(registry: str) -> bool:
    """Whether ``registry`` is PyPI itself, decided on the parsed host.

    A prefix test on the URL string reads `https://pypi.org.evil.test/simple` as PyPI,
    and then this script asks *real* PyPI how old that name is: a package served by a
    lookalike index would clear the hold on metadata from an index it never came from.
    A URL is not a string for this purpose — the host is the part that decides who
    answered, so it is the part compared, exactly. `urlsplit().hostname` is already
    lowercased and strips any userinfo and port, which is what makes the comparison
    exact rather than another prefix test in disguise.
    """
    parts = urlsplit(registry)
    return parts.scheme in {"http", "https"} and parts.hostname == PYPI_HOST


def locked_pypi_packages(lock_text: str) -> list[tuple[str, str]]:
    """`(name, version)` for every package the lock resolved from PyPI."""
    lock = tomllib.loads(lock_text)
    out: list[tuple[str, str]] = []
    for pkg in lock.get("package", []):
        if _is_pypi(pkg.get("source", {}).get("registry", "")):
            out.append((pkg["name"], pkg["version"]))
    return out


def uploaded_at(name: str, version: str) -> datetime | None:
    """The earliest upload time of the release's files, or None when PyPI has no record."""
    req = urllib.request.Request(
        PYPI.format(name=name, version=version), headers={"Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.load(resp)
    except Exception:
        return None
    times = [f["upload_time_iso_8601"] for f in data.get("urls", []) if f.get("upload_time_iso_8601")]
    if not times:
        return None
    return min(datetime.fromisoformat(t.replace("Z", "+00:00")) for t in times)


def upload_times(packages: list[tuple[str, str]]) -> dict[tuple[str, str], datetime | None]:
    with ThreadPoolExecutor(max_workers=16) as pool:
        stamps = list(pool.map(lambda nv: uploaded_at(*nv), packages))
    return dict(zip(packages, stamps, strict=True))


def too_young(
    stamps: dict[tuple[str, str], datetime | None], *, hours: float, allow: set[str]
) -> list[tuple[str, str, datetime]]:
    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    return [
        (name, version, at)
        for (name, version), at in stamps.items()
        if at is not None and at > cutoff and name not in allow
    ]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--hours", type=float, default=48.0)
    parser.add_argument("--allow", action="append", default=[], help="package name exempt from the hold")
    parser.add_argument("--lock", type=Path, default=ROOT / "uv.lock")
    args = parser.parse_args(argv)
    packages = locked_pypi_packages(args.lock.read_text(encoding="utf-8"))
    stamps = upload_times(packages)
    unknown = sum(1 for at in stamps.values() if at is None)
    young = too_young(stamps, hours=args.hours, allow=set(args.allow))
    print(f"checked {len(packages)} PyPI packages against a {args.hours:g}h hold, {unknown} unknown to PyPI")
    if packages and unknown == len(packages):
        # Every lookup failing is an outage, not a clean bill; a pass here would be silent.
        print("no upload time could be read for any package — PyPI unreachable?", file=sys.stderr)
        return 1
    for name, version, at in sorted(young):
        print(f"  {name}=={version} uploaded {at.isoformat()} — inside the hold")
    if young:
        print("re-run after the hold, or pass --allow <name> for a deliberate urgent bump", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
