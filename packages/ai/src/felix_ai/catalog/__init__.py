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


@dataclass(frozen=True)
class ModelPricing:
    """USD per 1M tokens, plus optional request-wide long-context tiers.

    Tiers are not marginal: crossing a threshold reprices every token of the request, so
    the matching tier's rates replace the base rates rather than applying to the excess.
    No bundled entry sets tiers — thresholds and rates move, and a stale number here both
    mis-charges a tenant and mis-enforces the fail-closed `limits.max_cost_usd`. Supply
    them per deployment through a manifest price override.
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
    # `None` means *unknown*, not free. `ModelPricing()`'s field defaults are Claude
    # Sonnet's list price, so an entry that simply omitted rates used to bill an
    # unrelated model at $3/$15 per Mtok. A model with no known rates must price at
    # nothing and be reported as unmeterable, never guessed at.
    pricing: ModelPricing | None = field(default_factory=ModelPricing)
    quirks: ModelQuirks = field(default_factory=ModelQuirks)
    # Whether the model accepts thinking at all. The *level* vocabulary lives in
    # `felix.session.thinking`; importing it here would cycle back through
    # felix.session.__init__ -> compaction -> patterns -> model.py, so the catalog
    # records the capability and `usage.catalog` expands it into level names.
    supports_thinking: bool = False
    input_modalities: tuple[str, ...] = ("text",)


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

_OPUS_PRICE = ModelPricing(input=5.0, output=25.0, cache_read=0.5, cache_write=6.25)
_SONNET_PRICE = ModelPricing(input=3.0, output=15.0, cache_read=0.3, cache_write=3.75)
_HAIKU_PRICE = ModelPricing(input=1.0, output=5.0, cache_read=0.1, cache_write=1.25)
_FABLE_PRICE = ModelPricing(input=10.0, output=50.0, cache_read=1.0, cache_write=12.5)

_FRONTIER = ModelCatalogEntry(
    context_window=1_000_000,
    max_output_tokens=128_000,
    pricing=_SONNET_PRICE,
    quirks=_MODERN_QUIRKS,
    supports_thinking=True,
    input_modalities=_TEXT_AND_IMAGE,
)
_PRE_46 = ModelCatalogEntry(
    context_window=200_000,
    max_output_tokens=64_000,
    pricing=_SONNET_PRICE,
    quirks=_LEGACY_QUIRKS,
    supports_thinking=True,
    input_modalities=_TEXT_AND_IMAGE,
)

_FAMILY = replace(_PRE_46, quirks=_MODERN_QUIRKS, max_output_tokens=128_000)

_CATALOG: dict[str, ModelCatalogEntry] = {
    # --- Claude, current generation (1M context, adaptive thinking) ---
    "claude-fable-5": replace(_FRONTIER, pricing=_FABLE_PRICE),
    "claude-mythos-5": replace(_FRONTIER, pricing=_FABLE_PRICE),
    "claude-opus-5": replace(
        _FRONTIER,
        pricing=_OPUS_PRICE,
        quirks=replace(_MODERN_QUIRKS, thinking_on_by_default=True),
    ),
    "claude-opus-4-8": replace(_FRONTIER, pricing=_OPUS_PRICE),
    "claude-opus-4-7": replace(_FRONTIER, pricing=_OPUS_PRICE),
    "claude-opus-4-6": replace(_FRONTIER, pricing=_OPUS_PRICE, quirks=_V46_QUIRKS),
    "claude-sonnet-5": _FRONTIER,
    "claude-sonnet-4-6": replace(_FRONTIER, quirks=_V46_QUIRKS),
    # --- Claude, pre-4.6 (200K context, fixed thinking budgets) ---
    "claude-opus-4-5": replace(
        _PRE_46,
        pricing=_OPUS_PRICE,
        quirks=replace(_LEGACY_QUIRKS, effort=True, effort_xhigh=False),
    ),
    "claude-sonnet-4-5": _PRE_46,
    "claude-haiku-4-5": replace(_PRE_46, pricing=_HAIKU_PRICE),
    # Family fallbacks for ids with no exact entry. Split defaults on purpose:
    # conservative on *context*, since advertising 1M for an unrecognised snapshot of an
    # old family invites a request the model rejects; but *modern* on request shape,
    # since sending a parameter the model removed is a hard 400 while omitting an
    # optional one is not. Guessing "current generation" is the direction that fails safe.
    "claude-opus": replace(_FAMILY, pricing=_OPUS_PRICE),
    "claude-sonnet": _FAMILY,
    "claude-haiku": replace(_FAMILY, pricing=_HAIKU_PRICE),
    "claude-fable": replace(_FAMILY, pricing=_FABLE_PRICE),
    "claude-mythos": replace(_FAMILY, pricing=_FABLE_PRICE),
    "claude": _FAMILY,
    # --- OpenAI ---
    # No bundled rate: this family had no entry in the price table and billed at the
    # default. Consolidating must not quietly introduce pricing that was never asserted.
    "gpt-4.1": ModelCatalogEntry(
        context_window=1_047_576,
        max_output_tokens=32_768,
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
    "o1": ModelCatalogEntry(context_window=128_000, supports_thinking=True),
    "o3": ModelCatalogEntry(context_window=128_000, supports_thinking=True),
    "o4": ModelCatalogEntry(context_window=128_000, supports_thinking=True),
    # --- Local ---
    # Free: a model served by a local Ollama or vLLM costs nothing per token. This is a
    # statement about the deployment, not a rate card, and it is the reason a local
    # model must not inherit the unknown-price path.
    "llama": ModelCatalogEntry(context_window=128_000, pricing=ModelPricing(0.0, 0.0, 0.0, 0.0)),
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
    quirks=_MODERN_QUIRKS,
)


def entry_for(model_id: str | None) -> ModelCatalogEntry:
    """The catalog entry for a wire model id, by longest matching key."""
    mid = (model_id or "").strip().lower()
    if not mid:
        return _DEFAULT
    best: tuple[int, ModelCatalogEntry] | None = None
    for key, entry in _CATALOG.items():
        if key in mid and (best is None or len(key) > best[0]):
            best = (len(key), entry)
    return best[1] if best else _DEFAULT


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


def is_priced(model_id: str | None) -> bool:
    """Whether Felix knows this model's rates well enough to enforce a spend cap."""
    return entry_for(model_id).pricing is not None


__all__ = [
    "ModelCatalogEntry",
    "ModelPricing",
    "ModelQuirks",
    "all_entries",
    "clamp_effort",
    "entry_for",
    "is_priced",
]
