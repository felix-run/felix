"""Model price table for per-message cost estimation.

Rates live on the model catalog alongside context window and request quirks, so a model
is described in one place. `DEFAULT_PRICES` is kept as a view over it for callers and
tests that read the table directly.
"""

from __future__ import annotations

from typing import Any

from felix.model_catalog import all_entries, entry_for, is_priced

# An unknown model has no rates at all, which is different from having zero rates: it must
# contribute nothing to spend rather than contribute a guess. `_UNPRICED` is what the cost
# maths sees, and `is_priced()` is how callers tell the two apart.
_UNPRICED: dict[str, Any] = {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0}


def _prices_view() -> dict[str, dict[str, Any]]:
    view = {
        key: (entry.pricing.as_dict() if entry.pricing is not None else dict(_UNPRICED))
        for key, entry in all_entries().items()
    }
    view["default"] = dict(_UNPRICED)
    return view


DEFAULT_PRICES: dict[str, dict[str, Any]] = _prices_view()


def _lookup_price(model_id: str) -> dict[str, Any]:
    """Rates for a model id, resolved through the catalog's single matching rule."""
    pricing = entry_for(model_id).pricing
    return pricing.as_dict() if pricing is not None else dict(_UNPRICED)


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


__all__ = ["DEFAULT_PRICES", "estimate_cost", "is_priced", "usage_with_cost"]
