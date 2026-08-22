"""Model catalog metadata for OpenAI-compatible listing."""

from __future__ import annotations

from typing import Any

from felix.session.thinking import THINKING_LEVELS
from felix.usage.pricing import _lookup_price

# Approximate context windows for known model families.
_CONTEXT_WINDOWS: dict[str, int] = {
    "claude-sonnet": 200_000,
    "claude-haiku": 200_000,
    "claude": 200_000,
    "gpt-4.1": 1_047_576,
    "gpt-4o": 128_000,
    "gpt-4": 128_000,
    "llama": 128_000,
    "default": 128_000,
}


def context_window_for(model_id: str | None, *, override: int | None = None) -> int:
    if override is not None and override > 0:
        return int(override)
    mid = (model_id or "").lower()
    for key, window in _CONTEXT_WINDOWS.items():
        if key != "default" and key in mid:
            return window
    return _CONTEXT_WINDOWS["default"]


def supported_thinking_levels(model_id: str | None = None) -> list[str]:
    """Return thinking levels; empty when the model family does not support them."""
    mid = (model_id or "").lower()
    # Conservative: advertise for frontier chat models that accept thinking budgets.
    if any(k in mid for k in ("claude", "gpt-4", "o1", "o3", "o4")):
        return list(THINKING_LEVELS)
    return ["off"]


def model_catalog_entry(
    *,
    model_id: str,
    owned_by: str = "felix",
    context_window: int | None = None,
    price: dict[str, float] | None = None,
    modalities: list[str] | None = None,
    created: int = 0,
) -> dict[str, Any]:
    """Build an OpenAI-shaped model object with Felix catalog extensions."""
    prices = price or _lookup_price(model_id)
    return {
        "id": model_id,
        "object": "model",
        "created": created,
        "owned_by": owned_by,
        "felix": {
            "contextWindow": context_window_for(model_id, override=context_window),
            "cost": {
                "inputPerMillion": float(prices.get("input") or 0),
                "outputPerMillion": float(prices.get("output") or 0),
                "cacheReadPerMillion": float(prices.get("cache_read") or 0),
                "cacheWritePerMillion": float(
                    prices.get("cache_write") or prices.get("input") or 0
                ),
            },
            "modalities": modalities or ["text"],
            "supportedThinkingLevels": supported_thinking_levels(model_id),
        },
    }


def catalog_from_manifest(name: str, manifest: Any | None = None) -> dict[str, Any]:
    """Enrich a manifest listing with model-spec metadata when available."""
    model_id = name
    context_window = None
    price = None
    if manifest is not None:
        spec = getattr(getattr(manifest, "spec", None), "model", None)
        if spec is not None:
            mid = getattr(spec, "id", None)
            if mid:
                model_id = str(mid)
            session = getattr(getattr(manifest, "spec", None), "session", None)
            if session is not None:
                cw = getattr(session, "context_window_tokens", None)
                if cw:
                    context_window = int(cw)
            raw_price = getattr(spec, "price", None)
            if isinstance(raw_price, dict) and raw_price:
                price = dict(raw_price)
    entry = model_catalog_entry(
        model_id=name,
        context_window=context_window,
        price=price or _lookup_price(model_id),
    )
    # Keep OpenAI id as the manifest name (Felix convention); nest provider model under felix.
    entry["felix"]["providerModel"] = model_id if model_id != name else None
    return entry


__all__ = [
    "catalog_from_manifest",
    "context_window_for",
    "model_catalog_entry",
    "supported_thinking_levels",
]
