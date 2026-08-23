"""Model price table for per-message cost estimation."""

from __future__ import annotations

from typing import Any

# USD per 1M tokens — approximate public list prices; override via manifest meta.
# Longest matching key wins, so "claude-opus-5" is not shadowed by "claude-opus".
# Cache reads are 0.1x input and cache writes 1.25x input across the Claude family.
#
# A price entry may also carry "tiers": a list of
# ``{"input_tokens_above": N, "input": …, "output": …, "cache_read": …, "cache_write": …}``
# rows. Several providers bill long-context requests at a higher rate across the *whole*
# request once total input passes a threshold, rather than only on the tokens above it —
# so the highest matching tier replaces the base rates entirely. No bundled entry sets
# tiers: the thresholds and rates move, and a wrong number here silently mis-charges a
# tenant and mis-enforces `limits.max_cost_usd`. Fill them from the provider's current
# price sheet, per deployment, via a manifest price override.
DEFAULT_PRICES: dict[str, dict[str, Any]] = {
    "default": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75},
    "claude-fable": {"input": 10.0, "output": 50.0, "cache_read": 1.0, "cache_write": 12.5},
    "claude-mythos": {"input": 10.0, "output": 50.0, "cache_read": 1.0, "cache_write": 12.5},
    "claude-opus": {"input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25},
    "claude-sonnet": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75},
    # Haiku 4.5 is $1.00 / $5.00; the previous 0.8 / 4.0 under-reported every run.
    "claude-haiku": {"input": 1.0, "output": 5.0, "cache_read": 0.1, "cache_write": 1.25},
    "gpt-4o": {"input": 2.5, "output": 10.0, "cache_read": 1.25, "cache_write": 2.5},
    "gpt-4o-mini": {"input": 0.15, "output": 0.6, "cache_read": 0.075, "cache_write": 0.15},
}


def _lookup_price(model_id: str) -> dict[str, Any]:
    mid = (model_id or "").lower()
    # Longest match, not first match: dict order previously decided whether
    # "claude-opus-5" matched "claude-opus" or something shorter.
    best: tuple[int, dict[str, Any]] | None = None
    for key, price in DEFAULT_PRICES.items():
        if key != "default" and key in mid and (best is None or len(key) > best[0]):
            best = (len(key), price)
    return best[1] if best else DEFAULT_PRICES["default"]


def _apply_tier(price: dict[str, Any], total_input_tokens: int) -> dict[str, Any]:
    """Fold the highest matching long-context tier into the base rates.

    Tiers are request-wide, not marginal: crossing a threshold reprices every input and
    output token of the request, so the matching tier's rates replace the base rates
    rather than applying only to the excess.
    """
    tiers = price.get("tiers")
    if not isinstance(tiers, list) or not tiers:
        return price
    matched: dict[str, Any] | None = None
    threshold = -1
    for tier in tiers:
        if not isinstance(tier, dict):
            continue
        above = int(tier.get("input_tokens_above") or 0)
        if total_input_tokens > above and above > threshold:
            matched, threshold = tier, above
    if matched is None:
        return price
    merged = {k: v for k, v in price.items() if k != "tiers"}
    merged.update({k: v for k, v in matched.items() if k != "input_tokens_above"})
    return merged


def estimate_cost(
    *,
    model_id: str = "",
    tokens_input: int = 0,
    tokens_output: int = 0,
    cache_read: int = 0,
    cache_creation: int = 0,
    price_override: dict[str, Any] | None = None,
) -> dict[str, float]:
    """Return cost breakdown in USD."""
    p = dict(_lookup_price(model_id))
    if price_override:
        p.update(price_override)
    p = _apply_tier(p, tokens_input + cache_read + cache_creation)
    inp = tokens_input / 1_000_000 * float(p.get("input") or 0)
    out = tokens_output / 1_000_000 * float(p.get("output") or 0)
    cr = cache_read / 1_000_000 * float(p.get("cache_read") or 0)
    cw = cache_creation / 1_000_000 * float(p.get("cache_write") or p.get("input") or 0)
    return {
        "input": round(inp, 8),
        "output": round(out, 8),
        "cacheRead": round(cr, 8),
        "cacheWrite": round(cw, 8),
        "total": round(inp + out + cr + cw, 8),
    }


def usage_with_cost(
    usage: Any,
    *,
    model_id: str = "",
    price_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize TokenUsage / dict into a usage block with cost."""
    if usage is None:
        return {}
    if isinstance(usage, dict):
        inp = int(usage.get("input") or usage.get("tokens_input") or 0)
        out = int(usage.get("output") or usage.get("tokens_output") or 0)
        cr = int(usage.get("cache_read") or usage.get("cacheRead") or 0)
        cw = int(usage.get("cache_creation") or usage.get("cacheWrite") or 0)
        reasoning = usage.get("reasoning")
    else:
        inp = int(getattr(usage, "input", 0) or 0)
        out = int(getattr(usage, "output", 0) or 0)
        cr = int(getattr(usage, "cache_read", 0) or 0)
        cw = int(getattr(usage, "cache_creation", 0) or 0)
        reasoning = getattr(usage, "reasoning", None)
    cost = estimate_cost(
        model_id=model_id,
        tokens_input=inp,
        tokens_output=out,
        cache_read=cr,
        cache_creation=cw,
        price_override=price_override,
    )
    total = inp + out + cr + cw
    out_d: dict[str, Any] = {
        "input": inp,
        "output": out,
        "cacheRead": cr,
        "cacheWrite": cw,
        "totalTokens": total,
        "cost": cost,
    }
    if reasoning is not None:
        out_d["reasoning"] = int(reasoning)
    return out_d


__all__ = ["DEFAULT_PRICES", "estimate_cost", "usage_with_cost"]
