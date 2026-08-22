"""Manifest schema contract — apiVersion felix/v1."""

from __future__ import annotations

from pathlib import Path

import pytest
from felix.manifests.loader import list_bundled, load_bundled, load_manifest_file
from felix.manifests.schema import API_VERSION, MANIFEST_KIND, Manifest, assert_valid_manifest_name
from pydantic import ValidationError


def test_api_version_is_felix_v1() -> None:
    assert API_VERSION == "felix/v1"
    assert MANIFEST_KIND == "Agent"


def test_assert_valid_manifest_name() -> None:
    assert_valid_manifest_name("quick")
    with pytest.raises(ValueError):
        assert_valid_manifest_name("")
    with pytest.raises(ValueError):
        assert_valid_manifest_name("bad name")


def test_minimal_manifest_roundtrip() -> None:
    m = Manifest.model_validate(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "unit-test"},
            "spec": {"pattern": "react"},
        }
    )
    assert m.apiVersion == "felix/v1"
    assert m.metadata.name == "unit-test"
    with pytest.raises(ValidationError):
        Manifest.model_validate(
            {
                "apiVersion": "felix/v1",
                "kind": "Agent",
                "metadata": {"name": "x"},
                "spec": {"pattern": "react", "unknown_field": True},
            }
        )


def test_bundled_manifests_load() -> None:
    root = Path(__file__).resolve().parents[2]
    manifests_dir = root / "manifests"
    if not manifests_dir.is_dir():
        pytest.skip("manifests/ missing")
    names = list_bundled(bundled_dir=manifests_dir)
    for name in ("quick", "deep", "router", "oss-only", "hybrid-router", "support"):
        assert name in names, name
        m = load_bundled(name, bundled_dir=manifests_dir)
        assert m.apiVersion == "felix/v1"
        loaded = load_manifest_file(manifests_dir / f"{name}.yaml")
        assert loaded.metadata.name == name
