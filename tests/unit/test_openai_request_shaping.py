"""The OpenAI path shapes its request; it used to just send everything.

`ModelQuirks` had exactly one reader, on the Anthropic path — the docstring said so and it
was true. So the OpenAI-compatible path, which is also Ollama and every LiteLLM/vLLM
gateway, had no output clamp and no sampling suppression, and the `o1`/`o3`/`o4` catalog
entries could never have worked. Meanwhile it emitted an Anthropic `thinking` block into
every request, which a server that validates its schema rejects outright.
"""

from __future__ import annotations

from typing import Any

from felix_ai.catalog import entry_for, known_entry_for
from felix_ai.wire.openai_completions import apply_openai_thinking_cache


class _Spec:
    cache = False
    thinking_budget: int | None = None


class _Thinking(_Spec):
    thinking_budget = 8192


# --- the catalog distinguishes matched from guessed -------------------------------------


def test_an_unknown_model_matches_nothing_but_still_sizes() -> None:
    """`entry_for` must keep answering for sizing; `known_entry_for` must not invent."""
    assert known_entry_for("totally-unknown-model") is None
    assert entry_for("totally-unknown-model").context_window == 128_000


def test_a_known_model_matches() -> None:
    assert known_entry_for("gpt-4.1") is not None


# --- an unknown endpoint is shaped as little as possible --------------------------------


def test_nothing_is_stripped_from_an_unknown_endpoint() -> None:
    """`_DEFAULT.quirks` describes the current Claude generation, which has
    `sampling=False`. Applying that to an unknown OpenAI endpoint would strip
    `temperature` from a model that accepts it."""
    body: dict[str, Any] = {"temperature": 0.7, "max_tokens": 4096}
    apply_openai_thinking_cache(body, _Spec(), "some-local-model")
    assert body["temperature"] == 0.7
    assert body["max_tokens"] == 4096


def test_no_anthropic_thinking_block_reaches_an_openai_endpoint() -> None:
    """`thinking` is not an OpenAI field. It was emitted unconditionally, so a strict
    server — vLLM, LM Studio, most self-written gateways — rejected the whole request."""
    body: dict[str, Any] = {}
    apply_openai_thinking_cache(body, _Thinking(), "gpt-4.1")
    assert "thinking" not in body


def test_reasoning_effort_is_not_sent_to_a_model_that_cannot_take_it() -> None:
    body: dict[str, Any] = {}
    apply_openai_thinking_cache(body, _Thinking(), "some-local-model")
    assert "reasoning_effort" not in body


def test_reasoning_effort_is_sent_to_a_model_that_can() -> None:
    body: dict[str, Any] = {}
    apply_openai_thinking_cache(body, _Thinking(), "gpt-4.1")
    assert body["reasoning_effort"] == "medium"


# --- the reasoning models become reachable ----------------------------------------------


def test_the_o_series_rejects_sampling_and_renames_max_tokens() -> None:
    """These entries were in the catalog and unreachable: the o-series rejects
    `temperature` and requires `max_completion_tokens`, and both were sent regardless."""
    body: dict[str, Any] = {"temperature": 0.7, "top_p": 0.9, "max_tokens": 4096}
    apply_openai_thinking_cache(body, _Spec(), "o3-mini")
    assert "temperature" not in body
    assert "top_p" not in body
    assert "max_tokens" not in body
    assert body["max_completion_tokens"] == 4096


def test_a_normal_openai_model_keeps_its_sampling_params() -> None:
    body: dict[str, Any] = {"temperature": 0.7, "max_tokens": 4096}
    apply_openai_thinking_cache(body, _Spec(), "gpt-4o")
    assert body["temperature"] == 0.7
    assert body["max_tokens"] == 4096


def test_output_is_clamped_to_what_the_model_grants() -> None:
    """The Anthropic path clamped; this one did not, so a request over the ceiling was a
    hard 400 rather than a smaller answer."""
    ceiling = entry_for("gpt-4o").max_output_tokens
    body: dict[str, Any] = {"max_tokens": ceiling * 4}
    apply_openai_thinking_cache(body, _Spec(), "gpt-4o")
    assert body["max_tokens"] == ceiling


def test_an_unset_max_tokens_is_not_invented() -> None:
    """Unlike Anthropic, OpenAI does not require it — adding one would cap answers that
    were deliberately left unbounded."""
    body: dict[str, Any] = {}
    apply_openai_thinking_cache(body, _Spec(), "gpt-4o")
    assert "max_tokens" not in body


def test_an_anthropic_model_behind_an_openai_shim_still_gets_its_thinking_block() -> None:
    """The reason the block was ever emitted here: LiteLLM routes Anthropic models over
    chat-completions, and they want the Anthropic shape. Now it goes only to them."""
    body: dict[str, Any] = {}
    apply_openai_thinking_cache(body, _Thinking(), "claude-sonnet-4-5")
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 8192}


def test_the_current_claude_generation_gets_no_budget_block() -> None:
    """`budget_tokens` is removed on 4.6+ and returns a hard 400."""
    body: dict[str, Any] = {}
    apply_openai_thinking_cache(body, _Thinking(), "claude-sonnet-5")
    assert "thinking" not in body
