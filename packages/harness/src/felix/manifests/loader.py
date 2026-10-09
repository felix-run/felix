"""Load manifests from YAML/JSON files and the bundled manifests/ directory."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from pydantic import ValidationError as PydanticValidationError
from ruamel.yaml import YAML

from felix.logging_setup import loggable
from felix.manifests.compat import drop_retired, log_dropped
from felix.manifests.schema import Manifest, assert_valid_manifest_name

_yaml = YAML(typ="safe")
logger = logging.getLogger("felix.manifests.loader")
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
    """Validate authored input — strictly, which is the point.

    Every caller here is someone *writing* a manifest: a PUT body, a YAML file, a bundled
    agent. `extra=forbid` is what makes `spec.toolz` an error instead of a field that
    silently configures nothing. Reading one back out of a store is a different question;
    see `parse_stored_manifest`.
    """
    try:
        return Manifest.model_validate(raw)
    except PydanticValidationError as exc:
        raise ManifestParseError(_render(exc)) from exc


def parse_stored_manifest(raw: Any, *, origin: str) -> Manifest:
    """Validate a manifest that was already accepted once and has been sitting in a store.

    Identical to `parse_manifest` except that fields the schema has since retired are
    dropped with a warning rather than failing the load. A row written in August cannot
    be re-authored by its operator retroactively, and refusing to serve it turns a field
    removal into an outage that surfaces as a failed request — see `compat.RETIRED`.

    Everything else still fails: a typo is not a retired field, and the strictness that
    catches it is worth keeping on both paths.
    """
    cleaned, dropped = drop_retired(raw)
    log_dropped(dropped, origin=origin)
    try:
        return parse_manifest(cleaned)
    except ManifestParseError as exc:
        # Named, because the anonymity is what made the original outage invisible: a bare
        # `spec.model.region: Extra inputs are not permitted` says nothing about *which*
        # stored manifest is unserviceable, and the operator's next question is always
        # which one. `parse_manifest` cannot say — only the caller knows the row.
        raise ManifestParseError(f"stored manifest {loggable(origin)}: {exc}") from exc


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


def _configured_manifests_dir() -> Path | None:
    """`FELIX_MANIFESTS_DIR`, if set. Not cached — settings can be reloaded.

    A set directory that is missing is refused at boot (`Settings.validate_runtime`), not here.
    """
    from felix.config import get_settings

    try:
        raw = (get_settings().manifests_dir or "").strip()
    except Exception:
        logger.warning(
            "could not read FELIX_MANIFESTS_DIR; serving the bundled manifests only", exc_info=True
        )
        return None
    return Path(raw).expanduser() if raw else None


def _names_in(root: Path) -> set[str]:
    if not root.is_dir():
        return set()
    return {p.stem for p in root.iterdir() if p.suffix in {".yaml", ".yml", ".json"} and p.is_file()}


def shadowed_names(bundled_dir: str | Path | None = None) -> list[str]:
    """Names both the bundled `manifests/` and `FELIX_MANIFESTS_DIR` hold.

    Refused rather than resolved by order. `contributor` lives in the extra directory and is
    protected there; were a same-named file under `manifests/` to win, a change adding one would
    replace the manifest that governs the agent without touching the protected file.
    """
    roots = _roots(bundled_dir)
    if len(roots) < 2:
        return []
    return sorted(_names_in(roots[0]) & _names_in(roots[1]))


def _roots(bundled_dir: str | Path | None) -> list[Path]:
    """Where a bundled manifest may come from, in order. An explicit `bundled_dir` is the whole set;
    otherwise the bundled `manifests/`, then `FELIX_MANIFESTS_DIR`."""
    if bundled_dir:
        return [Path(bundled_dir)]
    roots = [_default_bundled_dir()]
    extra = _configured_manifests_dir()
    if extra is not None:
        roots.append(extra)
    return roots


def load_bundled(
    name: str,
    *,
    bundled_dir: str | Path | None = None,
) -> Manifest:
    """Load a bundled manifest by name: `manifests/`, then `FELIX_MANIFESTS_DIR`."""
    assert_valid_manifest_name(name)
    if name in _bundled_cache:
        return _bundled_cache[name]
    if name in shadowed_names(bundled_dir):
        raise ValueError(
            f"Manifest {name!r} is in both manifests/ and FELIX_MANIFESTS_DIR; refusing to pick one"
        )
    for root in _roots(bundled_dir):
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
    names: set[str] = set()
    for root in _roots(bundled_dir):
        names |= _names_in(root)
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
    "parse_stored_manifest",
]
