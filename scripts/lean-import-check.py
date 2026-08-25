#!/usr/bin/env python3
"""Import every module in a lean install.

The default `uv sync` and the default Docker image ship without extras. Optional
dependencies (boto3, temporalio, duckdb, playwright, presidio, …) must therefore
be imported inside the function that needs them, never at module scope.

Importing the top-level packages is not enough to prove that — a stray import in
a module nothing else pulls in stays invisible. This walks every module instead.

Run after a lean install:
    uv sync --locked --no-dev && uv run --no-sync python scripts/lean-import-check.py
"""

from __future__ import annotations

import importlib
import pkgutil
import sys

PACKAGES = ("felix", "felix_api", "felix_cli", "felix_worker")

# Modules that legitimately import an optional dependency at module scope, and are
# therefore expected to fail here. See EXTRA_ONLY_MODULES in tests/unit/test_invariants.py:
# `@workflow.run` rejects a class declared inside a function, so the Temporal
# definitions cannot be built lazily the way every other optional binding is. The
# invariant test asserts nothing imports them eagerly, which is what keeps a lean
# install working despite this skip.
EXTRA_ONLY = {"felix.durability._temporal_workflow"}


def main() -> int:
    failures: list[str] = []
    imported = 0

    for name in PACKAGES:
        try:
            package = importlib.import_module(name)
        except Exception as exc:
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
            continue
        imported += 1
        for module in pkgutil.walk_packages(package.__path__, prefix=f"{name}."):
            if module.name in EXTRA_ONLY:
                continue
            imported += 1
            try:
                importlib.import_module(module.name)
            except Exception as exc:
                failures.append(f"{module.name}: {type(exc).__name__}: {exc}")

    if failures:
        print(f"{len(failures)} module(s) cannot be imported without optional extras:\n", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        print(
            "\nMove the import inside the function that needs it (see the plugin-seam "
            "skill), or add the dependency to the core requirements deliberately.",
            file=sys.stderr,
        )
        return 1

    print(f"lean import check OK — {imported} modules imported with no extras installed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
