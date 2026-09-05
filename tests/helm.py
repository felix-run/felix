"""`helm template` for tests, with the require-or-skip gate CI relies on.

`FELIX_REQUIRE_HELM=1` in the CI pytest job turns a missing binary into a failure: a
skipped chart test looks exactly like a passing one. Every test that renders the chart
goes through here so the gate cannot be forgotten by the next file.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "deploy" / "helm" / "felix"
REQUIRE_ENV = "FELIX_REQUIRE_HELM"


def helm_or_skip() -> None:
    """Call at module level (skips the whole file) or at the top of one test."""
    if shutil.which("helm") is not None:
        return
    if os.environ.get(REQUIRE_ENV):
        pytest.fail(f"{REQUIRE_ENV} is set but no `helm` binary is on PATH")  # pragma: no cover
    # `allow_module_level` is what makes the module-level call a skip rather than a
    # collection error; inside a test body it is inert.
    pytest.skip("helm binary not on PATH", allow_module_level=True)


def render(*set_values: str) -> dict[tuple[str, str], dict[str, Any]]:
    """Every object the chart renders, keyed by (kind, name)."""
    cmd = ["helm", "template", "felix", str(CHART)]
    for value in set_values:
        cmd += ["--set", value]
    out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    docs = [d for d in YAML(typ="safe").load_all(out) if d]
    return {(d["kind"], d["metadata"]["name"]): d for d in docs}
