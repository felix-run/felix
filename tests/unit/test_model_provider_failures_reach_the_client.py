"""A model provider that is down or unconfigured must say so, not answer `internal error`.

Found in a real run: `oss-only` with no local model listening, and a `workers_ai` route with no
`account_id`, both reached the chat UI as `internal error (request …)`. The first was a bare
`httpx.ConnectError` escaping the wire layer; the second a `ProviderConfigError` whose message
was written for an operator but was not on the relay list. Neither is a Felix fault, and the
generic message sent the operator looking for one.

The real app, the real compiler and the real wire client — only the endpoint is dead.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from felix_ai.providers.base import ProviderConfigError
from felix_ai.wire.transport import ModelUnreachableError
from httpx import ASGITransport, AsyncClient

# Port 9 is discard: nothing listens, so the connect is refused at once.
DEAD = "http://127.0.0.1:9/v1"


def _settings(**overrides: object):
    from felix.config import Settings

    base = {
        "database_url": "memory://provider-failures",
        "object_store": "memory",
        "allow_insecure": True,
        "auth_mode": "none",
        "host": "127.0.0.1",
        "environment": "development",
        "redis_url": "",
    }
    return Settings(**{**base, **overrides})


def test_an_unreachable_provider_is_a_typed_gateway_error() -> None:
    from felix.patterns.model import ModelGatewayError
    from felix.patterns.model_composites import _is_provider_error
    from felix_api.errors import client_safe_message

    refused = ModelUnreachableError("workers_ai", httpx.ConnectError("refused http://10.0.0.7:8000"))
    assert isinstance(refused, ModelGatewayError)
    assert refused.status == 503
    assert client_safe_message(refused) == "workers_ai provider unreachable (ConnectError)"
    # The endpoint is internal topology: logged, never relayed.
    assert "10.0.0.7" not in client_safe_message(refused)
    assert "10.0.0.7" in refused.body
    # A fallback chain advances past a dead provider instead of failing the run.
    assert _is_provider_error(refused)

    assert ModelUnreachableError("workers_ai", httpx.ReadTimeout("slow")).status == 504


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """`post_with_retry` retries a refused connect twice; the sleeps are not under test."""
    from felix_ai.wire import transport

    monkeypatch.setattr(transport, "_BASE_BACKOFF_S", 0.0)


def _failing_send(monkeypatch: pytest.MonkeyPatch, outcome: Exception | int) -> None:
    """Every outbound request raises `outcome`, or is answered with that status."""

    async def send(self: httpx.AsyncClient, request: httpx.Request, **_: object) -> httpx.Response:
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(outcome, request=request, text='{"error":"upstream"}')

    monkeypatch.setattr(httpx.AsyncClient, "send", send)


# A configured base_url replaces the account-id template, so no account_id is needed.
_DEAD_WORKERS_AI = json.dumps({"workers_ai": {"base_url": DEAD, "api_key": "cf"}})


def _dead_client():
    from felix.patterns.model import build_one_model

    settings = _settings(model_provider_options=_DEAD_WORKERS_AI)
    return build_one_model(settings, None, "glm-5.3-cf")


async def _drain(client: object, entry: str) -> None:
    from felix_ai.types import ChatMessage

    messages = [ChatMessage(role="user", content="hi")]
    if entry == "chat":
        await client.chat(messages, [])  # type: ignore[attr-defined]
        return
    async for _ in getattr(client, entry)(messages, []):
        pass


@pytest.mark.parametrize("entry", ["chat", "stream_turn", "stream"])
async def test_every_entry_point_types_a_refused_connection(entry: str) -> None:
    """Against the real socket: port 9 refuses, nothing is stubbed."""
    with pytest.raises(ModelUnreachableError) as caught:
        await _drain(_dead_client(), entry)
    assert caught.value.label == "workers_ai"
    assert caught.value.status == 503


async def test_a_timeout_out_of_the_client_is_a_504(monkeypatch: pytest.MonkeyPatch) -> None:
    _failing_send(monkeypatch, httpx.ReadTimeout("slow"))
    with pytest.raises(ModelUnreachableError) as caught:
        await _drain(_dead_client(), "chat")
    assert caught.value.status == 504


@pytest.mark.parametrize("entry", ["chat", "stream_turn"])
async def test_an_http_error_names_the_routed_provider(monkeypatch: pytest.MonkeyPatch, entry: str) -> None:
    """Not the wire format: a Workers AI 404 read `openai provider returned HTTP 404`."""
    from felix.patterns.model import ModelGatewayError

    _failing_send(monkeypatch, 400)
    with pytest.raises(ModelGatewayError) as caught:
        await _drain(_dead_client(), entry)
    assert not isinstance(caught.value, ModelUnreachableError)
    assert str(caught.value) == "workers_ai provider returned HTTP 400"


@pytest.mark.parametrize("entry", ["chat", "stream_turn"])
async def test_the_anthropic_wire_names_the_routed_provider_too(
    monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    from felix.patterns.model import ModelGatewayError
    from felix_ai.types import ModelRoute
    from felix_ai.wire.anthropic_messages import AnthropicMessagesClient

    _failing_send(monkeypatch, 400)
    client = AnthropicMessagesClient(
        model_id="gw",
        route=ModelRoute(provider="my-gateway", model="claude-sonnet-5"),
        settings=_settings(),
        spec=None,
        base_url="https://gateway.invalid",
        api_key="x",
    )
    with pytest.raises(ModelGatewayError) as caught:
        await _drain(client, entry)
    assert str(caught.value) == "my-gateway provider returned HTTP 400"


async def test_the_stream_names_the_dead_provider() -> None:
    from felix_api.app import create_app

    settings = _settings(model_provider_options=_DEAD_WORKERS_AI)
    app = create_app(settings=settings, plugins=[])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", timeout=30) as client:
        resp = await client.post(
            "/chat/stream",
            json={"manifest": "oss-only", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 200
    assert "workers_ai provider unreachable (ConnectError)" in resp.text
    assert "internal error" not in resp.text
    assert "127.0.0.1:9" not in resp.text


def test_a_missing_provider_option_is_relayed() -> None:
    from felix_api.errors import client_safe_message

    exc = ProviderConfigError(
        "provider 'workers_ai' needs account_id — set it in FELIX_MODEL_PROVIDER_OPTIONS"
    )
    assert client_safe_message(exc) == str(exc)


async def test_an_unconfigured_provider_answers_503_naming_the_option() -> None:
    from felix_api.app import create_app

    settings = _settings(
        default_model_id="cf",
        model_routes=json.dumps({"cf": {"provider": "workers_ai", "model": "@cf/meta/llama-3.3-70b"}}),
    )
    app = create_app(settings=settings, plugins=[])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", timeout=30) as client:
        plain = await client.post(
            "/chat", json={"manifest": "quick", "messages": [{"role": "user", "content": "hi"}]}
        )
        streamed = await client.post(
            "/chat/stream", json={"manifest": "quick", "messages": [{"role": "user", "content": "hi"}]}
        )
        v1 = await client.post(
            "/v1/chat/completions", json={"model": "quick", "messages": [{"role": "user", "content": "hi"}]}
        )
    assert plain.status_code == 503, plain.text
    assert "needs account_id" in plain.json()["detail"]
    assert "needs account_id" in streamed.text
    assert "internal error" not in streamed.text
    # The same fault is the same status on the OpenAI-compatible surface.
    assert v1.status_code == 503, v1.text
    assert "needs account_id" in v1.json()["error"]["message"]


# Production, 0.6.0: `contributor` and `triage` name `secret:GITHUB_MCP_TOKEN`, which the
# deployment's secrets backend did not hold; `decider-support` routes to `typesafe` with no
# `api_key`. All three reached the chat UI as `internal error (request …)` — the stream
# compiles inside its generator, past the 503 mapping `/chat` applies before it opens.


def test_a_missing_secret_is_relayed_by_name() -> None:
    from felix.secrets import SecretNotFoundError
    from felix_api.errors import client_safe_message

    assert client_safe_message(SecretNotFoundError("secret not found: GITHUB_MCP_TOKEN")) == (
        "secret not found: GITHUB_MCP_TOKEN"
    )


async def test_a_missing_manifest_secret_names_it_on_every_surface(monkeypatch: pytest.MonkeyPatch) -> None:
    from felix.config import get_settings
    from felix.manifests.loader import clear_bundled_cache
    from felix_api.app import create_app

    monkeypatch.delenv("GITHUB_MCP_TOKEN", raising=False)
    # `triage` is not bundled; the builder stack serves it from manifests/self.
    monkeypatch.setenv("FELIX_MANIFESTS_DIR", str(Path(__file__).resolve().parents[2] / "manifests" / "self"))
    get_settings.cache_clear()
    clear_bundled_cache()
    try:
        app = create_app(settings=_settings(secrets_backend="env"), plugins=[])
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", timeout=30
        ) as client:
            plain = await client.post(
                "/chat", json={"manifest": "triage", "messages": [{"role": "user", "content": "hi"}]}
            )
            streamed = await client.post(
                "/chat/stream", json={"manifest": "triage", "messages": [{"role": "user", "content": "hi"}]}
            )
            v1 = await client.post(
                "/v1/chat/completions",
                json={"model": "triage", "messages": [{"role": "user", "content": "hi"}]},
            )
    finally:
        monkeypatch.delenv("FELIX_MANIFESTS_DIR")
        get_settings.cache_clear()
        clear_bundled_cache()
    assert plain.status_code == 503, plain.text
    assert plain.json()["detail"] == "secret not found: GITHUB_MCP_TOKEN"
    assert "secret not found: GITHUB_MCP_TOKEN" in streamed.text
    assert "internal error" not in streamed.text
    assert v1.status_code == 503, v1.text
    assert "secret not found: GITHUB_MCP_TOKEN" in v1.text


def test_an_unconfigured_decision_provider_is_a_provider_config_error() -> None:
    from felix_ai.decide import get_decision_provider

    factory = get_decision_provider("typesafe")
    assert factory is not None
    with pytest.raises(ProviderConfigError, match="needs api_key"):
        factory("jev", "jev-1", {}, None)


async def test_the_stream_names_the_unconfigured_decider() -> None:
    from felix_api.app import create_app

    app = create_app(settings=_settings(), plugins=[])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", timeout=30) as client:
        streamed = await client.post(
            "/chat/stream",
            json={"manifest": "decider-support", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert "decision provider 'workers_ai' needs account_id" in streamed.text
    assert "internal error" not in streamed.text
