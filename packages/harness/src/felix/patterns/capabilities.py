"""Per-model request capabilities for the Anthropic path.

The request builder emitted one shape for every Claude model. That shape is now rejected
by the current generation:

* ``thinking: {"type": "enabled", "budget_tokens": N}`` — `budget_tokens` is **removed**
  on Fable 5, Opus 5, Opus 4.8/4.7 and Sonnet 5 (HTTP 400). Adaptive thinking replaces
  it, with depth controlled by ``output_config.effort``.
* ``temperature`` (and ``top_p``/``top_k``) — **removed** on the whole 4.6+ family (400).

So "does this model accept X" has to be data, not an assumption. Matching is by longest
prefix, so a dated snapshot id resolves to its family.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelCapabilities:
    """What one model's request surface accepts."""

    # thinking: {"type": "adaptive"} — the 4.6+ shape.
    adaptive_thinking: bool = False
    # thinking: {"type": "enabled", "budget_tokens": N} — pre-4.6 shape.
    budget_tokens: bool = True
    # temperature / top_p / top_k.
    sampling: bool = True
    # output_config.effort, and whether "xhigh" is one of the accepted levels.
    effort: bool = False
    effort_xhigh: bool = False
    # Hard ceiling for max_tokens.
    max_output_tokens: int = 8_192
    context_window: int = 200_000
    # Thinking is on unless explicitly disabled (Opus 5 behaves this way; 4.7/4.8 do not).
    thinking_on_by_default: bool = False


# Current generation: adaptive thinking, no budget_tokens, no sampling params.
_MODERN = ModelCapabilities(
    adaptive_thinking=True,
    budget_tokens=False,
    sampling=False,
    effort=True,
    effort_xhigh=True,
    max_output_tokens=128_000,
    context_window=1_000_000,
)

# 4.6: adaptive thinking arrived, sampling still accepted, budget_tokens deprecated but
# functional, and `xhigh` did not exist yet.
_V46 = ModelCapabilities(
    adaptive_thinking=True,
    budget_tokens=True,
    sampling=True,
    effort=True,
    effort_xhigh=False,
    max_output_tokens=128_000,
    context_window=1_000_000,
)

# Pre-4.6: fixed thinking budgets, sampling allowed, no effort.
_LEGACY = ModelCapabilities(
    adaptive_thinking=False,
    budget_tokens=True,
    sampling=True,
    effort=False,
    max_output_tokens=64_000,
    context_window=200_000,
)

# Longest-prefix wins, so "claude-opus-4-5-20251101" resolves to the 4.5 entry rather
# than to "claude-opus-4".
_TABLE: dict[str, ModelCapabilities] = {
    "claude-fable-5": _MODERN,
    "claude-mythos-5": _MODERN,
    "claude-opus-5": ModelCapabilities(
        adaptive_thinking=True,
        budget_tokens=False,
        sampling=False,
        effort=True,
        effort_xhigh=True,
        max_output_tokens=128_000,
        context_window=1_000_000,
        thinking_on_by_default=True,
    ),
    "claude-opus-4-8": _MODERN,
    "claude-opus-4-7": _MODERN,
    "claude-opus-4-6": _V46,
    "claude-sonnet-5": _MODERN,
    "claude-sonnet-4-6": _V46,
    "claude-haiku-4-5": ModelCapabilities(
        adaptive_thinking=False,
        budget_tokens=True,
        sampling=True,
        effort=False,
        max_output_tokens=64_000,
        context_window=200_000,
    ),
    "claude-sonnet-4-5": _LEGACY,
    "claude-opus-4-5": ModelCapabilities(
        adaptive_thinking=False,
        budget_tokens=True,
        sampling=True,
        effort=True,
        effort_xhigh=False,
        max_output_tokens=64_000,
        context_window=200_000,
    ),
}

# Unknown Claude ids: assume the current generation rather than the legacy shape.
# Sending a removed parameter is a hard 400; omitting an optional one is not, so this is
# the safe direction to guess in.
_DEFAULT_CLAUDE = _MODERN


def capabilities_for(model: str) -> ModelCapabilities:
    """Capabilities for a wire model id, by longest matching prefix."""
    mid = (model or "").strip().lower()
    if not mid:
        return _DEFAULT_CLAUDE
    best: tuple[int, ModelCapabilities] | None = None
    for prefix, caps in _TABLE.items():
        if mid.startswith(prefix) and (best is None or len(prefix) > best[0]):
            best = (len(prefix), caps)
    return best[1] if best else _DEFAULT_CLAUDE


def clamp_effort(level: str, caps: ModelCapabilities) -> str:
    """Coerce an effort level to one the model accepts."""
    lvl = (level or "").strip().lower()
    if lvl not in {"low", "medium", "high", "xhigh", "max"}:
        return "high"
    if lvl == "xhigh" and not caps.effort_xhigh:
        return "high"
    return lvl


__all__ = ["ModelCapabilities", "capabilities_for", "clamp_effort"]
