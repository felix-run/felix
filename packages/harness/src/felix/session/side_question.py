"""Side questions: answer one question about a thread without adding to it (`POST /chat/ask`).

During a long run an operator often wants to ask *about* the conversation — what file is it on,
why that approach — and the only way to ask was to send a message. That becomes part of the
thread, which the agent rereads on every later turn; mid-run it has to be a steer, which cancels
the remaining tool calls, or a follow-up, which waits for the run to end.

A side question renders the thread the way its next turn would — the manifest's own session
strategy and budgets, through `runtime.session_plumbing` — and makes one model call with no
tools bound. Nothing is written to the session log: the strategy reads through `_ReadOnlySession`,
which drops any write, so a compacting strategy over budget summarises for this answer and
leaves the log for the next turn to compact. No lease is taken and no phase is set, because the
moment a side question is most useful is while something else holds the thread.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

from felix.audit.emit import emit_agent_audit
from felix.config import Settings
from felix.context import async_run_with_context, get_context
from felix.hooks import chat_with_model_hooks, model_hook_context, run_filter_history
from felix.patterns.types import ChatMessage
from felix.session.types import GetEventsOpts, Session, SessionEvent, WakeState

logger = logging.getLogger("felix.session.side_question")

# The reply that means "the conversation does not say". A sentinel rather than a judgement made
# on the answer's wording, so a client can tell "the model doesn't know" from an answer.
NOT_IN_CONTEXT = "NOT_IN_CONTEXT"

_INSTRUCTION = (
    "This is a side question about the conversation above, asked by the person operating it. "
    "It is not part of the conversation and nothing you say here will be added to it. Answer "
    "only from what the conversation contains, briefly. If the conversation does not contain "
    f"the answer, reply with exactly {NOT_IN_CONTEXT} and nothing else; do not guess.\n\n"
    "Question: "
)


class _ReadOnlySession:
    """A session's reads, with every write dropped.

    An allowlist, not a proxy of everything: a strategy that reaches for a write this does not
    know about gets an `AttributeError`, never a write that lands. `get_event_skeletons` is
    present only when the underlying store has it, because `compaction._load_branch` asks for it
    by `getattr` and takes the slow path without it.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self.id = session.id
        self.dropped_writes = 0
        skeletons = getattr(session, "get_event_skeletons", None)
        if skeletons is not None:
            self.get_event_skeletons = skeletons

    async def get_events(self, opts: GetEventsOpts | None = None) -> list[SessionEvent]:
        return await self._session.get_events(opts)

    async def head(self) -> dict[str, int]:
        return await self._session.head()

    async def wake(self) -> WakeState:
        return await self._session.wake()

    async def append(self, event: Any) -> int | None:
        self.dropped_writes += 1
        return None

    async def append_batch(self, events: list[Any]) -> list[int]:
        self.dropped_writes += 1
        return []

    async def reset(self) -> None:
        self.dropped_writes += 1


def _side_answer_dict(
    thread_id: str, *, status: str, answer: str, usage: dict[str, Any] | None
) -> dict[str, Any]:
    """The `POST /chat/ask` response. A builder of its own so the client contract can record it."""
    return {"thread_id": thread_id, "status": status, "answer": answer, "usage": usage or {}}


def _read_answer(text: str) -> tuple[str, str]:
    """`(status, answer)` for the model's reply: the sentinel alone, or wrapped in punctuation, is
    `not_in_context`; anything else is an answer."""
    stripped = text.strip()
    if stripped.strip(" .`'\"*").upper() == NOT_IN_CONTEXT:
        return "not_in_context", ""
    return "answered", stripped


async def answer_side_question(
    settings: Settings,
    *,
    manifest: Any,
    manifest_id: str,
    tenant_id: str,
    thread_id: str,
    question: str,
    tools: Any,
) -> dict[str, Any]:
    """Answer `question` from `thread_id`'s active branch, leaving the thread exactly as it was."""
    from felix.manifests.builder import BuildDeps, _resolve_system_prompt
    from felix.patterns.model import build_model, record_model_usage
    from felix.runtime import default_object_store, session_plumbing
    from felix.session.tree import sync_leaf

    session_store, strategy = session_plumbing(settings, manifest, tenant_id)
    model = build_model(settings, manifest.spec.model)
    # The manifest's prompt as a turn resolves it, less what only matters with tools bound: the
    # skill catalog and tool guidance are left out because this call can use neither.
    system_prompt = await _resolve_system_prompt(
        manifest,
        BuildDeps(
            tools=tools, settings=settings, object_store=default_object_store(settings), tenant_id=tenant_id
        ),
    )
    incoming = [ChatMessage(role="user", content=_INSTRUCTION + question)]
    messages = [ChatMessage(role="system", content=system_prompt), *incoming]
    readonly: _ReadOnlySession | None = None
    if session_store is not None:
        session = session_store.open(thread_id)
        # The process's leaf index, which the branch walk reads, brought up to date with the
        # store — the same read a turn and `/chat/compact` start with. It moves no leaf.
        await sync_leaf(session)
        readonly = _ReadOnlySession(session)

    ctx = get_context()
    async with async_run_with_context(replace(ctx, manifest_id=manifest_id, thread_id=thread_id)):
        if readonly is not None:
            messages = await strategy.render(
                readonly, incoming, {"system_prompt": system_prompt, "model": model}
            )
        hook_ctx = model_hook_context(model, manifest_id=manifest_id, thread_id=thread_id, purpose="ask")
        messages = await run_filter_history(messages, context=hook_ctx)
        result = await chat_with_model_hooks(model, messages, [], context=hook_ctx)
        usage = record_model_usage(result, model, manifest_id=manifest_id) or None
        status, answer = _read_answer(result.message.content or "")
        if readonly is not None and readonly.dropped_writes:
            logger.info(
                "side question on %s rendered over budget; %d session write(s) dropped",
                thread_id,
                readonly.dropped_writes,
            )
        emit_agent_audit(
            "side_question",
            status="ok",
            manifest_id=manifest_id,
            payload={"thread_id": thread_id, "status": status, "chars": len(answer)},
        )
    return _side_answer_dict(thread_id, status=status, answer=answer, usage=usage)


__all__ = ["NOT_IN_CONTEXT", "answer_side_question"]
