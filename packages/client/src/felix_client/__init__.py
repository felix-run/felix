"""Experimental Python client for a Felix harness over HTTP.

Covers chat (prompt, stream, steer, follow-up, durable runs and their polling), approvals, and
GitHub login (`github_device_login`, with the token kept by `save_token`/`load_token`); the
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
from felix_client.login import (
    DeviceCode,
    LoginError,
    LoginToken,
    github_device_login,
    load_token,
    save_token,
    token_path,
)

__all__ = [
    "RUN_POLL_CEILING_SECONDS",
    "RUN_POLL_FACTOR",
    "RUN_POLL_FLOOR_SECONDS",
    "RUN_TERMINAL",
    "DeviceCode",
    "FelixClient",
    "LoginError",
    "LoginToken",
    "github_device_login",
    "load_token",
    "save_token",
    "token_path",
]
