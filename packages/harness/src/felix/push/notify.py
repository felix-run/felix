"""Tell a tenant's subscribed browsers that a run is waiting on a person.

Two moments call this, both inline in whichever process runs the agent (the API for a
streamed run, the worker for a durable one): an approval row going pending, and an agent
asking a question (`ui_request`). Each send is a held background task, so the frame that
announces the same moment on a live stream is never behind a push service's round trip.

What a push carries is deliberately thin: what kind of wait, which thread, and for an
approval its id, tool and deadline. Never arguments, a prompt's text or a reason -- the
message crosses a third party's servers, and the decision belongs on the surface that can
show the call in full. Subscribing needs `approvals:read`, the scope the `approval_required`
frame itself is gated on, so a push tells no one anything that frame would not.

A push service answering 404 or 410 means that browser is gone; its row is deleted. Anything
else is counted and dropped: a missed push is a page the person opens a little later, not a
run that fails.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from felix.config import Settings
from felix.observability.metrics import record_counter

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey

logger = logging.getLogger("felix.push")

# The background sends, held: asyncio keeps only a weak reference to a task.
_in_flight: set[asyncio.Task[None]] = set()
# Resolved keys by their configured value, so a `secret:` ref is fetched once per process.
_keys: dict[str, EllipticCurvePrivateKey] = {}

# How long a push service holds a message for a browser that is offline. An approval's own
# deadline bounds it when there is one; past that the push would announce a decision already
# made by timeout.
DEFAULT_TTL_SECONDS = 300
MIN_TTL_SECONDS = 30
MAX_TTL_SECONDS = 24 * 60 * 60
# Sends in flight at once for one wait.
SEND_CONCURRENCY = 8


def enabled(settings: Settings) -> bool:
    return settings.push_configured()


def host_allowed(settings: Settings, endpoint: str) -> bool:
    """Whether `endpoint` is a canonical https URL on one of `FELIX_PUSH_ALLOWED_HOSTS`.

    Canonical means what a browser actually produces -- a lowercase host, no port, no userinfo
    -- and anything else is refused rather than normalised. Userinfo would make httpx send
    Basic auth in place of the VAPID header, and a port or a capital letter would put an
    audience in the token the push service does not recognise: every send refused, for ever.
    """
    parts = urlsplit(endpoint)
    try:
        port = parts.port
    except ValueError:
        return False
    host = parts.hostname
    if parts.scheme != "https" or not host or port is not None or parts.username or parts.password:
        return False
    if parts.netloc != host or not host.isascii() or host.endswith("."):
        return False
    for raw in settings.push_allowed_hosts.split(","):
        pattern = raw.strip().lower()
        if not pattern:
            continue
        if pattern.startswith("*.") and len(pattern) > 2:
            # `.push.apple.com` as a suffix: a subdomain matches, the bare apex cannot.
            if host.endswith(pattern[1:]):
                return True
        elif host == pattern:
            return True
    return False


async def vapid_private_key(settings: Settings) -> EllipticCurvePrivateKey:
    """The configured VAPID key, resolving a `secret:` ref once."""
    from felix.push.webpush import private_key_from_b64

    raw = settings.push_vapid_private_key.strip()
    if raw not in _keys:
        value = raw
        if raw.startswith("secret:"):
            from felix.secrets import build_secrets, resolve_secret_value

            value = await resolve_secret_value(build_secrets(settings), raw)
        else:
            from felix.secrets import register_resolved_secret

            # A literal key is masked in output like a resolved one.
            register_resolved_secret(value)
        _keys[raw] = private_key_from_b64(value)
    return _keys[raw]


def approval_pending(settings: Settings, row: dict[str, Any]) -> None:
    """A new approval row: tell the tenant. Called only for a row just created, never a reused one."""
    message: dict[str, Any] = {
        "kind": "approval",
        "approval_id": row.get("id"),
        "tool_name": row.get("tool_name"),
        "thread_id": row.get("thread_id") or "",
        "expires_at": row.get("expires_at"),
    }
    # The rule's ttl is the harness's wait; with none it waits its default, which this matches.
    ttl = row.get("ttl_seconds")
    _schedule(
        settings,
        str(row.get("tenant_id") or ""),
        message,
        ttl if isinstance(ttl, int) else DEFAULT_TTL_SECONDS,
    )


def question_asked(settings: Settings, tenant_id: str, *, thread_id: str | None) -> None:
    """An agent asked the person something (`ui_request`): tell the tenant, without the question.

    The `ui_request` frame reaches one thread's stream; this reaches every subscriber in the
    tenant. So it carries the thread and nothing that answers the question -- not even the
    request id `POST /chat/ui` takes. The page that opens reads the question from the thread.
    """
    message = {"kind": "question", "thread_id": thread_id or ""}
    _schedule(settings, tenant_id, message, DEFAULT_TTL_SECONDS)


def _schedule(settings: Settings, tenant_id: str, message: dict[str, Any], ttl: int) -> None:
    if not enabled(settings) or not tenant_id:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    # A fresh context, not the caller's: the agent's request context carries its manifest's
    # metric allowlist, which would silently drop `felix_push_delivery`. The store binds the
    # tenant for itself.
    task = loop.create_task(
        send_to_tenant(settings, tenant_id, message, ttl=ttl), context=contextvars.Context()
    )
    _in_flight.add(task)
    task.add_done_callback(_finished)


def _finished(task: asyncio.Task[None]) -> None:
    _in_flight.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.warning("push send failed", exc_info=task.exception())


async def send_to_tenant(settings: Settings, tenant_id: str, message: dict[str, Any], *, ttl: int) -> None:
    """Encrypt `message` to each of the tenant's browsers and post it. Never raises per browser."""
    from felix.push import store
    from felix.push.webpush import encrypt, vapid_authorization

    subscriptions = await store.list_for_tenant(settings, tenant_id)
    if not subscriptions:
        return
    key = await vapid_private_key(settings)
    body = json.dumps(message, separators=(",", ":")).encode()
    ttl = max(MIN_TTL_SECONDS, min(MAX_TTL_SECONDS, ttl))

    delivered: list[str] = []
    failed: list[str] = []
    gone: list[str] = []
    # Bounded: a tenant's every subscription at once is up to 200 TLS handshakes from the
    # process running the agent.
    slots = asyncio.Semaphore(SEND_CONCURRENCY)

    async def one(sub: dict[str, Any]) -> None:
        if not host_allowed(settings, sub["endpoint"]):
            # Allowed when it subscribed and not now: the operator narrowed the list.
            gone.append(sub["id"])
            record_counter("felix_push_delivery", {"outcome": "host_refused"})
            return
        try:
            payload = encrypt(body, ua_public=sub["p256dh"], auth_secret=sub["auth"])
            headers = {
                "Authorization": vapid_authorization(
                    sub["endpoint"], private_key=key, subject=settings.push_vapid_subject.strip()
                ),
                "Content-Encoding": "aes128gcm",
                "Content-Type": "application/octet-stream",
                "TTL": str(ttl),
                # A person is waiting on this; let the device wake for it.
                "Urgency": "high",
            }
            async with slots:
                status = await post(settings, sub["endpoint"], headers, payload)
        except Exception as exc:  # one browser's failure is not the others'
            logger.info("push to one subscription failed: %s", type(exc).__name__)
            failed.append(sub["id"])
            record_counter("felix_push_delivery", {"outcome": "error"})
            return
        if status in (404, 410):
            gone.append(sub["id"])
            record_counter("felix_push_delivery", {"outcome": "gone"})
        elif 200 <= status < 300:
            delivered.append(sub["id"])
            record_counter("felix_push_delivery", {"outcome": "delivered"})
        else:
            failed.append(sub["id"])
            record_counter("felix_push_delivery", {"outcome": f"http_{status // 100}xx"})

    await asyncio.gather(*(one(sub) for sub in subscriptions))
    await store.record_outcomes(settings, tenant_id, delivered=delivered, failed=failed, gone=gone)


async def post(settings: Settings, endpoint: str, headers: dict[str, str], body: bytes) -> int:
    """POST one encrypted message through the egress guard; the status is the whole answer.

    Public, and looked up at call time, on purpose: it is the network seam, which tests replace
    so that every other line of a send -- encryption, headers, outcomes -- runs for real.
    """
    import httpx
    from felix_ai.wire.transport import DEFAULT_CONNECT_TIMEOUT_S

    from felix.security.egress import post_for_status, safe_async_client

    timeout = httpx.Timeout(float(settings.push_timeout_seconds), connect=DEFAULT_CONNECT_TIMEOUT_S)
    return await post_for_status(
        safe_async_client(timeout=timeout),
        endpoint,
        content=body,
        headers=headers,
        deadline_s=float(settings.push_timeout_seconds),
    )


def reset_push_keys_for_tests() -> None:
    _keys.clear()


__all__ = [
    "approval_pending",
    "enabled",
    "host_allowed",
    "post",
    "question_asked",
    "reset_push_keys_for_tests",
    "send_to_tenant",
    "vapid_private_key",
]
