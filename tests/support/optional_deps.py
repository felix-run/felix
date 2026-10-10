"""Gating for tests that need an optional extra installed.

A module-level `pytest.importorskip` collapses a whole file into a single collect-time
skip. That is right locally — not everyone installs `--all-extras` — and wrong in CI,
where it made `test_temporal_backend.py` (six tests) and the DuckDB warehouse tests
disappear from every run without appearing in the skip count. The suite simply reported
fewer tests, which looks exactly like a pass.

This is the `FELIX_CONFORMANCE_REQUIRE_POSTGRES` idea applied to extras: CI installs the
extras it expects and sets `FELIX_REQUIRE_OPTIONAL_EXTRAS=1`, so a missing one fails the
job instead of quietly removing coverage. `tests/unit/test_invariants.py` asserts that
every extra named here is one the CI test job actually installs.
"""

from __future__ import annotations

import importlib
import os
from typing import Any

import pytest

REQUIRE_ENV = "FELIX_REQUIRE_OPTIONAL_EXTRAS"


def require_optional(module: str, extra: str) -> Any:
    """Import `module`, or skip the caller — unless CI promised the extra is present."""
    if os.environ.get(REQUIRE_ENV):
        try:
            return importlib.import_module(module)
        except ImportError:  # pragma: no cover — only reachable on a broken CI install
            pytest.fail(
                f"{REQUIRE_ENV} is set but {module!r} is missing: the CI test job is expected "
                f"to install felix-harness[{extra}], so these tests cannot be skipped here"
            )
    return pytest.importorskip(module, reason=f"felix-harness[{extra}] not installed")
