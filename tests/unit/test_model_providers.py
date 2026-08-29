"""The provider seam, from registration through to a credential and a masked key.

Nothing exercised registration -> route resolution -> `build_one_model` before this: every
model test hands a hand-rolled double straight to a pattern, which skips the whole path a
third-party provider actually travels. These cover that path.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.config import Settings
from felix.patterns.model import (
    build_one_model,
    parse_provider_options,
    provider_factory,
    resolve_provider_config,
)
from felix.patterns.model_registry import (
    get_model_provider,
    register_model_provider,
    reset_model_provider_registry,
)
from felix_ai.providers import ANTHROPIC, OPENAI_COMPATIBLE, builtin_provider_specs
from felix_ai.types import ChatMessage, ModelChatResult, ModelRoute, TokenUsage


class _ThirdPartyClient:
    """The minimum a provider must be: an id, a route, and a turn."""

    def __init__(self, model_id: str, route: ModelRoute, base_url: str, api_key: str) -> None:
        self.model_id = model_id
        self.route = route
        self.base_url = base_url
        self.api_key = api_key

    async def chat(self, messages: list[ChatMessage], tools: list[Any], opts: Any = None) -> ModelChatResult:
        return ModelChatResult(
            message=ChatMessage(role="assistant", content=f"via {self.base_url}"),
            usage=TokenUsage(input=1, output=1),
        )


def _settings(**kw: Any) -> Settings:
    return Settings(database_url="memory://providers", object_store="memory", **kw)


@pytest.fixture(autouse=True)
def _restore_registry():
    """Each test gets the builtins back, since registration is process-wide."""
    from felix.patterns.model import register_builtin_providers

    yield
    reset_model_provider_registry()
    register_builtin_providers()


# --- a plugin provider reaches the model loop ------------------------------------------


def test_a_plugin_provider_resolves_through_model_routes() -> None:
    """The path a third-party provider travels, which nothing covered."""
    from felix.plugins import get_registry

    def factory(model_id: str, route: ModelRoute, spec: Any, settings: Settings) -> Any:
        return _ThirdPartyClient(model_id, route, "https://third.invalid/v1", "k")

    get_registry().register_model_provider("thirdparty", factory)
    assert get_model_provider("thirdparty") is not None

    settings = _settings(
        model_routes='{"tp":{"provider":"thirdparty","model":"tp-1"}}',
    )
    client = build_one_model(settings, None, "tp")
    assert client.model_id == "tp"
    assert client.route == ModelRoute(provider="thirdparty", model="tp-1")


def test_registering_the_builtins_again_keeps_a_plugin_provider() -> None:
    """Registration is additive and last-write-wins, not a reset.

    A regression guard rather than a captured bug: this held before too. It is worth
    pinning because `build_model` used to lazily bootstrap behind an
    `if not list_model_providers()` sentinel, and that sentinel is only ever correct while
    registration stays additive — implement it as `clear()` then re-register and every
    plugin provider disappears with no error. The sentinel itself is gone; `felix.patterns`
    registers the builtins at import, before anything can call `build_model`.
    """
    from felix.patterns.model import register_builtin_providers

    register_model_provider("thirdparty", lambda *a, **k: None)
    register_builtin_providers()

    assert get_model_provider("thirdparty") is not None
    assert get_model_provider("anthropic") is not None


# --- credentials ------------------------------------------------------------------------


def test_provider_options_supply_a_credential_settings_has_no_field_for() -> None:
    """`Settings` is `extra="ignore"`, so `FELIX_THIRDPARTY_API_KEY` never lands on it.
    Without an options map a registered provider could not be given a key at all."""
    from felix_ai.providers.base import ProviderSpec
    from felix_ai.wire.openai_completions import OpenAICompletionsClient

    spec = ProviderSpec(
        name="thirdparty",
        wire=OpenAICompletionsClient,
        base_url_default="https://default.invalid/v1",
    )
    settings = _settings(
        model_provider_options=(
            '{"thirdparty":{"base_url":"https://configured.invalid/v1","api_key":"sk-third-party"}}'
        )
    )
    base_url, api_key = resolve_provider_config(spec, settings)
    assert base_url == "https://configured.invalid/v1"
    assert api_key == "sk-third-party"


def test_provider_options_override_a_builtin_field() -> None:
    """Most specific source wins: the options map beats the provider's named field."""
    settings = _settings(
        anthropic_api_key="from-field",
        model_provider_options='{"anthropic":{"api_key":"from-options"}}',
    )
    _, api_key = resolve_provider_config(ANTHROPIC, settings)
    assert api_key == "from-options"


