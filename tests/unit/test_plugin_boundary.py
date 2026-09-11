"""Plugin boundary — only composition may list plugins."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "packages" / "harness" / "src" / "felix"
API = ROOT / "apps" / "api" / "src" / "felix_api"


def _py_files(base: Path) -> list[Path]:
    """Every `.py` under `base` — and never an empty list.

    Answering `[]` for a directory that has moved turns the boundary scan below into a
    scan over nothing, which passes. The plugin seam is exactly the rule that would then
    stop being enforced with no failure to show for it.
    """
    assert base.is_dir(), f"{base} is not a directory — has the source tree moved?"
    files = [p for p in base.rglob("*.py") if p.is_file()]
    assert files, f"no Python files under {base}; a scan over nothing passes by default"
    return files


def test_composition_defines_installed_plugins() -> None:
    composition = API / "composition.py"
    assert composition.is_file()
    tree = ast.parse(composition.read_text(encoding="utf-8"))
    names = {
        node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "installed_plugins" in names
    assert "compose" in names


def test_no_hardcoded_optional_plugin_imports() -> None:
    """Core must not hard-import optional plugin packages by name."""
    banned = {"felix_commerce", "felix_enterprise"}
    scanned = 0
    offenders: list[str] = []
    for path in _py_files(HARNESS) + _py_files(API):
        scanned += 1
        text = path.read_text(encoding="utf-8")
        for name in banned:
            if f"import {name}" in text or f"from {name}" in text:
                offenders.append(f"{path.relative_to(ROOT)}:{name}")
    assert scanned > 100, f"only {scanned} core files scanned; the boundary is not being checked"
    assert offenders == [], (
        "core must not import an optional plugin package by name — everything goes through "
        f"felix/plugins.py; see composition.py for the one exception: {offenders}"
    )


def test_installed_plugins_callable() -> None:
    from felix_api.composition import installed_plugins

    plugins = installed_plugins()
    assert isinstance(plugins, list)
