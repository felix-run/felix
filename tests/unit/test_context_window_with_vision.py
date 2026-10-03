"""The window a thread is compacted against, when a vision route may answer some of its turns.

Compaction sized the window from the primary route alone. With a vision route composed
(`spec.model.vision_model`), every call carrying an image goes to that route instead -- and once
an image is in a thread, every later call carries it. A vision model with a smaller window than
the primary was then handed a history compacted for the larger one, and the provider refused it.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from felix.config import Settings


def _settings(**routes: dict[str, Any]) -> Settings:
    return Settings(model_routes=json.dumps(routes), default_model_id="cheap", default_vision_model_id="")


TEXT_ONLY_128K = {"provider": "openai", "model": "@cf/openai/gpt-oss-120b"}
SEEING_24K = {
    "provider": "openai",
    "model": "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
    "modalities": ["text", "image"],
}
SEEING_262K = {"provider": "openai", "model": "@cf/moonshotai/kimi-k2.6"}


def _manifest(**model: Any) -> Any:
    return SimpleNamespace(
        spec=SimpleNamespace(model=SimpleNamespace(**{"id": None, "vision_model": None, **model}))
    )


def _window(manifest: Any, settings: Settings, declared: int | None = None) -> int:
    from felix.config import get_settings
    from felix.runtime import _context_window_for_manifest

    get_settings.cache_clear()
    return _context_window_for_manifest(manifest, SimpleNamespace(context_window_tokens=declared), settings)


@pytest.fixture(autouse=True)
def _routes_from_settings(monkeypatch: pytest.MonkeyPatch) -> Any:
    """`parse_model_routes()` reads the process settings; point them at each test's routes."""
    yield
    from felix.config import get_settings

    get_settings.cache_clear()


def test_a_smaller_vision_window_is_the_one_compacted_against(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(cheap=TEXT_ONLY_128K, seer=SEEING_24K)
    monkeypatch.setenv("FELIX_MODEL_ROUTES", settings.model_routes)
    assert _window(_manifest(vision_model="seer"), settings) == 24_000


def test_the_default_vision_route_counts_too(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(cheap=TEXT_ONLY_128K, seer=SEEING_24K)
    settings.default_vision_model_id = "seer"
    monkeypatch.setenv("FELIX_MODEL_ROUTES", settings.model_routes)
    assert _window(_manifest(), settings) == 24_000


def test_a_larger_vision_window_leaves_the_primarys(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(cheap=TEXT_ONLY_128K, seer=SEEING_262K)
    monkeypatch.setenv("FELIX_MODEL_ROUTES", settings.model_routes)
    assert _window(_manifest(vision_model="seer"), settings) == 128_000


def test_no_vision_route_composed_means_the_primarys_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """A primary that can see, or one the catalog cannot vouch for, is never rerouted."""
    settings = _settings(cheap=SEEING_262K, seer=SEEING_24K)
    monkeypatch.setenv("FELIX_MODEL_ROUTES", settings.model_routes)
    assert _window(_manifest(vision_model="seer"), settings) == 262_144


def test_a_declared_window_still_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(cheap=TEXT_ONLY_128K, seer=SEEING_24K)
    monkeypatch.setenv("FELIX_MODEL_ROUTES", settings.model_routes)
    assert _window(_manifest(vision_model="seer"), settings, declared=50_000) == 50_000
