"""One record per model family, and one rule for finding it.

"Which model is this, and what does it accept" was answered by three separate tables
using three different matching rules:

* `patterns/capabilities.py` — longest **prefix** wins. Shapes the Anthropic request.
* `usage/catalog.py._CONTEXT_WINDOWS` — **substring**, and **first key in dict order**
  wins, so inserting a key in the wrong position silently changed answers.
* `usage/pricing.py.DEFAULT_PRICES` — **substring**, longest wins. Prices every run.

They overlapped and disagreed. Context window was defined twice: the capabilities table
said `claude-opus-4-5` is 200K while the catalog's `claude-opus` substring claimed 1M for
the same id, and `/v1/models` published the second number. Adding a model meant editing
three tables in three formats and getting three matching rules right; missing one
degraded quietly rather than failing.

This module holds the record and the lookup. `capabilities_for`, `context_window_for`,
and the price table are now views over it, so a model is described in exactly one place.

Matching is by the **longest key that appears anywhere in the id**. Substring rather than
prefix because provider-qualified ids are real — `us.anthropic.claude-opus-4-5-v1:0`
carries a vendor prefix — and longest-wins because `claude-opus-4-5` must beat both
`claude-opus` and `claude`.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any


@dataclass(frozen=True)
class ModelQuirks:
    """What one model's request surface accepts.

    A removed parameter is a hard 400, so these are recorded per family rather than
    assumed. Consulted on the Anthropic path only.
    """

    # thinking: {"type": "adaptive"} — the 4.6+ shape.
    adaptive_thinking: bool = False
    # thinking: {"type": "enabled", "budget_tokens": N} — pre-4.6 shape.
    budget_tokens: bool = True
    # temperature / top_p / top_k.
    sampling: bool = True
    # output_config.effort, and whether "xhigh" is one of the accepted levels.
    effort: bool = False
    effort_xhigh: bool = False
    # Thinking is on unless explicitly disabled (Opus 5 behaves this way; 4.7/4.8 do not).
    thinking_on_by_default: bool = False
    # OpenAI reasoning models renamed `max_tokens` to `max_completion_tokens` and reject
    # the old spelling outright.
    max_completion_tokens: bool = False
    # Native structured outputs: `output_config.format = {"type": "json_schema", ...}` on
    # /v1/messages, answered as a text block holding the JSON. Unlike a forced tool it works
    # with extended thinking on. Off by default because sending it to a model without it is a
    # 400, while not sending it only costs the guarantee on the paths a forced tool cannot cover.
    structured_outputs: bool = False
    # `tool_choice` `{"type": "any"}` / `{"type": "tool"}`. Fable 5.1, Mythos 5.1, Opus 5.5 and
    # Sonnet 5.5 reject both with a 400 ("type "tool" and "any" are not supported for this
    # model"); `auto` and `none` still work. True here because every earlier model accepts
    # them — the entries that cannot vouch for a model (family keys, `_DEFAULT`) turn it off.
    forced_tool_choice: bool = True


@dataclass(frozen=True)
class ModelPricing:
    """USD per 1M tokens, plus optional request-wide long-context tiers.

    Tiers are not marginal: crossing a threshold reprices every token of the request, so
    the matching tier's rates replace the base rates rather than applying to the excess.
    No bundled entry sets tiers, and none should: Anthropic bills Claude 4.6 and later at one
    rate across the whole 1M window (a 900K-token request costs per token what a 9K one does),
    and the pre-4.6 entries here are sized at 200K, below any threshold that ever applied.
    """

    input: float = 3.0
    output: float = 15.0
    cache_read: float = 0.3
    cache_write: float = 3.75
    tiers: tuple[dict[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "input": self.input,
            "output": self.output,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
        }
        if self.tiers:
            d["tiers"] = [dict(t) for t in self.tiers]
        return d


@dataclass(frozen=True)
class ModelCatalogEntry:
    """Everything Felix knows about one model family."""

    context_window: int = 128_000
    max_output_tokens: int = 8_192
    # `None` means *unknown*, not free — and it is the default on purpose. `ModelPricing()`
    # carries Claude Sonnet's list price in its own field defaults, so a catalog entry that
    # simply omitted rates billed an unrelated model at $3/$15 per Mtok. Several did: the
    # `gpt-4.1`, `gpt-4`, `o1`/`o3`/`o4` and `llama` entries all state, in a comment, that
    # they have no bundled rate — and all of them were priced as Sonnet anyway. An entry
    # that does not state rates does not have them.
    pricing: ModelPricing | None = None
    quirks: ModelQuirks = field(default_factory=ModelQuirks)
    # Whether the model accepts thinking at all. The *level* vocabulary lives in
    # `felix.session.thinking`; importing it here would cycle back through
    # felix.session.__init__ -> compaction -> patterns -> model.py, so the catalog
    # records the capability and `usage.catalog` expands it into level names.
    supports_thinking: bool = False
    input_modalities: tuple[str, ...] = ("text",)
    # Vouched for as *unable* to take an image, which `("text",)` above does not say: that
    # is the default every entry inherits, including family keys like `llama` that also
    # match `llama3.2-vision`. Only this flag lets the harness reroute or drop an image
    # (`accepts_images`), so an entry sets it only for weights known to be text-only.
    text_only: bool = False
    # The wire dialect this model natively speaks. It is not the dialect it is *reached*
    # through: an Anthropic model behind a LiteLLM shim is addressed with OpenAI
    # chat-completions but still wants Anthropic's `thinking` block, and that is the only
    # way to know which requests should carry one. `ModelQuirks.budget_tokens` cannot
    # answer it — that flag defaults to True, so every OpenAI entry looks pre-4.6 Anthropic.
    native_wire: str = ""


_TEXT_AND_IMAGE: tuple[str, ...] = ("text", "image")

# Current Claude generation: adaptive thinking, no budget_tokens, no sampling params.
_MODERN_QUIRKS = ModelQuirks(
    adaptive_thinking=True,
    budget_tokens=False,
    sampling=False,
    effort=True,
    effort_xhigh=True,
)
# 4.6: adaptive thinking arrived, sampling still accepted, budget_tokens deprecated but
# functional, and `xhigh` did not exist yet.
_V46_QUIRKS = ModelQuirks(
    adaptive_thinking=True,
    budget_tokens=True,
    sampling=True,
    effort=True,
    effort_xhigh=False,
)
# Pre-4.6: fixed thinking budgets, sampling allowed, no effort.
_LEGACY_QUIRKS = ModelQuirks(adaptive_thinking=False, budget_tokens=True, sampling=True, effort=False)
# Native structured outputs, by model rather than by generation: Opus 4.8 and Haiku 4.5 have
# them while Opus 4.6/4.7 and every Sonnet 4.x do not.
_NATIVE = {"structured_outputs": True}
# Native structured outputs, and no forced tool choice: the 5.1 / 5.5 point releases.
_NATIVE_UNFORCED = {"structured_outputs": True, "forced_tool_choice": False}
# A family key or an unknown id cannot vouch for the model behind it: `claude-opus` answers for
# `claude-opus-6` as readily as for `claude-opus-4-1`. A forced choice sent to a model that
# rejects it is a 400 on every structured turn, while not forcing only downgrades the schema to
# an offer — so these say "no forcing", and "no native format", which is a 400 the other way.
_UNVOUCHED = {"structured_outputs": False, "forced_tool_choice": False}

# USD per MTok from https://platform.claude.com/docs/en/about-claude/pricing, read 2026-09-30.
# `cache_write` is the 5-minute write (1.25x input). Matching is by longest substring, so a
# point release with different rates needs its own key: without one, `claude-opus-5-5` was
# billed as `claude-opus-5` and `claude-fable-5-1` read its cache at `claude-fable-5`'s rate.
_OPUS_PRICE = ModelPricing(input=5.0, output=25.0, cache_read=0.5, cache_write=6.25)
# Cache reads at 0.05x input, not the usual 0.1x.
_OPUS_55_PRICE = ModelPricing(input=4.0, output=20.0, cache_read=0.2, cache_write=5.0)
# Sonnet 4.x. Sonnet 5 is cheaper; its $2/$10 launch price became the standard one.
_SONNET_PRICE = ModelPricing(input=3.0, output=15.0, cache_read=0.3, cache_write=3.75)
_SONNET_5_PRICE = ModelPricing(input=2.0, output=10.0, cache_read=0.2, cache_write=2.5)
_HAIKU_PRICE = ModelPricing(input=1.0, output=5.0, cache_read=0.1, cache_write=1.25)
_FABLE_PRICE = ModelPricing(input=10.0, output=50.0, cache_read=1.0, cache_write=12.5)
# Fable 5.1 and Mythos 5.1: the same tier, with cache reads at 0.025x input.
_FABLE_51_PRICE = ModelPricing(input=10.0, output=50.0, cache_read=0.25, cache_write=12.5)

_FRONTIER = ModelCatalogEntry(
    context_window=1_000_000,
    max_output_tokens=128_000,
    pricing=_SONNET_PRICE,
    quirks=_MODERN_QUIRKS,
    supports_thinking=True,
    input_modalities=_TEXT_AND_IMAGE,
    native_wire="anthropic",
)
_PRE_46 = ModelCatalogEntry(
    context_window=200_000,
    max_output_tokens=64_000,
    pricing=_SONNET_PRICE,
    quirks=_LEGACY_QUIRKS,
    supports_thinking=True,
    input_modalities=_TEXT_AND_IMAGE,
    native_wire="anthropic",
)

_FAMILY = replace(_PRE_46, quirks=replace(_MODERN_QUIRKS, **_UNVOUCHED), max_output_tokens=128_000)

_OPUS_5 = replace(
    _FRONTIER,
    pricing=_OPUS_PRICE,
    quirks=replace(_MODERN_QUIRKS, thinking_on_by_default=True, **_NATIVE),
)
_NATIVE_FRONTIER = replace(_FRONTIER, quirks=replace(_MODERN_QUIRKS, **_NATIVE))
_UNFORCED_FRONTIER = replace(_FRONTIER, quirks=replace(_MODERN_QUIRKS, **_NATIVE_UNFORCED))

_CATALOG: dict[str, ModelCatalogEntry] = {
    # --- Claude, current generation (1M context, adaptive thinking) ---
    "claude-fable-5-1": replace(_UNFORCED_FRONTIER, pricing=_FABLE_51_PRICE),
    "claude-mythos-5-1": replace(_UNFORCED_FRONTIER, pricing=_FABLE_51_PRICE),
    "claude-fable-5": replace(_NATIVE_FRONTIER, pricing=_FABLE_PRICE),
    "claude-mythos-5": replace(_NATIVE_FRONTIER, pricing=_FABLE_PRICE),
    "claude-opus-5-5": replace(
        _OPUS_5, pricing=_OPUS_55_PRICE, quirks=replace(_OPUS_5.quirks, **_NATIVE_UNFORCED)
    ),
    "claude-opus-5": _OPUS_5,
    "claude-opus-4-8": replace(_NATIVE_FRONTIER, pricing=_OPUS_PRICE),
    "claude-opus-4-7": replace(_FRONTIER, pricing=_OPUS_PRICE),
    "claude-opus-4-6": replace(_FRONTIER, pricing=_OPUS_PRICE, quirks=_V46_QUIRKS),
    # Its own key although priced as Sonnet 5: it rejects a forced tool choice, and under the
    # `claude-sonnet-5` key it was sent one on every structured turn.
    "claude-sonnet-5-5": replace(_UNFORCED_FRONTIER, pricing=_SONNET_5_PRICE),
    "claude-sonnet-5": replace(_NATIVE_FRONTIER, pricing=_SONNET_5_PRICE),
    "claude-sonnet-4-6": replace(_FRONTIER, quirks=_V46_QUIRKS),
    # --- Claude, pre-4.6 (200K context, fixed thinking budgets) ---
    "claude-opus-4-5": replace(
        _PRE_46,
        pricing=_OPUS_PRICE,
        quirks=replace(_LEGACY_QUIRKS, effort=True, effort_xhigh=False, **_NATIVE),
    ),
    "claude-sonnet-4-5": _PRE_46,
    "claude-haiku-4-5": replace(_PRE_46, pricing=_HAIKU_PRICE, quirks=replace(_LEGACY_QUIRKS, **_NATIVE)),
    # Family fallbacks for ids with no exact entry. Split defaults on purpose:
    # conservative on *context*, since advertising 1M for an unrecognised snapshot of an
    # old family invites a request the model rejects; but *modern* on request shape,
    # since sending a parameter the model removed is a hard 400 while omitting an
    # optional one is not. Guessing "current generation" is the direction that fails safe.
    # They neither force a tool choice nor send a native format (`_UNVOUCHED`), with two
    # exceptions below so the 4.x snapshots they used to cover keep their forced schema tool.
    "claude-opus": replace(_FAMILY, pricing=_OPUS_PRICE),
    "claude-sonnet": _FAMILY,
    "claude-haiku": replace(_FAMILY, pricing=_HAIKU_PRICE),
    "claude-fable": replace(_FAMILY, pricing=_FABLE_PRICE),
    "claude-mythos": replace(_FAMILY, pricing=_FABLE_PRICE),
    "claude": _FAMILY,
    # Every 3.x id (`claude-3-5-sonnet-latest`, `claude-3-7-sonnet-…`, `claude-3-haiku-…`) matched
    # only `claude` above, which neither forces nor goes native. They all accept a forced choice
    # and none has native structured outputs, so they keep the forced route.
    "claude-3": replace(_FAMILY, quirks=replace(_FAMILY.quirks, forced_tool_choice=True)),
    # `claude-sonnet-4-20250514`, `claude-opus-4-1`, `claude-opus-4-20250514`: the family entry
    # in every field but one. All of them accept a forced choice. An unreleased 4.x would land
    # here too and be forced; the 5.x line, which is where forcing was withdrawn, cannot.
    "claude-sonnet-4": replace(_FAMILY, quirks=replace(_FAMILY.quirks, forced_tool_choice=True)),
    "claude-opus-4": replace(
        _FAMILY, pricing=_OPUS_PRICE, quirks=replace(_FAMILY.quirks, forced_tool_choice=True)
    ),
    # Opus 4.1 has native structured outputs; Opus 4 does not.
    "claude-opus-4-1": replace(
        _FAMILY,
        pricing=_OPUS_PRICE,
        quirks=replace(_FAMILY.quirks, structured_outputs=True, forced_tool_choice=True),
    ),
    # --- OpenAI ---
    "gpt-4.1": ModelCatalogEntry(
        context_window=1_047_576,
        max_output_tokens=32_768,
        pricing=ModelPricing(input=2.0, output=8.0, cache_read=0.5, cache_write=2.0),
        supports_thinking=True,
        input_modalities=_TEXT_AND_IMAGE,
    ),
    "gpt-4.1-mini": ModelCatalogEntry(
        context_window=1_047_576,
        max_output_tokens=32_768,
        pricing=ModelPricing(input=0.4, output=1.6, cache_read=0.1, cache_write=0.4),
        supports_thinking=True,
        input_modalities=_TEXT_AND_IMAGE,
    ),
    "gpt-4.1-nano": ModelCatalogEntry(
        context_window=1_047_576,
        max_output_tokens=32_768,
        pricing=ModelPricing(input=0.1, output=0.4, cache_read=0.025, cache_write=0.1),
        supports_thinking=True,
        input_modalities=_TEXT_AND_IMAGE,
    ),
    "gpt-4o-mini": ModelCatalogEntry(
        context_window=128_000,
        max_output_tokens=16_384,
        pricing=ModelPricing(input=0.15, output=0.6, cache_read=0.075, cache_write=0.15),
        supports_thinking=True,
        input_modalities=_TEXT_AND_IMAGE,
    ),
    "gpt-4o": ModelCatalogEntry(
        context_window=128_000,
        max_output_tokens=16_384,
        pricing=ModelPricing(input=2.5, output=10.0, cache_read=1.25, cache_write=2.5),
        supports_thinking=True,
        input_modalities=_TEXT_AND_IMAGE,
    ),
    "gpt-4": ModelCatalogEntry(context_window=128_000, supports_thinking=True),
    # OpenAI reasoning families. Context and rates were never tabulated for these, so
    # they keep the default; only their thinking support was previously recognised.
    "o1": ModelCatalogEntry(
        context_window=128_000,
        supports_thinking=True,
        quirks=ModelQuirks(sampling=False, max_completion_tokens=True),
    ),
    "o3": ModelCatalogEntry(
        context_window=128_000,
        supports_thinking=True,
        quirks=ModelQuirks(sampling=False, max_completion_tokens=True),
    ),
    "o4": ModelCatalogEntry(
        context_window=128_000,
        supports_thinking=True,
        quirks=ModelQuirks(sampling=False, max_completion_tokens=True),
    ),
    # --- Cloudflare Workers AI ---
    # USD per MTok from https://developers.cloudflare.com/workers-ai/platform/pricing/ and each
    # model's page, read 2026-10-01. Cloudflare meters in neurons and publishes these per-token
    # rates as the conversion; the 10,000 free neurons a day are not subtracted, so spend reads
    # high rather than low. Every key carries the `@cf/` prefix, so the same weights served by
    # Groq, Together or a local runtime never match one of these rates. The OpenAI wire counts cached
    # tokens as plain input, so Kimi's cache rate is recorded and not yet applied.
    # Cloudflare states no output limit separate from the window, and `apply_request_shaping`
    # clamps `max_tokens` to `max_output_tokens` — so each entry sets it to its window. The
    # 8,192 default would have cut a gpt-oss reasoning turn short where nothing clamped before.
    "@cf/moonshotai/kimi-k2.6": ModelCatalogEntry(
        context_window=262_144,
        max_output_tokens=262_144,
        pricing=ModelPricing(input=0.95, output=4.0, cache_read=0.16, cache_write=0.95),
        input_modalities=_TEXT_AND_IMAGE,
        quirks=ModelQuirks(**_UNVOUCHED),
    ),
    "@cf/openai/gpt-oss-120b": ModelCatalogEntry(
        context_window=128_000,
        max_output_tokens=128_000,
        pricing=ModelPricing(input=0.35, output=0.75, cache_read=0.35, cache_write=0.35),
        quirks=ModelQuirks(**_UNVOUCHED),
        text_only=True,
    ),
    "@cf/openai/gpt-oss-20b": ModelCatalogEntry(
        context_window=128_000,
        max_output_tokens=128_000,
        pricing=ModelPricing(input=0.2, output=0.3, cache_read=0.2, cache_write=0.2),
        quirks=ModelQuirks(**_UNVOUCHED),
        text_only=True,
    ),
    # The entries from here to GLM-4.7 Flash are read from each model page, 2026-10-06.
    # Cached input is recorded and not yet applied.
    "@cf/zai-org/glm-5.3": ModelCatalogEntry(
        context_window=1_048_576,
        max_output_tokens=1_048_576,
        pricing=ModelPricing(input=1.40, output=4.40, cache_read=0.26, cache_write=1.40),
        quirks=ModelQuirks(**_UNVOUCHED),
        text_only=True,
    ),
    "@cf/zai-org/glm-5.3-flash": ModelCatalogEntry(
        context_window=1_048_576,
        max_output_tokens=1_048_576,
        pricing=ModelPricing(input=0.15, output=0.50, cache_read=0.03, cache_write=0.15),
        input_modalities=_TEXT_AND_IMAGE,
        quirks=ModelQuirks(**_UNVOUCHED),
    ),
    "@cf/moonshotai/kimi-k2.7-code": ModelCatalogEntry(
        context_window=262_144,
        max_output_tokens=262_144,
        pricing=ModelPricing(input=0.95, output=4.0, cache_read=0.19, cache_write=0.95),
        input_modalities=_TEXT_AND_IMAGE,
        quirks=ModelQuirks(**_UNVOUCHED),
    ),
    "@cf/qwen/qwen3.8-27b": ModelCatalogEntry(
        context_window=262_144,
        max_output_tokens=262_144,
        pricing=ModelPricing(input=0.45, output=3.20, cache_read=0.05, cache_write=0.45),
        input_modalities=_TEXT_AND_IMAGE,
        quirks=ModelQuirks(**_UNVOUCHED),
    ),
    "@cf/deepseek-ai/deepseek-v4-pro-0813": ModelCatalogEntry(
        context_window=1_048_576,
        max_output_tokens=1_048_576,
        pricing=ModelPricing(input=1.32, output=3.96, cache_read=0.044, cache_write=1.32),
        quirks=ModelQuirks(**_UNVOUCHED),
        text_only=True,
    ),
    "@cf/deepseek-ai/deepseek-v4-flash-0731": ModelCatalogEntry(
        context_window=1_048_576,
        max_output_tokens=1_048_576,
        pricing=ModelPricing(input=0.44, output=1.32, cache_read=0.014, cache_write=0.44),
        quirks=ModelQuirks(**_UNVOUCHED),
        text_only=True,
    ),
    "@cf/zai-org/glm-4.7-flash": ModelCatalogEntry(
        context_window=131_072,
        max_output_tokens=131_072,
        pricing=ModelPricing(input=0.0605, output=0.4, cache_read=0.0605, cache_write=0.0605),
        quirks=ModelQuirks(**_UNVOUCHED),
        text_only=True,
    ),
    "@cf/meta/llama-4-scout-17b-16e-instruct": ModelCatalogEntry(
        context_window=131_000,
        max_output_tokens=131_000,
        pricing=ModelPricing(input=0.27, output=0.85, cache_read=0.27, cache_write=0.27),
        input_modalities=_TEXT_AND_IMAGE,
        quirks=ModelQuirks(**_UNVOUCHED),
    ),
    # 24K on Workers AI, not Llama 3.3's native 128K: too small for an agent whose prompt
    # carries a skill catalogue, which is why no default route points here.
    "@cf/meta/llama-3.3-70b-instruct-fp8-fast": ModelCatalogEntry(
        context_window=24_000,
        max_output_tokens=24_000,
        pricing=ModelPricing(input=0.293, output=2.253, cache_read=0.293, cache_write=0.293),
        quirks=ModelQuirks(**_UNVOUCHED),
        text_only=True,
    ),
    "@cf/qwen/qwen3-30b-a3b-fp8": ModelCatalogEntry(
        context_window=32_768,
        max_output_tokens=32_768,
        pricing=ModelPricing(input=0.0509, output=0.335, cache_read=0.0509, cache_write=0.0509),
        quirks=ModelQuirks(**_UNVOUCHED),
        text_only=True,
    ),
    # --- Decision models ---
    # TypeSafe Jev answers typed questions and generates no text; output is not billed.
    # One key covers `jev-latest`, `jev-1.13.0` and Workers AI's `typesafe/jev`.
    "jev": ModelCatalogEntry(
        context_window=32_000,
        # No generated text to bound; the floor every entry shares, not a real limit.
        max_output_tokens=1_024,
        pricing=ModelPricing(input=0.042, output=0.0, cache_read=0.0, cache_write=0.0),
        text_only=True,
    ),
    # Cloudflare's Clef, Jev-API compatible, on Workers AI. 64K context; it also reads
    # images, which the decider does not send yet. Longest key wins, so `clef-flash` is
    # not billed at Clef's rate.
    "@cf/cloudflare/clef": ModelCatalogEntry(
        context_window=65_536,
        max_output_tokens=1_024,
        pricing=ModelPricing(input=0.24, output=0.0, cache_read=0.0, cache_write=0.0),
        text_only=True,
    ),
    "@cf/cloudflare/clef-flash": ModelCatalogEntry(
        context_window=65_536,
        max_output_tokens=1_024,
        pricing=ModelPricing(input=0.09, output=0.0, cache_read=0.0, cache_write=0.0),
        text_only=True,
    ),
    # --- Llama, wherever it is served ---
    # Unpriced, not free. Llama is sold by Workers AI, Groq, Together and Fireworks at
    # different rates, and `entry_for` matches by substring, so pricing this entry at zero
    # would have made every hosted Llama free to `limits.max_cost_usd`. A hosted rate
    # lives under that host's own key, like the `@cf/` entries above.
    "llama": ModelCatalogEntry(context_window=128_000),
}

# Unknown ids split their defaults on purpose. The *request shape* assumes the current
# Claude generation, because sending a parameter that was removed is a hard 400 while
# omitting an optional one is not — so guessing "modern" fails safe. The *context window*
# stays at the conservative 128K that `/v1/models` has always advertised, because
# over-advertising a window invites a client to send a request the model will reject.
_DEFAULT = ModelCatalogEntry(
    context_window=128_000,
    max_output_tokens=128_000,
    # Deliberately unpriced. This used to be `ModelPricing()`, whose defaults are Claude
    # Sonnet's rates — so every model Felix did not recognise, including anything an
    # operator added through `FELIX_MODEL_ROUTES`, was billed at $3/$15 per Mtok and
    # measured against `limits.max_cost_usd` on that basis. A 20x-wrong number is worse
    # than no number, because it looks like enforcement.
    pricing=None,
    # Unvouched for, like the family keys: no forced tool choice, no native format.
    quirks=replace(_MODERN_QUIRKS, **_UNVOUCHED),
)


def entry_for(model_id: str | None) -> ModelCatalogEntry:
    """The catalog entry for a wire model id, by longest matching key.

    Falls back to `_DEFAULT` for an unrecognised id, which is right for sizing a context
    window and wrong for shaping a request — see `known_entry_for`.
    """
    return known_entry_for(model_id) or _DEFAULT


def all_entries() -> dict[str, ModelCatalogEntry]:
    """Every catalog key and its entry, for callers that publish a table view."""
    return dict(_CATALOG)


def clamp_effort(level: str, quirks: ModelQuirks) -> str:
    """Coerce an effort level to one the model accepts."""
    lvl = (level or "").strip().lower()
    if lvl not in {"low", "medium", "high", "xhigh", "max"}:
        return "high"
    if lvl == "xhigh" and not quirks.effort_xhigh:
        return "high"
    return lvl


# The harness's thinking levels (`felix.session.thinking.THINKING_LEVELS`) as the effort a
# model that takes one is sent. The vocabulary is repeated rather than imported because this
# package may not import `felix`; `tests/unit/test_thinking_effort.py` pins the two together.
#
# Effort used to be derived from the level's *budget*, through thresholds at 4,096 / 16,384 /
# 32,768 that none of the budgets were chosen against: minimal, low, medium and high all sent
# `low`, xhigh sent `medium`, max sent `high`, and the top two tiers were unreachable
# (felix-run/felix#398). A level now names its effort directly; `minimal` has no tier of its
# own, so it shares `low`. Clamp the result with `clamp_effort` for the model at hand.
EFFORT_FOR_LEVEL: dict[str, str] = {
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
}

# For a spec carrying a budget and no level (a manifest that sets `thinking_budget` itself):
# the effort of the highest level whose budget this one reaches. The floors are the level
# budgets in `felix.session.thinking.THINKING_BUDGETS`, so a budget a level would have set
# lands on that level's effort, and anything at or above the `max` budget reaches the top.
_EFFORT_BUDGET_FLOORS: tuple[tuple[int, str], ...] = (
    (32_000, "max"),
    (8_192, "xhigh"),
    (2_048, "high"),
    (1_024, "medium"),
)


def effort_for_budget(budget: int) -> str:
    """The effort a bare thinking budget asks for, read against the level budgets."""
    for floor, effort in _EFFORT_BUDGET_FLOORS:
        if budget >= floor:
            return effort
    return "low"


def effort_for_spec(spec: Any) -> str | None:
    """The unclamped effort a model spec asks for, or `None` when thinking is off.

    Thinking is on when the spec carries a budget — the same gate the budget path uses, so
    the two paths never disagree about *whether* to think. The level decides *how hard*
    when it names one; otherwise the budget does.
    """
    budget = getattr(spec, "thinking_budget", None) if spec is not None else None
    if not budget:
        return None
    level = str(getattr(spec, "thinking_level", None) or "").strip().lower()
    return EFFORT_FOR_LEVEL.get(level) or effort_for_budget(int(budget))


def known_entry_for(model_id: str | None) -> ModelCatalogEntry | None:
    """The catalog entry for a model, or `None` when nothing matched.

    `entry_for` answers with `_DEFAULT` for an unrecognised id, which is right for sizing a
    context window but wrong for *shaping a request*: `_DEFAULT.quirks` describes the
    current Claude generation, so applying it to an unknown OpenAI-compatible endpoint
    would strip `temperature` from a model that accepts it. A rule you did not actually
    match is not a rule.
    """
    mid = (model_id or "").strip().lower()
    if not mid:
        return None
    best: tuple[int, ModelCatalogEntry] | None = None
    for key, entry in _CATALOG.items():
        if key in mid and (best is None or len(key) > best[0]):
            best = (len(key), entry)
    return best[1] if best else None


def accepts_images(model_id: str | None, modalities: tuple[str, ...] | None = None) -> bool | None:
    """Whether a model takes image input: True, False, or `None` for not known.

    `modalities` is the route's own declaration (`FELIX_MODEL_ROUTES`) and wins, because
    the operator knows what is behind a custom route and the catalog matches by substring.
    Otherwise False only for an entry vouched `text_only`, and `None` for everything Felix
    cannot vouch for either way -- which callers treat as "send it", the behaviour before
    this check existed, rather than stripping images from a vision model it failed to name.
    """
    if modalities is not None:
        return "image" in modalities
    entry = known_entry_for(model_id)
    if entry is None:
        return None
    if "image" in entry.input_modalities:
        return True
    return False if entry.text_only else None


def is_priced(model_id: str | None) -> bool:
    """Whether Felix knows this model's rates well enough to enforce a spend cap."""
    return entry_for(model_id).pricing is not None


__all__ = [
    "EFFORT_FOR_LEVEL",
    "ModelCatalogEntry",
    "ModelPricing",
    "ModelQuirks",
    "accepts_images",
    "all_entries",
    "clamp_effort",
    "effort_for_budget",
    "effort_for_spec",
    "entry_for",
    "is_priced",
    "known_entry_for",
]
