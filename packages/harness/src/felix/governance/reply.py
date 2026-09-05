"""The reply-path controls: final-response judges and PII guardrails on the agent's reply.

The tool wrappers in `manifests/builder.py` govern what tools return. Nothing governed
what the *agent* returned: `wrap_final_response_judges` passed events straight through on
`stream_events`, so the only outbound model-call control was inert on the primary chat
surface, and `guardrails.targets: [input, output]` scrubbed user input and tool output
and let the reply through untouched.

`ReplyControlsAgent` wraps the agent. On `invoke` the reply is screened before it is
returned. On a stream every frame carrying reply text is held until the run ends, and the
reply is released screened — a control that lets every token through and then objects
has controlled nothing — while structural frames (tool calls, approvals, run phases)
stream as they happen. Every assistant message in the output is screened, not only the
final one: a model that emits a preamble before its tool calls has already said it.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

from felix.manifests.schema import Guardrails, JudgeRule
from felix.patterns.types import Agent, ChatMessage, Event, InvokeInput, InvokeOutput, copy_agent_surface

logger = logging.getLogger("felix.governance.reply")

PII_BLOCKED_REPLY = "[guardrails] PII blocked in reply"
JUDGE_DENIED_PREFIX = "[judge denied]"

# Frames that carry the model's reply text. `thinking_delta` is reasoning, not the reply,
# and passes through; so does a `session_progress` frame carrying thinking.
REPLY_TEXT_EVENTS = frozenset({"text_delta", "on_chat_model_stream"})
_TERMINAL_EVENTS = frozenset({"on_chain_end", "done"})


def reply_pii_enabled(guardrails: Guardrails | None) -> bool:
    """Whether `guardrails.providers: [pii]` reaches the agent's own reply.

    `output` is everything leaving the model boundary — tool output *and* the reply;
    `final_response` is the reply alone. Either enables this.
    """
    if guardrails is None or "pii" not in guardrails.providers:
        return False
    targets = set(guardrails.targets)
    return not targets or bool(targets & {"output", "final_response"})


def final_response_judges(guardrails: Guardrails | None) -> list[JudgeRule]:
    return [j for j in (guardrails.judges if guardrails else []) if j.final_response]


def reply_controls_enabled(guardrails: Guardrails | None) -> bool:
    return reply_pii_enabled(guardrails) or bool(final_response_judges(guardrails))


def carries_reply_text(item: Any) -> bool:
    """Whether a stream item is one of the frames the model's reply text rides on.

    The react loop emits each text delta twice: as `text_delta` and again inside a
    `session_progress` envelope (`progress.type == "assistant_delta"`). Holding one and
    passing the other would stream the raw reply at token rate anyway.
    """
    if not isinstance(item, Event):
        return False
    if item.event in REPLY_TEXT_EVENTS:
        return True
    if item.event == "session_progress":
        progress = item.data.get("progress")
        if isinstance(progress, dict) and progress.get("type") == "assistant_delta":
            return progress.get("kind", "text") == "text"
    return False


def _text_event(content: str) -> Event:
    return Event(event="text_delta", data={"chunk": {"content": content}, "delta": content})


def _output_from_terminal(item: Any) -> InvokeOutput | None:
    """The run's output, from whichever terminal item arrived first."""
    if isinstance(item, InvokeOutput):
        return item
    if not isinstance(item, Event):
        return None
    if item.event == "on_chain_end":
        output = item.data.get("output")
        return output if isinstance(output, InvokeOutput) else None
    if item.event == "done":
        final = item.data.get("final")
        if not isinstance(final, dict):
            return None
        messages = [
            ChatMessage.model_validate(m) for m in item.data.get("messages") or [] if isinstance(m, dict)
        ]
        return InvokeOutput(
            messages=messages,
            final=ChatMessage.model_validate(final),
            stop_reason=item.data.get("stop_reason") or "end_turn",
        )
    return None


