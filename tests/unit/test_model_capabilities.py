"""The Anthropic request builder emitted one shape for every Claude model.

That shape is rejected by the current generation: `budget_tokens` is removed on Fable 5,
Opus 5, Opus 4.8/4.7 and Sonnet 5, and sampling parameters are removed on the whole 4.6+
family — both HTTP 400. So the manifest's thinking levels hard-failed against every
current model, and `stop_reason` was never read at all, meaning a truncated answer and a
safety refusal both presented as a normal completion.
"""

from __future__ import annotations

import pytest
from felix.model_catalog import clamp_effort, entry_for
from felix.patterns.react import _status_for_stop
from felix_ai.wire.anthropic_messages import _ANTHROPIC_STOP, apply_anthropic_thinking_cache
from felix_ai.wire.base import map_stop as _map_stop
from felix_ai.wire.openai_completions import _OPENAI_STOP

# --- the capability table -------------------------------------------------------


@pytest.mark.parametrize(
    "model",
    ["claude-opus-5", "claude-sonnet-5", "claude-fable-5", "claude-opus-4-8", "claude-opus-4-7"],
)
def test_current_models_reject_budget_tokens_and_sampling(model: str) -> None:
    caps = entry_for(model)
    assert caps.quirks.adaptive_thinking is True
    assert caps.quirks.budget_tokens is False, "sending budget_tokens here is a 400"
    assert caps.quirks.sampling is False, "sending temperature here is a 400"


@pytest.mark.parametrize("model", ["claude-sonnet-4-5", "claude-haiku-4-5"])
def test_legacy_models_keep_budget_tokens(model: str) -> None:
    caps = entry_for(model)
    assert caps.quirks.adaptive_thinking is False
    assert caps.quirks.budget_tokens is True
    assert caps.quirks.sampling is True


def test_46_family_takes_adaptive_but_still_allows_sampling() -> None:
    caps = entry_for("claude-opus-4-6")
    assert caps.quirks.adaptive_thinking is True
    assert caps.quirks.sampling is True
    assert caps.quirks.effort_xhigh is False, "xhigh arrived after 4.6"


def test_dated_snapshot_resolves_to_its_family() -> None:
    assert entry_for("claude-haiku-4-5-20251001").quirks.budget_tokens is True


def test_unknown_claude_id_assumes_current_generation() -> None:
    """Omitting an optional parameter is not an error; sending a removed one is a 400,
    so guessing 'modern' is the safe direction."""
    caps = entry_for("claude-something-unreleased")
    assert caps.quirks.budget_tokens is False
    assert caps.quirks.sampling is False


def test_effort_is_clamped_to_what_the_model_accepts() -> None:
    assert clamp_effort("xhigh", entry_for("claude-opus-5").quirks) == "xhigh"
    assert clamp_effort("xhigh", entry_for("claude-opus-4-6").quirks) == "high"
    assert clamp_effort("nonsense", entry_for("claude-opus-5").quirks) == "high"


# --- the emitted request body ---------------------------------------------------


def _body(model: str, budget: int | None = 5000) -> dict:
    from felix.manifests.schema import ModelSpec

    body: dict = {"model": model, "temperature": 0.7, "max_tokens": 1024}
    spec = ModelSpec(thinking_budget=budget) if budget else None
    apply_anthropic_thinking_cache(body, spec, model)
    return body


def test_current_model_body_has_no_400_parameters() -> None:
    body = _body("claude-opus-5")
    assert body["thinking"] == {"type": "adaptive"}
    assert "temperature" not in body
    assert "top_p" not in body and "top_k" not in body
    assert body["output_config"]["effort"] in {"low", "medium", "high", "xhigh", "max"}


def test_legacy_model_body_keeps_the_old_shape() -> None:
    body = _body("claude-sonnet-4-5")
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 5000}
    assert body["temperature"] == 1


def test_output_ceiling_is_enforced_even_without_a_spec() -> None:
    body: dict = {"model": "claude-haiku-4-5", "max_tokens": 999_999}
    apply_anthropic_thinking_cache(body, None, "claude-haiku-4-5")
    assert body["max_tokens"] == 64_000


def test_larger_ceiling_for_current_models() -> None:
    body: dict = {"model": "claude-opus-5", "max_tokens": 999_999}
    apply_anthropic_thinking_cache(body, None, "claude-opus-5")
    assert body["max_tokens"] == 128_000


