"""The hosted provider tier, and what makes it a table rather than modules.

Most inference endpoints speak OpenAI chat-completions, so a provider is a base URL and a
credential. The two things that are not uniform — a path variable (Cloudflare puts the
account id in the URL) and a routing header (AI Gateway) — are properties of the row.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.config import Settings
from felix.patterns.model import provider_factory, resolve_provider_config
from felix.patterns.model_registry import get_model_provider
from felix_ai.providers import builtin_provider_specs
from felix_ai.providers.base import ProviderConfigError
from felix_ai.types import ModelRoute
from felix_ai.wire.openai_completions import OpenAICompletionsClient


def _spec(name: str):
    return next(s for s in builtin_provider_specs() if s.name == name)


def _settings(**kw: Any) -> Settings:
    return Settings(database_url="memory://hosted", object_store="memory", **kw)


# The base URL and a representative wire model *are* the content of a table row, so both
# are pinned per provider. `base_url.startswith("https://")` passed with groq pointed at
# api.totally-wrong.invalid.
HOSTED_ENDPOINTS = {
    "groq": "https://api.groq.com/openai/v1",
    "together": "https://api.together.ai/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "cerebras": "https://api.cerebras.ai/v1",
    "fireworks": "https://api.fireworks.ai/inference/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "xai": "https://api.x.ai/v1",
    "mistral": "https://api.mistral.ai/v1",
    "google": "https://generativelanguage.googleapis.com/v1beta/openai",
}
HOSTED = list(HOSTED_ENDPOINTS)

# One id per provider, so the unpriced claim below is asserted about each of them rather
# than about the same three ids ten times.
REPRESENTATIVE_MODEL = {
    "groq": "llama-3.3-70b-versatile",
    "together": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
    "deepseek": "deepseek-chat",
    "cerebras": "llama3.1-8b",
    "fireworks": "accounts/fireworks/models/llama-v3p3-70b-instruct",
    "openrouter": "meta-llama/llama-3.3-70b-instruct",
    "xai": "grok-4",
    "mistral": "mistral-large-latest",
    "google": "gemini-2.5-pro",
    "workers_ai": "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
}


@pytest.mark.parametrize("name", [*HOSTED, "workers_ai"])
def test_every_hosted_provider_is_registered(name: str) -> None:
    assert get_model_provider(name) is not None


@pytest.mark.parametrize("name", HOSTED)
def test_a_hosted_provider_is_configured_entirely_from_options(name: str) -> None:
    """No `Settings` field per vendor — that is the pattern that does not scale."""
    settings = _settings(model_provider_options=f'{{"{name}":{{"api_key":"sk-test"}}}}')
    base_url, api_key, headers = resolve_provider_config(_spec(name), settings)
    assert api_key == "sk-test"
    assert base_url == HOSTED_ENDPOINTS[name]
    assert headers == {}


@pytest.mark.parametrize("name", [*HOSTED, "workers_ai"])
def test_every_hosted_provider_key_is_masked(name: str) -> None:
    """A credential that never reaches the redaction list survives into tool output."""
    from felix.secrets import collected_secret_values

    settings = _settings(
        model_provider_options=f'{{"{name}":{{"api_key":"sk-leaky-credential","account_id":"acct"}}}}'
    )
    assert "sk-leaky-credential" in collected_secret_values(settings)


def test_a_credential_is_masked_whatever_the_option_is_called() -> None:
    """Secrecy is decided by allowlisting the option names that are *not* credentials.

    Matching names containing key/token/secret/password is a denylist, and it failed open
    for exactly the third-party providers the options map exists to serve: a provider whose
    credential option is called `credential`, `authorization` or `bearer` had its value
    published verbatim in tool output. `_TRUSTED_TRANSPORTS` decides trust the same way and
    for the same reason.
    """
    from felix.secrets import collected_secret_values

    settings = _settings(
        model_provider_options=(
            '{"acme":{"credential":"cred-must-be-masked",'
            '"authorization":"Bearer must-be-masked",'
            '"bearer":"tok-must-be-masked",'
            '"api_key":"sk-must-be-masked",'
            '"base_url":"https://acme.invalid/v1",'
            '"account_id":"acct-1234567",'
            '"gateway_id":"gw-1234567"}}'
        )
    )
    masked = collected_secret_values(settings)
    for secret in (
        "cred-must-be-masked",
        "Bearer must-be-masked",
        "tok-must-be-masked",
        "sk-must-be-masked",
    ):
        assert secret in masked, secret
    # Addressing, not credentials — redacting these would corrupt legitimate tool output.
    assert "https://acme.invalid/v1" not in masked
    assert "acct-1234567" not in masked
    assert "gw-1234567" not in masked


def test_an_unknown_provider_option_is_masked_by_default() -> None:
    """The point of the allowlist: a provider Felix has never heard of, whose credential
    option is named something nobody anticipated, is still redacted."""
    from felix.secrets import collected_secret_values

    settings = _settings(model_provider_options='{"newthing":{"handshake_blob":"opaque-credential-x"}}')
    assert "opaque-credential-x" in collected_secret_values(settings)


# --- the path variable ------------------------------------------------------------------


def test_workers_ai_puts_the_account_id_in_the_path() -> None:
    settings = _settings(model_provider_options='{"workers_ai":{"api_key":"cf-token","account_id":"abc123"}}')
    base_url, api_key, headers = resolve_provider_config(_spec("workers_ai"), settings)
    assert base_url == "https://api.cloudflare.com/client/v4/accounts/abc123/ai/v1"
    assert api_key == "cf-token"
    assert headers == {}


def test_a_missing_path_variable_says_what_to_set() -> None:
    """It cannot fall back to a default — there is no account-less endpoint — so the error
    has to name the option and the setting it goes in."""
    settings = _settings(model_provider_options='{"workers_ai":{"api_key":"cf-token"}}')
    with pytest.raises(ProviderConfigError) as exc:
        resolve_provider_config(_spec("workers_ai"), settings)
    assert "account_id" in str(exc.value)
    assert "FELIX_MODEL_PROVIDER_OPTIONS" in str(exc.value)


def test_the_gateway_id_is_a_header_not_a_different_endpoint() -> None:
    """Routing through AI Gateway is opt-in on the same provider; without it the header is
    absent rather than empty, because an empty routing hint is not the same as none."""
    settings = _settings(
        model_provider_options=('{"workers_ai":{"api_key":"cf","account_id":"abc","gateway_id":"my-gw"}}')
    )
    base_url, _key, headers = resolve_provider_config(_spec("workers_ai"), settings)
    assert base_url == "https://api.cloudflare.com/client/v4/accounts/abc/ai/v1"
    assert headers == {"cf-aig-gateway-id": "my-gw"}


def test_workers_ai_builds_an_openai_client_carrying_the_header() -> None:
    settings = _settings(
        model_provider_options=('{"workers_ai":{"api_key":"cf","account_id":"abc","gateway_id":"gw"}}')
    )
    client = provider_factory(_spec("workers_ai"))(
        "cf-fast",
        ModelRoute(provider="workers_ai", model="@cf/meta/llama-3.3-70b-instruct-fp8-fast"),
        None,
        settings,
    )
    assert isinstance(client, OpenAICompletionsClient)
    sent = client._headers({"Authorization": "Bearer cf", "Content-Type": "application/json"})
    assert sent["cf-aig-gateway-id"] == "gw"
    assert sent["Authorization"] == "Bearer cf"


def test_a_provider_header_can_remove_one_the_wire_format_sets() -> None:
    """An override to the empty string drops the header — how a provider says "not that
    one" without the wire format knowing any such provider exists."""
    client = OpenAICompletionsClient(
        model_id="m",
        route=ModelRoute(provider="x", model="m"),
        settings=_settings(),
        spec=None,
        base_url="https://example.invalid/v1",
        api_key="k",
        extra_headers={"Authorization": "", "cf-aig-authorization": "Bearer gw"},
    )
    sent = client._headers({"Authorization": "Bearer k", "Content-Type": "application/json"})
    assert "Authorization" not in sent
    assert sent["cf-aig-authorization"] == "Bearer gw"
    assert sent["Content-Type"] == "application/json"


