"""`spec.extensions` — the one relaxation of the manifest schema's `extra="forbid"`.

Without it a plugin that registers a pattern or a tool had no way to be configured
from a manifest at all: every unknown key was a validation error.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.config import Settings
from felix.manifests.builder import BuildDeps, build_agent
from felix.manifests.schema import Manifest
from felix.patterns.registry import register_pattern, reset_pattern_registry
from felix.tools.provider import InMemoryToolProvider
from pydantic import ValidationError


def _manifest(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "apiVersion": "felix/v1",
        "kind": "Agent",
        "metadata": {"name": "ext-probe"},
        "spec": spec,
    }


@pytest.fixture
def _restore_patterns() -> Any:
    from felix.patterns.registry import _patterns

    saved = dict(_patterns)
    yield
    reset_pattern_registry()
    _patterns.update(saved)


@pytest.mark.asyncio
async def test_extensions_reach_the_pattern_build_context(_restore_patterns: Any) -> None:
    seen: dict[str, Any] = {}

    async def _capture(ctx: dict[str, Any]) -> Any:
        seen.update(ctx)

        class _Agent:
            tools: list[Any] = []

        return _Agent()

    register_pattern("ext-probe-pattern", _capture)
    settings = Settings()

    await build_agent(
        _manifest(
            {
                "pattern": "ext-probe-pattern",
                "extensions": {"acme-billing": {"plan": "pro", "seats": 5}},
            }
        ),
        deps=BuildDeps(tools=InMemoryToolProvider(), settings=settings, tenant_id="t"),
        settings=settings,
    )

    assert seen["extensions"] == {"acme-billing": {"plan": "pro", "seats": 5}}


def test_extensions_defaults_to_empty_and_is_namespaced() -> None:
    m = Manifest.model_validate(_manifest({"pattern": "react"}))
    assert m.spec.extensions == {}

    m2 = Manifest.model_validate(
        _manifest({"pattern": "react", "extensions": {"a": {"x": 1}, "b": {"y": 2}}})
    )
    assert set(m2.spec.extensions) == {"a", "b"}


def test_an_unknown_top_level_spec_key_is_still_rejected() -> None:
    """The relaxation must be confined to `extensions` — `extra="forbid"` elsewhere."""
    with pytest.raises(ValidationError, match="whatever"):
        Manifest.model_validate(_manifest({"pattern": "react", "whatever": {"x": 1}}))


def test_an_unknown_key_inside_a_governance_block_is_still_rejected() -> None:
    with pytest.raises(ValidationError):
        Manifest.model_validate(_manifest({"pattern": "react", "limits": {"nope": 1}}))
