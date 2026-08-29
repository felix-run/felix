"""Model provider registry — re-exported from `felix_ai.registry`.

There must be exactly one registry: a provider written against `felix_ai` and one written
against `felix` have to land in the same dict, or `build_one_model` finds only half of
them. This module is the harness-side name for it, kept because README, the reference
plugin and `PluginRegistry.register_model_provider` all point here.
"""

from __future__ import annotations

from felix_ai.registry import (
    ModelProviderFactory,
    get_model_provider,
    list_model_providers,
    register_model_provider,
    reset_model_provider_registry,
)

__all__ = [
    "ModelProviderFactory",
    "get_model_provider",
    "list_model_providers",
    "register_model_provider",
    "reset_model_provider_registry",
]
