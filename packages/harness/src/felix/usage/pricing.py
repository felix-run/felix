"""Model price table for per-message cost estimation."""

from __future__ import annotations

from typing import Any

# USD per 1M tokens — approximate public list prices; override via manifest meta.
DEFAULT_PRICES: dict[str, dict[str, float]] = {
    "default": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75},
    "claude-sonnet": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75},
    "claude-haiku": {"input": 0.8, "output": 4.0, "cache_read": 0.08, "cache_write": 1.0},
    "gpt-4o": {"input": 2.5, "output": 10.0, "cache_read": 1.25, "cache_write": 2.5},
    "gpt-4o-mini": {"input": 0.15, "output": 0.6, "cache_read": 0.075, "cache_write": 0.15},
}


def _lookup_price(model_id: str) -> dict[str, float]:
    mid = (model_id or "").lower()
    for key, price in DEFAULT_PRICES.items():
        if key != "default" and key in mid:
            return price
    return DEFAULT_PRICES["default"]


def estimate_cost(
    *,
    model_id: str = "",
    tokens_input: int = 0,
    tokens_output: int = 0,
    cache_read: int = 0,
    cache_creation: int = 0,
    price_override: dict[str, float] | None = None,
) -> dict[str, float]:
    """Return cost breakdown in USD."""
    p = dict(_lookup_price(model_id))
    if price_override:
        p.update(price_override)
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
    price_override: dict[str, float] | None = None,
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
