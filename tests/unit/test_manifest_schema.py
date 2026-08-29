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
    for name in ("quick", "deep", "router", "oss-only", "hybrid-router", "support", "governed"):
        assert name in names, name
        m = load_bundled(name, bundled_dir=manifests_dir)
        assert m.apiVersion == "felix/v1"
        loaded = load_manifest_file(manifests_dir / f"{name}.yaml")
        assert loaded.metadata.name == name


def test_governed_manifest_passes_production_governance() -> None:
    from felix.config import Settings
    from felix.manifests.governance import validate_governance
    from felix.manifests.loader import load_bundled

    root = Path(__file__).resolve().parents[2]
    manifests_dir = root / "manifests"
    if not manifests_dir.is_dir():
        pytest.skip("manifests/ missing")
    m = load_bundled("governed", bundled_dir=manifests_dir)
    validate_governance(m, Settings(environment="production", allow_insecure=True))
    assert m.spec.governance.pin_compile is True
    assert m.spec.command_screening.include_defaults is True
    # Outbound MCP is optional in the example (commented until secret exists).
    assert m.spec.mcp == []


def test_integration_timeouts_are_bounded_and_floored() -> None:
    """No manifest may pin a connection open past the run's own ceiling, or below 1ms.

    `timeout_ms` was unbounded on every ref that carries one, so a tenant-supplied manifest
    could ask for a 24-hour outbound call. The ceiling is derived rather than picked — a call
    that outlasts `ABSOLUTE_LIMITS["max_wall_clock_seconds"]` cannot complete inside the run
    waiting on it. The floor matters too: three of the five conversion sites floored at one
    second and two did not, so a negative value reached httpx as a deadline in the past.
    """
    from felix.manifests.schema import (
        ABSOLUTE_LIMITS,
        MAX_INTEGRATION_TIMEOUT_MS,
        MAX_INTEGRATION_TIMEOUT_S,
        A2APeerRef,
        BrowserToolRef,
        ClientToolRef,
        ContainerRef,
        McpServerRef,
        SandboxRef,
    )

    assert ABSOLUTE_LIMITS["max_wall_clock_seconds"] == MAX_INTEGRATION_TIMEOUT_S

    builders = (
        lambda ms: McpServerRef(name="a", url="https://e.com/m", timeout_ms=ms),
        lambda ms: A2APeerRef(name="p", url="https://e.com", timeout_ms=ms),
        lambda ms: ContainerRef(name="c", gateway_url="https://e.com", image="i", timeout_ms=ms),
        lambda ms: SandboxRef(name="s", binding="python:3.14-slim", timeout_ms=ms),
        lambda ms: BrowserToolRef(name="b", binding="chromium", timeout_ms=ms),
    )
    for build in builders:
        assert build(MAX_INTEGRATION_TIMEOUT_MS).timeout_ms == MAX_INTEGRATION_TIMEOUT_MS
        for bad in (MAX_INTEGRATION_TIMEOUT_MS + 1, 0, -1, -3_600_000):
            with pytest.raises(ValidationError):
                build(bad)

    # The sixth timeout in this schema, which spelled the same ceiling as a bare literal.
    assert ClientToolRef(name="t", timeout_seconds=MAX_INTEGRATION_TIMEOUT_S)
    with pytest.raises(ValidationError):
        ClientToolRef(name="t", timeout_seconds=MAX_INTEGRATION_TIMEOUT_S + 1)


def test_hold_open_knobs_are_bounded() -> None:
    """Bound every tenant-supplied field that parks a resource, not just the timeouts.

    An unanswered approval holds the request, an asyncio task and a Redis connection for its
    whole TTL, and the waiter polls once a second for the duration — so an unbounded
    `ttl_seconds` was a longer hold than any `timeout_ms` this schema caps.

    The ref lists are capped because validating one ref resolves its hostname through a
    synchronous `getaddrinfo` inside a pydantic validator, on the API event loop: list length
    multiplies a blocking call.
    """
    from felix.manifests.schema import (
        MAX_INTEGRATION_TIMEOUT_S,
        MAX_REFS,
        ApprovalRule,
        CommandScreening,
        Spec,
    )

    assert ApprovalRule(id="r", ttl_seconds=MAX_INTEGRATION_TIMEOUT_S)
    with pytest.raises(ValidationError):
        ApprovalRule(id="r", ttl_seconds=MAX_INTEGRATION_TIMEOUT_S + 1)
    with pytest.raises(ValidationError):
        CommandScreening(approval_ttl_seconds=MAX_INTEGRATION_TIMEOUT_S + 1)

    over = [{"name": f"r{i}", "url": "https://example.com/m"} for i in range(MAX_REFS + 1)]
    with pytest.raises(ValidationError):
        Spec(pattern="react", mcp_servers=over)
