"""Providers that speak the OpenAI chat-completions wire format.

Most hosted inference endpoints do. For those, a provider is a base URL and the name of a
credential — not a module — so they belong in a table where adding one is a row and the
secret handling comes for free.
"""

from __future__ import annotations

from felix_ai.providers.base import ProviderSpec
from felix_ai.wire.openai_completions import OpenAICompletionsClient

OPENAI_COMPATIBLE: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        name="openai",
        wire=OpenAICompletionsClient,
        base_url_default="https://api.openai.com/v1",
        # Spelled `litellm_base_url` for historical reasons: pointing the OpenAI provider at
        # a gateway is how Felix reached everything it had no provider for.
        base_url_config_key="litellm_base_url",
        api_key_config_key="openai_api_key",
        secret_names=("OPENAI_API_KEY", "openai_api_key", "felix/openai_api_key"),
        ensure_v1_suffix=True,
    ),
    ProviderSpec(
        name="ollama",
        wire=OpenAICompletionsClient,
        base_url_default="http://localhost:11434",
        base_url_config_key="ollama_base_url",
        api_key_literal="ollama",
        ensure_v1_suffix=True,
    ),
)

__all__ = ["OPENAI_COMPATIBLE"]
