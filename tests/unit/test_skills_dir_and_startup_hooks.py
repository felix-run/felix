"""`FELIX_SKILLS_DIR` precedence, and the startup-hook guard.

Bundled skills resolved only from `__file__`-relative repo paths, so a
pip-installed Felix had none and no way to point at its own. The override
direction between the bundled dir and the configured one is a real decision, so
it is pinned here rather than left to whichever `dict.update` runs last.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


def _write_skill(root: Path, name: str, description: str) -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nBody.\n", encoding="utf-8"
    )


@pytest.fixture(autouse=True)
def _clear_caches() -> Any:
    from felix.skills import loader

    loader._bundled_cache.clear()
    yield
    loader._bundled_cache.clear()


@pytest.mark.asyncio
async def test_a_configured_dir_adds_skills(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from felix.skills.loader import load_manifest_skills

    extra = tmp_path / "skills"
    _write_skill(extra, "acme-refunds", "Handle a refund.")

    import felix.config as config_mod

    monkeypatch.setattr(config_mod, "get_settings", lambda: config_mod.Settings(skills_dir=str(extra)))

    catalog = await load_manifest_skills([{"name": "acme-refunds"}])
    skill = catalog.get("acme-refunds")

    assert skill is not None
    assert "refund" in skill.description.lower()


@pytest.mark.asyncio
async def test_the_configured_dir_wins_over_a_same_named_bundled_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pinned deliberately: last-writer-wins, and the operator's dir is last.

    An operator pointing FELIX_SKILLS_DIR at their own copy expects it to take
    effect; silently preferring the image's bundled copy would be surprising.
    """
    from felix.skills.loader import load_manifest_skills

    bundled = tmp_path / "bundled"
    configured = tmp_path / "configured"
    _write_skill(bundled, "shared", "From the bundled directory.")
    _write_skill(configured, "shared", "From the configured directory.")

    import felix.config as config_mod
    import felix.skills.loader as loader

    monkeypatch.setattr(loader, "_default_bundled_dir", lambda: bundled)
    monkeypatch.setattr(config_mod, "get_settings", lambda: config_mod.Settings(skills_dir=str(configured)))

    catalog = await load_manifest_skills([{"name": "shared"}])

    assert catalog.get("shared").description == "From the configured directory."


@pytest.mark.asyncio
async def test_an_unset_or_missing_dir_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    import felix.config as config_mod
    from felix.skills.loader import _configured_skills_dir

    monkeypatch.setattr(config_mod, "get_settings", lambda: config_mod.Settings(skills_dir=""))
    assert _configured_skills_dir() is None

    monkeypatch.setattr(config_mod, "get_settings", lambda: config_mod.Settings(skills_dir="/no/such/dir"))
    assert _configured_skills_dir() is None


def test_a_raising_startup_hook_does_not_kill_the_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One bad third-party hook must not take down startup."""
    import felix.plugins as plugins_mod
    from fastapi.testclient import TestClient
    from felix.config import Settings
    from felix.plugins import PluginRegistry
    from felix_api.app import create_app

    ran: list[str] = []

    async def _explodes(app: Any) -> None:
        ran.append("bad")
        raise RuntimeError("hook is misconfigured")

    async def _fine(app: Any) -> None:
        ran.append("good")

    registry = PluginRegistry()
    registry.register_startup_hook(_explodes)
    registry.register_startup_hook(_fine)
    monkeypatch.setattr(plugins_mod, "_registry", registry)

    app = create_app(settings=Settings(auth_mode="none", host="127.0.0.1"), plugins=[])
    # TestClient as a context manager runs the lifespan; httpx's ASGITransport
    # does not, which would make this test pass without ever invoking a hook.
    with TestClient(app) as client:
        resp = client.get("/ready")

    assert resp.status_code == 200
    # The bad hook ran, was contained, and did not prevent the next one.
    assert ran == ["bad", "good"]
