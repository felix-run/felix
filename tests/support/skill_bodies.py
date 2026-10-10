"""SKILL.md bodies the skill tests share, rather than one copy per file.

Two invoice bodies, because their second step differs: the e2e tests assert `ROUTING_STEP`
reached the activated instructions; the unit tests assert on "Route amounts over the limit".
"""

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
