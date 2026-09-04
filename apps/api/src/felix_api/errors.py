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
    from felix.manifests.loader import ManifestParseError
    from felix.manifests.pin import ManifestDriftError
    from felix.patterns.model import ModelGatewayError

    types: tuple[type[BaseException], ...] = (
        ModelGatewayError,
        ManifestDriftError,
        InboundAuthError,
        # Rendered by felix.manifests.loader without the offending value, precisely so it
        # can travel: the whole point of the refusal is that an operator can read it.
        ManifestParseError,
    )
    try:
        # Optional package on a lean install, so its absence must not turn a content
        # filter's explanation into "internal error" -- that message is the entire
        # point of the response, and losing it silently is worse than the alert this
        # funnel was built to answer.
        from felix.governance.inbound import InboundScreeningError
    except ImportError:  # pragma: no cover - exercised only on a lean install
        return types
    return (*types, InboundScreeningError)


def client_safe_message(exc: BaseException, *, authored_for_clients: bool = False) -> str:
    """The message to put in a response body for `exc`.

    Curated for the types that opted in; otherwise a fixed string plus the request id,
    so the report and the traceback can be joined up without the traceback travelling.

    `authored_for_clients` is for the handful of sites that raise a *builtin* exception
    type whose message they wrote for a client to read -- `unknown_thinking_level:high`
    echoes back what the caller sent. The type cannot carry that fact, so the call site
    asserts it. It is deliberately verbose and greppable: every relay in the codebase
    should be findable in one search, and a reviewer should be able to ask "who decided
    this string was safe" and get an answer from the line itself.
    """
    if authored_for_clients or isinstance(exc, _relayable()):
        # `.detail` where the type provides one: it is the field those errors carry
        # *for* display, and preferring it keeps every caller on one line of code
        # rather than each deciding which attribute is the client-facing one.
        detail = getattr(exc, "detail", None)
        return str(detail) if isinstance(detail, str) and detail else str(exc)
    request_id = get_request_id()
    if request_id and request_id != "-":
        return f"internal error (request {request_id})"
    return "internal error"
