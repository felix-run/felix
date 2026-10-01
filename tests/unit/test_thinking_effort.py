"""A thinking level names an effort; it does not round-trip through its budget.

felix-run/felix#398: on every model that takes an effort rather than a budget, the level's
budget was turned back into an effort through thresholds at 4,096 / 16,384 / 32,768. None of
the level budgets (128 … 32,000) were chosen against those, so minimal, low, medium and high
all sent `effort: low`, xhigh sent `medium`, max sent `high`, and the top two tiers were never
sent at all — even to models whose catalog entry accepts `xhigh`.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.manifests.schema import ModelSpec
from felix.session.thinking import THINKING_BUDGETS, THINKING_LEVELS, apply_thinking_to_spec
from felix_ai.catalog import EFFORT_FOR_LEVEL, effort_for_budget, entry_for
from felix_ai.wire.anthropic_messages import apply_anthropic_thinking_cache
from felix_ai.wire.openai_completions import apply_openai_thinking_cache

_ON = [level for level in THINKING_LEVELS if level != "off"]


def _spec(level: str, model: str) -> Any:
    return apply_thinking_to_spec(ModelSpec(id=model), level)


def _anthropic(level: str, model: str) -> dict[str, Any]:
    body: dict[str, Any] = {"model": model, "max_tokens": 1024}
    apply_anthropic_thinking_cache(body, _spec(level, model), model)
    return body


def _openai(level: str, model: str) -> dict[str, Any]:
    body: dict[str, Any] = {"model": model, "messages": []}
    apply_openai_thinking_cache(body, _spec(level, model), model)
    return body


def test_thinking_levels_reach_distinct_efforts_issue_398() -> None:
    """The repro from the issue: `high` on a current Claude model sent `effort: low`."""
    model = "claude-opus-5"
    assert entry_for(model).quirks.effort_xhigh is True
    sent = {level: _anthropic(level, model)["output_config"]["effort"] for level in _ON}
    assert sent == {
        "minimal": "low",
        "low": "low",
        "medium": "medium",
        "high": "high",
        "xhigh": "xhigh",
        "max": "max",
    }


# --- the vocabulary is the harness's -----------------------------------------------------


def test_every_level_that_thinks_names_an_effort() -> None:
    """`felix_ai` cannot import the level vocabulary, so it repeats it; this keeps the two
    from drifting apart. A level missing here would quietly fall back to its budget."""
    assert set(EFFORT_FOR_LEVEL) == set(_ON)


@pytest.mark.parametrize("level", _ON)
def test_a_level_budget_alone_lands_on_the_level_effort(level: str) -> None:
    """A manifest may set `thinking_budget` without a level. Each level's own budget must
    read back as that level's effort, which is what the old thresholds broke."""
    budget = THINKING_BUDGETS[level]  # type: ignore[index]
    assert budget is not None
    assert effort_for_budget(budget) == EFFORT_FOR_LEVEL[level]


def test_budgets_between_levels_take_the_level_below() -> None:
    assert effort_for_budget(5_000) == "high"
    assert effort_for_budget(20_000) == "xhigh"
    assert effort_for_budget(64_000) == "max"


# --- Anthropic, adaptive models --------------------------------------------------------


@pytest.mark.parametrize(
    ("level", "effort"),
    [
        ("minimal", "low"),
        ("low", "low"),
        ("medium", "medium"),
        ("high", "high"),
        ("xhigh", "high"),
        ("max", "max"),
    ],
)
def test_xhigh_is_clamped_where_the_model_has_no_such_tier(level: str, effort: str) -> None:
    """Opus 4.6 takes effort but predates `xhigh`; sending it there is a 400."""
    model = "claude-opus-4-6"
    assert entry_for(model).quirks.effort_xhigh is False
    assert _anthropic(level, model)["output_config"]["effort"] == effort


def test_off_sends_no_thinking_and_no_effort() -> None:
    body = _anthropic("off", "claude-opus-5")
    assert "thinking" not in body
    assert "effort" not in body.get("output_config", {})


def test_a_bare_budget_still_drives_effort_without_a_level() -> None:
    body: dict[str, Any] = {"model": "claude-opus-5", "max_tokens": 1024}
    apply_anthropic_thinking_cache(body, ModelSpec(thinking_budget=32_000), "claude-opus-5")
    assert body["output_config"]["effort"] == "max", "the max budget reaches the top tier"


def test_the_level_outranks_a_budget_that_disagrees_with_it() -> None:
    """The schema says a level overrides the budget; the effort must follow the level."""
    spec = ModelSpec(thinking_budget=128, thinking_level="max")
    body: dict[str, Any] = {"model": "claude-opus-5", "max_tokens": 1024}
    apply_anthropic_thinking_cache(body, spec, "claude-opus-5")
    assert body["output_config"]["effort"] == "max"


# --- Anthropic, budget models: unchanged ------------------------------------------------


@pytest.mark.parametrize("level", _ON)
def test_a_budget_model_is_still_sent_the_level_budget(level: str) -> None:
    body = _anthropic(level, "claude-sonnet-4-5")
    assert body["thinking"] == {"type": "enabled", "budget_tokens": THINKING_BUDGETS[level]}  # type: ignore[index]
    assert "output_config" not in body


# --- OpenAI `reasoning_effort` ------------------------------------------------------------


@pytest.mark.parametrize("model", ["o3", "gpt-4.1"])
@pytest.mark.parametrize(
    ("level", "effort"),
    [
        ("minimal", "low"),
        ("low", "low"),
        ("medium", "medium"),
        ("high", "high"),
        ("xhigh", "high"),
        ("max", "high"),
    ],
)
def test_reasoning_effort_follows_the_level(model: str, level: str, effort: str) -> None:
    """`reasoning_effort` has no tier above `high`, so the top two levels share it."""
    assert _openai(level, model)["reasoning_effort"] == effort


def test_off_sends_no_reasoning_effort() -> None:
    assert "reasoning_effort" not in _openai("off", "o3")
