"""felix.usage — durable token/turn meters, cost, and model catalog."""

from felix.usage.catalog import (
    catalog_from_manifest,
    context_window_for,
    model_catalog_entry,
    supported_thinking_levels,
)
from felix.usage.pricing import estimate_cost, usage_with_cost
from felix.usage.store import flush_pending, query, record_tokens

__all__ = [
    "catalog_from_manifest",
    "context_window_for",
    "estimate_cost",
    "flush_pending",
    "model_catalog_entry",
    "query",
    "record_tokens",
    "supported_thinking_levels",
    "usage_with_cost",
]
