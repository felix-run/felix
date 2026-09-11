"""Load a `scripts/*.py` file as a module, so its functions can be tested without a
console entry point. The scripts are stdlib-only and run by path in CI."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        f"_script_{name.replace('-', '_')}", ROOT / "scripts" / f"{name}.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
