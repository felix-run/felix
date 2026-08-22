"""Load manifests from YAML/JSON files and the bundled manifests/ directory."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from felix.manifests.schema import Manifest, assert_valid_manifest_name

_yaml = YAML(typ="safe")
_bundled_cache: dict[str, Manifest] = {}


def _default_bundled_dir() -> Path:
    # packages/harness/src/felix/manifests/loader.py → repo manifests/
    here = Path(__file__).resolve().parent
    candidates = [
        here.parents[4] / "manifests",  # /…/felix/manifests (repo root)
        Path.cwd() / "manifests",
        here / "bundled",
    ]
    for c in candidates:
        if c.is_dir() and (any(c.glob("*.yaml")) or any(c.glob("*.yml"))):
            return c
    for c in candidates:
        if c.is_dir():
            return c
    return here / "bundled"


def parse_manifest(raw: Any) -> Manifest:
    return Manifest.model_validate(raw)


def load_manifest_data(data: str | bytes, *, source: str = "inline") -> Manifest:
    text = data.decode("utf-8") if isinstance(data, bytes) else data
    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        raw = json.loads(text)
    else:
        raw = _yaml.load(text)
    if raw is None:
        raise ValueError(f"Empty manifest from {source}")
    return parse_manifest(raw)


def load_manifest_file(path: str | Path) -> Manifest:
    p = Path(path)
    return load_manifest_data(p.read_text(encoding="utf-8"), source=str(p))


def load_bundled(
    name: str,
    *,
    bundled_dir: str | Path | None = None,
) -> Manifest:
    """Load a bundled manifest by name from the manifests/ directory."""
    assert_valid_manifest_name(name)
    if name in _bundled_cache:
        return _bundled_cache[name]
    root = Path(bundled_dir) if bundled_dir else _default_bundled_dir()
    for ext in (".yaml", ".yml", ".json"):
        candidate = root / f"{name}{ext}"
        if candidate.is_file():
            m = load_manifest_file(candidate)
            _bundled_cache[name] = m
            return m
    raise FileNotFoundError(f"Unknown bundled manifest: {name}")


def list_bundled(*, bundled_dir: str | Path | None = None) -> list[str]:
    root = Path(bundled_dir) if bundled_dir else _default_bundled_dir()
    if not root.is_dir():
        return []
    names: set[str] = set()
    for p in root.iterdir():
        if p.suffix in {".yaml", ".yml", ".json"} and p.is_file():
            names.add(p.stem)
    return sorted(names)


def clear_bundled_cache() -> None:
    _bundled_cache.clear()


load_manifest = load_bundled


__all__ = [
    "clear_bundled_cache",
    "list_bundled",
    "load_bundled",
    "load_manifest",
    "load_manifest_data",
    "load_manifest_file",
    "parse_manifest",
]
