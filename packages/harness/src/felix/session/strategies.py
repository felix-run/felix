"""SessionStrategy implementations — full_replay / windowed / summarizing / semantic."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from felix.patterns.types import ChatMessage
from felix.session.types import (
    Session,
    SessionEvent,
    SessionRenderOpts,
    SessionStrategy,
    event_to_chat_message,
)

logger = logging.getLogger("felix.session.strategies")

SUMMARY_METADATA_TYPE = "session_summary"


def is_pinned(event: SessionEvent) -> bool:
    return bool((event.metadata or {}).get("pinned"))


class FullReplaySessionStrategy:
    async def render(
        self,
        session: Session,
        incoming: list[ChatMessage],
        opts: SessionRenderOpts | dict[str, Any],
    ) -> list[ChatMessage]:
        system_prompt = opts.system_prompt if isinstance(opts, SessionRenderOpts) else opts["system_prompt"]
        events = await session.get_events()
        from felix.session.tree import active_branch_events

        branch = active_branch_events(events, session_id=getattr(session, "id", ""))
        from felix.session.types import include_in_llm_context

        history = [event_to_chat_message(e) for e in branch if include_in_llm_context(e)]
        return [ChatMessage(role="system", content=system_prompt), *history, *incoming]


class WindowedSessionStrategy:
    def __init__(self, max_turns: int) -> None:
        self.max_turns = max_turns

    async def render(
        self,
        session: Session,
        incoming: list[ChatMessage],
        opts: SessionRenderOpts | dict[str, Any],
    ) -> list[ChatMessage]:
        system_prompt = opts.system_prompt if isinstance(opts, SessionRenderOpts) else opts["system_prompt"]
        events = await session.get_events()
        from felix.session.tree import active_branch_events

        branch = active_branch_events(events, session_id=getattr(session, "id", ""))
        from felix.session.types import include_in_llm_context

        filtered = [e for e in branch if include_in_llm_context(e)]
        pinned = [e for e in filtered if is_pinned(e)]
        unpinned = [e for e in filtered if not is_pinned(e)]
        windowed = unpinned[-self.max_turns :] if self.max_turns > 0 else []
        merged = sorted([*pinned, *windowed], key=lambda e: e.seq)
        return [
            ChatMessage(role="system", content=system_prompt),
            *[event_to_chat_message(e) for e in merged],
            *incoming,
        ]


class SummarizingSessionStrategy:
    def __init__(self, keep: int) -> None:
        self.keep = keep

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
        summaries = [
            e
            for e in all_events
            if e.kind == "audit" and (e.metadata or {}).get("type") == SUMMARY_METADATA_TYPE
        ]
        summaries.sort(key=lambda e: e.seq, reverse=True)
        covered = -1
        latest_summary = summaries[0] if summaries else None
        if latest_summary and latest_summary.metadata:
            covered = int(latest_summary.metadata.get("covers_to_seq") or -1)

        from felix.session.types import include_in_llm_context

        raw = [e for e in all_events if include_in_llm_context(e) and e.seq > covered]
        pinned = [e for e in raw if is_pinned(e)]
        compactable = [e for e in raw if not is_pinned(e)]

        summary_msg: ChatMessage | None = None
        if latest_summary and latest_summary.content:
            summary_msg = ChatMessage(
                role="system",
                content=f"[conversation summary]\n{latest_summary.content}",
            )

        if len(compactable) <= self.keep:
            merged = sorted([*pinned, *compactable], key=lambda e: e.seq)
            out = [ChatMessage(role="system", content=system_prompt)]
            if summary_msg:
                out.append(summary_msg)
            out.extend(event_to_chat_message(e) for e in merged)
            out.extend(incoming)
            return out

        older, newer = compactable[: -self.keep], compactable[-self.keep :]
        if model is None:
            # Honest fallback: windowed keep + explicit truncation notice.
            note = ChatMessage(
                role="system",
                content=(
                    f"[session] summarizing unavailable (no model); "
                    f"kept last {self.keep} of {len(compactable)} turns "
                    f"(dropped {len(older)} older turns)."
                ),
            )
            merged = sorted([*pinned, *newer], key=lambda e: e.seq)
            out = [ChatMessage(role="system", content=system_prompt), note]
            if summary_msg:
                out.append(summary_msg)
            out.extend(event_to_chat_message(e) for e in merged)
            out.extend(incoming)
            return out

        if model is not None:
            try:
                text = "\n".join(f"{e.role}: {e.content}" for e in older if e.content)
                result = await model.chat(
                    [
                        ChatMessage(
                            role="system",
                            content=(
                                "Summarize this conversation briefly (3-5 sentences). "
                                "Preserve goals, decisions, constraints, and pending work."
                            ),
                        ),
                        ChatMessage(role="user", content=text),
                    ],
                    [],
                )
                summary_text = result.message.content
                from felix.session.types import AppendableEvent

                await session.append(
                    AppendableEvent(
                        kind="audit",
                        content=summary_text,
                        metadata={
                            "type": SUMMARY_METADATA_TYPE,
                            "covers_to_seq": older[-1].seq if older else covered,
                        },
                    )
                )
                summary_msg = ChatMessage(
                    role="system",
                    content=f"[conversation summary]\n{summary_text}",
                )
            except Exception:
                logger.debug("summarization failed; falling back to windowed", exc_info=True)
                note = ChatMessage(
                    role="system",
                    content=(
                        f"[session] summarization failed; kept last {self.keep} of {len(compactable)} turns."
                    ),
                )
                merged = sorted([*pinned, *newer], key=lambda e: e.seq)
                return [
                    ChatMessage(role="system", content=system_prompt),
                    note,
                    *[event_to_chat_message(e) for e in merged],
                    *incoming,
                ]

        merged = sorted([*pinned, *newer], key=lambda e: e.seq)
        out = [ChatMessage(role="system", content=system_prompt)]
        if summary_msg:
            out.append(summary_msg)
        out.extend(event_to_chat_message(e) for e in merged)
        out.extend(incoming)
        return out


class SemanticSessionStrategy:
    """Top-N relevance by embeddings when available, else keyword overlap."""

    def __init__(self, top_n: int) -> None:
        self.top_n = top_n

    async def render(
        self,
        session: Session,
        incoming: list[ChatMessage],
        opts: SessionRenderOpts | dict[str, Any],
    ) -> list[ChatMessage]:
        system_prompt = opts.system_prompt if isinstance(opts, SessionRenderOpts) else opts["system_prompt"]
        query = " ".join(m.content for m in incoming if m.role == "user").lower()
        tokens = set(query.split()) if query else set()
        events = await session.get_events()
        from felix.session.tree import active_branch_events

        branch = active_branch_events(events, session_id=getattr(session, "id", ""))
        from felix.session.types import include_in_llm_context

        candidates = [e for e in branch if include_in_llm_context(e)]
        pinned = [e for e in candidates if is_pinned(e)]
        unpinned = [e for e in candidates if not is_pinned(e)]
        query_text = " ".join(m.content for m in incoming if m.role == "user")
        used_embeddings = False
        ranked: list[SessionEvent]
        try:
            from felix.embeddings import rank_indices_by_query_async

            blobs = [e.content or "" for e in unpinned]
            order = await rank_indices_by_query_async(query_text, blobs, "bge-base-en-v1.5")
            if order is not None:
                ranked = [unpinned[i] for i in order][: self.top_n]
                used_embeddings = True
            else:
                ranked = []
        except Exception:
            ranked = []
        if not used_embeddings:

            def score(e: SessionEvent) -> int:
                words = set((e.content or "").lower().split())
                return len(tokens & words)

            ranked = sorted(unpinned, key=score, reverse=True)[: self.top_n]
        merged = sorted([*pinned, *ranked], key=lambda e: e.seq)
        kind = "embeddings" if used_embeddings else "keyword overlap, not embeddings"
        note = ChatMessage(
            role="system",
            content=(
                f"[session] semantic selection ({kind}): "
                f"included {len(merged)} of {len(candidates)} events (top_n={self.top_n})."
            ),
        )
        return [
            ChatMessage(role="system", content=system_prompt),
            note,
            *[event_to_chat_message(e) for e in merged],
            *incoming,
        ]


SessionStrategyFactory = Callable[..., SessionStrategy]

_strategies: dict[str, SessionStrategyFactory] = {}

# Built-in prefixes, so an unrecognised strategy can name what it could have been.
_BUILTIN_STRATEGIES = ("full_replay", "windowed:N", "summarizing:N", "compacting[:N]", "semantic:N")

# The prefixes the parser below claims. A plugin may not take one of these.
_BUILTIN_STRATEGY_PREFIXES = frozenset({"full_replay", "windowed", "summarizing", "compacting", "semantic"})


def register_session_strategy(prefix: str, factory: SessionStrategyFactory) -> None:
    """Register a session strategy under ``prefix``.

    ``SessionStrategy`` is a Protocol and `spec.session.strategy` is an open
    string, but the parser was closed, so a third party could implement the
    interface and never be selected. A registered prefix matches either exactly
    (``"mine"``) or as ``"mine:<arg>"``; the factory receives the argument text
    (empty string when absent) plus the same keyword budget the built-ins get.
    """
    if prefix in _BUILTIN_STRATEGY_PREFIXES:
        # Same rule as auth modes: a plugin may add a strategy, never silently
        # replace a built-in. Strategy governs how much context reaches the model,
        # so shadowing `compacting` from an installed package is the same class of
        # risk as shadowing `api_key`.
        raise ValueError(
            f"session strategy {prefix!r} is built in and cannot be overridden "
            f"(built-ins: {', '.join(sorted(_BUILTIN_STRATEGY_PREFIXES))})"
        )
    _strategies[prefix] = factory


def list_session_strategies() -> list[str]:
    return sorted(_strategies)


def _match_registered(raw: str) -> tuple[SessionStrategyFactory, str] | None:
    """Longest prefix wins, so resolution does not depend on registration order."""
    for prefix in sorted(_strategies, key=len, reverse=True):
        factory = _strategies[prefix]
        if raw == prefix:
            return factory, ""
        if raw.startswith(f"{prefix}:"):
            return factory, raw[len(prefix) + 1 :]
    return None


def get_session_strategy(
    spec: str,
    *,
    reserve_tokens: int = 16384,
    keep_recent_tokens: int = 20000,
    context_window_tokens: int = 128000,
    compaction_enabled: bool = True,
) -> SessionStrategy:
    """Parse strategy string: full_replay | windowed:N | summarizing:N | semantic:N | compacting."""
    from felix.session.compaction import CompactingSessionStrategy

    raw = (spec or "full_replay").strip()

    registered = _match_registered(raw)
    if registered is not None:
        factory, arg = registered
        return factory(
            arg,
            reserve_tokens=reserve_tokens,
            keep_recent_tokens=keep_recent_tokens,
            context_window_tokens=context_window_tokens,
            compaction_enabled=compaction_enabled,
        )

    if raw.startswith("windowed:"):
        n = int(raw.split(":", 1)[1] or "20")
        return WindowedSessionStrategy(n)
    if raw.startswith("summarizing:"):
        n = int(raw.split(":", 1)[1] or "20")
        # Upgrade summarizing to token-aware compaction with a turn floor.
        return CompactingSessionStrategy(
            reserve_tokens=reserve_tokens,
            keep_recent_tokens=keep_recent_tokens,
            context_window_tokens=context_window_tokens,
            enabled=compaction_enabled,
            keep_turns=n,
        )
    if raw.startswith("compacting"):
        # compacting or compacting:keepTurns
        keep_turns = None
        if ":" in raw:
            try:
                keep_turns = int(raw.split(":", 1)[1] or "0") or None
            except ValueError:
                keep_turns = None
        return CompactingSessionStrategy(
            reserve_tokens=reserve_tokens,
            keep_recent_tokens=keep_recent_tokens,
            context_window_tokens=context_window_tokens,
            enabled=compaction_enabled,
            keep_turns=keep_turns,
        )
    if raw.startswith("semantic:"):
        n = int(raw.split(":", 1)[1] or "10")
        return SemanticSessionStrategy(n)
    if raw != "full_replay":
        # Silently degrading meant a typo ("windowed-20", "compact") bought full
        # replay and an unbounded context, with nothing in the logs to say so.
        known = ", ".join((*_BUILTIN_STRATEGIES, *list_session_strategies()))
        logger.warning(
            "unknown session strategy %r; falling back to full_replay (known: %s)",
            raw,
            known,
        )
    return FullReplaySessionStrategy()


full_replay_session_strategy = FullReplaySessionStrategy()

__all__ = [
    "FullReplaySessionStrategy",
    "SemanticSessionStrategy",
    "SummarizingSessionStrategy",
    "WindowedSessionStrategy",
    "full_replay_session_strategy",
    "get_session_strategy",
    "is_pinned",
    "list_session_strategies",
    "register_session_strategy",
]
