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
        bills_per_token=False,
    ),
)


def _compat(
    name: str,
    base_url: str,
    env_key: str,
    *,
    config_key: str | None = None,
    ensure_v1: bool = False,
    header_options: tuple[tuple[str, str], ...] = (),
) -> ProviderSpec:
    """One OpenAI-compatible provider. The whole point is that this is a row."""
    return ProviderSpec(
        name=name,
        wire=OpenAICompletionsClient,
        base_url_default=base_url,
        base_url_config_key=config_key,
        api_key_config_key=config_key and None,
        secret_names=(env_key, env_key.lower(), f"felix/{env_key.lower()}"),
        ensure_v1_suffix=ensure_v1,
        header_options=header_options,
    )


# Hosted endpoints that speak OpenAI chat-completions. Each is configured through
# FELIX_MODEL_PROVIDER_OPTIONS, e.g. {"groq": {"api_key": "..."}} — there is deliberately
# no `Settings` field per vendor, because that is the pattern that does not scale and that
# left a plugin's provider with nowhere to put a key.
_HOSTED: tuple[ProviderSpec, ...] = (
    _compat("groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    _compat("together", "https://api.together.ai/v1", "TOGETHER_API_KEY"),
    _compat("deepseek", "https://api.deepseek.com", "DEEPSEEK_API_KEY", ensure_v1=True),
    _compat("cerebras", "https://api.cerebras.ai/v1", "CEREBRAS_API_KEY"),
    _compat("fireworks", "https://api.fireworks.ai/inference/v1", "FIREWORKS_API_KEY"),
    _compat("openrouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    _compat("xai", "https://api.x.ai/v1", "XAI_API_KEY"),
    _compat("mistral", "https://api.mistral.ai/v1", "MISTRAL_API_KEY"),
    # Gemini through its OpenAI-compatible endpoint rather than a fourth wire format.
    _compat(
        "google",
        "https://generativelanguage.googleapis.com/v1beta/openai",
        "GEMINI_API_KEY",
    ),
    # Cloudflare Workers AI. The account id goes in the URL path, which is what the
    # `{account_id}` template is for — a provider needing a path variable should be a row,
    # not a special case in the factory. `gateway_id` is optional: setting it routes the
    # same request through AI Gateway for logging, caching and rate limiting, which is a
    # header rather than a different endpoint.
    #
    # This is an outbound HTTPS call to api.cloudflare.com, the same shape as any other
    # hosted provider. It is not Cloudflare *compute* — Felix still runs where the operator
    # runs it — and the no-Workers/DO/Hyperdrive invariant is unchanged.
    _compat(
        "workers_ai",
        "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1",
        "CLOUDFLARE_API_TOKEN",
        header_options=(("cf-aig-gateway-id", "gateway_id"),),
    ),
)

OPENAI_COMPATIBLE = (*OPENAI_COMPATIBLE, *_HOSTED)

__all__ = ["OPENAI_COMPATIBLE"]
