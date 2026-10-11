"""Signed completion webhooks for durable runs, delivered from the worker.

A durable run that names `spec.execution.webhooks` is announced to each of those endpoints when
it reaches a terminal status, so a caller need not poll `GET /chat/runs/{token}`. Three choices
shape it:

* **Operator-registered endpoints, named by id.** `FELIX_WEBHOOK_ENDPOINTS` maps an id to a URL
  and a signing secret; a manifest only names ids. A manifest author holds a tenant scope, and a
  tenant-supplied URL on a path carrying run output is an exfiltration channel that SSRF checks
  do not address — the destination is the problem, not its address.
* **A sweep, not a hook.** The API replica that accepted the run may be gone when it finishes,
  and a fiber may finish on any worker. A worker sweep over "terminal, delivery pending, due"
  covers both, and is retried by the same schedule that runs it.
* **State on the run's own row.** `webhook_status` / `webhook_due_at` / `webhook_state`
  (migration 0019): a dead letter is `webhook_status = 'dead'` beside the run it was about, not a
  second store to reconcile.

Deliveries are signed as Standard Webhooks (`webhook-id`, `webhook-timestamp`,
`webhook-signature: v1,<base64 HMAC-SHA256>`), so a receiver can verify them with an existing
library. The id is stable across retries, which is what a receiver dedupes on.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from felix.config import Settings
from felix.db.models import Fiber
from felix.db.session import _use_memory, get_session_factory
from felix.durability.fibers import (
    FIBER_TERMINAL_STATUSES,
    _fiber_dict,
    _memory_fibers,
    now_ms,
    retry_delay_ms,
)
from felix.observability.metrics import record_counter

logger = logging.getLogger("felix.durability.webhooks")

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
# Held on a row while one worker delivers it; a crashed worker's claim lapses after this.
WEBHOOK_CLAIM_MS = 120_000
WEBHOOK_BATCH = 50
# A sweep stops starting deliveries past this, well inside the claim: rows it never reached keep
# their claim until it lapses and the next sweep takes them, rather than being delivered twice by
# two sweeps at once because one receiver was slow.
WEBHOOK_SWEEP_BUDGET_MS = WEBHOOK_CLAIM_MS // 2
_EVENTS = {
    "completed": "run.completed",
    "failed": "run.failed",
    "expired": "run.expired",
    "dead": "run.dead",
}


class WebhookEndpointError(ValueError):
    """A run named an endpoint the operator has not registered for its tenant."""


@dataclass(frozen=True, slots=True)
class WebhookEndpoint:
    name: str
    url: str
    # A literal, or a `secret:NAME` ref resolved through FELIX_SECRETS_BACKEND at delivery.
    secret: str
    # `None` is `"tenants": "*"`, written out by the operator: an endpoint open to every tenant
    # lets one tenant's run arrive signed with the secret another tenant's receiver trusts.
    tenants: frozenset[str] | None = None
    private: bool = False
    # Open to manifest hooks (`manifest_hooks.py`), which send prompts, tool arguments and results
    # -- far more than a run notification. Its own decision, off by default.
    hooks: bool = False

    def allows(self, tenant_id: str) -> bool:
        return self.tenants is None or tenant_id in self.tenants


def parse_webhook_endpoints(settings: Any) -> dict[str, WebhookEndpoint]:
    """`FELIX_WEBHOOK_ENDPOINTS`, validated. Raises `ValueError` naming what is wrong."""
    raw = str(getattr(settings, "webhook_endpoints", "") or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"not valid JSON ({exc.msg})") from None
    if not isinstance(parsed, dict):
        raise ValueError("must be a JSON object of endpoint id -> {url, secret}")
    insecure_ok = getattr(settings, "environment", "") == "development" and bool(
        getattr(settings, "allow_insecure", False)
    )
    out: dict[str, WebhookEndpoint] = {}
    for name, spec in parsed.items():
        if not isinstance(name, str) or not _NAME_RE.match(name):
            raise ValueError(f"endpoint id {name!r} must match {_NAME_RE.pattern}")
        if not isinstance(spec, dict):
            raise ValueError(f"endpoint {name!r} must be an object")
        url = str(spec.get("url") or "")
        parts = urlsplit(url)
        if parts.scheme not in ("https", "http") or not parts.hostname:
            raise ValueError(f"endpoint {name!r} url must be an absolute http(s) URL")
        if parts.scheme == "http" and not insecure_ok:
            raise ValueError(f"endpoint {name!r} url must be https outside development with allow_insecure")
        if parts.username or parts.password:
            # Userinfo reaches logs and error messages with the URL; the credential is `secret`.
            raise ValueError(f"endpoint {name!r} url must not carry credentials")
        secret = spec.get("secret")
        if not isinstance(secret, str) or not secret.strip():
            raise ValueError(f"endpoint {name!r} needs a signing `secret` (a literal or secret:NAME)")
        if secret.startswith("whsec_"):
            try:
                base64.b64decode(secret.removeprefix("whsec_"), validate=True)
            except binascii.Error:
                raise ValueError(f"endpoint {name!r} `whsec_` secret is not valid base64") from None
        # Required, with "*" for every tenant: the open endpoint has to be a written decision,
        # because a forgotten key would otherwise make it one.
        tenants = spec.get("tenants")
        if tenants != "*" and not (
            isinstance(tenants, list) and tenants and all(isinstance(t, str) for t in tenants)
        ):
            raise ValueError(f'endpoint {name!r} `tenants` must be a list of tenant ids, or "*" for all')
        private = spec.get("private", False)
        if not isinstance(private, bool):
            raise ValueError(f"endpoint {name!r} `private` must be true or false")
        hooks = spec.get("hooks", False)
        if not isinstance(hooks, bool):
            raise ValueError(f"endpoint {name!r} `hooks` must be true or false")
        out[name] = WebhookEndpoint(
            name=name,
            url=url,
            secret=secret,
            tenants=None if tenants == "*" else frozenset(tenants),
            private=private,
            hooks=hooks,
        )
    return out


def parse_tenant_endpoint_bindings(raw: str) -> dict[str, tuple[str, ...]]:
    """Comma-separated `tenant=endpoint_id` -- the shape of a setting that routes one kind of
    event per tenant (`FELIX_SKILL_UPDATE_WEBHOOKS`) -- as tenant to its endpoint ids, in the order
    given, each once. A tenant is named once per endpoint. Raises `ValueError` naming the bad
    entry; there is no wildcard on either side."""
    out: dict[str, list[str]] = {}
    for entry in (e.strip() for e in (raw or "").split(",")):
        if not entry:
            continue
        tenant, sep, endpoint = (p.strip() for p in entry.partition("="))
        if not sep or not tenant or not endpoint:
            raise ValueError(f"entry {entry!r} must be tenant=endpoint_id")
        if tenant == "*" or "*" in endpoint:
            raise ValueError(f"entry {entry!r}: there is no wildcard; bind each tenant to its endpoint")
        ids = out.setdefault(tenant, [])
        if endpoint not in ids:
            ids.append(endpoint)
    return {tenant: tuple(ids) for tenant, ids in out.items()}


def validate_tenant_endpoint_bindings(settings: Any, raw: str) -> None:
    """Every id ``raw`` binds is a `FELIX_WEBHOOK_ENDPOINTS` endpoint that opens to its tenant.
    `ValueError` otherwise, one message for both: an operator's typo and an endpoint fenced to
    other tenants are fixed in the same place."""
    bindings = parse_tenant_endpoint_bindings(raw)
    if not bindings:
        return
    registry = parse_webhook_endpoints(settings)
    for tenant, ids in bindings.items():
        for name in ids:
            endpoint = registry.get(name)
            if endpoint is None or not endpoint.allows(tenant):
                raise ValueError(
                    f"{tenant}={name}: no FELIX_WEBHOOK_ENDPOINTS endpoint {name!r} registered for {tenant!r}"
                )


def endpoints_for_run(settings: Settings, tenant_id: str, names: list[str]) -> list[str]:
    """The endpoint ids a new run will announce to, or `WebhookEndpointError`.

    One message for "not registered" and "not yours": telling a tenant an id exists but belongs
    to someone else is a registry oracle.
    """
    registry = parse_webhook_endpoints(settings)
    chosen = list(dict.fromkeys(names))
    for name in chosen:
        endpoint = registry.get(name)
        if endpoint is None or not endpoint.allows(tenant_id):
            raise WebhookEndpointError(f"unknown webhook endpoint: {name}")
    return chosen


def sign(secret: str, message_id: str, timestamp: int, body: bytes) -> str:
    """The Standard Webhooks `webhook-signature` value for one delivery.

    A `whsec_`-prefixed secret is the spec's base64 key; any other string is used as its bytes,
    so an operator can paste either what a receiver library generated or a plain shared secret.
    """
    key = base64.b64decode(secret.removeprefix("whsec_")) if secret.startswith("whsec_") else secret.encode()
    mac = hmac.new(key, f"{message_id}.{timestamp}.".encode() + body, hashlib.sha256).digest()
    return "v1," + base64.b64encode(mac).decode()


def _payload(row: dict[str, Any]) -> dict[str, Any]:
    from felix.durability.runs import run_view

    view = run_view(row)
    view.pop("webhooks", None)  # delivery bookkeeping, not the run
    steps = list((row.get("state_json") or {}).get("steps") or [])
    thread_id = (steps[0] or {}).get("thread_id") if steps else None
    return {
        "type": _EVENTS.get(str(row.get("status")), "run.finished"),
        "tenant_id": row.get("tenant_id"),
        "thread_id": thread_id,
        "run": view,
    }


def endpoint_client(settings: Settings, endpoint: WebhookEndpoint, timeout_s: float) -> Any:
    """The HTTP client a delivery to `endpoint` uses: the egress-pinned one, or -- for an endpoint
    the operator marked `private` -- a plain one. Shared with manifest hooks (`manifest_hooks.py`)."""
    import httpx
    from felix_ai.wire.transport import DEFAULT_CONNECT_TIMEOUT_S

    timeout = httpx.Timeout(timeout_s, connect=min(DEFAULT_CONNECT_TIMEOUT_S, timeout_s))
    if endpoint.private:
        # The operator marked this endpoint as reachable on a private network, which the egress
        # guard exists to refuse. Operator configuration, never a manifest value; redirects are
        # still not followed, and no proxy from the environment is picked up.
        return httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False)
    from felix.security.egress import safe_async_client

    allow_http = settings.environment == "development" and settings.allow_insecure
    return safe_async_client(timeout=timeout, allow_http=allow_http)


async def _post(settings: Settings, endpoint: WebhookEndpoint, headers: dict[str, str], body: bytes) -> int:
    from felix.security.egress import post_for_status

    client = endpoint_client(settings, endpoint, float(settings.webhook_timeout_seconds))

    # Bounded as a whole as well as per read, so a receiver dripping bytes cannot hold the sweep.
    return await post_for_status(
        client,
        endpoint.url,
        content=body,
        headers=headers,
        deadline_s=float(settings.webhook_timeout_seconds),
    )


def canonical_body(obj: Any) -> bytes:
    """The bytes a delivery signs and sends: compact, key-sorted JSON, anything JSON lacks (a
    datetime in a run view) as its `str`. One spelling for every webhook kind, so a body is the
    same bytes on every retry and a receiver can recompute nothing differently."""
    return json.dumps(obj, separators=(",", ":"), sort_keys=True, default=str).encode()


def delivery_outcome(endpoints: dict[str, dict[str, Any]]) -> tuple[str, int | None]:
    """A delivery's own status from its endpoints' -- `pending` while any endpoint is, else
    `delivered` when all were, else `dead` -- and when its next attempt is due."""
    pending = [ep for ep in endpoints.values() if ep.get("status") == "pending"]
    if pending:
        return "pending", now_ms() + retry_delay_ms(max(int(ep.get("attempts") or 1) for ep in pending))
    delivered = all(ep.get("status") == "delivered" for ep in endpoints.values())
    return ("delivered" if delivered else "dead"), None


class WebhookSender:
    """One sweep's way of sending a signed webhook: built once per sweep with the endpoint
    registry and secrets provider it read, and the ``kind`` every attempt is counted under in
    `felix_webhook_delivery` (`run`, `skill_update`). What a run's completion webhook and a skill's
    update notification share -- the signing, the egress-pinned client, the backoff and the dead
    letter -- and what the next kind of webhook reuses."""

    def __init__(
        self, settings: Settings, registry: dict[str, WebhookEndpoint], provider: Any, kind: str
    ) -> None:
        self.settings = settings
        self._registry = registry
        self._provider = provider
        self._kind = kind

    def _count(self, endpoint: str, outcome: str) -> None:
        record_counter(
            "felix_webhook_delivery", {"kind": self._kind, "endpoint": endpoint, "outcome": outcome}
        )

    async def attempt(
        self,
        endpoints: dict[str, dict[str, Any]],
        *,
        tenant_id: str,
        body: bytes,
        message_id: Callable[[str], str],
        subject: str,
        still_routed: Callable[[str], bool] = lambda _name: True,
        deadline: int | None = None,
    ) -> dict[str, dict[str, Any]]:
        """One signed attempt at every endpoint of ``endpoints`` still `pending`, each updated
        in place (`status`, `attempts`, `last_error`, `delivered_at`) and returned. An endpoint
        the registry no longer has, no longer opens to ``tenant_id``, or that ``still_routed``
        disowns goes `dead` without a request. Past ``deadline`` (epoch ms) no further endpoint
        is tried: the rest stay `pending`, untried, for the next sweep."""
        from felix.secrets import register_resolved_secret, resolve_secret_value

        timestamp = int(time.time())
        for name, ep in endpoints.items():
            if ep.get("status") != "pending":
                continue
            endpoint = self._registry.get(name)
            if endpoint is None or not endpoint.allows(tenant_id) or not still_routed(name):
                ep.update(status="dead", last_error="endpoint no longer registered for this tenant")
                self._count(name, "dead")
                continue
            if deadline is not None and now_ms() > deadline:
                break
            ep["attempts"] = int(ep.get("attempts") or 0) + 1
            msg_id = message_id(name)
            try:
                secret = await resolve_secret_value(self._provider, endpoint.secret)
                if not secret:
                    raise ValueError("signing secret resolved to nothing")
                register_resolved_secret(secret)
                headers = {
                    "content-type": "application/json",
                    "user-agent": "Felix-Webhooks/1",
                    "webhook-id": msg_id,
                    "webhook-timestamp": str(timestamp),
                    "webhook-signature": sign(secret, msg_id, timestamp, body),
                }
                status = await _post(self.settings, endpoint, headers, body)
                if 200 <= status < 300:
                    ep.update(status="delivered", delivered_at=now_ms(), last_error="")
                    self._count(name, "delivered")
                    continue
                ep["last_error"] = f"HTTP {status}"
            except Exception as exc:
                # The type only: an egress refusal, a timeout or a resolver error, never a message
                # that could quote the URL's query or the secret's name back into the run view.
                ep["last_error"] = type(exc).__name__
                logger.warning("webhook %s delivery for %s failed: %s", name, subject, type(exc).__name__)
            if ep["attempts"] >= self.settings.webhook_max_attempts:
                ep["status"] = "dead"
                self._count(name, "dead")
            else:
                self._count(name, "retry")
        return endpoints


async def _deliver_row(
    settings: Settings, row: dict[str, Any], registry: dict[str, WebhookEndpoint], provider: Any
) -> None:
    """Announce one claimed run, with a sender of its own: for a caller holding one row."""
    await _send_row(row, WebhookSender(settings, registry, provider, "run"))


async def _send_row(row: dict[str, Any], sender: WebhookSender, deadline: int | None = None) -> None:
    state = dict(row.get("webhook_state") or {})
    endpoints = {name: dict(ep or {}) for name, ep in (state.get("endpoints") or {}).items()}
    state["endpoints"] = await sender.attempt(
        endpoints,
        tenant_id=str(row.get("tenant_id")),
        body=canonical_body(_payload(row)),
        message_id=lambda name: f"{row['id']}:{name}",
        subject=f"run {row.get('id')}",
        deadline=deadline,
    )
    status, due_at = delivery_outcome(state["endpoints"])
    await _save_delivery(sender.settings, row, status=status, due_at=due_at, state=state)


async def _claim_due(settings: Settings, ts: int) -> list[dict[str, Any]]:
    """Terminal runs with a delivery due, each claimed by pushing its `webhook_due_at` forward."""
    until = ts + WEBHOOK_CLAIM_MS
    if _use_memory(settings):
        claimed: list[dict[str, Any]] = []
        for row in _memory_fibers.values():
            due = row.get("webhook_due_at")
            if (
                row.get("webhook_status") == "pending"
                and row.get("status") in FIBER_TERMINAL_STATUSES
                and (due is None or due <= ts)
            ):
                row["webhook_due_at"] = until
                claimed.append(dict(row))
                if len(claimed) >= WEBHOOK_BATCH:
                    break
        return claimed

    from sqlalchemy import select

    from felix.db.session import rls_bypass

    # Cross-tenant maintenance, like the fiber sweep: without the bypass RLS returns nothing.
    with rls_bypass():
        factory = get_session_factory(settings=settings)
        async with factory() as db:
            stmt = (
                select(Fiber)
                .where(
                    Fiber.webhook_status == "pending",
                    Fiber.status.in_(tuple(FIBER_TERMINAL_STATUSES)),
                    Fiber.webhook_due_at.is_(None) | (Fiber.webhook_due_at <= ts),
                )
                .order_by(Fiber.updated_at)
                .limit(WEBHOOK_BATCH)
                .with_for_update(skip_locked=True)
            )
            rows = (await db.scalars(stmt)).all()
            for row in rows:
                row.webhook_due_at = until
            out = [_fiber_dict(row) for row in rows]
            await db.commit()
            return out


async def _save_delivery(
    settings: Settings, row: dict[str, Any], *, status: str, due_at: int | None, state: dict[str, Any]
) -> None:
    """Write the delivery columns alone — never `status` or `state_json`, which are the run's."""
    if _use_memory(settings):
        stored = _memory_fibers.get((row["tenant_id"], row["id"]))
        if stored is not None:
            stored.update(webhook_status=status, webhook_due_at=due_at, webhook_state=state)
        return

    from sqlalchemy import update

    from felix.db.session import rls_bypass

    with rls_bypass():
        factory = get_session_factory(settings=settings)
        async with factory() as db:
            await db.execute(
                update(Fiber)
                .where(Fiber.tenant_id == row["tenant_id"], Fiber.id == row["id"])
                .values(webhook_status=status, webhook_due_at=due_at, webhook_state=state)
            )
            await db.commit()


