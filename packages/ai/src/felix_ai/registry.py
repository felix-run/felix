"""Model provider registry.

Re-exported from `felix.patterns.model_registry`, which is where it has always lived and
where the harness reads it. It is surfaced here so a provider written against `felix_ai`
alone — the whole point of the package boundary — can register itself without importing
the harness.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

ModelProviderFactory = Callable[..., Any]

_providers: dict[str, ModelProviderFactory] = {}


def register_model_provider(name: str, factory: ModelProviderFactory) -> None:
    _providers[name] = factory


def get_model_provider(name: str) -> ModelProviderFactory | None:
    return _providers.get(name)


def list_model_providers() -> list[str]:
    return list(_providers.keys())


def reset_model_provider_registry() -> None:
    _providers.clear()


__all__ = [
    "ModelProviderFactory",
    "get_model_provider",
    "list_model_providers",
    "register_model_provider",
    "reset_model_provider_registry",
]