class ReplyControlsAgent:
    """An `Agent` whose reply has been through the reply-path controls."""

    def __init__(self, inner: Agent, guardrails: Guardrails, manifest_id: str) -> None:
        self._inner = inner
        self._manifest_id = manifest_id
        self._pii = reply_pii_enabled(guardrails)
        self._block_pii = bool(guardrails.block_on_match)
        self._judges = final_response_judges(guardrails)
        copy_agent_surface(self, inner, manifest_id=manifest_id)

    # --- screening ---------------------------------------------------------------------

    def _audit(self, event_type: str, status: str, payload: dict[str, Any]) -> None:
        from felix.audit.emit import emit_agent_audit

        emit_agent_audit(event_type, status=status, payload=payload, manifest_id=self._manifest_id)

    def _screen_pii(self, text: str) -> str:
        """The text with PII redacted, or the block notice when the manifest blocks."""
        if not self._pii or not text:
            return text
        from felix.governance.pii import redact_pii

        result = redact_pii(text)
        if not result.matched:
            return text
        self._audit(
            "guardrails_reply", "blocked" if self._block_pii else "redacted", {"engine": result.engine}
        )
        return PII_BLOCKED_REPLY if self._block_pii else result.text

    async def _judge(self, text: str) -> str | None:
        """A denial notice when a final-response judge scores the reply under threshold."""
        if not self._judges:
            return None
        from felix.config import get_settings
        from felix.governance.judges import judge_score

        settings = get_settings()
        for judge in self._judges:
            score = await judge_score(text, judge, settings=settings)
            threshold = float(judge.threshold or 0.7)
            if score < threshold:
                self._audit(
                    "judge_deny",
                    "denied",
                    {"judge": judge.name, "score": round(score, 4), "threshold": threshold},
                )
                return f"{JUDGE_DENIED_PREFIX} {judge.name}: score={score:.2f} < {threshold}"
        return None

    async def screen(self, out: InvokeOutput) -> tuple[InvokeOutput, bool]:
        """(screened output, whether anything changed).

        Every assistant message is redacted, by identity and by role, so a preamble the
        model emitted before its tool calls is governed like the answer. Judges score the
        final reply, after redaction — they judge what ships.
        """
        changed = False
        screened: dict[int, ChatMessage] = {}

        def screened_message(m: ChatMessage, content: str) -> ChatMessage:
            nonlocal changed
            if content == (m.content or ""):
                return m
            changed = True
            return replace(m, content=content)

        messages: list[ChatMessage] = []
        for m in out.messages:
            if m.role == "assistant" and m.content:
                m = screened.setdefault(id(m), screened_message(m, self._screen_pii(m.content)))
            messages.append(m)
        final = out.final
        stop_reason = out.stop_reason
        if final is not None:
            content = self._screen_pii(final.content or "")
            denial = await self._judge(content)
            replacement = screened_message(final, denial if denial is not None else content)
            if denial is not None or (self._block_pii and content == PII_BLOCKED_REPLY):
                # The client is not reading the model's answer. On the OpenAI wire this
                # is `content_filter`, which is also what a provider refusal maps to.
                stop_reason = "refusal"
            if replacement is not final:
                messages = [
                    replacement if m is final or m is screened.get(id(final)) else m for m in messages
                ]
            final = replacement
            if denial is not None:
                # The denial is the whole reply: a preamble the model wrote before its
                # tool calls was never judged, so it is withheld rather than shipped.
                messages = [
                    screened_message(m, "") if m.role == "assistant" and m is not final and m.content else m
                    for m in messages
                ]
        return InvokeOutput(messages=messages, final=final, stop_reason=stop_reason), changed

    # --- the Agent surface --------------------------------------------------------------

    async def invoke(self, input: InvokeInput) -> InvokeOutput:
        screened, _changed = await self.screen(await self._inner.invoke(input))
        return screened

    async def stream_events(self, input: InvokeInput) -> AsyncIterator[Any]:
        held: list[Event] = []
        decided: tuple[InvokeOutput, bool] | None = None
        released = False

        async def decide(output: InvokeOutput) -> tuple[InvokeOutput, bool]:
            nonlocal decided
            if decided is None:
                decided = await self.screen(output)
            return decided

        def release(screened: InvokeOutput, changed: bool) -> list[Event]:
            """The reply text, exactly once: the held frames when nothing changed, one
            frame per screened assistant message otherwise."""
            nonlocal released
            if released:
                return []
            released = True
            if not changed:
                return held
            texts = [m.content for m in screened.messages if m.role == "assistant" and m.content]
            if (
                screened.final is not None
                and screened.final.content
                and screened.final not in screened.messages
            ):
                texts.append(screened.final.content)
            return [_text_event(t) for t in texts]

        async for item in self._inner.stream_events(input):
            if carries_reply_text(item):
                held.append(item)
                continue
            output = _output_from_terminal(item)
            if output is None:
                yield item
                continue
            screened, changed = await decide(output)
            for ev in release(screened, changed):
                yield ev
            if isinstance(item, InvokeOutput):
                yield screened if changed else item
            elif item.event == "on_chain_end":
                yield Event(event=item.event, data={**item.data, "output": screened}) if changed else item
            else:
                data = {
                    **item.data,
                    "final": screened.final.model_dump()
                    if screened.final is not None
                    else item.data.get("final"),
                    "messages": [m.model_dump() for m in screened.messages],
                    "stop_reason": screened.stop_reason,
                }
                yield Event(event=item.event, data=data) if changed else item
        if held and not released:
            # A stream that ended without a terminal frame still owes its held text.
            text = "".join(ev.text for ev in held if ev.event in REPLY_TEXT_EVENTS) or "".join(
                str(ev.data.get("progress", {}).get("delta") or "") for ev in held
            )
            final = ChatMessage(role="assistant", content=text)
            screened, changed = await decide(InvokeOutput(messages=[final], final=final))
            for ev in release(screened, changed):
                yield ev


__all__ = [
    "JUDGE_DENIED_PREFIX",
    "PII_BLOCKED_REPLY",
    "REPLY_TEXT_EVENTS",
    "ReplyControlsAgent",
    "carries_reply_text",
    "final_response_judges",
    "reply_controls_enabled",
    "reply_pii_enabled",
]