# --- honesty about what we do not know ---------------------------------------------------


@pytest.mark.parametrize("name", [*HOSTED, "workers_ai"])
def test_a_hosted_provider_ships_unpriced(name: str) -> None:
    """Deliberate. Inventing per-token rates is exactly the bug that made every unknown
    model bill at Claude Sonnet's — and Workers AI bills in neurons, not tokens, so a
    per-token rate for it would be fiction. `spec.model.price` is the supported answer,
    and a declared `max_cost_usd` on one of these is refused at compile rather than
    enforced against a number nobody chose."""
    from felix.model_catalog import is_priced

    assert not is_priced(REPRESENTATIVE_MODEL[name]), name


def test_only_providers_that_serve_embeddings_are_selectable_as_one() -> None:
    """Registering the whole OpenAI-compatible table was wrong in a way that looked right:
    several of these endpoints implement chat only, so `FELIX_MEMORY_EMBEDDER=groq` passed
    the registry-backed startup validation and failed at the first embed. The capability is
    declared on the row, not inferred from the wire format."""
    from felix.memory.embedder import list_embedder_backends
    from felix_ai.providers import builtin_provider_specs

    backends = set(list_embedder_backends())
    for spec in builtin_provider_specs():
        assert (spec.name in backends) == spec.supports_embeddings, spec.name
    assert "workers_ai" in backends
    assert "groq" not in backends


def test_a_provider_default_embedding_model_beats_the_schema_default() -> None:
    """`FELIX_MEMORY_EMBEDDING_MODEL` defaults to `bge-base-en-v1.5`, a
    sentence-transformers name — reading it unconditionally sent that string to OpenAI and
    to Google as a model id. Only an explicitly set value wins."""
    from felix.memory.embedder import _build_compat

    settings = _settings(model_provider_options='{"google":{"api_key":"k"}}')
    assert settings.memory_embedding_model == "bge-base-en-v1.5", "the schema default"
    assert _build_compat("google")(settings).model == "text-embedding-004"
    assert (
        _build_compat("workers_ai")(
            _settings(model_provider_options='{"workers_ai":{"api_key":"k","account_id":"a"}}')
        ).model
        == "@cf/baai/bge-base-en-v1.5"
    )


def test_an_explicit_embedding_model_still_wins() -> None:
    from felix.memory.embedder import _build_compat

    settings = _settings(
        memory_embedding_model="text-embedding-3-large",
        model_provider_options='{"google":{"api_key":"k"}}',
    )
    assert _build_compat("google")(settings).model == "text-embedding-3-large"
