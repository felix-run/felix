"""A plugin's own agents: `PluginRegistry.register_manifest_dir`, read by the bundled loader.

An example agent built on a plugin's tools cannot live in `manifests/`: every install serves that
directory, and one without the plugin would list the agent and fail to compile it on every
request (`contributor`'s shape, `test_manifests_dir.py`). So the plugin ships the agent and
registers where; the loader serves it after `manifests/` and `FELIX_MANIFESTS_DIR`, and refuses a
name two places hold rather than picking one.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from felix.config import get_settings
from felix.manifests.loader import clear_bundled_cache, list_bundled, load_bundled, shadowed_names
from felix.manifests.resolver import clear_resolver_cache
from felix.plugins import get_registry

from tests.support.optional_deps import require_optional

AGENT = """\
apiVersion: felix/v1
kind: Agent
metadata:
  name: {name}
  version: 1.0.0
  description: A plugin's example agent.
spec:
  pattern: react
  system_prompt:
    inline: You help.
  tools: [calculator]
"""


@pytest.fixture
def plugin_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A directory of manifests registered as a plugin's, in the process-wide registry."""

    def register(*names: str) -> Path:
        root = tmp_path / "plugin-manifests"
        root.mkdir(exist_ok=True)
        for name in names:
            (root / f"{name}.yaml").write_text(AGENT.format(name=name), encoding="utf-8")
        monkeypatch.setattr(get_registry(), "_manifest_dirs", [*get_registry().manifest_dirs(), root])
        clear_bundled_cache()
        clear_resolver_cache()
        return root

    yield register
    clear_bundled_cache()
    clear_resolver_cache()


def test_a_plugins_agent_is_listed_and_loaded_beside_the_bundled_ones(plugin_dir) -> None:
    plugin_dir("gadget")
    assert {"gadget", "quick"} <= set(list_bundled())
    assert load_bundled("gadget").metadata.name == "gadget"


def test_a_plugins_agent_named_like_a_bundled_one_is_refused_not_preferred(plugin_dir) -> None:
    plugin_dir("quick")
    assert "quick" in shadowed_names()
    with pytest.raises(ValueError, match="more than one"):
        load_bundled("quick")


def test_a_name_in_the_manifests_dir_and_a_plugins_is_refused(
    plugin_dir, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    extra = tmp_path / "extra"
    extra.mkdir()
    (extra / "gadget.yaml").write_text(AGENT.format(name="gadget"), encoding="utf-8")
    monkeypatch.setenv("FELIX_MANIFESTS_DIR", str(extra))
    get_settings.cache_clear()
    try:
        plugin_dir("gadget")
        assert shadowed_names() == ["gadget"]
        with pytest.raises(ValueError, match="more than one"):
            load_bundled("gadget")
    finally:
        monkeypatch.delenv("FELIX_MANIFESTS_DIR")
        get_settings.cache_clear()


def test_without_the_plugin_its_agents_are_not_offered(monkeypatch: pytest.MonkeyPatch) -> None:
    # Loaded first: the loader loads plugins itself, which would register the directory again.
    monkeypatch.setattr(get_registry(), "_loaded", True)
    monkeypatch.setattr(get_registry(), "_manifest_dirs", [])
    clear_bundled_cache()
    assert {"concierge", "travel", "markets", "inbox"}.isdisjoint(list_bundled())
    with pytest.raises(FileNotFoundError):
        load_bundled("concierge")


def test_the_connectors_plugin_registers_its_agents_by_the_production_entry_point() -> None:
    require_optional("felix_connectors", "connectors")
    from importlib.metadata import entry_points

    from felix.plugins import PluginRegistry

    (ep,) = [e for e in entry_points(group="felix.plugins") if e.name == "connectors"]
    registry = PluginRegistry()
    ep.load()(registry)
    (root,) = registry.manifest_dirs()
    assert {p.stem for p in root.glob("*.yaml")} == {"concierge", "travel", "markets", "inbox"}
    assert {"concierge", "travel", "markets", "inbox"} <= set(list_bundled())


def test_a_plugins_directory_is_absolute_and_kept_once(tmp_path: Path) -> None:
    from felix.plugins import PluginRegistry

    registry = PluginRegistry()
    with pytest.raises(ValueError, match="absolute"):
        registry.register_manifest_dir("manifests")
    registry.register_manifest_dir(tmp_path)
    registry.register_manifest_dir(tmp_path / ".")
    assert registry.manifest_dirs() == [tmp_path.resolve()]


def test_a_plugin_directory_inside_a_bundled_one_is_ignored_not_doubled(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from felix.manifests.loader import _default_bundled_dir

    bundled = _default_bundled_dir().resolve()
    monkeypatch.setattr(get_registry(), "_loaded", True)
    monkeypatch.setattr(get_registry(), "_manifest_dirs", [bundled])
    clear_bundled_cache()
    assert shadowed_names() == []
    assert load_bundled("quick").metadata.name == "quick"
    assert "inside a bundled one" in caplog.text


def test_a_refusal_names_every_directory_holding_the_name(plugin_dir) -> None:
    root = plugin_dir("quick")
    with pytest.raises(ValueError) as refused:
        load_bundled("quick")
    assert str(root.resolve()) in str(refused.value)
    assert "manifests" in str(refused.value).split("(")[1]
