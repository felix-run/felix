"""Drive the reference plugin in `examples/felix-plugin-example/`.

It is 150 lines of executable documentation that nothing executed: not a workspace
member, not installed, not type-checked, not imported by any test. It is also the
only place holding the real entry-point metadata, which `test_plugin_entry_point`'s
fakes cannot check. If it rots, every third party starts from broken instructions.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "felix-plugin-example"


@pytest.fixture
def example_register() -> Any:
    """Import the example package off disk without installing it."""
    src = str(EXAMPLE / "src")
    added = src not in sys.path
    if added:
        sys.path.insert(0, src)
    try:
        import felix_plugin_example

        yield felix_plugin_example.register
    finally:
        if added:
            sys.path.remove(src)
        sys.modules.pop("felix_plugin_example", None)


@pytest.fixture(autouse=True)
def _restore_registries() -> Any:
    """The example registers into process-wide registries; put them all back."""
    from felix import storage as storage_mod
    from felix.patterns import registry as pattern_reg
    from felix.session import strategies as strategies_mod

    saved = (
        dict(pattern_reg._patterns),
        dict(strategies_mod._strategies),
        dict(storage_mod._backends),
    )
    yield
    for live, snapshot in zip(
        (pattern_reg._patterns, strategies_mod._strategies, storage_mod._backends),
        saved,
        strict=True,
    ):
        live.clear()
        live.update(snapshot)


def test_the_entry_point_declaration_is_valid() -> None:
    """A typo here is invisible to every other test — the fakes supply their own."""
    meta = tomllib.loads((EXAMPLE / "pyproject.toml").read_text(encoding="utf-8"))
    entry_points = meta["project"]["entry-points"]["felix.plugins"]

    assert entry_points, "no felix.plugins entry point declared"
    module, _, attr = next(iter(entry_points.values())).partition(":")
    assert module == "felix_plugin_example"
    assert attr == "register"
    assert (EXAMPLE / "src" / module / "__init__.py").is_file()


def test_register_wires_every_seam_it_claims(example_register: Any) -> None:
    from felix.patterns.registry import list_patterns
    from felix.plugins import PluginRegistry
    from felix.session.strategies import list_session_strategies
    from felix.storage import list_object_stores

    registry = PluginRegistry()
    example_register(registry)

    assert [p.name for p in registry.plugins] == ["example"]
    assert "example-echo" in list_patterns()
    assert "example-null" in list_object_stores()
    assert "example-pairs" in list_session_strategies()


def test_the_example_tool_is_registered_and_runs(example_register: Any) -> None:
    import asyncio

    from felix.plugins import PluginRegistry
    from felix.tools.provider import InMemoryToolProvider

    registry = PluginRegistry()
    example_register(registry)
    provider = InMemoryToolProvider()
    registry.plugins[0].register_tools(provider.register)

    assert provider.has("example__greet")
    out = asyncio.run(provider.get("example__greet").executor.execute({"name": "ada"}, None))
    assert "ada" in (out if isinstance(out, str) else out.content)


def test_the_example_route_mounts(example_register: Any) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from felix.plugins import PluginRegistry

    registry = PluginRegistry()
    example_register(registry)
    app = FastAPI()
    registry.plugins[0].routes(app, tools=None)

    body = TestClient(app).get("/example/ping").json()
    assert body == {"plugin": "example", "status": "ok"}


def test_the_example_backends_build(example_register: Any) -> None:
    from felix.config import Settings
    from felix.plugins import PluginRegistry
    from felix.session.strategies import get_session_strategy
    from felix.storage import build_object_store

    example_register(PluginRegistry())

    # Both are selected by ordinary config, which is the point of the seam.
    assert build_object_store(Settings(object_store="example-null")) is not None
    # `example-pairs:3` means three pairs, i.e. a six-turn window.
    strategy = get_session_strategy("example-pairs:3")
    assert getattr(strategy, "max_turns", None) == 6
