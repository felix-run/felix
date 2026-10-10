"""SKILL.md bodies the skill tests share, so a wording change happens in one place."""

from __future__ import annotations

INVOICE_TRIAGE = """# Invoice triage

Use this when an invoice arrives.

## Steps

1. Read the vendor and the amount.
2. Route amounts over the limit to finance.
"""

# The e2e skill tests assert this routing line reached the activated instructions verbatim.
ROUTING_STEP = "Route amounts over 500 to the finance queue."
INVOICE_TRIAGE_ROUTED = (
    "# Invoice triage\n\nUse this when an invoice arrives.\n\n## Steps\n\n"
    f"1. Read the vendor and the amount.\n2. {ROUTING_STEP}\n"
)
