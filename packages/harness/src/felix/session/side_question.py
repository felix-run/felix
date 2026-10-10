"""Side questions: answer one question about a thread without adding to it (`POST /chat/ask`).

During a long run an operator often wants to ask *about* the conversation — what file is it on,
why that approach — and the only way to ask was to send a message. That becomes part of the
thread, which the agent rereads on every later turn; mid-run it has to be a steer, which cancels
the remaining tool calls, or a follow-up, which waits for the run to end.

A side question is answered by the thread's own manifest, under the controls a turn of it would
be: its inbound auth and compile pin, its governance checks, its input screening on the question,
its replay screening on the history, and its reply controls (PII, final-response judges) on the
answer. What it does not share with a turn is any write. The history is rendered by the
manifest's session strategy from the *stored* summary — no summariser call, no compaction hook —
through `_ReadOnlySession`, which drops anything a strategy tries to append; the branch is walked
from the leaf the store holds without moving this process's leaf index; and no lease, phase,
pin, steer or follow-up is touched, because the moment a side question is most useful is while a
run or another tab holds the thread.

The history reaches the model as a transcript in one message, not as the thread's own turns: a
thread mid-run ends on tool calls with no result yet, which a provider refuses as a turn
history, and this call binds no tools for the calls it does have to refer to.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import replace
from typing import Any

from felix.audit.emit import emit_agent_audit
from felix.config import Settings
from felix.context import async_run_with_context, get_context
from felix.hooks import chat_with_model_hooks, model_hook_context, run_filter_history
from felix.logging_setup import loggable
from felix.patterns.types import ChatMessage
from felix.session.compaction import _UNTRUSTED_NOTICE, fence_untrusted
from felix.session.types import GetEventsOpts, Session, SessionEvent, WakeState

logger = logging.getLogger("felix.session.side_question")

# The reply that means "the conversation does not say". A sentinel rather than a judgement made
# on the answer's wording, so a client can tell "the model doesn't know" from an answer.
NOT_IN_CONTEXT = "NOT_IN_CONTEXT"

_INSTRUCTION = (
    "Below is the transcript of a conversation, then a side question about it from the person "
    "operating it. The question is not part of the conversation and nothing you say here will "
    "be added to it. Answer only from what the transcript contains, briefly. If it does not "
    f"contain the answer, reply with exactly {NOT_IN_CONTEXT} and nothing else; do not guess."
)

# The prompt a manifest with sub-agents is asked under. Its own prompt is a router's or a
# planner's — "choose exactly one of these routes" — which answers a side question with a route.
_COMPOSITE_PROMPT = "You answer questions about a conversation an agent team has been having."


class UnknownThreadError(LookupError):
    """The thread has no recorded manifest: it has never had a turn, so there is nothing to ask."""


class UnknownManifestError(LookupError):
    """The manifest the thread last ran under no longer resolves."""


class _ReadOnlySession:
    """A session's reads, with every write dropped, under an id of its own.

    An allowlist, not a proxy of everything: a strategy that reaches for a write this does not
    know about gets an `AttributeError`, never a write that lands. `get_event_skeletons` is
    present only when the underlying store has it, because `compaction._load_branch` asks for it
    by `getattr` and takes the slow path without it.

    `id` is not the thread's. The branch walk reads this process's leaf index by session id,
    and the only way to fill that index for the thread is `sync_leaf`, which moves the leaf and
    its epoch a turn in flight is relying on — past a rewind it has not followed. Under its own
    id the walk reads the leaf `_rendered` put there from the store, and the thread's entry is
    never touched.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self.id = f"side-question:{uuid.uuid4().hex}"
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
    thread_id: str, *, manifest_id: str, status: str, answer: str, usage: dict[str, Any] | None
) -> dict[str, Any]:
    """The `POST /chat/ask` response. A builder of its own so the client contract can record it."""
    return {
        "thread_id": thread_id,
        "manifest": manifest_id,
        "status": status,
        "answer": answer,
        "usage": usage or {},
    }


def _read_answer(text: str) -> tuple[str, str]:
    """`(status, answer)` for the model's reply: the sentinel alone, or wrapped in punctuation, is
    `not_in_context`; anything else is an answer."""
    stripped = text.strip()
    if stripped.strip(" .`'\"*").upper() == NOT_IN_CONTEXT:
        return "not_in_context", ""
    return "answered", stripped


