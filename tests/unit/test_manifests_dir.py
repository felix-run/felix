"""`contributor` and `triage` are not bundled; `FELIX_MANIFESTS_DIR` is how a stack serves them.

Every install served every file in `manifests/`, so a production deployment without the builder
stack's workspace and shell runner listed `contributor` and failed to compile it — `shell_tools[run]:
shell tools are disabled` — on every request for it. They live in `manifests/self/` now, and only a
deployment that points `FELIX_MANIFESTS_DIR` there (`compose.self.yml`) serves them.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from felix.config import Settings, get_settings
from felix.manifests.loader import clear_bundled_cache, list_bundled, load_bundled
from felix.manifests.resolver import clear_resolver_cache, resolve_manifest

ROOT = Path(__file__).resolve().parents[2]
SELF = ROOT / "manifests" / "self"
SELF_BUILD = {"contributor", "triage"}


@pytest.fixture
def manifests_dir(monkeypatch: pytest.MonkeyPatch):
    """Set FELIX_MANIFESTS_DIR the way a deployment does: in the environment `get_settings` reads."""

    def set_to(path: Path | str | None) -> None:
        if path is None:
            monkeypatch.delenv("FELIX_MANIFESTS_DIR", raising=False)
        else:
            monkeypatch.setenv("FELIX_MANIFESTS_DIR", str(path))
        get_settings.cache_clear()
        clear_bundled_cache()
        clear_resolver_cache()

    yield set_to
    monkeypatch.delenv("FELIX_MANIFESTS_DIR", raising=False)
    get_settings.cache_clear()
    clear_bundled_cache()
    clear_resolver_cache()


def test_the_self_build_manifests_are_not_bundled(manifests_dir) -> None:
    manifests_dir(None)

    assert SELF_BUILD.isdisjoint(list_bundled()), list_bundled()
    for name in SELF_BUILD:
        with pytest.raises(FileNotFoundError):
            load_bundled(name)


def test_the_setting_serves_them_after_the_bundled_ones(manifests_dir) -> None:
    manifests_dir(SELF)

    names = list_bundled()
    assert set(names) >= SELF_BUILD and "quick" in names, names
    assert load_bundled("contributor").metadata.name == "contributor"


async def test_the_resolver_finds_them_through_the_setting(manifests_dir) -> None:
    """The path a request takes: no stored version, so it falls through to the bundled lookup."""
    manifests_dir(SELF)

    resolved = await resolve_manifest("default", "triage")

    assert resolved.source == "bundled" and resolved.manifest.metadata.name == "triage"


def test_a_name_in_both_directories_is_refused_not_picked(manifests_dir, tmp_path: Path) -> None:
    """Neither may shadow the other. `contributor` is protected where it lives; a same-named file
    winning from the other directory would replace it without touching the protected file."""
    (tmp_path / "quick.yaml").write_text((SELF / "triage.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    manifests_dir(tmp_path)

    with pytest.raises(ValueError, match="both"):
        load_bundled("quick")


def test_a_shadowing_directory_is_refused_at_boot(tmp_path: Path) -> None:
    (tmp_path / "quick.yaml").write_text("{}", encoding="utf-8")
    settings = Settings(manifests_dir=str(tmp_path), allow_insecure=True, environment="development")

    with pytest.raises(RuntimeError, match="quick"):
        settings.validate_runtime()


def test_a_missing_directory_is_refused_at_boot(tmp_path: Path) -> None:
    """Unrefused, a typo would leave the stack's manifests unserved and say nothing."""
    settings = Settings(
        manifests_dir=str(tmp_path / "absent"), allow_insecure=True, environment="development"
    )

    with pytest.raises(RuntimeError, match="FELIX_MANIFESTS_DIR"):
        settings.validate_runtime()


def test_compose_self_points_the_api_and_worker_at_the_image_copy() -> None:
    """Never at /workspace: that is the checkout the agent edits, and it must not edit its own manifest."""
    text = (ROOT / "deploy" / "docker" / "compose.self.yml").read_text(encoding="utf-8")

    assert "FELIX_MANIFESTS_DIR: /app/manifests/self" in text
    assert "FELIX_MANIFESTS_DIR: /workspace" not in text
