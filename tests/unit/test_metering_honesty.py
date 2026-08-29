"""Spend metering must be right or absent — never plausible and wrong.

`limits.max_cost_usd` is a governance control. Three things quietly defeated it: the
catalog's default rates are Claude Sonnet's, so an unrecognised model was billed at
$3/$15 per Mtok; `entry_for` was fed the *logical* route id, which matches nothing, so
every custom route landed on that default; and a turn reporting no usage accumulated
nothing at all, leaving the run uncapped with no signal.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from felix.config import Settings
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.manifests.governance import GovernanceError, assert_cost_limit_is_measurable
from felix.manifests.loader import parse_manifest
from felix.patterns.model import record_usage, wire_model_id
from felix.usage.pricing import estimate_cost
from felix_ai.catalog import is_priced
from felix_ai.types import ChatMessage, ModelChatResult, ModelRoute, TokenUsage


def _settings(**kw: Any) -> Settings:
    return Settings(database_url="memory://metering", object_store="memory", **kw)


# --- the catalog refuses to guess ---------------------------------------------------


def test_an_unknown_model_is_unpriced_rather_than_billed_as_sonnet() -> None:
    assert not is_priced("some-model-nobody-has-heard-of")
    cost = estimate_cost(
        model_id="some-model-nobody-has-heard-of",
        tokens_input=1_000_000,
        tokens_output=1_000_000,
    )
    # Previously $3 + $15. A wrong number is worse than none: it looks like enforcement.
    assert cost["total"] == 0.0


def test_a_known_model_is_still_priced() -> None:
    assert is_priced("claude-sonnet-5")
    cost = estimate_cost(model_id="claude-sonnet-5", tokens_input=1_000_000, tokens_output=0)
    assert cost["total"] > 0.0


def test_free_is_a_property_of_the_provider_not_the_model_name() -> None:
    """Llama runs on a laptop *and* is sold by Workers AI, Groq, Together and Fireworks,
    and `entry_for` matches by substring — so pricing the `llama` catalog entry at zero
    would make every hosted Llama free to `limits.max_cost_usd`. Locality lives on the
    provider instead."""
    from felix_ai.providers import builtin_provider_specs

    assert not is_priced("llama3.2")
    assert not is_priced("@cf/meta/llama-3.3-70b-instruct-fp8-fast")
    by_name = {s.name: s for s in builtin_provider_specs()}
    assert by_name["ollama"].bills_per_token is False
    assert by_name["workers_ai"].bills_per_token is True


def test_a_local_route_can_still_declare_a_spend_cap() -> None:
    """Spend on a local runtime is zero, so the cap holds without any rates."""
    settings = _settings(model_routes='{"local":{"provider":"ollama","model":"llama3.2"}}')
    assert_cost_limit_is_measurable(_manifest("local", limits={"max_cost_usd": 5.0}), settings)


# --- the wire id is what gets priced -------------------------------------------------


class _Client:
    def __init__(self, logical: str, wire: str) -> None:
        self.model_id = logical
        self.route = ModelRoute(provider="openai", model=wire)


def test_the_wire_model_is_what_prices_a_turn() -> None:
    """`client.model_id` is the operator's route name and matches nothing in the catalog."""
    assert wire_model_id(_Client("fast", "claude-sonnet-5")) == "claude-sonnet-5"
    assert estimate_cost(model_id="fast", tokens_input=1_000_000)["total"] == 0.0
    assert estimate_cost(model_id="claude-sonnet-5", tokens_input=1_000_000)["total"] > 0.0


def test_wire_model_id_falls_back_when_a_client_has_no_route() -> None:
    class _NoRoute:
        model_id = "bare"

    assert wire_model_id(_NoRoute()) == "bare"


async def _spend(usage: TokenUsage | None, *, logical: str, wire: str) -> float:
    ctx = RequestContext(settings=_settings(), auth=AuthContext(), manifest_id="m")
    async with async_run_with_context(ctx):
        record_usage(
            ModelChatResult(message=ChatMessage(role="assistant", content="x"), usage=usage),
            manifest_id="m",
            model_id=logical,
            wire_model_id=wire,
        )
    return ctx.limit_state.cost_usd


@pytest.mark.asyncio
async def test_a_custom_route_accrues_the_wire_models_cost() -> None:
    spend = await _spend(TokenUsage(input=1_000_000), logical="fast", wire="claude-sonnet-5")
    assert spend > 0.0


@pytest.mark.asyncio
async def test_an_unpriced_route_accrues_nothing() -> None:
    spend = await _spend(TokenUsage(input=1_000_000), logical="fast", wire="mystery-model")
    assert spend == 0.0


