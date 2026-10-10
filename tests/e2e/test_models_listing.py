"""`GET /v1/models` describes each manifest by the model it runs on (#342).

Everything about the model used to be looked up by the manifest's *name*, which the catalog does
not know: a manifest on the default route listed no `providerModel`, and the fallback window,
modalities and thinking levels whatever it actually ran on. Through the production app, so the
settings it resolves against are the ones the request path uses.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from felix.manifests.loader import load_manifest_file
from felix.model_catalog import entry_for
from felix.usage.catalog import supported_thinking_levels
from felix.usage.pricing import _lookup_price

from tests.e2e.conftest import DEFAULT_ROUTE, WIRE_MODEL


async def _listed(boot: Any) -> dict[str, dict[str, Any]]:
    async with boot([]) as app:
        resp = await app.client.get("/v1/models")
    assert resp.status_code == 200, resp.text
    return {m["id"]: m for m in resp.json()["data"]}


async def test_a_manifest_on_the_default_route_is_described_by_that_routes_model(boot: Any) -> None:
    served, unknown = entry_for(WIRE_MODEL), entry_for("quick")
    assert (served.context_window, served.input_modalities) != (
        unknown.context_window,
        unknown.input_modalities,
    ), "the served model must differ from the catalog's fallback, or this cannot fail"

    quick = (await _listed(boot))["quick"]["felix"]

    assert quick["providerModel"] == DEFAULT_ROUTE
    assert quick["contextWindow"] == served.context_window
    assert quick["modalities"] == list(served.input_modalities)
    assert quick["supportedThinkingLevels"] == supported_thinking_levels(WIRE_MODEL)
    manifest = load_manifest_file(Path(__file__).parents[2] / "manifests" / "quick.yaml")
    assert quick["description"] == manifest.metadata.description


async def test_a_manifest_naming_a_route_reports_that_route(boot: Any) -> None:
    listed = await _listed(boot)

    hybrid = listed["hybrid-router"]
    served = entry_for(WIRE_MODEL)

    assert hybrid["id"] == "hybrid-router", "the id stays the manifest's name"
    assert hybrid["felix"]["providerModel"] == "claude-haiku"
    # Every e2e route is the scripted one on WIRE_MODEL: read by route name, `claude-haiku`
    # would be priced and sized as the real Haiku.
    assert hybrid["felix"]["contextWindow"] == served.context_window
    assert hybrid["felix"]["cost"]["inputPerMillion"] == _lookup_price(WIRE_MODEL)["input"]
    assert _lookup_price(WIRE_MODEL)["input"] != _lookup_price("claude-haiku")["input"], "or this cannot fail"
