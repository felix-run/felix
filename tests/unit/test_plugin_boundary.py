"""Plugin boundary — only composition may list plugins."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "packages" / "harness" / "src" / "felix"
API = ROOT / "apps" / "api" / "src" / "felix_api"


def _py_files(base: Path) -> list[Path]:
    if not base.is_dir():
        return []
    return [p for p in base.rglob("*.py") if p.is_file()]


def test_composition_defines_installed_plugins() -> None:
    composition = API / "composition.py"
    assert composition.is_file()
    tree = ast.parse(composition.read_text(encoding="utf-8"))
    names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "installed_plugins" in names
    assert "compose" in names


def test_no_hardcoded_commerce_import_outside_composition_seat() -> None:
    """Core must not hard-import optional plugin packages."""
    banned = {"felix_commerce", "felix_enterprise"}
    offenders: list[str] = []
    for path in _py_files(HARNESS) + _py_files(API):
        # plugins.py may mention package names in load_optional_plugins discovery.
        if path.name in {"plugins.py", "composition.py"}:
            continue
        text = path.read_text(encoding="utf-8")
        for name in banned:
            if f"import {name}" in text or f"from {name}" in text:
                offenders.append(f"{path.relative_to(ROOT)}:{name}")
    assert offenders == []


def test_installed_plugins_callable() -> None:
    from felix_api.composition import installed_plugins

    plugins = installed_plugins()
    assert isinstance(plugins, list)
