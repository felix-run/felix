"""`/v1/models` reads modalities and price the way routing and metering read them (#342).

The e2e listing test runs every route on one scripted model; these pin the two places the
listing has to agree with something other than the catalog: a route's own `modalities` and a
composed vision route (as `vision_plan` decides), and a partial `spec.model.price`, which metering
merges over the catalog's rates rather than replacing them.
"""

from __future__ import annotations

import json
from typing import Any

from felix.config import Settings
from felix.manifests.loader import parse_manifest
from felix.usage.catalog import catalog_from_manifest
from felix.usage.pricing import _lookup_price

from tests.support.factories import make_settings

TEXT_ONLY = "@cf/openai/gpt-oss-120b"  # vouched `text_only` in the catalog


def _settings(**routes: dict[str, Any]) -> Settings:
    return make_settings(
        model_routes=json.dumps(routes),
    )


def _manifest(**model: Any) -> Any:
    return parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": "m"}, "spec": {"model": model}}
    )


def test_the_catalog_entry_for_the_text_only_model_is_text_only() -> None:
    settings = _settings(plain={"provider": "workers_ai", "model": TEXT_ONLY})
    assert catalog_from_manifest("m", _manifest(id="plain"), settings)["felix"]["modalities"] == ["text"]


def test_a_routes_declared_modalities_win_over_the_catalog() -> None:
    settings = _settings(
        seeing={"provider": "workers_ai", "model": TEXT_ONLY, "modalities": ["text", "image"]},
    )
    listed = catalog_from_manifest("m", _manifest(id="seeing"), settings)
    assert "image" in listed["felix"]["modalities"]


def test_a_composed_vision_route_lets_the_manifest_take_images() -> None:
    settings = _settings(
        plain={"provider": "workers_ai", "model": TEXT_ONLY},
        eyes={"provider": "anthropic", "model": "claude-sonnet-4-5"},
    )
    listed = catalog_from_manifest("m", _manifest(id="plain", vision_model="eyes"), settings)
    assert "image" in listed["felix"]["modalities"]


def test_a_partial_price_override_keeps_the_catalogs_other_rates() -> None:
    settings = _settings(sonnet={"provider": "anthropic", "model": "claude-sonnet-4-5"})
    cost = catalog_from_manifest("m", _manifest(id="sonnet", price={"input": 1.0}), settings)["felix"]["cost"]
    assert cost["inputPerMillion"] == 1.0
    assert cost["outputPerMillion"] == _lookup_price("claude-sonnet-4-5")["output"] > 0
