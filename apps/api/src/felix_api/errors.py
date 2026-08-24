"""What an API client may be told about a failure.

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

from felix.logging_setup import get_request_id


def _relayable() -> tuple[type[BaseException], ...]:
    """Exception types whose message is written to be read by an API client.

    Resolved on call rather than at import so this module stays importable from
    anywhere in the app without ordering constraints.
    """
    from felix.manifests.inbound_auth import InboundAuthError
    from felix.manifests.pin import ManifestDriftError
    from felix.patterns.model import ModelGatewayError

    return (ModelGatewayError, ManifestDriftError, InboundAuthError)


def client_safe_message(exc: BaseException) -> str:
    """The message to put in a response body for `exc`.

    Curated for the types that opted in; otherwise a fixed string plus the request id,
    so the report and the traceback can be joined up without the traceback travelling.
    """
    if isinstance(exc, _relayable()):
        return str(exc)
    request_id = get_request_id()
    if request_id and request_id != "-":
        return f"internal error (request {request_id})"
    return "internal error"
