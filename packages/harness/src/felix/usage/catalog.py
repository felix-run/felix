"""Model catalog metadata for OpenAI-compatible listing."""

from __future__ import annotations

from typing import Any

from felix.model_catalog import entry_for
from felix.session.thinking import THINKING_LEVELS
from felix.usage.pricing import _lookup_price


def context_window_for(model_id: str | None, *, override: int | None = None) -> int:
    """Context window for a model id, from the catalog unless the manifest overrides it."""
    if override is not None and override > 0:
        return int(override)
    return entry_for(model_id).context_window


def supported_thinking_levels(model_id: str | None = None) -> list[str]:
    """Thinking levels the model accepts; `["off"]` when it supports none.

    The catalog records only *whether* a model thinks; the level vocabulary lives here
    because `felix.session.thinking` cannot be imported from the catalog without cycling
    back through the pattern layer.
    """
    return list(THINKING_LEVELS) if entry_for(model_id).supports_thinking else ["off"]


def modalities_for(model_id: str | None = None) -> list[str]:
    """Input modalities the model accepts."""
    return list(entry_for(model_id).input_modalities)


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
                "cacheWritePerMillion": float(prices.get("cache_write") or prices.get("input") or 0),
            },
            "modalities": modalities or modalities_for(model_id),
            "supportedThinkingLevels": supported_thinking_levels(model_id),
        },
    }


def catalog_from_manifest(name: str, manifest: Any | None = None) -> dict[str, Any]:
    """Enrich a manifest listing with model-spec metadata when available."""
    model_id = name
    context_window = None
    price = None
    starters: list[dict[str, str]] = []
    greeting: dict[str, str | None] | None = None
    if manifest is not None:
        meta = getattr(manifest, "metadata", None)
        starters = [{"title": s.title, "prompt": s.prompt} for s in (getattr(meta, "starters", None) or [])]
        declared = getattr(meta, "greeting", None)
        if declared is not None:
            greeting = {"headline": declared.headline, "subtitle": declared.subtitle}
        # The window compaction uses: the declared value, else the model's own — resolved
        # through the route, or through the default route when the manifest names no model.
        # Asked for every manifest, not only one with a `model` block: without one, the
        # listing fell back to looking the window up by the *manifest's* name, i.e. 128K.
        from felix.runtime import _context_window_for_manifest

        session = getattr(getattr(manifest, "spec", None), "session", None)
        context_window = _context_window_for_manifest(manifest, session)
        spec = getattr(getattr(manifest, "spec", None), "model", None)
        if spec is not None:
            mid = getattr(spec, "id", None)
            if mid:
                model_id = str(mid)
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
    # Always a list, empty when the manifest declares none or could not be resolved, so a
    # client can tell "this harness lists starters" from an older one that has no key.
    entry["felix"]["starters"] = starters
    # `null` when the manifest declares none: the client's own greeting stands.
    entry["felix"]["greeting"] = greeting
    return entry


__all__ = [
    "catalog_from_manifest",
    "context_window_for",
    "modalities_for",
    "model_catalog_entry",
    "supported_thinking_levels",
]
