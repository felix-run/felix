"""Cross-provider handoff — serialize context when the model family changes."""

from __future__ import annotations

import logging
from typing import Any

from felix.patterns.types import ChatMessage

logger = logging.getLogger("felix.session.handoff")


def _routes(routes: dict[str, Any] | None = None) -> dict[str, Any]:
    """The route table, loaded once per call when the caller did not supply one."""
    if routes is not None:
        return routes
    try:
        from felix.patterns.model import parse_model_routes

        return dict(parse_model_routes())
    except Exception:  # pragma: no cover - a handoff note is never worth failing a run
        logger.debug("model routes unavailable for handoff", exc_info=True)
        return {}


def provider_family(model_id: str | None, *, routes: dict[str, Any] | None = None) -> str:
    """The provider a logical model id routes to, for handoff decisions.

    This used to fall back to sniffing the id for `claude`, `gpt`, `llama` and friends —
    the last substring-matched vendor branch in the harness. It got two things wrong that
    matter: two *different* unrecognised providers both answered `unknown`, so no handoff
    was generated when one was needed and a thread's tool calls and images were replayed
    to a model that could not read them; and it only ever agreed with the route table by
    coincidence of naming.

    The `routes` argument already existed and no caller passed it. Now the table is the
    only source, and an id that is not in it is `unknown` — but *distinctly* unknown, keyed
    on the id itself, so two unrecognised models still count as a family change.
    """
    if not model_id:
        return "unknown"
    route = _routes(routes).get(model_id)
    provider = getattr(route, "provider", None) or (
        route.get("provider") if isinstance(route, dict) else None
    )
    if provider:
        return str(provider)
    # Not in the table. Treat it as its own family rather than lumping every unknown id
    # together, which is what made a genuine cross-provider switch look like a no-op.
    return f"unknown:{model_id}"


def needs_handoff(
    previous_model: str | None,
    next_model: str | None,
    *,
    routes: dict[str, Any] | None = None,
) -> bool:
    if not previous_model or not next_model or previous_model == next_model:
        return False
    table = _routes(routes)
    return provider_family(previous_model, routes=table) != provider_family(next_model, routes=table)


def serialize_for_handoff(messages: list[ChatMessage], *, max_chars: int = 24_000) -> str:
    """Flatten conversation to plain text safe for any provider."""
    lines: list[str] = []
    for m in messages:
        role = m.role or "assistant"
        if role == "system":
            continue
        body = m.content or ""
        if m.attachments:
            for att in m.attachments:
                body += f"\n[image:{att.filename or att.url}]"
        if m.tool_calls:
            calls = ", ".join(f"{tc.name}({tc.args})" for tc in m.tool_calls)
            body = (body + f"\n[tools: {calls}]").strip()
        lines.append(f"{role.upper()}: {body}")
    text = "\n\n".join(lines)
    if len(text) > max_chars:
        text = text[: max_chars - 20] + "\n…[truncated]"
    return text


def handoff_system_message(
    messages: list[ChatMessage],
    *,
    previous_model: str | None,
    next_model: str | None,
    routes: dict[str, Any] | None = None,
) -> ChatMessage | None:
    """Build a system note to prepend when switching provider families."""
    if not needs_handoff(previous_model, next_model, routes=routes):
        return None
    transcript = serialize_for_handoff(messages)
    return ChatMessage(
        role="system",
        content=(
            f"[model handoff] Switched from {previous_model} to {next_model}. "
            f"Prior conversation (text-only):\n\n{transcript}"
        ),
    )


__all__ = [
    "handoff_system_message",
    "needs_handoff",
    "provider_family",
    "serialize_for_handoff",
]
