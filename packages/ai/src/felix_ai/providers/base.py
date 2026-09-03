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

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from felix_ai.wire.base import HttpModelClient


class ProviderConfigError(ValueError):
    """A provider is registered but cannot be addressed with what it was given."""


_PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


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
    # Headers beyond auth, as (header name, option key). The header is sent only when the
    # option is set, which is how one provider covers both "direct" and "routed through a
    # gateway" without being two providers.
    header_options: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    # Whether tokens cost money here. False for a local runtime, where spend is trivially
    # zero and a declared `limits.max_cost_usd` is therefore enforceable without rates.
    # This cannot be read off the model: Llama runs on a laptop and is also sold by four
    # hosted providers, and the catalog matches model ids by substring.
    bills_per_token: bool = True
    # Whether this endpoint serves `/embeddings` as well as `/chat/completions`. Not an
    # inference from the wire format: several hosted providers implement chat only, and
    # registering them as selectable embedders made `FELIX_MEMORY_EMBEDDER=groq` pass
    # startup validation and fail at the first embed instead.
    supports_embeddings: bool = False
    # The model to embed with when the operator sets no `FELIX_MEMORY_EMBEDDING_MODEL`.
    embedding_model: str = ""

    def placeholders(self, base_url: str | None = None) -> tuple[str, ...]:
        """Option names an endpoint template needs, e.g. `account_id`."""
        return tuple(dict.fromkeys(_PLACEHOLDER.findall(base_url or self.base_url_default)))

    def resolve_base_url(self, configured: str | None, options: Mapping[str, str] | None = None) -> str:
        """The endpoint for this provider, given whatever the operator configured.

        The template may carry `{option}` placeholders filled from the provider's options —
        Cloudflare puts the account id in the URL path, and several others put a region or
        a project there. Templating keeps that a property of the row rather than a special
        case in the factory.
        """
        base = (configured or "").strip() or self.base_url_default
        opts = options or {}
        missing = [name for name in self.placeholders(base) if not opts.get(name)]
        if missing:
            raise ProviderConfigError(
                f"provider {self.name!r} needs {', '.join(sorted(set(missing)))} — set it in "
                f'FELIX_MODEL_PROVIDER_OPTIONS, e.g. {{"{self.name}": {{"{missing[0]}": "..."}}}}'
            )
        base = _PLACEHOLDER.sub(lambda m: opts[m.group(1)], base)
        if self.ensure_v1_suffix and not base.rstrip("/").endswith("/v1"):
            base = base.rstrip("/") + "/v1"
        return base

    def addressing_option_names(self, configured_base_url: str | None = None) -> frozenset[str]:
        """Option names this provider consumes as *addressing* rather than as a credential.

        The authority for this is `resolve_base_url` and `resolve_headers` right here, which
        is why it lives beside them: `felix.secrets` needs the same answer to decide what to
        redact, and re-deriving it there meant the two could disagree — and did, because a
        placeholder in an operator-*configured* base URL is not in the default template.

        Pass the configured URL when there is one, or a `{region}`-style gateway template
        has its region masked out of every tool result.
        """
        return frozenset(
            {
                "base_url",
                *self.placeholders(configured_base_url or self.base_url_default),
                *(key for _header, key in self.header_options),
            }
        )

    def resolve_headers(self, options: Mapping[str, str] | None = None) -> dict[str, str]:
        """Provider headers for this configuration; absent options contribute nothing."""
        opts = options or {}
        return {header: opts[key] for header, key in self.header_options if opts.get(key)}


def placeholder_names(base_url: str | None) -> frozenset[str]:
    """Placeholders in a URL template, for a provider with no descriptor to ask.

    A plugin registers a bare factory, not a `ProviderSpec`, so there is nothing to consult
    when deciding whether one of its options is addressing. What it templates into its own
    endpoint is still derivable from the endpoint.
    """
    return frozenset(_PLACEHOLDER.findall(base_url or ""))


__all__ = ["ProviderConfigError", "ProviderSpec", "placeholder_names"]
