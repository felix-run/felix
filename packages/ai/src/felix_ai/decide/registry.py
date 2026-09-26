"""Decision provider registry — the open list `FELIX_DECISION_ROUTES` selects from.

Separate from the model provider registry on purpose: a decision provider answers typed
questions and has no `chat`, so a route to one from `spec.model.id` would build a client
that fails on its first turn. Two names may coincide — `workers_ai` is both — because they
share a credential in `FELIX_MODEL_PROVIDER_OPTIONS`, not an interface.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

DecisionProviderFactory = Callable[..., Any]

_providers: dict[str, DecisionProviderFactory] = {}


def register_decision_provider(name: str, factory: DecisionProviderFactory) -> None:
    _providers[name] = factory


def get_decision_provider(name: str) -> DecisionProviderFactory | None:
    return _providers.get(name)


def list_decision_providers() -> list[str]:
    return list(_providers.keys())


def reset_decision_provider_registry() -> None:
    _providers.clear()


__all__ = [
    "DecisionProviderFactory",
    "get_decision_provider",
    "list_decision_providers",
    "register_decision_provider",
    "reset_decision_provider_registry",
]
