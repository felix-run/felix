"""Load manifests from YAML/JSON files and the bundled manifests/ directory."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError as PydanticValidationError
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


class ManifestParseError(ValueError):
    """A manifest that does not validate, rendered for an operator to read.

    Pydantic's own `ValidationError` reaches an HTTP client as "internal error": it is not in
    `felix_api.errors._relayable`, and `PUT /manifests` raised it outside its own try/except.
    So a manifest refused for a *stated* reason — `spec.policies` naming tools but no scopes,
    say — answered 500 with no explanation, and a stored manifest carrying that shape answered
    500 on every read. A refusal nobody can read is an outage, which is the shape the refusal
    existed to remove.

    A `ValueError` subclass so the `except ValueError` paths that already exist keep working.
    """


def _render(exc: PydanticValidationError) -> str:
    """Location and reason, never the offending value.

    `str(ValidationError)` embeds `input_value=`, and this message travels into HTTP bodies,
    `jobs_store.record_run(error=...)` and a fiber's `state_json`. A manifest carries inline
    credentials often enough — `extra_forbidden` on an `api_key` renders the key — that
    rendering the input here would be a new way for one to reach a management surface.
    """
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ())) or "manifest"
        parts.append(f"{loc}: {err.get('msg', 'invalid')}")
    return "; ".join(parts) or "manifest failed validation"


def parse_manifest(raw: Any) -> Manifest:
    try:
        return Manifest.model_validate(raw)
    except PydanticValidationError as exc:
        raise ManifestParseError(_render(exc)) from exc


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
    root_resolved = root.expanduser().resolve()
    for ext in (".yaml", ".yml", ".json"):
        # `name` reaches here from a URL path segment. assert_valid_manifest_name
        # already bars separators, so this cannot currently escape — but the
        # containment check is what makes that a property of this function rather
        # than of a regex two modules away, and it is what a scanner can see.
        candidate = (root_resolved / f"{name}{ext}").resolve()
        if not candidate.is_relative_to(root_resolved):
            raise ValueError(f"Manifest name escapes the bundled directory: {name}")
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
