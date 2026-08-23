"""An upstream provider's response body must not be relayed to API clients.

`ModelGatewayError` embedded `body[:200]` of the raw provider response in its message,
and both `/chat` and `/v1/chat/completions` return `str(exc)` to the caller — so provider
request ids, organization identifiers, quota and billing detail, and any echoed request
content were handed to whoever made the call. Flagged independently by CodeQL as
py/stack-trace-exposure.
"""

from __future__ import annotations

import logging

import pytest
from felix.patterns.model import ModelGatewayError

_SECRET_BODY = (
    '{"error":{"message":"insufficient_quota for org org_abc123",'
    '"request_id":"req_sensitive_9f3","internal_hint":"billing/customer/cus_XYZ"}}'
)


def test_message_excludes_the_upstream_body() -> None:
    exc = ModelGatewayError("anthropic", 429, _SECRET_BODY)
    msg = str(exc)
    assert "org_abc123" not in msg
    assert "req_sensitive_9f3" not in msg
    assert "cus_XYZ" not in msg
    # still useful to the caller
    assert "anthropic" in msg
    assert "429" in msg


def test_body_is_retained_for_server_side_logging() -> None:
    exc = ModelGatewayError("openai", 500, _SECRET_BODY)
    assert "req_sensitive_9f3" in exc.body
    assert exc.status == 500
    assert exc.label == "openai"


def test_body_is_bounded() -> None:
    exc = ModelGatewayError("openai", 500, "x" * 10_000)
    assert len(exc.body) == 2000


def test_empty_body_is_safe() -> None:
    assert ModelGatewayError("openai", 502, "").body == ""


@pytest.mark.asyncio
async def test_v1_response_does_not_leak_upstream_body(caplog: pytest.LogCaptureFixture) -> None:
    """End to end: the 502 payload carries no provider detail, but the log does."""
    import felix_api.routes.openai_compat as oc
    from felix.config import Settings
    from felix_api.app import create_app
    from httpx import ASGITransport, AsyncClient

    settings = Settings(
        database_url="memory://gateway",
        object_store="memory",
        allow_insecure=True,
        auth_mode="none",
        host="127.0.0.1",
        environment="development",
    )
    app = create_app(settings=settings, plugins=[])

    async def _boom(*a: object, **k: object) -> None:
        raise ModelGatewayError("anthropic", 429, _SECRET_BODY)

    # fail inside the agent invoke, where the route catches ModelGatewayError
    orig = oc.build_tenant_agent

    async def _agent(*a: object, **k: object):
        agent = await orig(*a, **k)
        agent.invoke = _boom  # type: ignore[method-assign]
        return agent

    oc.build_tenant_agent = _agent  # type: ignore[assignment]
    try:
        with caplog.at_level(logging.WARNING, logger="felix_api.routes.openai_compat"):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={"model": "quick", "messages": [{"role": "user", "content": "hi"}]},
                )
    finally:
        oc.build_tenant_agent = orig  # type: ignore[assignment]

    assert resp.status_code == 502
    payload = resp.text
    assert "org_abc123" not in payload
    assert "req_sensitive_9f3" not in payload
    assert resp.json()["error"]["type"] == "model_gateway_error"
    # the operator still gets the detail
    assert "req_sensitive_9f3" in caplog.text
