"""What a failure tells an API client.

CodeQL reports two `py/stack-trace-exposure` errors on `openai_compat.py`. Both turn
out to be narrowed to domain errors whose messages are written for clients —
`ModelGatewayError` documents that its `str()` is relayed and keeps the provider body
on `.body` for logs precisely because of it. What the rule was pointing at is the
*shape*: a bare `except Exception` whose scope then formats a message for a response.

The unflagged sites were the real ones. `chat.py` relayed `str(exc)` from a bare catch
into the SSE body, so a driver error, a serializer failure or an assertion reached an
external client verbatim. A caller told "relation felix_events does not exist" has
learned about our schema and nothing about their request.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ROUTES = ROOT / "apps/api/src/felix_api/routes"


def test_a_domain_error_still_reaches_the_client_intact() -> None:
    """Relaying is opt-in, not off — the useful messages must survive."""
    from felix.manifests.pin import ManifestDriftError
    from felix.patterns.model import ModelGatewayError
    from felix_api.errors import client_safe_message

    gateway = ModelGatewayError("anthropic", 429, '{"error":"org_abc quota exceeded"}')
    assert client_safe_message(gateway) == "anthropic provider returned HTTP 429"
    # The provider body is what must not travel, and it does not.
    assert "org_abc" not in client_safe_message(gateway)

    drift = ManifestDriftError("pinned compile hash no longer matches")
    assert client_safe_message(drift) == "pinned compile hash no longer matches"


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError('relation "felix_events" does not exist\nLINE 1: SELECT * FROM felix_events'),
        KeyError("tenant_secret_arn"),
        ValueError("could not connect to postgresql://felix:hunter2@10.0.0.7:5432/felix"),
        AssertionError("expected session lease at /srv/felix/state/leases/acme"),
    ],
    ids=["db-schema", "internal-key", "connection-string", "internal-path"],
)
def test_an_internal_error_tells_the_client_nothing_about_us(exc: Exception) -> None:
    from felix_api.errors import client_safe_message

    message = client_safe_message(exc)
    assert message.startswith("internal error")
    for leak in ("felix_events", "hunter2", "10.0.0.7", "/srv/felix", "tenant_secret_arn"):
        assert leak not in message, f"{leak!r} reached the client in {message!r}"


def test_the_message_carries_the_request_id_so_a_report_can_be_traced() -> None:
    """The replacement has to stay actionable, or it just moves the problem to support."""
    from felix.logging_setup import new_request_id, reset_request_id, set_request_id
    from felix_api.errors import client_safe_message

    token = set_request_id(new_request_id())
    try:
        message = client_safe_message(RuntimeError("boom"))
        assert message.startswith("internal error (request ")
    finally:
        reset_request_id(token)


# --- the shape, not just the instances ----------------------------------------------


def _bare_except_bodies(tree: ast.AST) -> list[ast.ExceptHandler]:
    """`except Exception:` / bare `except:` handlers — the ones with unbounded reach."""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        t = node.type
        if t is None or (isinstance(t, ast.Name) and t.id in {"Exception", "BaseException"}):
            out.append(node)
    return out


# The calls that put text into an HTTP response. Anything else a handler does with the
# exception -- logging it, testing its type, re-raising -- keeps it server-side.
RESPONSE_SINKS = {"error_frame", "JSONResponse", "HTTPException"}


@pytest.mark.parametrize("module", ["chat.py", "openai_compat.py"], ids=lambda p: p)
def test_no_broad_handler_relays_an_exception_into_a_response(module: str) -> None:
    """A broad handler may log the exception. It may not put it in the body.

    Stated structurally because the instances keep moving: this is the fourth site of
    this shape found in two days, and each was found by a scanner rather than by the
    edit that introduced it.
    """
    tree = ast.parse((ROUTES / module).read_text())
    for handler in _bare_except_bodies(tree):
        bound = handler.name
        if not bound:
            continue
        for node in ast.walk(handler):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name not in RESPONSE_SINKS:
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call) and getattr(inner.func, "id", "") == "client_safe_message":
                    break
            else:
                if any(isinstance(x, ast.Name) and x.id == bound for x in ast.walk(node)):
                    raise AssertionError(
                        f"{module}:{node.lineno} puts the caught exception `{bound}` into "
                        f"`{name}` — route it through `client_safe_message` instead."
                    )
