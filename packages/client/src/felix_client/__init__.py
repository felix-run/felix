"""Experimental Python client for a Felix harness over HTTP.

Covers chat (prompt, stream, steer, follow-up, durable runs and their polling) and approvals; the
OpenAPI document attached to each release is the full contract. Depends on httpx and nothing in
Felix, so installing it does not install the server. Experimental: the surface may change between
releases without a deprecation period.
"""

from felix_client.client import (
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
