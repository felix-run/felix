"""Signed completion webhooks: registry, signing, and the worker's delivery sweep.

The delivery runs against a real HTTP receiver on loopback, through the real egress guard
(`allow_http` in development is what lets it reach 127.0.0.1), because what matters about a
webhook is what arrives: the body, the headers, a signature the receiver can verify.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from typing import Any

import pytest
from felix.config import Settings
from felix.durability import fibers as F
from felix.durability.webhooks import (
    WebhookEndpointError,
    deliver_due_webhooks,
    endpoints_for_run,
    parse_webhook_endpoints,
    sign,
)

from tests.support.webhook_receiver import SECRET, receiver


def _settings(endpoints: dict[str, Any], **kw: Any) -> Settings:
    """Endpoints open to every tenant unless the test says otherwise."""
    endpoints = {name: {"tenants": "*", **spec} for name, spec in endpoints.items()}
    return Settings(
        database_url="memory://webhooks",
        object_store="memory",
        environment="development",
        allow_insecure=True,
        webhook_endpoints=json.dumps(endpoints),
        **kw,
    )


@pytest.fixture(autouse=True)
def _fresh() -> None:
    F.reset_memory_fibers()


async def _finished_run(
    settings: Settings, webhooks: list[str], *, status: str = "completed"
) -> dict[str, Any]:
    state = {
        "steps": [{"op": "invoke", "manifest_id": "m", "thread_id": "acme:t1"}],
        "cursor": 1,
        "stash": {"last": {"answer": "four", "manifest_id": "m"}},
    }
    row = await F.create_fiber(settings, "acme", kind="durable_chat", state=state, webhooks=webhooks)
    F._memory_fibers[("acme", row["id"])]["status"] = status
    return row


def _stored(row: dict[str, Any]) -> dict[str, Any]:
    return F._memory_fibers[("acme", row["id"])]


# --- the registry --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("endpoints", "fragment"),
    [
        ("not json", "JSON"),
        ({"Bad Id": {"url": "https://x.test", "secret": SECRET}}, "must match"),
        ({"ops": {"url": "ftp://x.test", "secret": SECRET}}, "http(s)"),
        ({"ops": {"url": "https://u:p@x.test", "secret": SECRET}}, "credentials"),
        ({"ops": {"url": "https://x.test"}}, "secret"),
        ({"ops": {"url": "https://x.test", "secret": SECRET, "tenants": "acme"}}, "tenants"),
        # Open to every tenant has to be written down, not left out.
        ({"ops": {"url": "https://x.test", "secret": SECRET}}, "tenants"),
        ({"ops": {"url": "https://x.test", "secret": "whsec_not base64!", "tenants": "*"}}, "base64"),
    ],
)
def test_a_malformed_registry_is_refused_naming_the_problem(endpoints: Any, fragment: str) -> None:
    raw = endpoints if isinstance(endpoints, str) else json.dumps(endpoints)
    with pytest.raises(ValueError, match=fragment.replace("(", r"\(").replace(")", r"\)")):
        parse_webhook_endpoints(Settings(database_url="memory://w", webhook_endpoints=raw))


def test_plain_http_only_in_development_with_allow_insecure() -> None:
    raw = json.dumps({"ops": {"url": "http://x.test", "secret": SECRET, "tenants": "*"}})
    strict = Settings(
        database_url="memory://w", environment="production", allow_insecure=False, webhook_endpoints=raw
    )
    with pytest.raises(ValueError, match="https"):
        parse_webhook_endpoints(strict)
    assert parse_webhook_endpoints(_settings({"ops": {"url": "http://x.test", "secret": SECRET}}))


def test_a_run_may_name_only_endpoints_registered_for_its_tenant() -> None:
    settings = _settings(
        {
            "shared": {"url": "https://x.test", "secret": SECRET},
            "acme-only": {"url": "https://y.test", "secret": SECRET, "tenants": ["acme"]},
        }
    )
    assert endpoints_for_run(settings, "globex", ["shared", "shared"]) == ["shared"]
    assert endpoints_for_run(settings, "acme", ["acme-only"]) == ["acme-only"]
    # Not registered and not yours read the same: the message is not a registry oracle.
    for tenant, name in (("globex", "acme-only"), ("acme", "nowhere")):
        with pytest.raises(WebhookEndpointError, match=f"unknown webhook endpoint: {name}"):
            endpoints_for_run(settings, tenant, [name])


def test_the_signature_matches_the_standard_webhooks_reference_vector() -> None:
    """The example from the Standard Webhooks specification, so receivers' libraries verify."""
    signature = sign(
        "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw",
        "msg_p5jXN8AQM9LWM0D4loKWxJek",
        1614265330,
        b'{"test": 2432232314}',
    )
    assert signature == "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE="