async def deliver_due_webhooks(settings: Settings) -> int:
    """Announce every finished run whose delivery is due. Returns how many runs were tried."""
    # No sweep lease, by choice: rows are claimed `FOR UPDATE SKIP LOCKED`, so sweeps on several
    # workers split the due runs between them rather than send one twice, and a run's
    # announcement should not wait for another worker's slow receiver. (The skill-update sweep
    # takes a lease for the opposite reason: its per-tenant share of a tick only means something
    # when one sweep hands out the tick.)
    registry = parse_webhook_endpoints(settings)
    due = await _claim_due(settings, now_ms())
    if not due:
        return 0
    from felix.secrets import build_secrets

    sender = WebhookSender(settings, registry, build_secrets(settings), "run")
    deadline = now_ms() + WEBHOOK_SWEEP_BUDGET_MS
    for row in due:
        if now_ms() > deadline:
            break
        try:
            await _send_row(row, sender, deadline)
        except Exception:
            # The bookkeeping itself; the claim lapses and the next sweep tries again.
            logger.warning("webhook delivery for run %s could not be recorded", row.get("id"), exc_info=True)
    return len(due)


__all__ = [
    "WEBHOOK_CLAIM_MS",
    "WebhookEndpoint",
    "WebhookEndpointError",
    "WebhookSender",
    "canonical_body",
    "deliver_due_webhooks",
    "delivery_outcome",
    "endpoints_for_run",
    "parse_tenant_endpoint_bindings",
    "parse_webhook_endpoints",
    "sign",
    "validate_tenant_endpoint_bindings",
]
