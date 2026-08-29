"""What a provider is: a wire format, an endpoint, and a credential.

Adding a provider used to mean a hand-written factory plus a `Settings` field plus a
`_HYDRATE_MAP` entry plus a `.env.example` block plus a README row — five edits in four
files, and the one that is easy to forget (`_HYDRATE_MAP`) is the one that also feeds
output masking, so forgetting it leaks the key into tool output rather than merely being
untidy. A descriptor collapses that to one row, and lets the harness *derive* the secret
handling instead of trusting an author to remember it.

The keys here are strings, not values: this package cannot see `Settings`. The harness
resolves them, which is also what lets a plugin supply a provider whose credential lives
somewhere `Settings` has never heard of.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from felix_ai.wire.base import HttpModelClient


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    """One provider: how to reach it, and how it is paid for.

    `base_url_config_key` and `api_key_config_key` name attributes on whatever config
    object the harness passes; either may be absent for a provider whose endpoint is fixed
    (Anthropic) or which needs no credential (a local Ollama).
    """

    name: str
    wire: type[HttpModelClient]
    base_url_default: str
    base_url_config_key: str | None = None
    api_key_config_key: str | None = None
    # A provider that takes any non-empty string, like Ollama, which authenticates nothing.
    api_key_literal: str | None = None
    # Names to try in the secrets backend, and the values to mask out of tool output.
    secret_names: tuple[str, ...] = field(default_factory=tuple)
    # Whether the endpoint is an OpenAI-style `/v1` root. Appended only when absent: the
    # original Ollama factory appended unconditionally, so an operator who set
    # FELIX_OLLAMA_BASE_URL to a URL already ending in /v1 got /v1/v1 and a 404.
    ensure_v1_suffix: bool = False

    def resolve_base_url(self, configured: str | None) -> str:
        """The endpoint for this provider, given whatever the operator configured."""
        base = (configured or "").strip() or self.base_url_default
        if self.ensure_v1_suffix and not base.rstrip("/").endswith("/v1"):
            base = base.rstrip("/") + "/v1"
        return base


__all__ = ["ProviderSpec"]