# --- stop_reason ----------------------------------------------------------------


def test_anthropic_stop_reasons_are_read_not_inferred() -> None:
    assert _map_stop("max_tokens", _ANTHROPIC_STOP, had_tool_calls=False) == "max_tokens"
    assert _map_stop("refusal", _ANTHROPIC_STOP, had_tool_calls=False) == "refusal"
    assert _map_stop("pause_turn", _ANTHROPIC_STOP, had_tool_calls=False) == "pause_turn"


def test_openai_finish_reasons_are_translated() -> None:
    assert _map_stop("length", _OPENAI_STOP, had_tool_calls=False) == "max_tokens"
    assert _map_stop("content_filter", _OPENAI_STOP, had_tool_calls=False) == "refusal"
    assert _map_stop("tool_calls", _OPENAI_STOP, had_tool_calls=True) == "tool_use"


def test_missing_stop_reason_falls_back_to_the_old_inference() -> None:
    assert _map_stop(None, _ANTHROPIC_STOP, had_tool_calls=True) == "tool_use"
    assert _map_stop("", _ANTHROPIC_STOP, had_tool_calls=False) == "end_turn"


def test_unrecognised_stop_reason_is_not_silently_a_completion() -> None:
    assert _map_stop("something_new", _ANTHROPIC_STOP, had_tool_calls=False) == "unknown"


def test_truncated_and_refused_runs_are_not_recorded_as_complete() -> None:
    """A cut-off answer used to be indistinguishable from a finished one."""
    assert _status_for_stop("max_tokens") == "truncated"
    assert _status_for_stop("refusal") == "refused"
    assert _status_for_stop("end_turn") == "complete"
    assert _status_for_stop("tool_use") == "complete"


# --- pricing and context --------------------------------------------------------


# (input, output, cache_read, cache_write) per MTok, from the Anthropic pricing page read on
# 2026-09-30. Every rate is asserted, not just input: cache reads are where the point
# releases differ, and a long agentic session is mostly cache reads.
_PUBLISHED = {
    "claude-fable-5-1": (10.0, 50.0, 0.25, 12.5),
    "claude-mythos-5-1": (10.0, 50.0, 0.25, 12.5),
    "claude-fable-5": (10.0, 50.0, 1.0, 12.5),
    "claude-opus-5-5": (4.0, 20.0, 0.2, 5.0),
    "claude-opus-5": (5.0, 25.0, 0.5, 6.25),
    "claude-opus-4-8": (5.0, 25.0, 0.5, 6.25),
    "claude-sonnet-5": (2.0, 10.0, 0.2, 2.5),
    "claude-sonnet-5-5": (2.0, 10.0, 0.2, 2.5),
    "claude-sonnet-4-6": (3.0, 15.0, 0.3, 3.75),
    # Haiku was priced at 0.8/4.0, under-reporting every run.
    "claude-haiku-4-5": (1.0, 5.0, 0.1, 1.25),
}


@pytest.mark.parametrize("model_id", sorted(_PUBLISHED))
def test_pricing_matches_the_published_rates(model_id: str) -> None:
    """Sonnet 5 was billed at Sonnet 4's $3/$15, and the point releases matched their
    predecessor's key by substring: `claude-opus-5-5` paid Opus 5's rates and
    `claude-fable-5-1` read its cache at four times the price. All in the direction of
    stopping a run under `limits.max_cost_usd` before its budget was spent."""
    from felix.usage.pricing import _lookup_price

    price = _lookup_price(model_id)
    assert (price["input"], price["output"], price["cache_read"], price["cache_write"]) == _PUBLISHED[
        model_id
    ]


def test_context_window_is_1m_for_the_current_family() -> None:
    from felix.usage.catalog import context_window_for

    assert context_window_for("claude-opus-5") == 1_000_000
    assert context_window_for("claude-sonnet-5") == 1_000_000
    assert context_window_for("claude-haiku-4-5") == 200_000


def test_default_routes_point_at_current_models() -> None:
    from felix.config import DEFAULT_MODEL_ROUTES

    wire = {r["model"] for r in DEFAULT_MODEL_ROUTES.values() if r["provider"] == "anthropic"}
    assert "claude-opus-5" in wire
    assert "claude-sonnet-5" in wire
    # no date-suffixed ids
    assert not any(m.count("-") > 3 and m.split("-")[-1].isdigit() for m in wire)
