"""Model catalog — moved to `felix_ai.catalog`.

Re-exported here because the catalog is read from `usage/pricing.py`, `usage/catalog.py`,
`patterns/react.py` and `runtime.py`, none of which should have to know the model layer
moved out of the harness.
"""

from __future__ import annotations

from felix_ai.catalog import (
    ModelCatalogEntry,
    ModelPricing,
    ModelQuirks,
    all_entries,
    clamp_effort,
    entry_for,
    is_priced,
)

__all__ = [
    "ModelCatalogEntry",
    "ModelPricing",
    "ModelQuirks",
    "all_entries",
    "clamp_effort",
    "entry_for",
    "is_priced",
]