def _transcript(history: list[ChatMessage]) -> str:
    """The rendered history as one text: who said what, which tools were called, what they gave."""
    from felix.patterns.model_vision import message_has_images

    lines: list[str] = []
    for m in history:
        text = m.content or ""
        if message_has_images(m):
            text = f"{text} [image omitted]".strip()
        if m.role == "tool":
            lines.append(f"tool {m.name or '?'} returned: {text}")
        elif m.role == "system":
            if text:
                lines.append(f"[context] {text}")
        else:
            if text:
                lines.append(f"{m.role}: {text}")
            for call in m.tool_calls or []:
                lines.append(f"{m.role} called {call.name}({json.dumps(call.args, default=str)})")
    return "\n\n".join(lines)


async def _manifest_of(settings: Settings, tenant_id: str, thread_id: str) -> str:
    from felix.session.thread_state import LAST_MANIFEST_KEY, get_thread_meta

    meta = await get_thread_meta(settings=settings, tenant_id=tenant_id, thread_id=thread_id)
    name = meta.get(LAST_MANIFEST_KEY) or meta.get("manifest_name")
    if not name:
        raise UnknownThreadError(thread_id)
    return str(name)


async def _rendered(
    settings: Settings,
    manifest: Any,
    tenant_id: str,
    thread_id: str,
    *,
    question: str,
    system_prompt: str,
    model: Any,
) -> list[ChatMessage]:
    """The thread as its next turn would read it, less any summary that turn would write.

    The question is handed to the strategy as the incoming turn, because a `semantic:N`
    strategy ranks history against it, and dropped from what is returned: it reaches the model
    once, outside the fenced transcript.
    """
    from felix.governance.image_screening import screen_session_strategy
    from felix.governance.inbound import replay_screener
    from felix.runtime import session_plumbing
    from felix.session.tree import set_leaf, stored_leaf

    session_store, strategy = session_plumbing(settings, manifest, tenant_id)
    if session_store is None:
        return []
    strategy = screen_session_strategy(strategy, replay_screener(manifest, settings))
    session = session_store.open(thread_id)
    readonly = _ReadOnlySession(session)
    placeholder = ChatMessage(role="user", content=question)
    set_leaf(readonly.id, await stored_leaf(session))
    try:
        rendered = await strategy.render(
            readonly,
            [placeholder],
            {"system_prompt": system_prompt, "model": model, "stored_summary_only": True},
        )
    finally:
        set_leaf(readonly.id, None)
    if readonly.dropped_writes:
        logger.warning(
            "side question on %s: the session strategy tried %d write(s); dropped",
            loggable(thread_id, limit=200),
            readonly.dropped_writes,
        )
    return [m for m in rendered if m is not placeholder]


async def _admitted(settings: Settings, auth: Any, tenant_id: str, thread_id: str) -> tuple[str, Any]:
    """The thread's own manifest, admitted as `prepare_tenant_invoke` and the compile admit a
    turn of it — inbound auth, the compile pin, the governance checks — with nothing written."""
    from felix.manifests.governance import assert_cost_limit_is_measurable, validate_governance
    from felix.manifests.inbound_auth import enforce_inbound_auth
    from felix.manifests.pin import check_thread_pin
    from felix.runtime import resolve_tenant_manifest

    manifest_id = await _manifest_of(settings, tenant_id, thread_id)
    try:
        resolved = await resolve_tenant_manifest(settings, tenant_id, manifest_id, thread_id=thread_id)
    except (LookupError, ValueError) as exc:
        raise UnknownManifestError(manifest_id) from exc
    manifest = resolved.manifest
    enforce_inbound_auth(manifest, auth)
    await check_thread_pin(
        settings=settings,
        tenant_id=tenant_id,
        thread_id=thread_id,
        manifest=manifest,
        version=resolved.version,
        resolved_out=resolved.sub_agents,
    )
    validate_governance(manifest, settings)
    assert_cost_limit_is_measurable(manifest, settings)
    return manifest_id, manifest


