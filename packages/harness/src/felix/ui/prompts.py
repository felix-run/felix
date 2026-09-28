"""Extension UI sub-protocol — select / confirm / input over SSE + waiters."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any, Literal

from felix.side_events import emit as emit_side_event
from felix.waiters import signal as waiter_signal
from felix.waiters import wait as waiter_wait
from felix.waiters import waiter_name

DEFAULT_TIMEOUT_SECONDS = 300.0
UiKind = Literal["select", "confirm", "input"]


@dataclass(slots=True)
class UiResponse:
    request_id: str
    kind: str
    value: Any = None
    cancelled: bool = False
    note: str = ""


def _waiter_name(thread_id: str | None, request_id: str) -> str:
    """The waiter a prompt blocks on: scoped to its thread, and so to its tenant.

    It used to be `ui:{request_id}` alone, which made the whole control the secrecy of a
    96-bit token: `POST /chat/ui` checked no tenant, no thread and no ownership, the one
    surface where every other route does. The thread a caller can name is namespaced to its
    own tenant by `effective_thread_id`, so putting the thread in the name means a prompt can
    only be answered from inside its own tenant's thread — the shape `client_bridge` already
    has for tool results. Routed through `waiter_name`, which escapes each part.
    """
    return waiter_name("ui", thread_id or "", request_id)


async def request_ui(
    thread_id: str | None,
    kind: UiKind,
    *,
    prompt: str,
    options: list[dict[str, Any]] | list[str] | None = None,
    default: Any = None,
    timeout: float | None = None,
    metadata: dict[str, Any] | None = None,
) -> UiResponse:
    """Emit a UI request on the SSE side-channel and wait for ``POST /chat/ui``."""
    request_id = secrets.token_urlsafe(12)
    payload: dict[str, Any] = {
        "request_id": request_id,
        "kind": kind,
        "prompt": prompt,
        "default": default,
        "thread_id": thread_id,
        "options": options or [],
        "metadata": metadata or {},
    }
    await emit_side_event(thread_id, "ui_request", payload)
    limit = DEFAULT_TIMEOUT_SECONDS if timeout is None else float(timeout)
    raw = await waiter_wait(_waiter_name(thread_id, request_id), timeout=limit)
    if raw is None:
        return UiResponse(request_id=request_id, kind=kind, cancelled=True, note="timeout")
    if raw.get("cancelled"):
        return UiResponse(
            request_id=request_id,
            kind=kind,
            cancelled=True,
            note=str(raw.get("note") or "cancelled"),
        )
    return UiResponse(
        request_id=request_id,
        kind=kind,
        value=raw.get("value"),
        cancelled=False,
        note=str(raw.get("note") or ""),
    )


async def request_select(
    thread_id: str | None,
    prompt: str,
    options: list[dict[str, Any]] | list[str],
    **kwargs: Any,
) -> UiResponse:
    return await request_ui(thread_id, "select", prompt=prompt, options=options, **kwargs)


async def request_confirm(
    thread_id: str | None,
    prompt: str,
    *,
    default: bool = False,
    **kwargs: Any,
) -> UiResponse:
    return await request_ui(
        thread_id, "confirm", prompt=prompt, default=default, options=["yes", "no"], **kwargs
    )


async def request_input(
    thread_id: str | None,
    prompt: str,
    *,
    default: str = "",
    **kwargs: Any,
) -> UiResponse:
    return await request_ui(thread_id, "input", prompt=prompt, default=default, **kwargs)


async def resolve_ui_response(
    thread_id: str,
    request_id: str,
    *,
    value: Any = None,
    cancelled: bool = False,
    note: str = "",
) -> dict[str, Any]:
    """Answer the prompt `request_id` on `thread_id`, which the caller must already have
    namespaced to its own tenant."""
    await waiter_signal(
        _waiter_name(thread_id, request_id),
        {"value": value, "cancelled": cancelled, "note": note},
    )
    return {"ok": True, "thread_id": thread_id, "request_id": request_id}


__all__ = [
    "UiResponse",
    "request_confirm",
    "request_input",
    "request_select",
    "request_ui",
    "resolve_ui_response",
]
