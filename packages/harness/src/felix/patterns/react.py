"""react pattern — canonical tool-calling loop."""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from felix.audit.emit import emit_agent_audit
from felix.config import get_settings
from felix.hooks import run_before_turn, run_filter_history
from felix.manifests.schema import ABSOLUTE_LIMITS, ModelSpec
from felix.observability.metrics import record_counter
from felix.patterns.model import (
    ModelChatResult,
    ModelGatewayError,
    build_model,
    record_usage,
    supports_stream_turn,
    wire_model_id,
)
from felix.patterns.overflow import is_context_overflow, is_silent_overflow
from felix.patterns.registry import PatternBuildContext, register_pattern
from felix.patterns.tool_runner import ToolRunner
from felix.patterns.types import (
    Agent,
    ChatMessage,
    Event,
    InvokeInput,
    InvokeOutput,
)
from felix.side_events import drain as drain_side_events
from felix.side_events import release as release_side_events
from felix.steer import (
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

logger = logging.getLogger("felix.patterns.react")

DEFAULT_RECURSION = 10


def _status_for_stop(stop: str) -> str:
    """Session status for a terminal stop reason.

    `max_tokens` means the answer is cut off mid-thought; `refusal` means a safety
    classifier declined. Neither is a completed turn, and recording either as one hides
    a partial or absent answer behind a successful-looking run.
    """
    if stop == "max_tokens":
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


@dataclass
class _ReactAgent:
    tools: list[Tool]
    pattern: str
    manifest_id: str
    manifest_version: str
    system_prompt: str
    model_spec: Any
    settings: Any
    recursion_limit: int
    limits: Any = None
    context_prelude: str = ""
    session_store: Any | None = None
    session_strategy: Any | None = None
    tenant_id: str = "default"
    memory_capture: Any | None = None
    tools_retrieval: Any | None = None
    procedural_memory: Any | None = None
    tool_execution: str = "sequential"
    steering_mode: str = "all"
    follow_up_mode: str = "all"
    compact_after_turn: bool = False
    _tool_map: dict[str, Tool] = field(init=False, repr=False)
    _last_model_id: str | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self._tool_map = {t.name: t for t in self.tools}
        self._tools = ToolRunner(
            tool_map=self._tool_map,
            manifest_id=self.manifest_id,
            tool_execution=self.tool_execution,
        )

    async def _active_tools(self, messages: list[ChatMessage]) -> list[Tool]:
        """The tools this step exposes, narrowed by `spec.tools_retrieval`.

        Async because selection encodes the query and every candidate description
        once a retrieval model is configured, and that must not run on the event
        loop. With retrieval off — the default — it stays inline.
        """
        return await select_tools_from_ctx_async(self.tools, messages, self.tools_retrieval)

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
        return build_model(settings, spec)

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
        model: Any,
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
            logger.debug("session append failed", exc_info=True)

    async def _stream_one_turn(
        self,
        model: Any,
        messages: list[ChatMessage],
        active_tools: list[Tool],
        thread_id: str | None,
        tenant_id: str,
    ) -> AsyncIterator[Event | ModelChatResult]:
        """Run one streamed turn, yielding display events and then the result.

        Extracted so the caller can retry the whole turn after compacting, which it can
        only do while nothing has been emitted.
        """
        if supports_stream_turn(model):
            stream_turn = model.stream_turn
            # One request for the whole turn. See `stream_turn` for why the
            # stream-then-chat pair it replaces was worse than it looked.
            async for item in stream_turn(messages, active_tools):
                if isinstance(item, ModelChatResult):
                    yield item
                    continue
                if thread_id and await is_aborted(tenant_id, thread_id):
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
        async for delta in model.stream(messages, active_tools):
            if thread_id and await is_aborted(tenant_id, thread_id):
                return
            yield Event(event="text_delta", data={"chunk": {"content": delta}, "delta": delta})
            yield Event(
                event="session_progress",
                data={"progress": {"type": "assistant_delta", "kind": "text", "delta": delta}},
            )

    async def _recover_from_overflow(
        self,
        thread_id: str | None,
        model: Any,
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

    def _overflowed(self, result: ModelChatResult, model: Any) -> bool:
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

    def _capture_model(self, turn_model: Any) -> Any:
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

    async def _maybe_capture_memory(self, input: InvokeInput, final: ChatMessage, model: Any) -> None:
        capture = self.memory_capture
        if capture is None or not getattr(capture, "enabled", False):
            return
        if self.settings is None:
            return
        user_text = " ".join(m.content for m in input.messages if m.role == "user")
        try:
            from felix.memory.capture import capture_from_turn

            await capture_from_turn(
                self.settings,
                input.tenant_id or self.tenant_id,
                manifest_id=self.manifest_id,
                user_text=user_text,
                assistant_text=final.content or "",
                capture=capture,
                model=self._capture_model(model),
                origin_seq=await self._turn_seq(input.thread_id),
                thread_id=input.thread_id or "",
            )
        except Exception:
            logger.debug("memory capture failed", exc_info=True)

    async def _inject_procedures(self, messages: list[ChatMessage], tenant_id: str) -> list[ChatMessage]:
        spec = self.procedural_memory
        if spec is None or not getattr(spec, "enabled", False) or self.settings is None:
            return messages
        if any(m.role == "system" and (m.content or "").startswith("[known procedures]") for m in messages):
            return messages
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
            return messages
        if block:
            messages.append(ChatMessage(role="system", content=block))
        return messages

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

        Position within ``messages`` is cache-neutral: the ephemeral breakpoints sit on
        the tool list and the system block, never on a message. So the prelude sits next
        to the system prompt, where it reads as framing, rather than at the tail, where
        it would read as the user's latest turn.
        """
        prelude = self._prelude_messages()
        if not prelude:
            return messages
        head = 0
        while head < len(messages) and messages[head].role == "system":
            head += 1
        return [*messages[:head], *prelude, *messages[head:]]

    async def _assemble_messages(self, input: InvokeInput, model: Any, tenant_id: str) -> list[ChatMessage]:
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
        return await self._inject_procedures(messages, tenant_id)

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
        async for item in self._run(input, emit_events=True):
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
        if not getattr(input, "thinking_level", None) and input.thread_id:
            level = await self._load_thinking_level(input.thread_id)
            if level:
                input.thinking_level = level  # type: ignore[attr-defined]
        model = self._resolve_model(input)
        await self._persist_model_change(input)
        tenant_id = input.tenant_id or "default"
        if input.thread_id:
            await ensure_run_queue(tenant_id, input.thread_id)
            await clear_abort(tenant_id, input.thread_id)
            if emit_events:
                yield Event(event="session_progress", data={"phase": "turn"})

        messages = await self._assemble_messages(input, model, tenant_id)

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

        produced: list[ChatMessage] = list(input.messages)
        await self._append_produced(input.thread_id, [m for m in input.messages if m.role == "user"])
        final = ChatMessage(role="assistant", content="")
        fatal = False

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

        try:
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

                injected = await run_before_turn(
                    messages,
                    context={"manifest_id": self.manifest_id, "thread_id": input.thread_id},
                )
                if injected:
                    messages.extend(injected)

                active_tools = await self._active_tools(messages)
                chunks: list[str] = []
                result: ModelChatResult | None = None

                # Retry once, and only while nothing has shipped: a client that has
                # already rendered deltas cannot un-render them, so a mid-stream
                # compaction would splice two different answers together.
                for attempt in (0, 1):
                    chunks = []
                    emitted = False
                    try:
                        if emit_events:
                            async for item in self._stream_one_turn(
                                model, messages, active_tools, input.thread_id, tenant_id
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
                            result = await model.chat(messages, active_tools)
                    except ModelGatewayError as exc:
                        if attempt or emitted or not is_context_overflow(exc):
                            raise
                        rebuilt = await self._recover_from_overflow(input.thread_id, model, reason="overflow")
                        if rebuilt is None:
                            raise
                        messages = rebuilt
                        active_tools = await self._active_tools(messages)
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
                            active_tools = await self._active_tools(messages)
                            result = None
                            continue
                    break

                if result is None:
                    result = await model.chat(messages, active_tools)

                record_usage(
                    result,
                    manifest_id=self.manifest_id,
                    model_id=model.model_id,
                    wire_model_id=wire_model_id(model),
                )
                usage_block = None
                if result.usage:
                    from felix.usage.pricing import usage_with_cost

                    usage_block = usage_with_cost(result.usage, model_id=model.model_id or "")
                assistant = result.message
                if not assistant.content and chunks:
                    assistant = ChatMessage(
                        role="assistant",
                        content="".join(chunks),
                        tool_calls=assistant.tool_calls,
                    )
                messages.append(assistant)
                produced.append(assistant)
                final = assistant

                # A response truncated at max_tokens or declined by a safety classifier is
                # not a completed turn, and recording either as one hides a partial or
                # absent answer behind a successful-looking run.
                stop_reason = getattr(result, "stop_reason", "end_turn")

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

                tool_msgs, had_fatal, all_terminate = await self._tools.run_batch(
                    list(assistant.tool_calls),
                    thread_id=input.thread_id,
                    tenant_id=tenant_id,
                )
                if emit_events:
                    for side in await drain_side_events(input.thread_id):
                        yield Event(event=str(side["event"]), data=dict(side["data"]))
                for tool_msg in tool_msgs:
                    if emit_events:
                        yield Event(
                            event="tool_end",
                            data={
                                "name": tool_msg.name,
                                "output": tool_msg.content,
                                "id": tool_msg.tool_call_id,
                            },
                        )
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

                await self._append_produced(input.thread_id, [assistant, *tool_msgs], usage=usage_block)
                if had_fatal:
                    # A fatal tool error ends the run. Follow-ups are not drained: the
                    # run did not reach a state a follow-up could sensibly continue from.
                    fatal = True
                    break
                if all_terminate:
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

            if not fatal and input.thread_id and not await is_aborted(tenant_id, input.thread_id):
                for follow in await drain_follow_up(
                    tenant_id,
                    input.thread_id,
                    mode=self.follow_up_mode,  # type: ignore[arg-type]
                ):
                    follow_chat = ChatMessage(role="user", content=follow.text)
                    produced.append(follow_chat)
                    if emit_events:
                        yield Event(event="follow_up", data={"content": follow.text})
                    await self._append_produced(input.thread_id, [follow_chat])
                    messages.append(follow_chat)
                    result = await model.chat(messages, await self._active_tools(messages))
                    record_usage(
                        result,
                        manifest_id=self.manifest_id,
                        model_id=model.model_id,
                        wire_model_id=wire_model_id(model),
                    )
                    assistant = result.message
                    messages.append(assistant)
                    produced.append(assistant)
                    final = assistant
                    if emit_events and assistant.content:
                        yield Event(
                            event="text_delta",
                            data={
                                "chunk": {"content": assistant.content},
                                "delta": assistant.content,
                            },
                        )
                    await self._append_produced(input.thread_id, [assistant])
        finally:
            if input.thread_id:
                await release_run_queue(tenant_id, input.thread_id)
                await release_side_events(input.thread_id)

        emit_agent_audit(
            "final_response",
            status="error" if fatal else "ok",
            manifest_id=self.manifest_id,
            payload={
                "thread_id": input.thread_id,
                "chars": len(final.content or ""),
            },
        )
        await self._maybe_capture_memory(input, final, model)

        output = InvokeOutput(messages=produced, final=final)
        if emit_events:
            yield Event(event="on_chain_end", data={"output": output})
            yield Event(
                event="done",
                data={
                    "final": final.model_dump(),
                    "messages": [m.model_dump() for m in produced],
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
        procedural_memory=ctx.get("procedural_memory"),
        tool_execution=tool_exec,
        steering_mode=steer_mode,
        follow_up_mode=follow_mode,
        compact_after_turn=compact_after,
    )


async def _build_react(ctx: PatternBuildContext) -> Agent:
    return build_react_agent(ctx)


register_pattern("react", _build_react, kind="single-agent")

__all__ = ["build_react_agent"]
