"""Token-threshold session compaction (Pi-style)."""

from __future__ import annotations

import logging
from typing import Any

from felix.hooks import run_before_compact
from felix.patterns.types import ChatMessage
from felix.session.types import (
    AppendableEvent,
    Session,
    SessionEvent,
    SessionRenderOpts,
    event_to_chat_message,
)

logger = logging.getLogger("felix.session.compaction")

COMPACTION_METADATA_TYPE = "compaction"
SUMMARY_METADATA_TYPE = "session_summary"

STRUCTURED_SUMMARY_PROMPT = """Summarize the conversation for continued work. Use this exact structure:

## Goal
[What the user is trying to accomplish]

## Constraints & Preferences
- [Requirements mentioned by user]

## Progress
### Done
- [x] [Completed tasks]

### In Progress
- [ ] [Current work]

### Blocked
- [Issues, if any]

## Key Decisions
- **[Decision]**: [Rationale]

## Next Steps
1. [What should happen next]

## Critical Context
- [Data needed to continue]
"""


def estimate_tokens(text: str | None) -> int:
    """Rough token estimate (~4 chars/token)."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def estimate_event_tokens(event: SessionEvent) -> int:
    n = estimate_tokens(event.content)
    if event.tool_calls:
        for tc in event.tool_calls:
            n += estimate_tokens(str(tc.get("name") or ""))
            n += estimate_tokens(str(tc.get("args") or ""))
    return n


def estimate_messages_tokens(messages: list[ChatMessage]) -> int:
    return sum(estimate_tokens(m.content) for m in messages)


def is_pinned(event: SessionEvent) -> bool:
    return bool((event.metadata or {}).get("pinned"))


class CompactingSessionStrategy:
    """Auto-compact when context exceeds ``context_window - reserve_tokens``."""

    def __init__(
        self,
        *,
        reserve_tokens: int = 16384,
        keep_recent_tokens: int = 20000,
        context_window_tokens: int = 128000,
        enabled: bool = True,
        keep_turns: int | None = None,
    ) -> None:
        self.reserve_tokens = reserve_tokens
        self.keep_recent_tokens = keep_recent_tokens
        self.context_window_tokens = context_window_tokens
        self.enabled = enabled
        # Optional turn-based floor when token budget alone is fine.
        self.keep_turns = keep_turns

    async def render(
        self,
        session: Session,
        incoming: list[ChatMessage],
        opts: SessionRenderOpts | dict[str, Any],
    ) -> list[ChatMessage]:
        if isinstance(opts, dict):
            system_prompt = str(opts.get("system_prompt") or "")
            model = opts.get("model")
        else:
            system_prompt = opts.system_prompt
            model = opts.model

        all_events = await session.get_events()
        # Prefer explicit compaction entries; fall back to legacy session_summary audits.
        summaries = [
            e
            for e in all_events
            if (
                e.kind == "compaction"
                or (
                    e.kind == "audit"
                    and (e.metadata or {}).get("type")
                    in {COMPACTION_METADATA_TYPE, SUMMARY_METADATA_TYPE}
                )
            )
        ]
        summaries.sort(key=lambda e: e.seq, reverse=True)
        covered = -1
        first_kept_id: str | None = None
        latest_summary = summaries[0] if summaries else None
        if latest_summary and latest_summary.metadata:
            covered = int(
                latest_summary.metadata.get("covers_to_seq")
                or latest_summary.metadata.get("first_kept_seq", -1)
                or -1
            )
            first_kept_id = latest_summary.metadata.get("first_kept_entry_id")

        # Active branch when tree metadata present.
        from felix.session.tree import active_branch_events

        branch = active_branch_events(all_events, session_id=getattr(session, "id", ""))
        raw = [
            e
            for e in branch
            if e.kind not in {"audit", "compaction"} and e.role != "system" and e.seq > covered
        ]
        # If first_kept_entry_id is set, prefer keeping from that event onward.
        if first_kept_id:
            kept_from = next(
                (e for e in raw if (e.metadata or {}).get("event_id") == first_kept_id),
                None,
            )
            if kept_from is not None:
                raw = [e for e in raw if e.seq >= kept_from.seq]

        pinned = [e for e in raw if is_pinned(e)]
        compactable = [e for e in raw if not is_pinned(e)]

        summary_msg: ChatMessage | None = None
        if latest_summary and latest_summary.content:
            summary_msg = ChatMessage(
                role="system",
                content=f"[conversation summary]\n{latest_summary.content}",
            )

        history_msgs = [event_to_chat_message(e) for e in compactable]
        context_tokens = (
            estimate_tokens(system_prompt)
            + (estimate_tokens(summary_msg.content) if summary_msg else 0)
            + estimate_messages_tokens(history_msgs)
            + estimate_messages_tokens(incoming)
        )
        threshold = max(0, self.context_window_tokens - self.reserve_tokens)
        needs_compact = self.enabled and context_tokens > threshold
        if self.keep_turns is not None and len(compactable) > self.keep_turns:
            needs_compact = True

        if not needs_compact:
            merged = sorted([*pinned, *compactable], key=lambda e: e.seq)
            out = [ChatMessage(role="system", content=system_prompt)]
            if summary_msg:
                out.append(summary_msg)
            out.extend(event_to_chat_message(e) for e in merged)
            out.extend(incoming)
            return out

        # Find cut: walk newest→oldest until keep_recent_tokens reached.
        kept: list[SessionEvent] = []
        kept_tokens = 0
        for e in reversed(compactable):
            t = estimate_event_tokens(e)
            if kept and kept_tokens + t > self.keep_recent_tokens:
                break
            kept.insert(0, e)
            kept_tokens += t
        if self.keep_turns is not None and len(kept) > self.keep_turns:
            kept = kept[-self.keep_turns :]
        older = [e for e in compactable if e.seq < (kept[0].seq if kept else -1)]

        if not older:
            merged = sorted([*pinned, *compactable], key=lambda e: e.seq)
            out = [ChatMessage(role="system", content=system_prompt)]
            if summary_msg:
                out.append(summary_msg)
            out.extend(event_to_chat_message(e) for e in merged)
            out.extend(incoming)
            return out

        custom = await run_before_compact(
            {
                "messages_to_summarize": older,
                "previous_summary": latest_summary.content if latest_summary else None,
                "tokens_before": context_tokens,
                "first_kept_entry_id": (kept[0].metadata or {}).get("event_id") if kept else None,
                "first_kept_seq": kept[0].seq if kept else None,
            },
            context={"session_id": getattr(session, "id", None)},
        )
        if custom and custom.get("cancel"):
            merged = sorted([*pinned, *compactable], key=lambda e: e.seq)
            return [
                ChatMessage(role="system", content=system_prompt),
                *[event_to_chat_message(e) for e in merged],
                *incoming,
            ]

        summary_text: str | None = None
        if custom:
            compaction = custom.get("compaction") if "compaction" in custom else custom
            if isinstance(compaction, dict) and compaction.get("summary"):
                summary_text = str(compaction["summary"])

        if summary_text is None and model is None:
            note = ChatMessage(
                role="system",
                content=(
                    f"[session] compaction unavailable (no model); "
                    f"kept ~{kept_tokens} recent tokens "
                    f"(dropped {len(older)} older events)."
                ),
            )
            merged = sorted([*pinned, *kept], key=lambda e: e.seq)
            out = [ChatMessage(role="system", content=system_prompt), note]
            if summary_msg:
                out.append(summary_msg)
            out.extend(event_to_chat_message(e) for e in merged)
            out.extend(incoming)
            return out

        if summary_text is None and model is not None:
            try:
                prev = f"\nPrevious summary:\n{latest_summary.content}" if latest_summary else ""
                text = "\n".join(f"{e.role}: {e.content}" for e in older if e.content)
                result = await model.chat(
                    [
                        ChatMessage(role="system", content=STRUCTURED_SUMMARY_PROMPT + prev),
                        ChatMessage(role="user", content=text[:120_000]),
                    ],
                    [],
                )
                summary_text = result.message.content
            except Exception:
                logger.debug("compaction summarization failed", exc_info=True)
                note = ChatMessage(
                    role="system",
                    content=(
                        f"[session] compaction failed; kept {len(kept)} recent events "
                        f"(dropped {len(older)})."
                    ),
                )
                merged = sorted([*pinned, *kept], key=lambda e: e.seq)
                return [
                    ChatMessage(role="system", content=system_prompt),
                    note,
                    *[event_to_chat_message(e) for e in merged],
                    *incoming,
                ]

        if summary_text:
            first_kept = kept[0] if kept else None
            await session.append(
                AppendableEvent(
                    kind="compaction",
                    content=summary_text,
                    metadata={
                        "type": COMPACTION_METADATA_TYPE,
                        "covers_to_seq": older[-1].seq,
                        "first_kept_seq": first_kept.seq if first_kept else None,
                        "first_kept_entry_id": (first_kept.metadata or {}).get("event_id")
                        if first_kept
                        else None,
                        "tokens_before": context_tokens,
                    },
                )
            )
            summary_msg = ChatMessage(
                role="system",
                content=f"[conversation summary]\n{summary_text}",
            )

        merged = sorted([*pinned, *kept], key=lambda e: e.seq)
        out = [ChatMessage(role="system", content=system_prompt)]
        if summary_msg:
            out.append(summary_msg)
        out.extend(event_to_chat_message(e) for e in merged)
        out.extend(incoming)
        return out


__all__ = [
    "COMPACTION_METADATA_TYPE",
    "CompactingSessionStrategy",
    "STRUCTURED_SUMMARY_PROMPT",
    "estimate_event_tokens",
    "estimate_messages_tokens",
    "estimate_tokens",
]
