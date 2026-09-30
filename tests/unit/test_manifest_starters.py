"""`metadata.starters`: the prompts a client offers on an empty thread, listed by `/v1/models`.

They lived in chat-ui as a table keyed by manifest name, so a renamed or newly published
manifest silently got a generic pair, and the terminal client had none at all. On the manifest,
every client reads the same prompts from the one listing it already fetches.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from felix.manifests.loader import ManifestParseError, parse_manifest
from felix.usage.catalog import catalog_from_manifest

REPO = Path(__file__).resolve().parents[2]


def _manifest(**metadata: Any) -> Any:
    return parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "listed", **metadata},
            "spec": {"model": {"id": "claude-opus-5"}},
        }
    )


def test_the_listing_carries_declared_starters_in_order() -> None:
    starters = [
        {"title": "List files", "prompt": "List the top-level files."},
        {"title": "Find TODOs", "prompt": "Search for TODO."},
    ]
    entry = catalog_from_manifest("listed", _manifest(starters=starters))
    assert entry["felix"]["starters"] == starters


def test_no_starters_is_an_empty_list_not_a_missing_key() -> None:
    """Present-and-empty is how a client tells this harness from one that predates the field."""
    assert catalog_from_manifest("listed", _manifest())["felix"]["starters"] == []
    assert catalog_from_manifest("unresolved", None)["felix"]["starters"] == []


@pytest.mark.parametrize(
    "starter",
    [
        {"title": "", "prompt": "x"},
        {"title": "x", "prompt": ""},
        {"title": "x" * 61, "prompt": "x"},
        {"title": "x", "prompt": "x", "icon": "star"},
    ],
)
def test_a_malformed_starter_is_refused(starter: dict[str, str]) -> None:
    with pytest.raises(ManifestParseError):
        _manifest(starters=[starter])


def test_more_than_eight_is_refused() -> None:
    with pytest.raises(ManifestParseError):
        _manifest(starters=[{"title": f"t{i}", "prompt": "p"} for i in range(9)])


@pytest.mark.parametrize("name", ["cowork", "quick", "deep", "support", "oss-only"])
def test_the_bundled_agents_that_had_client_side_starters_declare_them(name: str) -> None:
    import yaml

    raw = yaml.safe_load((REPO / "manifests" / f"{name}.yaml").read_text())
    assert parse_manifest(raw).metadata.starters


def test_the_listing_carries_a_declared_greeting() -> None:
    entry = catalog_from_manifest("listed", _manifest(greeting={"headline": "What should we fix?"}))
    assert entry["felix"]["greeting"] == {"headline": "What should we fix?", "subtitle": None}
    both = {"headline": "Hi", "subtitle": "One line."}
    assert catalog_from_manifest("listed", _manifest(greeting=both))["felix"]["greeting"] == both


def test_no_greeting_is_null_so_the_client_keeps_its_own() -> None:
    assert catalog_from_manifest("listed", _manifest())["felix"]["greeting"] is None
    assert catalog_from_manifest("unresolved", None)["felix"]["greeting"] is None


@pytest.mark.parametrize(
    "greeting",
    [
        {"subtitle": "no headline"},
        {"headline": ""},
        {"headline": "x" * 81},
        {"headline": "x", "subtitle": ""},
        {"headline": "x", "emoji": "wave"},
    ],
)
def test_a_malformed_greeting_is_refused(greeting: dict[str, str]) -> None:
    with pytest.raises(ManifestParseError):
        _manifest(greeting=greeting)