# --- delivery ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_finished_run_is_delivered_signed_and_the_run_view_says_so() -> None:
    from felix.durability.runs import get_durable_run

    async with receiver([200]) as (url, seen):
        settings = _settings({"ops": {"url": url, "secret": SECRET}})
        row = await _finished_run(settings, ["ops"])
        assert await deliver_due_webhooks(settings) == 1

    [request] = seen
    headers, body = request["headers"], request["body"]
    expected = base64.b64encode(
        hmac.new(
            SECRET.encode(),
            f"{headers['webhook-id']}.{headers['webhook-timestamp']}.".encode() + body,
            hashlib.sha256,
        ).digest()
    ).decode()
    assert headers["webhook-signature"] == f"v1,{expected}"
    assert headers["webhook-id"] == f"{row['id']}:ops", "stable across retries, for dedupe"
    payload = json.loads(body)
    assert payload["type"] == "run.completed"
    assert payload["tenant_id"] == "acme" and payload["thread_id"] == "acme:t1"
    assert payload["run"]["final"]["content"] == "four"
    assert _stored(row)["webhook_status"] == "delivered"
    view = await get_durable_run(settings, "acme", row["id"])
    assert view is not None and view["webhooks"] == {"ops": "delivered"}
    assert await deliver_due_webhooks(settings) == 0, "a delivered run is not delivered again"


@pytest.mark.asyncio
async def test_a_failed_delivery_backs_off_and_goes_dead_at_the_ceiling() -> None:
    async with receiver([500]) as (url, seen):
        settings = _settings({"ops": {"url": url, "secret": SECRET}}, webhook_max_attempts=2)
        row = await _finished_run(settings, ["ops"])

        await deliver_due_webhooks(settings)
        stored = _stored(row)
        assert stored["webhook_status"] == "pending"
        assert stored["webhook_due_at"] > F.now_ms(), "backed off, not retried on the next tick"
        assert stored["webhook_state"]["endpoints"]["ops"]["last_error"] == "HTTP 500"
        assert await deliver_due_webhooks(settings) == 0

        stored["webhook_due_at"] = 0  # the backoff lapses
        await deliver_due_webhooks(settings)
    assert len(seen) == 2
    assert _stored(row)["webhook_status"] == "dead"
    assert _stored(row)["webhook_state"]["endpoints"]["ops"]["status"] == "dead"


@pytest.mark.asyncio
async def test_a_run_still_going_is_not_announced() -> None:
    async with receiver([200]) as (url, seen):
        settings = _settings({"ops": {"url": url, "secret": SECRET}})
        await _finished_run(settings, ["ops"], status="running")
        assert await deliver_due_webhooks(settings) == 0
    assert seen == []


@pytest.mark.asyncio
async def test_an_endpoint_removed_after_the_run_started_is_dead_not_retried_forever() -> None:
    settings = _settings({"ops": {"url": "http://127.0.0.1:9/hook", "secret": SECRET}})
    row = await _finished_run(settings, ["ops"])
    await deliver_due_webhooks(_settings({}))
    assert _stored(row)["webhook_status"] == "dead"
    assert "no longer registered" in _stored(row)["webhook_state"]["endpoints"]["ops"]["last_error"]


