"""Where the repository and the test data live. New code uses these rather than counting `parents[N]`."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# Data only the tests read. `fixtures/eval` stays at the repo root: `felix eval`, the Makefile
# and the eval workflows read those datasets too.
FIXTURES = ROOT / "tests" / "fixtures"
