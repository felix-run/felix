"""Cross-provider handoff — serialize context when the model family changes."""

from __future__ import annotations

from typing import Any

from felix.patterns.types import ChatMessage


def provider_family(model_id: str | None, *, routes: dict[str, Any] | None = None) -> str:
    """Return a coarse provider family for handoff decisions."""
    mid = (model_id or "").lower()
    if routes and model_id in routes:
        route = routes[model_id]
        if isinstance(route, dict):
            return str(route.get("provider") or "unknown")
        return str(getattr(route, "provider", None) or "unknown")
    if "claude" in mid or "anthropic" in mid:
        return "anthropic"
    if mid.startswith("gpt") or "openai" in mid or mid.startswith("o1") or mid.startswith("o3"):
        return "openai"
    if "llama" in mid or "ollama" in mid or "mistral" in mid:
        return "oss"
    return "unknown"


def needs_handoff(previous_model: str | None, next_model: str | None) -> bool:
    if not previous_model or not next_model or previous_model == next_model:
        return False
    return provider_family(previous_model) != provider_family(next_model)


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
) -> ChatMessage | None:
    """Build a system note to prepend when switching provider families."""
    if not needs_handoff(previous_model, next_model):
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
