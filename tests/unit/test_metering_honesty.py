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
from felix_ai.catalog import entry_for, is_priced
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


def test_a_local_model_is_priced_at_zero_not_unknown() -> None:
    """Free is a fact about a local runtime, not an absence of information — and it must
    not take the unknown-price path, which would refuse a declared spend cap."""
    assert is_priced("llama3.2")
    assert entry_for("llama3.2").pricing is not None
    assert estimate_cost(model_id="llama3.2", tokens_input=1_000_000)["total"] == 0.0


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
