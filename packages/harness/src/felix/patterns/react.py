"""react pattern — canonical tool-calling loop."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import aclosing
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from felix_ai.types import split_file_ref

from felix.audit.emit import emit_agent_audit
from felix.config import get_settings
from felix.hooks import (
    model_hook_context,
    run_after_model,
    run_before_model,
    run_before_turn,
    run_filter_history,
)
from felix.manifests.schema import ABSOLUTE_LIMITS, ModelSpec
from felix.observability.metrics import record_counter
from felix.patterns.model import (
    ModelChatOptions,
    ModelChatResult,
    ModelClient,
    ModelGatewayError,
    build_model,
    record_model_usage,
    supports_stream_turn,
    wire_model_id,
)
from felix.patterns.overflow import is_context_overflow, is_silent_overflow
from felix.patterns.registry import PatternBuildContext, register_pattern
from felix.patterns.tool_runner import FatalCall, ToolRunner
from felix.patterns.types import (
    Agent,
    ChatMessage,
    Event,
    InvokeInput,
    InvokeOutput,
    StopReason,
)
from felix.side_events import drain as drain_side_events
from felix.side_events import release as release_side_events
from felix.steer import (
    AbortPoll,
    clear_abort,
    clear_cancel_flag,
    drain_follow_up,
    drain_steer,
    ensure_run_queue,
    is_aborted,
    release_run_queue,
)
from felix.tools.retrieval import select_tools_from_ctx_async
from felix.tools.types import Tool
from felix.workspace_notes import WorkspaceNote, mark_run_active, mark_run_idle
from felix.workspace_notes import drain as drain_workspace_notes

if TYPE_CHECKING:
    from felix.decisions import MeteredDecider
    from felix.manifest_hooks import ManifestHooks
    from felix.manifests.schema import HookEventName

logger = logging.getLogger("felix.patterns.react")

DEFAULT_RECURSION = 10

# How long to wait on a running tool batch before checking the side-event queue again.
# The queue has one reader (`drain`), so this is a poll rather than a wakeup; a quarter
# second is imperceptible against a decision a person has to make, and bounds the idle
# wakeups a blocked run costs to four a second.
SIDE_EVENT_POLL_SECONDS = 0.25


def _status_for_stop(stop: str) -> str:
    """Session status for a terminal stop reason.

    `max_tokens` means the answer is cut off mid-thought; `refusal` means a safety
    classifier declined. Neither is a completed turn, and recording either as one hides
    a partial or absent answer behind a successful-looking run.
    """
    if stop in {"max_tokens", "max_turns"}:
        return "truncated"
    if stop == "refusal":
        return "refused"
    return "complete"


def _quarantine_truncated_tool_calls(assistant: ChatMessage) -> list[ChatMessage]:
    """Fail every tool call on a response the model did not finish writing.

    A turn that stops on `max_tokens` can still carry a syntactically complete `tool_use`
    block whose arguments were cut off mid-write: the JSON parses, so the call validates,
    and nothing downstream can tell it apart from a call the model finished. Executing it
    runs a *different* action than the one the model intended — `{"path": "/srv/app/tmp"}`
    truncated to `{"path": "/srv"}` is a valid call to the wrong target.

    That also slips past governance rather than being caught by it: command screening
    inspects the arguments it is handed, so a shortened path or a `rm -rf` that lost its
    tail screens clean. The whole batch is failed rather than any part of it executed —
    a tool call is only trustworthy if the message carrying it was finished.
    """
    quarantined: list[ChatMessage] = []
    for call in assistant.tool_calls or []:
        if not call.id:
            call.id = f"call_{uuid.uuid4().hex[:12]}"
        quarantined.append(
            ChatMessage(
                role="tool",
                tool_call_id=call.id,
                name=call.name,
                content=(
                    "[error/truncated] The model reached its output limit while writing "
                    "this tool call, so the arguments may be incomplete and it was not "
                    "executed. Retry with a shorter request or a higher max_tokens."
                ),
            )
        )
    return quarantined


def _interrupted_tool_results(messages: list[ChatMessage], tool_map: dict[str, Tool]) -> list[ChatMessage]:
    """Close out tool calls that a previous run started and never finished.

    A run that dies mid-tool -- the process is killed, the fiber is reclaimed, the client
    disconnects -- leaves an assistant turn holding a tool call with no result. The
    provider requires every tool call in the history to be answered, so resuming that
    thread sends a transcript it rejects outright: the one situation `/chat/continue`
    exists for was the one it could not do.

    Each unanswered call gets a result saying it was interrupted. Whether the effect
    actually happened is not knowable from here, so the message says which way the tool
    was declared: `replay_safe` tools are safe for the model to call again, and anything
    else -- the default -- is explicitly not, because re-running a search costs latency
    while re-running a payment charges twice.
    """
    answered: set[str] = {m.tool_call_id for m in messages if m.role == "tool" and m.tool_call_id}
    results: list[ChatMessage] = []
    for message in messages:
        if message.role != "assistant" or not message.tool_calls:
            continue
        for call in message.tool_calls:
            if not call.id or call.id in answered:
                continue
            answered.add(call.id)
            tool = tool_map.get(call.name)
            retryable = bool(tool is not None and tool.replay_safe)
            advice = (
                "It is safe to call again."
                if retryable
                else "Do not assume it succeeded or failed, and do not call it again "
                "without checking; it may have already taken effect."
            )
            results.append(
                ChatMessage(
                    role="tool",
                    tool_call_id=call.id,
                    name=call.name,
                    content=f"[error/interrupted] This call did not finish. {advice}",
                )
            )
    return results


def _clamp(value: int, ceiling: int) -> int:
    return min(max(value, 1), ceiling)


def _model_spec_with_override(spec: Any, model_id: str | None) -> Any:
    if not model_id or spec is None:
        return spec
    if isinstance(spec, ModelSpec):
        data = spec.model_dump()
        data["id"] = model_id
        return ModelSpec.model_validate(data)
    # duck-typed
    try:
        clone = deepcopy(spec)
        clone.id = model_id
        return clone
    except Exception:
        return spec


def _tool_end_data(tool_msg: ChatMessage) -> dict[str, Any]:
    """A `tool_end` frame: the result, and the stored images the tool returned beside it.

    Only references (`felix-file://<id>`) go on the frame; a client fetches each from
    `GET /files/{id}` as it does an uploaded image. Before this a screenshot reached a
    watching client only on the next read of the session log, so a browser call's card read
    as a line of text for the whole run. An image still inline -- kept only when there was no
    request tenant to store it under, an eval run -- stays off the stream: its bytes are up
    to the attachment cap each, and the log keeps them for whoever reads it.
    """
    data: dict[str, Any] = {"name": tool_msg.name, "output": tool_msg.content, "id": tool_msg.tool_call_id}
    images = [
        {"url": a.url, "media_type": a.media_type}
        for a in tool_msg.attachments or ()
        if split_file_ref(a.url)
    ]
    if images:
        data["attachments"] = images
    return data


@dataclass
class _ReactAgent:
    tools: list[Tool]
    pattern: str
    manifest_id: str
    manifest_version: str
    system_prompt: str
    model_spec: Any
    # `repr=False`: `Settings` carries provider keys, and a dataclass repr of the agent in a
    # log line or a pytest introspection would print them.
    settings: Any = field(repr=False)
    recursion_limit: int
    limits: Any = None
    context_prelude: str = ""
    session_store: Any | None = None
    session_strategy: Any | None = None
    tenant_id: str = "default"
    memory_capture: Any | None = None
    tools_retrieval: Any | None = None
    # `spec.skill_suggestion`, built: suggests one skill per turn as a transient hint.
    skill_suggester: Any | None = None
    # `spec.decider`, built: a metered decision provider, or None when the manifest has none.
    decider: MeteredDecider | None = None
    procedural_memory: Any | None = None
    # The compile's `ReplyScreen` chain, when reply controls are on: memory capture extracts
    # from the reply as the controls ship it, not as the model wrote it.
    reply_screen: Any | None = None
    # `spec.hooks`, built: session_start and user_prompt_submit at the top of a run, pre/post tool
    # in the tool runner, stop below. Not `felix.hooks`, the in-process plugin seam.
    manifest_hooks: ManifestHooks | None = None
    tool_execution: str = "sequential"
    steering_mode: str = "all"
    follow_up_mode: str = "all"
    compact_after_turn: bool = False
    # `spec.output_schema`: the shape every answer this agent gives must have. Held on the
    # agent rather than read per request because it is the manifest's contract, not the
    # caller's — a client cannot widen or replace it.
    output_schema: dict[str, Any] | None = None
    _tool_map: dict[str, Tool] = field(init=False, repr=False)
    _last_model_id: str | None = field(default=None, init=False, repr=False)
    # Decider tool rankings for this agent, keyed by request and candidate set: selection
    # runs several times per step and the request does not change between them.
    _tool_rankings: dict[tuple[Any, ...], Any] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self._tool_map = {t.name: t for t in self.tools}
        self._tools = ToolRunner(
            tool_map=self._tool_map,
            manifest_id=self.manifest_id,
            tool_execution=self.tool_execution,
            manifest_hooks=self.manifest_hooks,
        )

    async def _active_tools(self, messages: list[ChatMessage]) -> list[Tool]:
        """The tools a run exposes, narrowed by `spec.tools_retrieval`, in manifest order.

        Async because selection encodes the query and every candidate description
        once a retrieval model is configured, and that must not run on the event
        loop. With retrieval off — the default — it stays inline.

        Manifest order rather than rank order: tool definitions are the front of the
        provider's cache prefix, so two runs that pick the same tools must send them
        byte-identically. The loop calls this once per run — see `_tools_for_run`.
        """
        chosen = await select_tools_from_ctx_async(
            self.tools,
            messages,
            self.tools_retrieval,
            decider=self.decider,
            cache=self._tool_rankings,
        )
        if chosen is self.tools:
            return chosen
        names = {t.name for t in chosen}
        return [t for t in self.tools if t.name in names]

    def _chat_options(self, input: InvokeInput) -> ModelChatOptions | None:
        """The caller's per-request sampling, bounded by the manifest.

        `max_tokens` may only come *down*: the wire prefers the caller's value over
        `spec.model.max_tokens`, and the output budget is checked at the top of a turn,
        so an unclamped value would let one request size a whole turn past the ceiling
        the operator declared — and past `limits.max_output_tokens` by a full turn.
        """
        if input.model_options is None and self.output_schema is None:
            # The common case, and the only one with nothing to send.
            return None
        # One construction path from here down. The manifest's `output_schema` used to get a
        # `ModelChatOptions` of its own when the caller sent none, so a second manifest-level
        # option would have had to be added in two places — and the one that landed in only
        # one of them would be invisible.
        opts = input.model_options or ModelChatOptions()
        spec = _model_spec_with_override(self.model_spec, input.model_id)
        ceiling = int(getattr(spec, "max_tokens", None) or ABSOLUTE_LIMITS["max_output_tokens"])
        # `limits` is None for a pattern builder that hands the loop a bare context;
        # `_over_budget` tolerates that, and so must this.
        if self.limits is not None and self.limits.max_output_tokens:
            ceiling = min(ceiling, int(self.limits.max_output_tokens))
        max_tokens = (
            opts.max_tokens if opts.max_tokens is None else max(1, min(int(opts.max_tokens), ceiling))
        )
        # The manifest's schema wins over anything the caller sent. `/v1` lets a client ask
        # for a `response_format`, and an agent published with an answer contract must keep
        # answering to it rather than to whichever shape the last request preferred.
        schema = self.output_schema or opts.output_schema
        return replace(opts, max_tokens=max_tokens, output_schema=schema)

    def _resolve_model(self, input: InvokeInput) -> Any:
        settings = self.settings or get_settings()
        spec = _model_spec_with_override(self.model_spec, input.model_id)
        # Apply live thinking level from thread meta or input when present.
        level = getattr(input, "thinking_level", None)
        if level:
            from felix.session.thinking import apply_thinking_to_spec

            spec = apply_thinking_to_spec(spec, level)
        elif getattr(spec, "thinking_level", None):
            from felix.session.thinking import apply_thinking_to_spec

            spec = apply_thinking_to_spec(spec, spec.thinking_level)
        return build_model(settings, spec, decider=self.decider)

    def _apply_handoff(
        self, messages: list[ChatMessage], *, previous: str | None, next_id: str | None
    ) -> list[ChatMessage]:
        if not next_id:
            return messages
        try:
            from felix.session.handoff import handoff_system_message

            note = handoff_system_message(messages, previous_model=previous, next_model=next_id)
            if note is not None:
                # Insert after the first system message when present.
                if messages and messages[0].role == "system":
                    return [messages[0], note, *messages[1:]]
                return [note, *messages]
        except Exception:
            logger.debug("handoff note failed", exc_info=True)
        return messages

    async def _maybe_compact_after_turn(
        self,
        messages: list[ChatMessage],
        *,
        thread_id: str | None,
        model: ModelClient,
    ) -> list[ChatMessage]:
        """Compact mid-run when over budget, then continue without aborting."""
        if not self.compact_after_turn:
            return messages
        if not thread_id or self.session_store is None or self.session_strategy is None:
            return messages
        compact_now = getattr(self.session_strategy, "compact_now", None)
        render = getattr(self.session_strategy, "render", None)
        if not callable(render):
            return messages
        try:
            from felix.session.compaction import estimate_messages_tokens

            strategy = self.session_strategy
            window = int(getattr(strategy, "context_window_tokens", 128000) or 128000)
            reserve = int(getattr(strategy, "reserve_tokens", 16384) or 16384)
            if estimate_messages_tokens(messages) <= max(0, window - reserve):
                return messages
            session = self.session_store.open(thread_id)
            if callable(compact_now):
                await compact_now(session, model=model, reason="after_turn")
            rebuilt = await render(
                session,
                [],
                {
                    "system_prompt": self.system_prompt,
                    "model": model,
                    "force_compact": True,
                    "compact_reason": "after_turn",
                    "will_retry": True,
                },
            )
            if rebuilt:
                return rebuilt
        except Exception:
            logger.debug("compact_after_turn failed", exc_info=True)
        return messages

    async def _load_thinking_level(self, thread_id: str | None) -> str | None:
        if not thread_id or self.settings is None:
            return None
        try:
            from felix.session.thread_state import get_thread_meta

            meta = await get_thread_meta(
                settings=self.settings,
                tenant_id=self.tenant_id,
                thread_id=thread_id,
            )
            level = meta.get("thinking_level")
            return str(level) if level and level != "off" else None
        except Exception:
            return None

    async def _sync_leaf(self, thread_id: str | None) -> None:
        """Take the thread's leaf from the store before this turn appends or renders anything.

        Every turn passes through `_run`, whatever surface started it (`/chat`, `/v1`, A2A,
        MCP, a durable fiber, a scheduled job, an eval), so this is the one place a turn's
        appends and its rendered branch are pinned to what the store holds rather than to what
        this process last saw. Once per turn: the turn's own appends move the leaf after it.
        """
        if not thread_id or self.session_store is None:
            return
        try:
            from felix.session.tree import sync_leaf

            await sync_leaf(self.session_store.open(thread_id))
        except Exception:
            # The turn can still run on this process's leaf; failing it here would turn a
            # read of the leaf into a lost turn.
            logger.warning("leaf sync failed for thread=%s", thread_id, exc_info=True)

    async def _persist_model_change(self, input: InvokeInput) -> None:
        if not input.model_id or not input.thread_id or self.session_store is None:
            return
        try:
            from felix.session.thread_state import update_thread_meta
            from felix.session.tree import annotate_and_append
            from felix.session.types import AppendableEvent

            session = self.session_store.open(input.thread_id)
            await annotate_and_append(
                session,
                [
                    AppendableEvent(
                        kind="model_change",
                        content=input.model_id,
                        metadata={"type": "model_change", "model_id": input.model_id},
                    )
                ],
            )
            await update_thread_meta(
                settings=self.settings,
                tenant_id=input.tenant_id or self.tenant_id,
                thread_id=input.thread_id,
                model_id=input.model_id,
            )
        except Exception:
            logger.debug("model_change persist failed", exc_info=True)

    async def _note_preview(self, input: InvokeInput) -> None:
        """Stash the thread's first user message for the session index, once per thread.

        `GET /chat/sessions` lists threads from their metadata alone, so this is what lets a
        client recognise a thread it did not start. Every turn passes here; only a thread's
        first one writes (`thread_state.note_first_message`).
        """
        if not input.thread_id or self.session_store is None:
            return
        text = next(
            (m.content for m in input.messages if m.role == "user" and m.content and m.content.strip()),
            None,
        )
        if text is None:
            return
        try:
            from felix.session.thread_state import note_first_message

            await note_first_message(
                settings=self.settings,
                tenant_id=input.tenant_id or self.tenant_id,
                thread_id=input.thread_id,
                text=text,
            )
        except Exception:
            # A missing preview lists the thread by its id, as before; not worth a turn.
            logger.warning("session preview write failed for thread=%s", input.thread_id, exc_info=True)

    async def _close_interrupted_gates(
        self, thread_id: str | None, tenant_id: str, interrupted: list[ChatMessage]
    ) -> None:
        """Withdraw what interrupted calls were waiting on. Never raises.

        A call that died waiting on an approval or a client left its gate open: the approval
        row `pending` to its deadline, the client request announced to every stream that
        attaches. Both asked about a call the model has now been told did not finish, and an
        approval given then installed a grant for nobody (felix-run/felix#531).
        """
        ids = [m.tool_call_id for m in interrupted if m.tool_call_id]
        if not thread_id or not ids:
            return
        try:
            from felix.tools import client_requests

            for call_id in ids:
                await client_requests.clear(thread_id, call_id, tenant_id=tenant_id)
        except Exception:
            logger.debug("clearing interrupted client requests failed", exc_info=True)
        if self.settings is None:
            return
        try:
            from felix.approvals.store import close_interrupted

            closed = await close_interrupted(self.settings, tenant_id, thread_id, ids)
            if closed:
                logger.info("closed %d approval(s) left by interrupted calls (thread=%s)", closed, thread_id)
        except Exception:
            logger.warning("closing interrupted approvals failed", exc_info=True)

    async def _append_produced(
        self,
        thread_id: str | None,
        messages: list[ChatMessage],
        *,
        usage: dict[str, Any] | None = None,
        status: str | None = None,
    ) -> None:
        if not thread_id or self.session_store is None or not messages:
            return
        try:
            from felix.session.tree import annotate_and_append
            from felix.session.types import chat_message_to_event

            events = []
            for i, m in enumerate(messages):
                ev = chat_message_to_event(m)
                md = dict(ev.metadata or {})
                if usage and i == len(messages) - 1 and m.role == "assistant":
                    md["usage"] = usage
                if status and m.role == "assistant":
                    md["status"] = status
                ev.metadata = md or None
                events.append(ev)
            session = self.session_store.open(thread_id)
            await annotate_and_append(session, events)
        except Exception:
            # A lost write is a hole in the transcript, and with reply controls on it is
            # also where a screening failure lands: not something to hide at debug.
            logger.warning("session append failed", exc_info=True)

    async def _stream_one_turn(
        self,
        model: ModelClient,
        messages: list[ChatMessage],
        active_tools: list[Tool],
        thread_id: str | None,
        tenant_id: str,
        opts: ModelChatOptions | None = None,
    ) -> AsyncIterator[Event | ModelChatResult]:
        """Run one streamed turn, yielding display events and then the result.

        Extracted so the caller can retry the whole turn after compacting, which it can
        only do while nothing has been emitted.
        """
        poll = AbortPoll(tenant_id, thread_id) if thread_id else None
        if supports_stream_turn(model):
            stream_turn = model.stream_turn
            # One request for the whole turn. See `stream_turn` for why the
            # stream-then-chat pair it replaces was worse than it looked.
            async for item in stream_turn(messages, active_tools, opts):
                if isinstance(item, ModelChatResult):
                    yield item
                    continue
                if poll is not None and await poll.aborted():
                    return
                if item.kind == "text":
                    yield Event(
                        event="text_delta",
                        data={"chunk": {"content": item.text}, "delta": item.text},
                    )
                elif item.kind == "thinking" and item.text:
                    # Reasoning has always been on the wire, but only inside the
                    # `session_progress` envelope below — a frame whose job is run
                    # phase, carrying model output as a passenger. Every consumer
                    # had to know to dig for it, and the one that renders the
                    # transcript read `phase` and dropped the rest.
                    #
                    # Its own name, shaped like `text_delta` so a reader that
                    # handles one can handle the other. The progress frame keeps
                    # carrying it: anything already reading it there still works.
                    yield Event(
                        event="thinking_delta",
                        data={"chunk": {"content": item.text}, "delta": item.text},
                    )
                yield Event(
                    event="session_progress",
                    data={
                        "progress": {
                            "type": "assistant_delta",
                            "kind": item.kind,
                            "delta": item.text,
                        }
                    },
                )
            return

        # A provider that only implements `stream()` cannot report tool calls or usage
        # from the streamed request, so the authoritative turn still costs a second call.
        # Plugin-supplied clients land here.
        async for delta in model.stream(messages, active_tools, opts):
            if poll is not None and await poll.aborted():
                return
            yield Event(event="text_delta", data={"chunk": {"content": delta}, "delta": delta})
            yield Event(
                event="session_progress",
                data={"progress": {"type": "assistant_delta", "kind": "text", "delta": delta}},
            )

    async def _before_model(
        self,
        messages: list[ChatMessage],
        tools: list[Tool],
        model: ModelClient,
        thread_id: str | None,
    ) -> list[ChatMessage]:
        """What one model call sends, after the `before_model` hooks; the run's history is unchanged."""
        return await run_before_model(
            messages,
            tools=[t.name for t in tools],
            context=model_hook_context(
                model, manifest_id=self.manifest_id, thread_id=thread_id, purpose="turn"
            ),
        )

    async def _after_model(
        self,
        assistant: ChatMessage,
        result: ModelChatResult,
        model: ModelClient,
        thread_id: str | None,
    ) -> ChatMessage:
        """The assistant message after the `after_model` hooks, which the run records and acts on."""
        return await run_after_model(
            assistant,
            stop_reason=getattr(result, "stop_reason", None),
            context=model_hook_context(
                model, manifest_id=self.manifest_id, thread_id=thread_id, purpose="turn"
            ),
        )

    async def _recover_from_overflow(
        self,
        thread_id: str | None,
        model: ModelClient,
        *,
        reason: str,
    ) -> list[ChatMessage] | None:
        """Force a compaction pass and re-render, or return None if that is not possible.

        Called when the provider says the request did not fit. Compaction is normally
        driven by a token estimate, and the estimate can be behind the truth or the
        configured window can be larger than the model really has — in which case the
        rejection is the first accurate signal that the conversation is too long.
        """
        if not thread_id or self.session_store is None or self.session_strategy is None:
            return None
        compact_now = getattr(self.session_strategy, "compact_now", None)
        render = getattr(self.session_strategy, "render", None)
        if not callable(compact_now) or not callable(render):
            return None
        try:
            session = self.session_store.open(thread_id)
            await compact_now(session, model=model, system_prompt=self.system_prompt, reason=reason)
            rebuilt = await render(
                session,
                [],
                {
                    "system_prompt": self.system_prompt,
                    "model": model,
                    "force_compact": True,
                    "compact_reason": reason,
                },
            )
        except Exception:
            logger.warning("compaction after context overflow failed", exc_info=True)
            return None
        if not rebuilt:
            return None
        record_counter(
            "felix_context_overflow_recovered",
            {"manifest_id": self.manifest_id, "reason": reason},
        )
        return list(rebuilt)

    def _overflowed(self, result: ModelChatResult, model: ModelClient) -> bool:
        """True when a turn that did not raise nonetheless did not fit."""
        usage = getattr(result, "usage", None)
        if usage is None:
            return False
        window = 0
        try:
            from felix.model_catalog import entry_for

            window = entry_for(wire_model_id(model)).context_window
        except Exception:
            window = 0
        return is_silent_overflow(
            stop_reason=getattr(result, "stop_reason", None),
            tokens_input=int(getattr(usage, "input", 0) or 0),
            tokens_output=int(getattr(usage, "output", 0) or 0),
            context_window=window,
        )

    def _audit_final_response(
        self,
        thread_id: str | None,
        final: ChatMessage,
        *,
        fatal: FatalCall | None,
        ended_denied: bool,
        denied_calls: int,
        died: BaseException | None = None,
    ) -> None:
        """The run's one `final_response` row, and on a failed run, why it failed (#543).

        `reasons` holds each cause that applies: `fatal` (a fatal tool's failure ended the run,
        named in `fatal_call` so the row leads to that call's `tool_call` row), `denied` (the
        last round had a refusal; the run usually replied around it), `cancelled` (the caller
        went away or the task was cancelled) and `exception` (the run raised, `error_type` saying
        what — #305: a model call that timed out left no row at all, so the run read as never
        having finished). The exception's message stays out, as a failed call's does.
        """
        reasons: list[str] = []
        payload: dict[str, Any] = {
            "thread_id": thread_id,
            "chars": len(final.content or ""),
            "denied_calls": denied_calls,
        }
        if fatal is not None:
            reasons.append("fatal")
            payload["fatal_call"] = {"tool_call_id": fatal.tool_call_id, "error_code": fatal.error_code.value}
        if ended_denied:
            reasons.append("denied")
        if isinstance(died, (asyncio.CancelledError, GeneratorExit)):
            reasons.append("cancelled")
        elif died is not None:
            reasons.append("exception")
            payload["error_type"] = type(died).__name__
        if reasons:
            payload["reasons"] = reasons
        emit_agent_audit(
            "final_response",
            status="error" if reasons else "ok",
            manifest_id=self.manifest_id,
            payload=payload,
        )

    def _note_stop_reason(self, stop_reason: str, thread_id: str | None) -> str:
        """Record a stop reason and return the session status it implies.

        `getattr` at the call sites: a plugin-supplied ModelClient (or a test double) may
        not populate `stop_reason`, and that must not break the run.
        """
        status = _status_for_stop(stop_reason)
        if status != "complete":
            logger.warning(
                "run ended on stop_reason=%s (manifest=%s thread=%s)",
                stop_reason,
                self.manifest_id,
                thread_id,
            )
            record_counter(
                "felix_run_stop_reason",
                {"manifest_id": self.manifest_id, "reason": stop_reason},
            )
        return status

    async def _turn_seq(self, thread_id: str | None) -> int | None:
        """This turn's ordinal on its thread, for stamping memory provenance.

        The session log's own `seq` is the turn clock — it is already monotonic per
        thread and allocated under an advisory lock — so there is no second counter to
        keep in step. Read once per turn so every fact the turn writes shares an
        ordinal; without that, an as-of reconstruction would see them appear
        one at a time.
        """
        if not thread_id or self.session_store is None:
            return None
        try:
            head = await self.session_store.open(thread_id).head()
            return int(head.get("seq") or 0)
        except Exception:
            logger.debug("turn seq lookup failed; storing memory without provenance", exc_info=True)
            return None

    def _capture_model(self, turn_model: ModelClient) -> Any:
        """The model fact extraction runs on.

        `spec.memory.capture.model` exists precisely so extraction does not run on the
        turn's model — it is a small, mechanical summarisation job on every turn, and
        billing it to a frontier model doubles the cost of having memory at all. The
        field was declared and never read, so extraction silently used the turn model.

        Falls back to the turn's model when the configured one cannot be built, since
        a memory captured on an expensive model still beats no memory.
        """
        capture = self.memory_capture
        wanted = str(getattr(capture, "model", "") or "")
        if not wanted or self.settings is None:
            return turn_model
        try:
            from felix.patterns.model import build_one_model

            return build_one_model(self.settings, self.model_spec, wanted)
        except Exception:
            logger.debug("capture model %r unavailable; using the turn model", wanted, exc_info=True)
            return turn_model

    async def _maybe_capture_memory(self, input: InvokeInput, final: ChatMessage, model: ModelClient) -> None:
        capture = self.memory_capture
        if capture is None or not getattr(capture, "enabled", False):
            return
        if self.settings is None:
            return
        user_text = " ".join(m.content for m in input.messages if m.role == "user")
        assistant_text: str | None = final.content or ""
        if self.reply_screen is not None and assistant_text:
            # Capture runs inside the reply controls, on the reply they have not screened yet.
            # A fact stored from text the controls redacted would come back in every later
            # prompt that recalls it; a denied reply is not an answer to learn from.
            assistant_text = await self.reply_screen.settle(assistant_text)
            if assistant_text is None:
                return
        try:
            from felix.memory.capture import capture_from_turn

            await capture_from_turn(
                self.settings,
                input.tenant_id or self.tenant_id,
                manifest_id=self.manifest_id,
                user_text=user_text,
                assistant_text=assistant_text,
                capture=capture,
                model=self._capture_model(model),
                origin_seq=await self._turn_seq(input.thread_id),
                thread_id=input.thread_id or "",
            )
        except Exception:
            logger.debug("memory capture failed", exc_info=True)

    async def _procedures(self, messages: list[ChatMessage], tenant_id: str) -> str | None:
        """Procedures recalled for this request — sent as transient guidance, not as `system`.

        This was appended as a system message, and the Anthropic wire folds every system
        message into the one cached `system` block: a per-request block there changed the
        cached prefix on every turn, so a manifest with procedural memory never read its
        system prompt from cache. It is also model-extracted text, which the prelude's
        docstring already keeps out of the instruction tier.
        """
        spec = self.procedural_memory
        if spec is None or not getattr(spec, "enabled", False) or self.settings is None:
            return None
        try:
            from felix.memory.procedural import query_from_user_messages, retrieve_procedures

            block = await retrieve_procedures(
                self.settings,
                tenant_id,
                manifest_id=self.manifest_id,
                query=query_from_user_messages(messages),
                spec=spec,
            )
        except Exception:
            logger.debug("procedural retrieve failed", exc_info=True)
            return None
        return block or None

    def _over_budget(self) -> bool:
        """True when a declared run budget is spent; trips the shared abort flag."""
        from felix.context import try_get_context
        from felix.limits import check_budgets, trip

        req = try_get_context()
        if req is None:
            return False
        ls = req.limit_state
        if ls.aborted:
            return True
        verdict = check_budgets(self.limits, ls)
        if verdict.exceeded:
            trip(ls, verdict.reason)
            logger.info("run over budget: %s", verdict.reason)
            return True
        return False

    async def _entry_hooks(self, input: InvokeInput) -> list[ChatMessage]:
        """Fire `session_start` (a thread's first run) and `user_prompt_submit`; raise `HookBlocked`
        on a refusal, else return their context as transient guidance.

        First thing in a run, so a refused prompt leaves nothing behind: no queue, no append."""
        hooks = self.manifest_hooks
        if hooks is None:
            return []
        from felix.manifest_hooks import HookBlocked

        prompt = next((str(m.content or "") for m in reversed(input.messages) if m.role == "user"), "")
        data = {"manifest_id": self.manifest_id, "prompt": prompt}
        guidance: list[ChatMessage] = []
        events: list[HookEventName] = []
        if hooks.has("session_start") and input.thread_id and self.session_store is not None:
            # No event yet, not "no metadata": the route writes the thread's metadata (its pin, its
            # preview) before the run, so `thread_exists` already answers yes on the first run.
            head = await self.session_store.open(input.thread_id).head()
            if not head.get("seq"):
                events.append("session_start")
        if hooks.has("user_prompt_submit"):
            events.append("user_prompt_submit")
        for event in events:
            outcome = await hooks.fire(event, data)
            if outcome.blocked:
                raise HookBlocked(outcome.hook_id, outcome.reason)
            if outcome.contexts:
                guidance.append(ChatMessage(role="user", content=outcome.context_block(), transient=True))
        return guidance

    async def _stop_hook(self, final: ChatMessage, continuations: int, step: int) -> ChatMessage | None:
        """Ask the `stop` hooks whether the agent may finish. Transient guidance to continue with,
        or None.

        Bounded twice: `MAX_STOP_CONTINUATIONS` per run, and the recursion limit -- a hook sending
        the agent back on the run's last step would end it at `max_turns` with nothing said. A hook
        that could not be asked lets the agent finish: `on_error: block` on `stop` would otherwise
        send it back for an outage. The reason is the hook's text, so it is fenced and screened
        and attached to the next call only, never stored as a turn the next run replays."""
        from felix.manifest_hooks import MAX_STOP_CONTINUATIONS, fence, screened

        hooks = self.manifest_hooks
        if hooks is None or not hooks.has("stop") or continuations >= MAX_STOP_CONTINUATIONS:
            return None
        if step + 1 >= self.recursion_limit:
            return None
        outcome = await hooks.fire(
            "stop", {"manifest_id": self.manifest_id, "final": str(final.content or "")}
        )
        if not outcome.blocked or outcome.errored:
            return None
        reason = await screened(outcome.reason or "the task is not finished")
        note = "The task is not finished yet; keep working.\n\n" + fence(outcome.hook_id, reason)
        if outcome.contexts:
            note = f"{note}\n\n{outcome.context_block()}"
        return ChatMessage(role="user", content=note, transient=True)

    async def _transient_guidance(self, messages: list[ChatMessage], tenant_id: str) -> list[ChatMessage]:
        """Messages for this run's first model call only — see `ChatMessage.transient`.

        Procedures and the skill hint are independent lookups, gathered so the first token
        waits for the slower of them rather than their sum.
        """
        import asyncio

        async def _none() -> None:
            return None

        hint = self.skill_suggester.hint(messages) if self.skill_suggester is not None else _none()
        notes = await asyncio.gather(self._procedures(messages, tenant_id), hint)
        return [ChatMessage(role="user", content=note, transient=True) for note in notes if note]

    def _prelude_messages(self) -> list[ChatMessage]:
        """Volatile per-run reference material, kept out of the cached system prefix.

        Recalled memory facts used to be appended to the system prompt. Caching is a
        prefix match over tools -> system -> messages, so a block that changes whenever
        memory writes a fact invalidated the whole cached prefix every turn. Rendering it
        as user-role material also keeps model-extracted text — which can originate in
        tool output — out of the developer-tier instruction channel.
        """
        if not self.context_prelude:
            return []
        return [ChatMessage(role="user", content=self.context_prelude)]

    def _with_prelude(self, messages: list[ChatMessage]) -> list[ChatMessage]:
        """Insert the per-run prelude directly after the leading system prompt.

        Applied *after* the session render, not before it. A strategy builds its own
        list from the session log and returns that, so a prelude placed in the list
        beforehand was discarded on every threaded turn — which is every turn that has
        a thread — and reached the model only on threadless invokes.

        It sits next to the system prompt, where it reads as framing, rather than at the
        tail, where it would read as the user's latest turn. That position is not free:
        the Anthropic wire also puts a breakpoint on the newest message, so a prelude that
        changed since the previous run — memory captured a fact — re-bills the whole
        conversation once, on that run's first call. Moving it later trades that for
        either dropping it after the first step or breaking the reasoning chain after
        tool results, which is why it stays here until a measurement says otherwise.
        """
        prelude = self._prelude_messages()
        if not prelude:
            return messages
        head = 0
        while head < len(messages) and messages[head].role == "system":
            head += 1
        return [*messages[:head], *prelude, *messages[head:]]

    async def _assemble_messages(
        self, input: InvokeInput, model: ModelClient, tenant_id: str
    ) -> list[ChatMessage]:
        """Build the message list a turn starts from.

        The session's own rendering of history if there is one, then the per-run prelude,
        a cross-model handoff note, the history filter hook, and any procedural memory. A
        session that fails to render degrades to the incoming messages rather than failing
        the run.
        """
        messages: list[ChatMessage] = [
            ChatMessage(role="system", content=self.system_prompt),
            *input.messages,
        ]
        if input.thread_id and self.session_store is not None and self.session_strategy is not None:
            try:
                session = self.session_store.open(input.thread_id)
                messages = await self.session_strategy.render(
                    session,
                    input.messages,
                    {"system_prompt": self.system_prompt, "model": model},
                )
            except Exception:
                logger.debug("session render failed; using incoming messages", exc_info=True)

        messages = self._with_prelude(messages)

        prev_model = self._last_model_id
        current_model = input.model_id or getattr(model, "model_id", None)
        messages = self._apply_handoff(messages, previous=prev_model, next_id=current_model)
        self._last_model_id = current_model

        messages = await run_filter_history(
            messages,
            context={"manifest_id": self.manifest_id, "thread_id": input.thread_id},
        )
        return messages

    async def _deliver_follow_ups(
        self,
        tenant_id: str,
        thread_id: str | None,
        messages: list[ChatMessage],
        produced: list[ChatMessage],
        delivered: list[ChatMessage],
        step: int,
        *,
        emit_events: bool,
    ) -> AsyncIterator[Event]:
        """Hand queued follow-ups to a run that has gone idle, so the loop takes another turn.

        Called where the loop would stop with an answer — a reply with no tool calls, or a
        terminal tool — and nowhere else. The follow-ups land in `delivered` (an async
        generator cannot return a value); the caller `continue`s when there are any, so a
        follow-up's turn is an ordinary step: streamed, tools run, overflow recovered, stop
        reason recorded, budgets and abort checked. It used to be a bare `model.chat` after the
        loop, which did none of that — a tool call in its reply was appended and never run.

        Left queued, for the thread's next run, when the run has no step left to answer with or
        has been aborted. A run that stops on a budget, a truncation or the step limit never
        reaches here, so its follow-ups wait the same way rather than buying an uncapped turn.
        """
        if not thread_id or step + 1 >= self.recursion_limit or await is_aborted(tenant_id, thread_id):
            return
        for follow in await drain_follow_up(
            tenant_id,
            thread_id,
            mode=self.follow_up_mode,  # type: ignore[arg-type]
        ):
            chat = ChatMessage(role="user", content=follow.text)
            messages.append(chat)
            produced.append(chat)
            delivered.append(chat)
            if emit_events:
                yield Event(event="follow_up", data={"content": follow.text})
        if delivered:
            await self._append_produced(thread_id, delivered)

    async def _record_workspace_notes(self, thread_id: str, notes: list[WorkspaceNote]) -> None:
        """Append each note to the log as the in-context entry `/chat/workspace/edited` would.

        `custom`, so a client can tell it from a turn the operator typed, with `in_context` set
        and the `user` role: a `system` custom entry is downgraded to a labelled user turn on
        render (`session.types._model_role_and_content`), and a note is the operator's own
        statement, not one the client added.
        """
        if self.session_store is None or not notes:
            return
        try:
            from felix.session.tree import annotate_and_append
            from felix.session.types import AppendableEvent

            await annotate_and_append(
                self.session_store.open(thread_id),
                [
                    AppendableEvent(kind="custom", role="user", content=n.text(), metadata=n.metadata())
                    for n in notes
                ],
            )
        except Exception:
            logger.warning("workspace note append failed for thread=%s", thread_id, exc_info=True)

    async def _deliver_workspace_notes(
        self,
        tenant_id: str,
        thread_id: str | None,
        messages: list[ChatMessage],
        *,
        emit_events: bool,
    ) -> AsyncIterator[Event]:
        """Put the operator's queued workspace edits in front of the next model call.

        Called before every model call, so a note sent mid-run is read on the run's next step
        rather than on its next run. Unlike a steer it cancels nothing: a batch already running
        finishes, and the note is read alongside its results. Logged as it is delivered, so the
        next run's history carries it once; drained notes leave the queue, so it is never
        delivered twice. Not added to the run's `produced` messages: it is not a turn anybody
        sent, and `done` lists those.
        """
        if not thread_id or self.session_store is None:
            return
        notes = await drain_workspace_notes(tenant_id, thread_id)
        if not notes:
            return
        await self._record_workspace_notes(thread_id, notes)
        for note in notes:
            messages.append(ChatMessage(role="user", content=note.text()))
            if emit_events:
                yield Event(event="workspace_note", data=note.event_data())

    async def invoke(self, input: InvokeInput) -> InvokeOutput:
        """Run a turn to completion and return the result.

        Drains the shared loop and picks out its terminal value; the display events it
        also yields are discarded here.
        """
        out: InvokeOutput | None = None
        async for item in self._run(input, emit_events=False):
            if isinstance(item, InvokeOutput):
                out = item
        if out is not None:
            return out
        return InvokeOutput(
            messages=list(input.messages),
            final=ChatMessage(role="assistant", content=""),
        )

    async def stream_events(self, input: InvokeInput) -> AsyncIterator[Event]:
        """Run a turn, emitting display events as they happen."""
        # Closed with this generator, not left to the garbage collector: a consumer that stops
        # early would otherwise finalise the run later, outside its request, where its
        # `final_response` row cannot be written.
        async with aclosing(self._run(input, emit_events=True)) as run:
            async for item in run:
                if isinstance(item, Event):
                    yield item

    async def _run(self, input: InvokeInput, *, emit_events: bool) -> AsyncIterator[Event | InvokeOutput]:
        """The turn loop. Yields display events, then exactly one `InvokeOutput`.

        `invoke` and `stream_events` were near-copies of each other — the same model
        resolution, message assembly, session render, handoff, history filter, turn loop,
        tool batching, steering and follow-ups, with one of them interleaving events. Every
        fix in the recent audit had to be written twice, and the copies had already drifted:
        streaming emitted no `user_input` or `final_response` audit record at all, and an
        abort was recorded to the session on one path but not the other.

        `emit_events` decides whether display events are produced and whether the model is
        called through the streaming path. It does not gate any state change: everything
        that touches the session, the audit log or the budget happens either way.
        """
        tenant_id = input.tenant_id or "default"
        # Before anything is written: a refused prompt must leave the thread as it was.
        hook_guidance = await self._entry_hooks(input)
        # First: `_persist_model_change` below is already an append.
        await self._sync_leaf(input.thread_id)
        if not getattr(input, "thinking_level", None) and input.thread_id:
            level = await self._load_thinking_level(input.thread_id)
            if level:
                input.thinking_level = level  # type: ignore[attr-defined]
        model = self._resolve_model(input)
        await self._persist_model_change(input)
        if input.thread_id:
            await ensure_run_queue(tenant_id, input.thread_id)
            await clear_abort(tenant_id, input.thread_id)
            if emit_events:
                yield Event(event="session_progress", data={"phase": "turn"})

        messages = await self._assemble_messages(input, model, tenant_id)
        # Per-request guidance, attached at the tail of the run's first model call (and its
        # overflow retry) and never added to `messages` — which is what gets persisted and what
        # the cache prefix is.
        transient = [*await self._transient_guidance(messages, tenant_id), *hook_guidance]

        interrupted = _interrupted_tool_results(messages, self._tool_map)
        if interrupted:
            logger.info(
                "closing %d interrupted tool call(s) before resuming (manifest=%s thread=%s)",
                len(interrupted),
                self.manifest_id,
                input.thread_id,
            )
            record_counter(
                "felix_interrupted_tool_calls",
                {"manifest_id": self.manifest_id},
            )
            messages.extend(interrupted)
            await self._append_produced(input.thread_id, interrupted)
            await self._close_interrupted_gates(input.thread_id, tenant_id, interrupted)

        produced: list[ChatMessage] = list(input.messages)
        await self._append_produced(input.thread_id, [m for m in input.messages if m.role == "user"])
        await self._note_preview(input)
        if input.thread_id:
            # A steer sent while the thread was idle is held for the next run and delivered
            # here, after that run's own turn. The loop below only drains steers between tool
            # rounds, so a run that called no tool never read one, and the queue was released
            # with it still inside — accepted, counted on the snapshot, and gone. All of them,
            # whatever `steering_mode` says: they have waited for a run, not for a tool round.
            for steermsg in await drain_steer(tenant_id, input.thread_id, mode="all"):
                steer_chat = ChatMessage(role="user", content=steermsg.text)
                messages.append(steer_chat)
                produced.append(steer_chat)
                if emit_events:
                    yield Event(event="steer", data={"content": steermsg.text})
                await self._append_produced(input.thread_id, [steer_chat])
            # A steer also raises "cancel the remaining tools", meant for a round in flight. One
            # held since the thread was idle has nothing to cancel — left set, it would cancel
            # this run's first tool round instead.
            await clear_cancel_flag(tenant_id, input.thread_id)
        final = ChatMessage(role="assistant", content="")
        fatal: FatalCall | None = None
        any_denied = False
        # Every refusal in the run, across rounds. `any_denied` is the *last* batch's: #311 made
        # `final_response.status` say whether the run ended on a refusal, so a run that recovered
        # after one still reads `ok`. The count is what tells that run apart from one with none.
        denied_calls = 0
        last_stop: StopReason = "end_turn"
        # The newest model call's usage block, for `done`. The last call's prompt is the
        # whole branch as the model saw it, so this is also how full the context is.
        last_usage: dict[str, Any] | None = None
        opts = self._chat_options(input)
        # The tool list is chosen once, on the run's first model call, and held. Re-ranking it
        # per step changed the set as the conversation grew, and any change to the tool list
        # — the front of the provider's cache prefix — re-billed the whole conversation at
        # full input price on that step. A tool used in an earlier run of the thread stays
        # in the selection (`used_tool_names`), so freezing costs only a tool that becomes
        # relevant mid-run, which the next run picks up.
        run_tools: list[Tool] | None = None

        async def _tools_for_run() -> list[Tool]:
            nonlocal run_tools
            if run_tools is None:
                run_tools = await self._active_tools(messages)
            return run_tools

        user_preview = next(
            (m.content for m in input.messages if m.role == "user" and m.content),
            "",
        )
        emit_agent_audit(
            "user_input",
            status="ok",
            manifest_id=self.manifest_id,
            payload={
                "user_input": (user_preview or "")[:2000],
                "thread_id": input.thread_id,
            },
        )

        # A run that can deliver workspace notes says so, so `/chat/workspace/edited` queues for
        # it rather than writing straight to the log. Marked here, immediately before the `try`
        # whose `finally` unmarks it.
        notes_thread = input.thread_id if input.thread_id and self.session_store is not None else None
        if notes_thread:
            await mark_run_active(tenant_id, notes_thread)
        try:
            stop_continuations = 0
            for _step in range(self.recursion_limit):
                if input.thread_id and await is_aborted(tenant_id, input.thread_id):
                    await self._append_produced(
                        input.thread_id, [final] if final.content else [], status="aborted"
                    )
                    if emit_events:
                        yield Event(event="aborted", data={"thread_id": input.thread_id})
                    break

                # Budgets are checked per turn as well as per tool call: a run with no
                # tool calls could otherwise burn wall clock and tokens unbounded.
                if self._over_budget():
                    break

                before_len = len(messages)
                messages = await self._maybe_compact_after_turn(
                    messages, thread_id=input.thread_id, model=model
                )
                if emit_events and len(messages) != before_len:
                    yield Event(
                        event="session_progress",
                        data={"phase": "compaction", "reason": "after_turn"},
                    )

                async for ev in self._deliver_workspace_notes(
                    tenant_id, input.thread_id, messages, emit_events=emit_events
                ):
                    yield ev

                injected = await run_before_turn(
                    messages,
                    context={"manifest_id": self.manifest_id, "thread_id": input.thread_id},
                )
                if injected:
                    messages.extend(injected)

                active_tools = await _tools_for_run()
                chunks: list[str] = []
                result: ModelChatResult | None = None

                # Retry once, and only while nothing has shipped: a client that has
                # already rendered deltas cannot un-render them, so a mid-stream
                # compaction would splice two different answers together.
                for attempt in (0, 1):
                    chunks = []
                    emitted = False
                    outgoing = await self._before_model(
                        [*messages, *transient], active_tools, model, input.thread_id
                    )
                    try:
                        if emit_events:
                            async for item in self._stream_one_turn(
                                model, outgoing, active_tools, input.thread_id, tenant_id, opts
                            ):
                                if isinstance(item, ModelChatResult):
                                    result = item
                                    continue
                                emitted = True
                                if item.event == "text_delta":
                                    chunks.append(str(item.data.get("delta") or ""))
                                yield item
                        else:
                            # No display to feed, so ask for the turn directly rather
                            # than streaming deltas nobody will read.
                            result = await model.chat(outgoing, active_tools, opts)
                    except ModelGatewayError as exc:
                        if attempt or emitted or not is_context_overflow(exc):
                            raise
                        rebuilt = await self._recover_from_overflow(input.thread_id, model, reason="overflow")
                        if rebuilt is None:
                            raise
                        messages = rebuilt
                        active_tools = await _tools_for_run()
                        continue
                    if (
                        attempt == 0
                        and not emitted
                        and result is not None
                        and self._overflowed(result, model)
                    ):
                        rebuilt = await self._recover_from_overflow(
                            input.thread_id, model, reason="overflow_silent"
                        )
                        if rebuilt is not None:
                            messages = rebuilt
                            active_tools = await _tools_for_run()
                            result = None
                            continue
                    break

                if result is None:
                    result = await model.chat(outgoing, active_tools, opts)

                usage_block = record_model_usage(result, model, manifest_id=self.manifest_id) or None
                last_usage = usage_block or last_usage
                # Guidance for the request, read once: repeating "load the skill" after the model
                # has acted on it invites a second activation, and trailing user text after every
                # tool result ends the assistant turn, which drops the reasoning chain when
                # thinking is on. Cache is unaffected either way — it always sat past the breakpoint.
                transient = []
                assistant = result.message
                if not assistant.content and chunks:
                    assistant = ChatMessage(
                        role="assistant",
                        content="".join(chunks),
                        tool_calls=assistant.tool_calls,
                    )
                assistant = await self._after_model(assistant, result, model, input.thread_id)
                messages.append(assistant)
                produced.append(assistant)
                final = assistant

                # A response truncated at max_tokens or declined by a safety classifier is
                # not a completed turn, and recording either as one hides a partial or
                # absent answer behind a successful-looking run.
                stop_reason = getattr(result, "stop_reason", "end_turn")
                if stop_reason == "tool_use" and not assistant.tool_calls:
                    # An after_model hook took the calls out, so this is the final answer; left
                    # as `tool_use`, the OpenAI wire reports `finish_reason: tool_calls` with none.
                    stop_reason = "end_turn"
                last_stop = stop_reason or "end_turn"

                if assistant.tool_calls and stop_reason == "max_tokens":
                    # Tool calls on an unfinished message may carry arguments that were
                    # cut off mid-write but still parse. Fail the batch, never run it.
                    tool_msgs = _quarantine_truncated_tool_calls(assistant)
                    for tool_msg in tool_msgs:
                        messages.append(tool_msg)
                        produced.append(tool_msg)
                    await self._append_produced(
                        input.thread_id,
                        [assistant, *tool_msgs],
                        usage=usage_block,
                        status=self._note_stop_reason(stop_reason, input.thread_id),
                    )
                    break

                if not assistant.tool_calls:
                    await self._append_produced(
                        input.thread_id,
                        [assistant],
                        usage=usage_block,
                        status=self._note_stop_reason(stop_reason, input.thread_id),
                    )
                    keep_going = await self._stop_hook(assistant, stop_continuations, _step)
                    if keep_going is not None:
                        stop_continuations += 1
                        # Transient: attached to the next model call only, not stored or replayed.
                        transient = [keep_going]
                        continue
                    delivered: list[ChatMessage] = []
                    async for ev in self._deliver_follow_ups(
                        tenant_id,
                        input.thread_id,
                        messages,
                        produced,
                        delivered,
                        _step,
                        emit_events=emit_events,
                    ):
                        yield ev
                    if delivered:
                        continue
                    break

                for call in assistant.tool_calls:
                    if not call.id:
                        call.id = f"call_{uuid.uuid4().hex[:12]}"
                    if emit_events:
                        yield Event(
                            event="tool_start",
                            data={"name": call.name, "input": call.args, "id": call.id},
                        )
                        yield Event(
                            event="tool_execution_update",
                            data={"name": call.name, "id": call.id, "status": "running"},
                        )

                # Written ahead of the batch, not with its results. A run that dies mid-batch
                # -- a worker restart, a lost fiber lease, a cancel -- left no trace of calls
                # that may already have taken effect: the message was appended only once the
                # whole batch returned. A re-run then re-asked the model from the user's turn,
                # and it issued the same writes again (felix-run/felix#531). Logged first, an
                # unfinished call is in the history, and `_interrupted_tool_results` closes it
                # on the next run as "may have already taken effect" rather than letting it be
                # issued blind. The results follow below.
                await self._append_produced(input.thread_id, [assistant], usage=usage_block)

                # Side events are drained *while* the batch runs, not after it. A gated
                # tool blocks inside `run_batch` in `wait_for_decision` for up to its
                # rule's TTL, and the `approval_required` frame that asks for the
                # decision is queued at block time -- so draining afterwards flushed it
                # in the same beat as `tool_end`, after the answer had already been
                # given. Measured: a stream blocked on a gated call for 75s carried no
                # `approval_required` at all, and the client showed a tool card sitting
                # at `running` with nothing to say why. The same applies to
                # `tool_request` and `ui_request`, which block the same way.
                batch = asyncio.create_task(
                    self._tools.run_batch(
                        list(assistant.tool_calls),
                        thread_id=input.thread_id,
                        tenant_id=tenant_id,
                    )
                )
                try:
                    if emit_events:
                        while True:
                            for side in await drain_side_events(input.thread_id):
                                yield Event(event=str(side["event"]), data=dict(side["data"]))
                            if batch.done():
                                break
                            await asyncio.wait({batch}, timeout=SIDE_EVENT_POLL_SECONDS)
                    tool_msgs, batch_fatal, all_terminate, batch_denied = await batch
                except BaseException:
                    # The batch no longer inherits cancellation from this frame, so a
                    # client that hangs up mid-tool would otherwise leave it running.
                    batch.cancel()
                    raise
                for tool_msg in tool_msgs:
                    if emit_events:
                        yield Event(event="tool_end", data=_tool_end_data(tool_msg))
                        yield Event(
                            event="tool_execution_update",
                            data={
                                "name": tool_msg.name,
                                "id": tool_msg.tool_call_id,
                                "status": "complete",
                            },
                        )
                    messages.append(tool_msg)
                    produced.append(tool_msg)

                await self._append_produced(input.thread_id, tool_msgs)
                any_denied = bool(batch_denied)
                denied_calls += batch_denied
                if batch_fatal is not None:
                    # A fatal tool error ends the run. Follow-ups are not drained: the
                    # run did not reach a state a follow-up could sensibly continue from.
                    fatal = batch_fatal
                    break
                if all_terminate:
                    delivered = []
                    async for ev in self._deliver_follow_ups(
                        tenant_id,
                        input.thread_id,
                        messages,
                        produced,
                        delivered,
                        _step,
                        emit_events=emit_events,
                    ):
                        yield ev
                    if delivered:
                        continue
                    break
                if input.thread_id and await is_aborted(tenant_id, input.thread_id):
                    if emit_events:
                        yield Event(event="aborted", data={"thread_id": input.thread_id})
                    break
                if input.thread_id:
                    await clear_cancel_flag(tenant_id, input.thread_id)
                    for steermsg in await drain_steer(
                        tenant_id,
                        input.thread_id,
                        mode=self.steering_mode,  # type: ignore[arg-type]
                    ):
                        steer_chat = ChatMessage(role="user", content=steermsg.text)
                        messages.append(steer_chat)
                        produced.append(steer_chat)
                        if emit_events:
                            yield Event(event="steer", data={"content": steermsg.text})
                        await self._append_produced(input.thread_id, [steer_chat])
            else:
                # `range(recursion_limit)` ran out with the model still asking for tools: the
                # last assistant message carries calls nothing executed. Until this branch the
                # run reported the model's own `tool_use` and a session status of complete —
                # the first live triage run stopped mid-sentence at step ten and nothing said
                # so. `_note_stop_reason` writes the warning and the counter; the status is
                # `truncated`, the same reading `max_tokens` gets.
                last_stop = "max_turns"
                await self._append_produced(
                    input.thread_id, [], status=self._note_stop_reason("max_turns", input.thread_id)
                )
                if emit_events:
                    yield Event(event="max_turns", data={"limit": self.recursion_limit})

        except BaseException as exc:
            # Written before the queue is released below, which awaits and so could itself be
            # cancelled. Every run writes exactly one `final_response`, a dead one included.
            self._audit_final_response(
                input.thread_id,
                final,
                fatal=fatal,
                ended_denied=any_denied,
                denied_calls=denied_calls,
                died=exc,
            )
            raise
        else:
            # Here rather than after the `finally`, for the same reason: a cancel landing in its
            # awaits would otherwise skip the row of a run that had finished.
            self._audit_final_response(
                input.thread_id, final, fatal=fatal, ended_denied=any_denied, denied_calls=denied_calls
            )
        finally:
            if notes_thread:
                # Unmarked first, then flushed: a note that saw the mark and was queued after the
                # run's last model call reaches the log here rather than waiting for a next run.
                # One queued after this flush is delivered at the thread's next run.
                await mark_run_idle(tenant_id, notes_thread)
                await self._record_workspace_notes(
                    notes_thread, await drain_workspace_notes(tenant_id, notes_thread)
                )
            if input.thread_id:
                await release_run_queue(tenant_id, input.thread_id)
                await release_side_events(input.thread_id)
        await self._maybe_capture_memory(input, final, model)

        output = InvokeOutput(messages=produced, final=final, stop_reason=last_stop)
        if emit_events:
            yield Event(event="on_chain_end", data={"output": output})
            yield Event(
                event="done",
                data={
                    "final": final.model_dump(),
                    "messages": [m.model_dump() for m in produced],
                    "stop_reason": last_stop,
                    # Same block the session log stores on the final message: `input` is
                    # the uncached part of the prompt, so the prompt is `input + cacheRead +
                    # cacheWrite`, and `totalTokens` adds the reply. Absent when the
                    # provider reported nothing.
                    **({"usage": last_usage} if last_usage else {}),
                },
            )
        yield output


def build_react_agent(ctx: PatternBuildContext) -> Agent:
    recursion = ctx.get("recursion_limit")
    limit = _clamp(
        int(recursion if recursion is not None else DEFAULT_RECURSION),
        ABSOLUTE_LIMITS["recursion_limit"],
    )
    execution = ctx.get("execution")
    session_spec = ctx.get("session_spec")
    tool_exec = "sequential"
    if execution is not None:
        tool_exec = str(getattr(execution, "tools", None) or "sequential")
    steer_mode = "all"
    follow_mode = "all"
    compact_after = False
    if session_spec is not None:
        steer_mode = str(getattr(session_spec, "steering_mode", None) or "all")
        follow_mode = str(getattr(session_spec, "follow_up_mode", None) or "all")
        compact_after = bool(getattr(session_spec, "compact_after_turn", False))
    return _ReactAgent(
        tools=list(ctx.get("tools") or []),
        pattern="react",
        manifest_id=str(ctx.get("manifest_id") or ""),
        manifest_version=str(ctx.get("manifest_version") or "1.0.0"),
        system_prompt=str(ctx.get("system_prompt") or ""),
        model_spec=ctx.get("model_spec"),
        settings=ctx.get("settings"),
        recursion_limit=limit,
        limits=ctx.get("limits"),
        context_prelude=str(ctx.get("context_prelude") or ""),
        session_store=ctx.get("session_store"),
        session_strategy=ctx.get("session_strategy"),
        tenant_id=str(ctx.get("tenant_id") or "default"),
        memory_capture=ctx.get("memory_capture"),
        tools_retrieval=ctx.get("tools_retrieval"),
        decider=ctx.get("decider"),
        skill_suggester=ctx.get("skill_suggester"),
        procedural_memory=ctx.get("procedural_memory"),
        reply_screen=ctx.get("reply_screen"),
        manifest_hooks=ctx.get("hooks"),
        tool_execution=tool_exec,
        steering_mode=steer_mode,
        follow_up_mode=follow_mode,
        compact_after_turn=compact_after,
        output_schema=ctx.get("output_schema"),
    )


async def _build_react(ctx: PatternBuildContext) -> Agent:
    return build_react_agent(ctx)


register_pattern("react", _build_react, kind="single-agent", honours_output_schema=True, honours_hooks=True)

__all__ = ["build_react_agent"]
