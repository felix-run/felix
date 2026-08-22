"""Procedural memory capture — inject active facts and extract new ones post-turn."""

from __future__ import annotations

import logging
import re
from typing import Any

from felix.config import Settings
from felix.manifests.schema import MemoryCapture
from felix.memory import store as memory_store
from felix.patterns.types import ChatMessage

logger = logging.getLogger("felix.memory.capture")

_FACT_LINE = re.compile(r"^\s*(?:[-*]|\d+[.)])\s*(.+)$")


async def active_facts_prompt(
    settings: Settings,
    tenant_id: str,
    *,
    manifest_id: str,
    limit: int = 20,
) -> str:
    rows = await memory_store.list_active(
        settings, tenant_id, manifest_id=manifest_id, kind="fact", limit=limit
    )
    if not rows:
        return ""
    lines = [f"- {r['content']}" for r in rows if r.get("content")]
    if not lines:
        return ""
    return "[known facts]\n" + "\n".join(lines)


def _heuristic_facts(text: str, *, max_facts: int, min_chars: int) -> list[str]:
    """Extract durable-looking statements without a model call."""
    facts: list[str] = []
    for raw in text.splitlines():
        m = _FACT_LINE.match(raw)
        candidate = (m.group(1) if m else raw).strip()
        if len(candidate) < min_chars:
            continue
        lower = candidate.lower()
        if any(skip in lower for skip in ("i think", "maybe", "not sure", "could you", "please", "?")):
            continue
        # Prefer declarative sentences.
        if candidate[0].isupper() and candidate.endswith("."):
            facts.append(candidate)
        if len(facts) >= max_facts:
            break
    return facts


async def capture_from_turn(
    settings: Settings,
    tenant_id: str,
    *,
    manifest_id: str,
    user_text: str,
    assistant_text: str,
    capture: MemoryCapture,
    model: Any | None = None,
) -> list[str]:
    """Extract and persist facts from a completed turn. Returns stored contents."""
    if not capture.enabled:
        return []
    blob = f"User: {user_text}\nAssistant: {assistant_text}".strip()
    if len(blob) < capture.min_chars:
        return []

    facts: list[str] = []
    if model is not None:
        try:
            result = await model.chat(
                [
                    ChatMessage(
                        role="system",
                        content=(
                            "Extract up to "
                            f"{capture.max_facts} durable facts from the dialogue. "
                            "Return one fact per line. No numbering. Skip ephemeral chatter."
                        ),
                    ),
                    ChatMessage(role="user", content=blob[:12000]),
                ],
                [],
            )
            for line in (result.message.content or "").splitlines():
                line = line.strip().lstrip("-* ").strip()
                if len(line) >= max(20, capture.min_chars // 2):
                    facts.append(line)
                if len(facts) >= capture.max_facts:
                    break
        except Exception:
            logger.debug("memory capture model call failed; using heuristic", exc_info=True)

    if not facts:
        facts = _heuristic_facts(
            assistant_text or user_text,
            max_facts=capture.max_facts,
            min_chars=max(20, capture.min_chars // 2),
        )

    stored: list[str] = []
    for fact in facts[: capture.max_facts]:
        try:
            await memory_store.put_memory(
                settings,
                tenant_id,
                content=fact,
                kind="fact",
                manifest_id=manifest_id,
                metadata={"source": "capture"},
            )
            stored.append(fact)
        except Exception:
            logger.debug("put_memory failed", exc_info=True)
    return stored


__all__ = ["active_facts_prompt", "capture_from_turn"]
