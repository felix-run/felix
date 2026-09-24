#!/usr/bin/env python3
"""Assert properties of the *rendered* Compose config that a file-level parse cannot see.

`docker compose config` is what the CI docker job already runs over every overlay, but only
for "does it parse". Two things are only visible after Compose has merged the files:

* the gcp overlay must leave no `build:` on any Felix service, so a host can never compile
  its own artifact — `!reset` applied through a YAML merge key does *not* do this, and the
  file still parses and the unit tests still pass;
* every Felix process must wait on the `migrate` one-shot completing;
* no Felix service bind-mounts a host directory at `/workspace` unless the operator asked for
  one with `FELIX_WORKSPACE_HOST` — the old default bind-mounted the deployment's own checkout.

Usage: docker compose -f … config --format json | scripts/check-compose-render.py [--published]
"""

from __future__ import annotations

import json
import os
import sys

FELIX_IMAGE_PREFIXES = ("felix:", "ghcr.io/felix-run/felix:")


def main(argv: list[str]) -> int:
    published = "--published" in argv
    services = json.load(sys.stdin)["services"]
    felix = {n: s for n, s in services.items() if str(s.get("image", "")).startswith(FELIX_IMAGE_PREFIXES)}
    if not felix:
        print("no Felix service in the rendered config — the scan found nothing", file=sys.stderr)
        return 1
    problems: list[str] = []
    for name, svc in felix.items():
        if published and svc.get("build"):
            problems.append(f"{name}: still has a build section, so `up` would compile on the host")
        if not os.environ.get("FELIX_WORKSPACE_HOST"):
            for mount in svc.get("volumes") or []:
                if (
                    isinstance(mount, dict)
                    and mount.get("target") == "/workspace"
                    and mount.get("type") != "volume"
                ):
                    problems.append(
                        f"{name}: /workspace is a {mount.get('type')} mount of {mount.get('source')!r}; "
                        "the default must be the named volume, not a host directory"
                    )
        if name == "migrate":
            continue
        cond = (svc.get("depends_on") or {}).get("migrate", {}).get("condition")
        if cond != "service_completed_successfully":
            problems.append(f"{name}: does not wait on migrate (condition={cond!r})")
    for line in problems:
        print(line, file=sys.stderr)
    print(f"checked {len(felix)} Felix services" + (" (published image)" if published else ""))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
