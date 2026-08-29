"""Built-in provider descriptors."""

from __future__ import annotations

from felix_ai.providers.anthropic import ANTHROPIC
from felix_ai.providers.base import ProviderSpec
from felix_ai.providers.compat import OPENAI_COMPATIBLE


def builtin_provider_specs() -> tuple[ProviderSpec, ...]:
    """Every provider Felix ships, in registration order."""
    return (ANTHROPIC, *OPENAI_COMPATIBLE)


__all__ = ["ANTHROPIC", "OPENAI_COMPATIBLE", "ProviderSpec", "builtin_provider_specs"]
