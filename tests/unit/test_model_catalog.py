"""One record per model family, and one rule for finding it.

"Which model is this, and what does it accept" used to be answered by three tables with
three different matching rules — longest prefix in `patterns/capabilities.py`, substring
with *first key in dict order* winning in `usage/catalog.py`, substring with longest
winning in `usage/pricing.py`. They overlapped, and on context window they disagreed.
"""

from __future__ import annotations

import pytest
from felix.model_catalog import ModelCatalogEntry, all_entries, clamp_effort, entry_for
from felix.usage.catalog import context_window_for, modalities_for, supported_thinking_levels
from felix.usage.pricing import _lookup_price


def test_context_window_agrees_across_every_view() -> None:
    """The disagreement that motivated this: `claude-opus-4-5` was 200K to the request
    builder and 1M to `/v1/models`, because one matched by prefix and the other matched
    the `claude-opus` substring first."""
    for model_id in (*all_entries(), "claude-opus-4-5", "claude-opus-5", "gpt-4o"):
        assert context_window_for(model_id) == entry_for(model_id).context_window


def test_opus_4_5_is_200k_not_1m() -> None:
    assert entry_for("claude-opus-4-5").context_window == 200_000
    assert context_window_for("claude-opus-4-5") == 200_000


def test_longest_key_wins_not_dict_order() -> None:
    """Order-independence is the point: a key added in the wrong position used to change
    answers silently."""
    assert entry_for("gpt-4o-mini").pricing.input == 0.15
    assert entry_for("gpt-4o").pricing.input == 2.5
    assert entry_for("claude-opus-5").context_window == 1_000_000
    assert entry_for("claude-opus-4-5").context_window == 200_000


def test_dated_snapshot_resolves_to_its_family() -> None:
    assert entry_for("claude-haiku-4-5-20251001").pricing.input == 1.0


def test_provider_qualified_ids_resolve() -> None:
    """Bedrock-style ids carry a vendor prefix, which is why matching is substring."""
    entry = entry_for("us.anthropic.claude-opus-4-5-v1:0")
    assert entry.context_window == 200_000
    assert entry.pricing.input == 5.0


def test_unknown_id_assumes_current_request_shape_but_a_modest_window() -> None:
    """The two halves default in opposite directions on purpose. Sending a parameter the
    model removed is a hard 400, so guess 'modern' on shape; over-advertising a context
    window invites a request the model rejects, so stay conservative there."""
    entry = entry_for("claude-something-unreleased")
    assert entry.quirks.budget_tokens is False
    assert entry.quirks.sampling is False

    unknown = entry_for("some-other-vendor-model")
    assert unknown.context_window == 128_000
    assert unknown.quirks.adaptive_thinking is True


def test_family_fallback_is_conservative_on_context() -> None:
    """An unrecognised snapshot of an old family must not be advertised as 1M."""
    assert entry_for("claude-sonnet-4").context_window == 200_000
    assert entry_for("claude-sonnet-4").quirks.budget_tokens is False, "shape still guesses modern"


def test_price_lookup_reads_the_same_record() -> None:
    for model_id in all_entries():
        assert _lookup_price(model_id)["input"] == entry_for(model_id).pricing.input


def test_pricing_dict_omits_tiers_when_there_are_none() -> None:
    """No bundled entry sets tiers; the key must be absent rather than empty."""
    assert "tiers" not in _lookup_price("claude-sonnet-5")


def test_thinking_levels_and_modalities_come_from_the_record() -> None:
    assert supported_thinking_levels("claude-opus-5") != ["off"]
    assert supported_thinking_levels("llama3.3:70b") == ["off"]
    assert "image" in modalities_for("claude-opus-5")
    assert modalities_for("llama3.3:70b") == ["text"]


def test_effort_is_clamped_to_what_the_model_accepts() -> None:
    assert clamp_effort("xhigh", entry_for("claude-opus-5").quirks) == "xhigh"
    assert clamp_effort("xhigh", entry_for("claude-opus-4-6").quirks) == "high"
    assert clamp_effort("nonsense", entry_for("claude-opus-5").quirks) == "high"


@pytest.mark.parametrize("model_id", sorted(all_entries()))
def test_every_entry_is_internally_coherent(model_id: str) -> None:
    entry = all_entries()[model_id]
    assert isinstance(entry, ModelCatalogEntry)
    assert entry.context_window >= 8_192
    assert entry.max_output_tokens >= 1_024
    assert entry.pricing.output >= entry.pricing.input, "output is never cheaper than input"
    assert entry.pricing.cache_read <= entry.pricing.input, "cache reads are a discount"
    assert "text" in entry.input_modalities