@pytest.mark.asyncio
async def test_a_redirect_is_not_followed() -> None:
    """A 3xx is a failed delivery: following it would send run output wherever it pointed."""
    async with receiver([302]) as (url, seen):
        settings = _settings({"ops": {"url": url, "secret": SECRET}})
        row = await _finished_run(settings, ["ops"])
        await deliver_due_webhooks(settings)
    assert len(seen) == 1
    assert _stored(row)["webhook_state"]["endpoints"]["ops"]["last_error"] == "HTTP 302"


@pytest.mark.asyncio
async def test_deliveries_go_through_the_egress_guard_unless_the_endpoint_says_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard is what refuses a private address; `private: true` is the operator's opt-out,
    and nothing else is. Asserted by which client carries the request, since in development
    the guard itself lets loopback through."""
    from felix.security import egress

    real = egress.safe_async_client
    guarded: list[bool] = []

    def spy(*a: Any, **kw: Any) -> Any:
        guarded.append(True)
        return real(*a, **kw)

    monkeypatch.setattr(egress, "safe_async_client", spy)
    async with receiver([200]) as (url, seen):
        await _finished_run(_settings({"ops": {"url": url, "secret": SECRET}}), ["ops"])
        await deliver_due_webhooks(_settings({"ops": {"url": url, "secret": SECRET}}))
        assert guarded == [True]

        await _finished_run(_settings({"ops": {"url": url, "secret": SECRET}}), ["ops"])
        await deliver_due_webhooks(_settings({"ops": {"url": url, "secret": SECRET, "private": True}}))
    assert guarded == [True], "a private endpoint bypasses the guard, and only it"
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_a_receiver_dripping_its_answer_is_cut_off_at_the_timeout() -> None:
    """The client timeout bounds each read, so a byte every so often would hold the sweep for
    as long as the receiver liked; the attempt as a whole is what is bounded."""
    import time

    async def drip(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        for byte in b"HTTP/1.1 200 OK\r\ncontent-length: 0\r\n\r\n":
            writer.write(bytes([byte]))
            await writer.drain()
            await asyncio.sleep(0.1)
        writer.close()

    server = await asyncio.start_server(drip, "127.0.0.1", 0)
    url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/hook"
    try:
        settings = _settings({"ops": {"url": url, "secret": SECRET}}, webhook_timeout_seconds=0.3)
        row = await _finished_run(settings, ["ops"])
        started = time.monotonic()
        await deliver_due_webhooks(settings)
        elapsed = time.monotonic() - started
    finally:
        server.close()
    assert elapsed < 2, elapsed
    assert _stored(row)["webhook_state"]["endpoints"]["ops"]["last_error"] == "TimeoutError"


@pytest.mark.asyncio
async def test_a_sweep_past_its_budget_leaves_the_rest_for_the_next(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rows a sweep did not reach keep their claim until it lapses, rather than a second sweep
    delivering them while the first is still at it."""
    from felix.durability import webhooks

    monkeypatch.setattr(webhooks, "WEBHOOK_SWEEP_BUDGET_MS", -1)
    async with receiver([200]) as (url, seen):
        settings = _settings({"ops": {"url": url, "secret": SECRET}})
        row = await _finished_run(settings, ["ops"])
        await deliver_due_webhooks(settings)
    assert seen == []
    assert _stored(row)["webhook_status"] == "pending"


@pytest.mark.asyncio
async def test_every_webhook_secret_is_masked_from_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Audit rows, session events and fiber state redact through the process-global list alone,
    so a signing secret has to be on it before any delivery — in the API as much as the worker."""
    from felix import secrets as S

    monkeypatch.setattr(S, "_resolved_secret_values", [])
    monkeypatch.setenv("OPS_HOOK_KEY", "resolved-signing-key-value")
    settings = _settings(
        {
            "ops": {"url": "https://x.test", "secret": "secret:OPS_HOOK_KEY"},
            "audit": {"url": "https://y.test", "secret": "literal-signing-key-value"},
        }
    )
    await S.hydrate_secrets(settings)
    masked = S.redact_text("a resolved-signing-key-value and a literal-signing-key-value")
    assert "resolved-signing-key-value" not in masked
    assert "literal-signing-key-value" not in masked
