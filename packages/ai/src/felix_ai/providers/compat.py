"""Providers that speak the OpenAI chat-completions wire format.

Most hosted inference endpoints do. For those, a provider is a base URL and the name of a
credential — not a module — so they belong in a table where adding one is a row and the
secret handling comes for free.
"""

from __future__ import annotations

from felix_ai.providers.base import ProviderSpec
from felix_ai.wire.openai_completions import OpenAICompletionsClient

_CORE: tuple[ProviderSpec, ...] = (
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
        supports_embeddings=True,
        embedding_model="text-embedding-3-small",
    ),
    ProviderSpec(
        name="ollama",
        wire=OpenAICompletionsClient,
        base_url_default="http://localhost:11434",
        base_url_config_key="ollama_base_url",
        api_key_literal="ollama",
        ensure_v1_suffix=True,
        bills_per_token=False,
        supports_embeddings=True,
        embedding_model="nomic-embed-text",
    ),
)


def _compat(
    name: str,
    base_url: str,
    *,
    ensure_v1: bool = False,
    header_options: tuple[tuple[str, str], ...] = (),
    supports_embeddings: bool = False,
    embedding_model: str = "",
) -> ProviderSpec:
    """One OpenAI-compatible provider. The whole point is that this is a row.

    These carry no `api_key_config_key` and no `secret_names` on purpose: they have no
    `Settings` field, so there is no attribute for the secrets backend to hydrate *into*.
    Their credential comes from `FELIX_MODEL_PROVIDER_OPTIONS`, where a `secret:NAME` value
    is resolved through the same backend at startup — the existing idiom for exactly this,
    already used by MCP and peer refs.

    An earlier version of this function passed `api_key_config_key=config_key and None`,
    which is the constant `None` for every input, and declared `secret_names` anyway. Since
    `_HYDRATE_MAP` is derived with `if spec.api_key_config_key and spec.secret_names`, all
    thirty of those names were inert — the descriptor promised a hydration path it did not
    have. Declaring nothing is honest; declaring something unreachable is worse than either.
    """
    return ProviderSpec(
        name=name,
        wire=OpenAICompletionsClient,
        base_url_default=base_url,
        ensure_v1_suffix=ensure_v1,
        header_options=header_options,
        supports_embeddings=supports_embeddings,
        embedding_model=embedding_model,
    )


# Hosted endpoints that speak OpenAI chat-completions. Each is configured through
# FELIX_MODEL_PROVIDER_OPTIONS, e.g. {"groq": {"api_key": "..."}} — there is deliberately
# no `Settings` field per vendor, because that is the pattern that does not scale and that
# left a plugin's provider with nowhere to put a key.
_HOSTED: tuple[ProviderSpec, ...] = (
    _compat(
        "groq",
        "https://api.groq.com/openai/v1",
    ),
    _compat(
        "together",
        "https://api.together.ai/v1",
    ),
    _compat(
        "deepseek",
        "https://api.deepseek.com",
        ensure_v1=True,
    ),
    _compat(
        "cerebras",
        "https://api.cerebras.ai/v1",
    ),
    _compat(
        "fireworks",
        "https://api.fireworks.ai/inference/v1",
    ),
    _compat(
        "openrouter",
        "https://openrouter.ai/api/v1",
    ),
    _compat(
        "xai",
        "https://api.x.ai/v1",
    ),
    _compat(
        "mistral",
        "https://api.mistral.ai/v1",
    ),
    # Gemini through its OpenAI-compatible endpoint rather than a fourth wire format.
    _compat(
        "google",
        "https://generativelanguage.googleapis.com/v1beta/openai",
        supports_embeddings=True,
        embedding_model="text-embedding-004",
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
        header_options=(("cf-aig-gateway-id", "gateway_id"),),
        supports_embeddings=True,
        embedding_model="@cf/baai/bge-base-en-v1.5",
    ),
)

OPENAI_COMPATIBLE: tuple[ProviderSpec, ...] = (*_CORE, *_HOSTED)

__all__ = ["OPENAI_COMPATIBLE"]
