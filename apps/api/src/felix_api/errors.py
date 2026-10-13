"""What an API client may be told about a failure.

The relay rules themselves live in `felix.errors`, so the harness writes a stored result a
route returns (a job run's error) by the same rule; they are re-exported here for the routes.

The SSE handlers relayed `str(exc)` from a bare `except Exception` straight into the
response body. Most of the time that is a tidy domain message; the rest of the time it
is whatever a driver, a serializer or an assertion happened to say, which is how
connection strings, internal paths and table names reach an external client. The client
cannot act on any of it either way — a caller who is told "relation felix_events does
not exist" has learned something about our schema and nothing about their request.

So relaying is opt-in. An exception type earns it by keeping its message deliberately
client-facing; `ModelGatewayError` is the clearest case, since it puts the provider's
response body on `.body` for logs and keeps it out of `str()` precisely because that
string is relayed. Everything else gets a stable message and the request id, which is
the thing that actually helps: it lets whoever reports the problem point an operator at
the exact logged traceback.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from felix.errors import _relayable, client_safe_message, internal_error_message
from felix.logging_setup import loggable

if TYPE_CHECKING:
    from felix.patterns.model import ModelGatewayError


def log_gateway_error(logger: logging.Logger, exc: ModelGatewayError) -> None:
    """The one place a `ModelGatewayError` is written to the log.

    `exc.body` is an upstream response — untrusted, multi-line, and kept off `str(exc)`
    precisely because that string is relayed to the client — and `exc.label` is a header
    value that is as forgeable as anything the client sends. Both go through `loggable`,
    and the discipline lives here rather than at each of the four routes that catch it.
    """
    logger.warning(
        "model gateway error label=%s status=%s body=%s",
        loggable(exc.label, limit=80),
        exc.status,
        loggable(exc.body),
    )


__all__ = ["_relayable", "client_safe_message", "internal_error_message", "log_gateway_error"]