# --- an unmetered turn is loud ---------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("usage", [None, TokenUsage()])
async def test_a_turn_reporting_no_usage_warns(usage: TokenUsage | None, caplog: Any) -> None:
    """It leaves the run uncapped, so it must not pass silently. The common cause is a
    provider whose streamed response omits usage — the OpenAI wire format needs
    `stream_options.include_usage`, and an implementation that forgets it makes the whole
    run free as far as `limits.max_cost_usd` is concerned."""
    with caplog.at_level(logging.WARNING, logger="felix.patterns.model"):
        await _spend(usage, logical="fast", wire="claude-sonnet-5")
    assert any("unmetered" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_a_metered_turn_does_not_warn(caplog: Any) -> None:
    with caplog.at_level(logging.WARNING, logger="felix.patterns.model"):
        await _spend(TokenUsage(input=10, output=5), logical="fast", wire="claude-sonnet-5")
    assert not any("unmetered" in r.message for r in caplog.records)


# --- a declared cap on an unpriceable model is refused at compile ------------------------


def _manifest(model_id: str, *, limits: dict[str, Any], price: dict[str, float] | None = None):
    model: dict[str, Any] = {"id": model_id}
    if price:
        model["price"] = price
    return parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "costed"},
            "spec": {"model": model, "limits": limits},
        }
    )


def test_a_declared_cost_cap_on_an_unpriced_model_is_refused() -> None:
    settings = _settings(model_routes='{"mystery":{"provider":"openai","model":"mystery-model"}}')
    with pytest.raises(GovernanceError, match="max_cost_usd"):
        assert_cost_limit_is_measurable(_manifest("mystery", limits={"max_cost_usd": 5.0}), settings)


def test_a_declared_cost_cap_on_a_priced_model_is_allowed() -> None:
    settings = _settings(model_routes='{"known":{"provider":"anthropic","model":"claude-sonnet-5"}}')
    assert_cost_limit_is_measurable(_manifest("known", limits={"max_cost_usd": 5.0}), settings)


def test_a_manifest_price_override_makes_an_unknown_model_cappable() -> None:
    """`spec.model.price` is the documented way to supply rates per deployment."""
    settings = _settings(model_routes='{"mystery":{"provider":"openai","model":"mystery-model"}}')
    assert_cost_limit_is_measurable(
        _manifest("mystery", limits={"max_cost_usd": 5.0}, price={"input": 1.0, "output": 2.0}),
        settings,
    )


def test_an_undeclared_cap_is_not_refused() -> None:
    """`effective_limits` fills max_cost_usd from ABSOLUTE_LIMITS. Refusing on that would
    break every local Ollama deployment over a ceiling the author never asked for."""
    settings = _settings(model_routes='{"mystery":{"provider":"openai","model":"mystery-model"}}')
    assert_cost_limit_is_measurable(_manifest("mystery", limits={"max_tool_calls": 5}), settings)


def test_an_unpriceable_fallback_is_refused_too() -> None:
    """The sibling docstring above says a typo in a *fallback* stays invisible until the
    primary is already failing. The same is true of an unpriceable one — and deleting the
    `fallbacks` line from the check left the suite green."""
    settings = _settings(
        model_routes=(
            '{"known":{"provider":"anthropic","model":"claude-sonnet-5"},'
            '"mystery":{"provider":"openai","model":"mystery-model"}}'
        )
    )
    manifest = _manifest("known", limits={"max_cost_usd": 5.0})
    manifest.spec.model.fallbacks = ["mystery"]
    with pytest.raises(GovernanceError, match="mystery"):
        assert_cost_limit_is_measurable(manifest, settings)


def test_a_route_absent_from_the_table_is_left_to_the_model_layer() -> None:
    """`build_one_model` reports an unknown logical id with the registered providers named;
    repeating that here would be a second, worse copy of the same message."""
    settings = _settings(model_routes="{}")
    assert_cost_limit_is_measurable(_manifest("nowhere", limits={"max_cost_usd": 5.0}), settings)


@pytest.mark.asyncio
async def test_the_cost_check_runs_at_compile() -> None:
    """Every other test here calls the function directly, so nothing held `build_agent` to
    actually invoking it — removing the call from the builder left the suite green."""
    from felix.manifests.builder import BuildDeps, build_agent
    from felix.tools.provider import InMemoryToolProvider

    settings = _settings(model_routes='{"mystery":{"provider":"openai","model":"mystery-model"}}')
    manifest = _manifest("mystery", limits={"max_cost_usd": 5.0})
    deps = BuildDeps(tools=InMemoryToolProvider(), settings=settings, tenant_id="t")
    with pytest.raises(GovernanceError, match="max_cost_usd"):
        await build_agent(manifest, deps=deps, settings=settings)