def test_a_builtin_still_reads_its_named_field() -> None:
    settings = _settings(anthropic_api_key="from-field")
    base_url, api_key = resolve_provider_config(ANTHROPIC, settings)
    assert api_key == "from-field"
    assert base_url == "https://api.anthropic.com"


@pytest.mark.parametrize("raw", ["not json", '["a"]', ""])
def test_malformed_provider_options_degrade_rather_than_fail(raw: str) -> None:
    assert parse_provider_options(_settings(model_provider_options=raw)) == {}


# --- masking ----------------------------------------------------------------------------


def test_a_provider_key_from_the_options_map_is_masked() -> None:
    """The hole this closes: a credential that is not a `Settings` attribute was never
    added to the redaction list, so it survived into tool output verbatim."""
    from felix.secrets import collected_secret_values

    settings = _settings(
        model_provider_options='{"thirdparty":{"api_key":"sk-must-be-masked","base_url":"https://x.invalid"}}'
    )
    values = collected_secret_values(settings)
    assert "sk-must-be-masked" in values
    # The endpoint is not a secret and masking it would corrupt legitimate output.
    assert "https://x.invalid" not in values


def test_every_builtin_provider_with_a_key_is_in_the_hydrate_map() -> None:
    """Derived, not hand-listed — that map also feeds output masking, so a provider missing
    from it leaks its key rather than merely failing to hydrate."""
    from felix.secrets import _HYDRATE_MAP

    for spec in builtin_provider_specs():
        if spec.api_key_config_key and spec.secret_names:
            assert spec.api_key_config_key in _HYDRATE_MAP, spec.name


# --- endpoints --------------------------------------------------------------------------


def test_an_ollama_base_url_that_already_ends_in_v1_is_not_doubled() -> None:
    """The old factory appended `/v1` unconditionally, so a configured `.../v1` became
    `/v1/v1` and every request 404'd."""
    ollama = next(s for s in OPENAI_COMPATIBLE if s.name == "ollama")
    assert ollama.resolve_base_url("http://host:11434/v1") == "http://host:11434/v1"
    assert ollama.resolve_base_url("http://host:11434") == "http://host:11434/v1"
    assert ollama.resolve_base_url(None) == "http://localhost:11434/v1"


def test_the_openai_provider_honours_a_gateway_url() -> None:
    openai = next(s for s in OPENAI_COMPATIBLE if s.name == "openai")
    settings = _settings(litellm_base_url="https://gateway.invalid")
    base_url, _ = resolve_provider_config(openai, settings)
    assert base_url == "https://gateway.invalid/v1"


def test_the_embedder_reaches_the_same_endpoint_as_the_model_client() -> None:
    """`_build_openai` read `settings.openai_base_url`, which is not a field on `Settings`
    and never was, so the embedder always went to api.openai.com no matter the gateway."""
    from felix.memory.embedder import build_embedder

    settings = _settings(
        memory_embedder="openai",
        litellm_base_url="https://gateway.invalid",
        openai_api_key="sk-embed",
    )
    embedder = build_embedder(settings)
    assert embedder._base_url == "https://gateway.invalid/v1"
    assert embedder._api_key == "sk-embed"


# --- startup validation -------------------------------------------------------------------


def test_an_unknown_provider_in_model_routes_fails_at_startup() -> None:
    """It used to surface mid-request, and only on the route actually taken — so a typo in
    a *fallback* stayed invisible until the primary was already failing."""
    settings = _settings(
        model_routes='{"typo":{"provider":"anthropc","model":"claude-sonnet-5"}}',
        auth_mode="none",
        allow_insecure=True,
        environment="development",
    )
    with pytest.raises(RuntimeError, match="anthropc"):
        settings.validate_runtime()


def test_a_known_provider_in_model_routes_passes_startup() -> None:
    settings = _settings(
        model_routes='{"ok":{"provider":"ollama","model":"llama3.2"}}',
        auth_mode="none",
        allow_insecure=True,
        environment="development",
    )
    settings.validate_runtime()


def test_provider_factory_builds_the_wire_client_the_descriptor_names() -> None:
    from felix_ai.wire.anthropic_messages import AnthropicMessagesClient

    client = provider_factory(ANTHROPIC)(
        "claude-sonnet",
        ModelRoute(provider="anthropic", model="claude-sonnet-5"),
        None,
        _settings(anthropic_api_key="k"),
    )
    assert isinstance(client, AnthropicMessagesClient)
    assert client.base_url == "https://api.anthropic.com"
