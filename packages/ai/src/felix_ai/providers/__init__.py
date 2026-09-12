"""Built-in provider descriptors."""

from __future__ import annotations

from felix_ai.providers.anthropic import ANTHROPIC
from felix_ai.providers.base import (
    CREDENTIAL_OPTION_NAMES,
    ProviderSpec,
    placeholder_names,
)
from felix_ai.providers.compat import OPENAI_COMPATIBLE


def builtin_provider_specs() -> tuple[ProviderSpec, ...]:
    """Every provider Felix ships, in registration order."""
    return (ANTHROPIC, *OPENAI_COMPATIBLE)


def provider_spec(name: str) -> ProviderSpec | None:
    """The descriptor for a provider name, or `None` for one Felix does not ship.

    `None` is the answer for a plugin-registered provider as well as an unknown one, so a
    caller reading a capability off this must treat absence as "not claimed" rather than as
    an error — which is what every default on `ProviderSpec` is already written to be.
    """
    return next((spec for spec in builtin_provider_specs() if spec.name == name), None)


__all__ = [
    "ANTHROPIC",
    "CREDENTIAL_OPTION_NAMES",
    "OPENAI_COMPATIBLE",
    "ProviderSpec",
    "builtin_provider_specs",
    "placeholder_names",
    "provider_spec",
]