async def _screened_answer(
    settings: Settings, manifest: Any, manifest_id: str, reply: str
) -> tuple[str, str]:
    """`(status, answer)`, through the reply controls a turn's answer passes through: PII redacted,
    or `withheld` with the notice when PII blocks it or a final-response judge refuses it."""
    from felix.governance.reply import PII_BLOCKED_REPLY
    from felix.manifests.builder import bind_decider, reply_screen_for

    status, answer = _read_answer(reply)
    screen = reply_screen_for(
        manifest.spec.guardrails, manifest_id, decider=bind_decider(manifest.spec.decider, settings)
    )
    if screen is None or not answer:
        return status, answer
    answer = await screen.redact_async(answer)
    if answer == PII_BLOCKED_REPLY:
        return "withheld", answer
    denial = await screen.judge(answer)
    return ("withheld", denial) if denial is not None else (status, answer)


async def _system_prompt(settings: Settings, manifest: Any, tenant_id: str, tools: Any) -> str:
    """The manifest's prompt as a turn resolves it, less what only matters with tools bound."""
    from felix.manifests.builder import BuildDeps, _resolve_system_prompt
    from felix.manifests.governance import apply_transparency_notice
    from felix.runtime import default_object_store

    if manifest.spec.sub_agents:
        prompt = _COMPOSITE_PROMPT
    else:
        prompt = await _resolve_system_prompt(
            manifest,
            BuildDeps(
                tools=tools,
                settings=settings,
                object_store=default_object_store(settings),
                tenant_id=tenant_id,
                workspace_root=getattr(settings, "workspace_root", None) or None,
                load_agents_md=bool(getattr(settings, "load_agents_md", False)),
            ),
        )
    if manifest.spec.governance.transparency_notice:
        prompt = apply_transparency_notice(prompt or "", manifest.metadata.name)
    return prompt


async def answer_side_question(
    settings: Settings,
    *,
    auth: Any,
    tenant_id: str,
    thread_id: str,
    question: str,
    tools: Any,
) -> dict[str, Any]:
    """Answer `question` from `thread_id`, as the thread's manifest, leaving the thread as it was.

    Raises `UnknownThreadError` for a thread with no turns, `UnknownManifestError` for a manifest
    that no longer resolves, and what a turn's admission raises for the rest: `InboundAuthError`,
    `ManifestDriftError`, `GovernanceError`, `InboundScreeningError`.
    """
    from felix.governance.inbound import apply_inbound_screening, inbound_controls_enabled
    from felix.patterns.model import build_model, record_model_usage

    manifest_id, manifest = await _admitted(settings, auth, tenant_id, thread_id)

    ctx = get_context()
    async with async_run_with_context(replace(ctx, manifest_id=manifest_id, thread_id=thread_id)):
        if inbound_controls_enabled(manifest):
            screened = await apply_inbound_screening(
                manifest, [ChatMessage(role="user", content=question)], settings
            )
            question = screened[-1].content or ""
        system_prompt = await _system_prompt(settings, manifest, tenant_id, tools)
        model = build_model(settings, manifest.spec.model)
        history = await _rendered(
            settings,
            manifest,
            tenant_id,
            thread_id,
            question=question,
            system_prompt=system_prompt,
            model=model,
        )
        hook_ctx = model_hook_context(model, manifest_id=manifest_id, thread_id=thread_id, purpose="ask")
        history = await run_filter_history(history, context=hook_ctx)
        messages = [
            ChatMessage(role="system", content=system_prompt),
            ChatMessage(
                role="user",
                # Fenced as a summariser's transcript is: it carries tool output, which may be
                # written to read as a closing tag and a question of its own.
                content=(
                    f"{_INSTRUCTION}{_UNTRUSTED_NOTICE}\n{fence_untrusted(_transcript(history[1:]))}\n\n"
                    f"Side question: {question}"
                ),
            ),
        ]
        result = await chat_with_model_hooks(model, messages, [], context=hook_ctx)
        usage = record_model_usage(result, model, manifest_id=manifest_id) or None
        status, answer = await _screened_answer(settings, manifest, manifest_id, result.message.content or "")
        emit_agent_audit(
            "side_question",
            status="ok",
            manifest_id=manifest_id,
            payload={"thread_id": thread_id, "status": status, "chars": len(answer)},
        )
    return _side_answer_dict(thread_id, manifest_id=manifest_id, status=status, answer=answer, usage=usage)


__all__ = ["NOT_IN_CONTEXT", "UnknownManifestError", "UnknownThreadError", "answer_side_question"]
