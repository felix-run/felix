"""Moved to the `felix-client` package: `from felix_client import FelixClient`.

Kept so existing `from felix.sdk import FelixClient` imports go on working. New code should import
`felix_client`, which installs without the harness.
"""

from felix_client import (
    RUN_POLL_CEILING_SECONDS,
    RUN_POLL_FACTOR,
    RUN_POLL_FLOOR_SECONDS,
    RUN_TERMINAL,
    FelixClient,
)

__all__ = [
    "RUN_POLL_CEILING_SECONDS",
    "RUN_POLL_FACTOR",
    "RUN_POLL_FLOOR_SECONDS",
    "RUN_TERMINAL",
    "FelixClient",
]
