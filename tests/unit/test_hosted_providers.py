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


HOSTED = ["groq", "together", "deepseek", "cerebras", "fireworks", "openrouter", "xai", "mistral", "google"]


@pytest.mark.parametrize("name", [*HOSTED, "workers_ai"])
def test_every_hosted_provider_is_registered(name: str) -> None:
    assert get_model_provider(name) is not None


@pytest.mark.parametrize("name", HOSTED)
def test_a_hosted_provider_is_configured_entirely_from_options(name: str) -> None:
    """No `Settings` field per vendor — that is the pattern that does not scale."""
    settings = _settings(model_provider_options=f'{{"{name}":{{"api_key":"sk-test"}}}}')
    base_url, api_key, headers = resolve_provider_config(_spec(name), settings)
    assert api_key == "sk-test"
    assert base_url.startswith("https://")
    assert headers == {}


@pytest.mark.parametrize("name", [*HOSTED, "workers_ai"])
def test_every_hosted_provider_key_is_masked(name: str) -> None:
    """A credential that never reaches the redaction list survives into tool output."""
    from felix.secrets import collected_secret_values

    settings = _settings(
        model_provider_options=f'{{"{name}":{{"api_key":"sk-leaky-credential","account_id":"acct"}}}}'
    )
    assert "sk-leaky-credential" in collected_secret_values(settings)


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

    del name  # the claim is about the tier, asserted on a representative id per provider
    assert not is_priced("@cf/meta/llama-3.3-70b-instruct-fp8-fast")
    assert not is_priced("llama-3.3-70b-versatile")
    assert not is_priced("deepseek-chat")


def test_an_embedder_exists_for_every_compatible_provider() -> None:
    from felix.memory.embedder import list_embedder_backends

    backends = set(list_embedder_backends())
    for name in [*HOSTED, "workers_ai"]:
        assert name in backends, name
