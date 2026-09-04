"""The Anthropic provider — a fixed endpoint on the messages wire format."""

from __future__ import annotations

from felix_ai.providers.base import ProviderSpec
from felix_ai.wire.anthropic_messages import AnthropicMessagesClient

ANTHROPIC = ProviderSpec(
    name="anthropic",
    wire=AnthropicMessagesClient,
    base_url_default="https://api.anthropic.com",
    api_key_config_key="anthropic_api_key",
    secret_names=(
        "ANTHROPIC_API_KEY",
        "anthropic_api_key",
        "felix-anthropic-api-key",
        "felix/anthropic_api_key",
    ),
)

__all__ = ["ANTHROPIC"]
