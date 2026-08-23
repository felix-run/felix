"""Long-context pricing is request-wide, so it has to reprice the whole request.

Several providers bill a higher rate on *every* token once total input crosses a
threshold. A flat per-model rate therefore under-reports exactly the long-context
requests that cost the most — and `limits.max_cost_usd` is a fail-closed governance
control reading that number, so a budget cap silently over-admits.
"""

from __future__ import annotations

import pytest
from felix.usage.pricing import estimate_cost

TIERED = {
    "input": 3.0,
    "output": 15.0,
    "cache_read": 0.3,
    "cache_write": 3.75,
    "tiers": [
        {"input_tokens_above": 200_000, "input": 6.0, "output": 22.5},
    ],
}


def test_below_threshold_uses_base_rates() -> None:
    cost = estimate_cost(tokens_input=100_000, tokens_output=1_000, price_override=TIERED)
    assert cost["input"] == pytest.approx(100_000 / 1_000_000 * 3.0)
    assert cost["output"] == pytest.approx(1_000 / 1_000_000 * 15.0)


def test_crossing_the_threshold_reprices_the_whole_request() -> None:
    """Not just the excess: every token bills at the tier rate."""
    cost = estimate_cost(tokens_input=250_000, tokens_output=1_000, price_override=TIERED)
    assert cost["input"] == pytest.approx(250_000 / 1_000_000 * 6.0)
    assert cost["output"] == pytest.approx(1_000 / 1_000_000 * 22.5)


def test_cached_tokens_count_toward_the_threshold() -> None:
    """The provider meters total input usage, not just the uncached part."""
    cost = estimate_cost(tokens_input=100_000, cache_read=150_000, price_override=TIERED)
    assert cost["input"] == pytest.approx(100_000 / 1_000_000 * 6.0)


def test_rates_absent_from_the_tier_fall_back_to_base() -> None:
    """The tier above sets no cache rates, so the base cache_read still applies."""
    cost = estimate_cost(tokens_input=250_000, cache_read=10_000, price_override=TIERED)
    assert cost["cacheRead"] == pytest.approx(10_000 / 1_000_000 * 0.3)


def test_highest_matching_tier_wins() -> None:
    price = {
        "input": 1.0,
        "output": 1.0,
        "tiers": [
            {"input_tokens_above": 100_000, "input": 2.0},
            {"input_tokens_above": 500_000, "input": 4.0},
        ],
    }
    assert estimate_cost(tokens_input=600_000, price_override=price)["input"] == pytest.approx(
        600_000 / 1_000_000 * 4.0
    )
    assert estimate_cost(tokens_input=200_000, price_override=price)["input"] == pytest.approx(
        200_000 / 1_000_000 * 2.0
    )


def test_untiered_prices_are_unaffected() -> None:
    """No bundled entry sets tiers; flat pricing must behave exactly as before."""
    cost = estimate_cost(model_id="claude-haiku-4-5", tokens_input=1_000_000)
    assert cost["input"] == 1.0
