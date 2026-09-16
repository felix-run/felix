"""The Anthropic messages wire format.

Carries the two things the OpenAI shape has no equivalent of: explicit `cache_control`
breakpoints, and signed thinking blocks that must be replayed verbatim across a tool-call
turn or the provider rejects the whole request.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from felix_ai.catalog import clamp_effort, entry_for
from felix_ai.types import (
    ChatMessage,
    ModelChatResult,
    StopReason,
    StreamDelta,
    TokenUsage,
    ToolCall,
    ToolSchema,
)
from felix_ai.wire.base import (
    HttpModelClient,
    inline_parts,
    iter_sse_json,
    map_stop,
    parse_tool_arguments,
    split_data_url,
    tool_json_schema,
)
from felix_ai.wire.transport import ModelGatewayError, post_with_retry

logger = logging.getLogger("felix_ai.wire.anthropic_messages")

# Non-streaming requests must stay under the SDK/HTTP timeout, so this is a floor that
# leaves room for a real answer rather than the previous 4096.
_DEFAULT_MAX_TOKENS = 16_000


def _effort_from_budget(budget: int) -> str:
    """Map a legacy thinking budget onto an effort level."""
    if budget < 4_096:
        return "low"
    if budget < 16_384:
        return "medium"
    if budget < 32_768:
        return "high"
    return "xhigh"


def apply_anthropic_thinking_cache(
    body: dict[str, Any], spec: Any, model: str = "", *, isolate_cache: bool = False
) -> None:
    """Attach thinking + ephemeral cache_control in the shape this model accepts.

    The previous version emitted one shape for every Claude model:
    ``thinking: {"type": "enabled", "budget_tokens": N}`` plus ``temperature: 1``. Both
    are **removed** on the current generation and return HTTP 400, so the manifest's
    thinking levels hard-failed against Opus 5, Sonnet 5, Fable 5, and Opus 4.7/4.8.
    """
    entry = entry_for(model or str(body.get("model") or ""))
    caps = entry.quirks

    # Sampling params are rejected outright on 4.6+, so drop what the caller set rather
    # than letting the request 400 on a parameter the model no longer accepts.
    if not caps.sampling:
        body.pop("temperature", None)
        body.pop("top_p", None)
        body.pop("top_k", None)

    def _clamp_output() -> None:
        # Never ask for more output than the model will grant. Applies regardless of
        # whether a model spec was supplied.
        requested = int(body.get("max_tokens") or _DEFAULT_MAX_TOKENS)
        body["max_tokens"] = min(requested, entry.max_output_tokens)

    if spec is None:
        _clamp_output()
        return

    budget = getattr(spec, "thinking_budget", None)
    if budget:
        n = int(budget)
        if caps.adaptive_thinking:
            # Depth is expressed as effort now; the budget is only a hint about how hard
            # the operator wants the model to think.
            body["thinking"] = {"type": "adaptive"}
            if caps.effort:
                body.setdefault("output_config", {})["effort"] = clamp_effort(_effort_from_budget(n), caps)
            if caps.sampling:
                body["temperature"] = 1
        elif caps.budget_tokens:
            body["thinking"] = {"type": "enabled", "budget_tokens": n}
            # Pre-4.6 requires temperature=1 when thinking is enabled.
            body["temperature"] = 1
            current = int(body.get("max_tokens") or _DEFAULT_MAX_TOKENS)
            if current <= n:
                body["max_tokens"] = n + 1024

    _clamp_output()
    if getattr(spec, "cache", False) and not isolate_cache:
        system = body.get("system")
        if isinstance(system, str) and system:
            body["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        tools = body.get("tools")
        if isinstance(tools, list) and tools:
            last = dict(tools[-1])
            last["cache_control"] = {"type": "ephemeral"}
            tools[-1] = last


_ANTHROPIC_STOP: dict[str, StopReason] = {
    "end_turn": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "stop_sequence": "stop_sequence",
    "pause_turn": "pause_turn",
    "refusal": "refusal",
}


def _anthropic_image_block(url: str, media_type: str | None) -> dict[str, Any] | None:
    """One image block, in whichever of Anthropic's two source forms the URL calls for.

    Anthropic has no URL form for inline bytes: a `data:` URL in a `url` source is a 400, and
    that is what this wire sent for every inline image — so an image an OpenAI SDK sends the
    documented way reached `gpt-4o` and failed on `claude-sonnet`, this harness's default.
    The OpenAI wire needs no equivalent; a data URL is native there.

    `None` only for an empty URL. `inline_parts` has already converted every other data URL to
    base64, so there is no longer a well-formed image this can decline to send.
    """
    if not url:
        return None
    inline = split_data_url(url)
    if inline is not None:
        media, payload = inline
        return {"type": "image", "source": {"type": "base64", "media_type": media, "data": payload}}
    return {
        "type": "image",
        "source": {"type": "url", "url": url, "media_type": media_type or "image/png"},
    }


def _anthropic_user_or_plain(m: ChatMessage) -> dict[str, Any]:
    """Convert a non-tool message for Anthropic, including image blocks.

    Images render on a user turn only, which is the sole place either API accepts one. That
    is the half that changed on the *other* wire, which used to send an image on an assistant
    turn; here a non-user message already resolved to `m.content`, and the parser fills that
    from the text parts. The join below is for a message built by hand rather than parsed,
    where the text lives only in the blocks — `content: ""` is itself an Anthropic 400.
    """
    parts = inline_parts(m)
    images = [p for p in parts if p.type != "text" and p.url]
    if not images:
        # A plain string, which is what every text turn sends and what the provider's prompt
        # cache keys on. Normalising unconditionally turned each of those into a one-element
        # parts list — accepted by the API, and a different request body for every turn in the
        # repo, for nothing.
        return {"role": m.role, "content": m.content}
    if m.role != "user":
        text = "\n".join(p.text for p in parts if p.type == "text" and p.text)
        return {"role": m.role, "content": text or m.content}

    blocks: list[dict[str, Any]] = []
    for part in parts:
        if part.type == "text" and part.text:
            blocks.append({"type": "text", "text": part.text})
        elif part.url:
            image = _anthropic_image_block(part.url, part.media_type)
            if image is not None:
                blocks.append(image)
    return {"role": "user", "content": blocks or m.content}


def _anthropic_thinking_blocks(m: ChatMessage) -> list[dict[str, Any]]:
    """Thinking blocks to replay for an assistant turn, in the order the model emitted them.

    Extended thinking combined with tool use is stateful: the provider signs each thinking
    block, and a later turn that replays the tool call must replay the signed reasoning
    with it. Felix captured neither, so a thinking-enabled manifest lost its reasoning at
    the first tool call and every following turn was answered without it.

    A `thinking` block is only replayable with the signature that was issued for it, so an
    unsigned one is dropped rather than sent — the provider rejects the whole turn on a
    missing or unverifiable signature. `redacted_thinking` carries no readable text but
    must still be echoed back, so it travels on its opaque `data` field alone.
    """
    blocks: list[dict[str, Any]] = []
    for raw in m.thinking or []:
        if not isinstance(raw, dict):
            continue
        kind = raw.get("type")
        if kind == "thinking" and raw.get("signature"):
            blocks.append(
                {
                    "type": "thinking",
                    "thinking": str(raw.get("thinking") or ""),
                    "signature": str(raw["signature"]),
                }
            )
        elif kind == "redacted_thinking" and raw.get("data"):
            blocks.append({"type": "redacted_thinking", "data": str(raw["data"])})
    return blocks


# The Anthropic messages API has no `response_format`, so a schema becomes a tool. The name is
# sent to the provider and read back off the response, and it is the one identifier that must
# not collide with a real tool the manifest bound.
STRUCTURED_OUTPUT_TOOL = "felix_structured_output"


def apply_anthropic_output_schema(body: dict[str, Any], schema: dict[str, Any]) -> None:
    """Ask for a schema-shaped answer the only way this wire can: a tool the model must call.

    `tool_choice` is `any` rather than `tool` whenever the turn also carries real tools.
    Naming this one would stop the model calling the others, and in a react loop the
    structured answer is the *last* turn rather than the only one — `any` says "end this turn
    in a tool call", and the structured tool is then the way to finish without doing more work.
    With no other tools there is nothing to preserve, so the choice is named outright.

    Extended thinking is the exception, and a loud one: Anthropic rejects any `tool_choice`
    but `auto` while `thinking` is set, so there the schema can only be offered. Call it after
    `apply_anthropic_thinking_cache`, which is what decides whether `thinking` is on the body.

    A caller's `/v1` `response_format` reaches here too, which means a *request* can set
    `tool_choice` on a manifest whose author asked for none. That is deliberate and it is the
    price of the request-supplied case working at all — leaving the choice unset there would
    make the feature advisory for every caller who is not also the operator. Two things bound
    it: the schema's text is screened like the turn it rides with
    (`governance.inbound.screen_output_schema`), and `any` is satisfiable without touching a
    real tool, since the structured-output tool is always the way to finish. A manifest that
    declares its own `spec.output_schema` overrides the caller's outright.
    """
    tools = list(body.get("tools") or [])
    if any(t.get("name") == STRUCTURED_OUTPUT_TOOL for t in tools):
        # Otherwise the fold below would swallow that tool's call and re-emit its arguments
        # as the turn's answer. The collision is unlikely and silent, which is the pair that
        # earns a raise rather than a comment asserting it cannot happen.
        raise ValueError(
            f"a bound tool is named {STRUCTURED_OUTPUT_TOOL!r}, which this wire reserves for "
            "structured output; rename the tool in the manifest"
        )
    tools.append(
        {
            "name": STRUCTURED_OUTPUT_TOOL,
            "description": (
                "Return the final answer. Call this exactly once, with the answer as its "
                "arguments. Do not answer in plain text."
            ),
            "input_schema": schema,
        }
    )
    body["tools"] = tools
    if body.get("thinking"):
        logger.warning(
            "extended thinking forbids a forced tool choice, so the output schema is offered "
            "to %s rather than required of it; the reply may be plain text",
            body.get("model") or "the model",
        )
        body["tool_choice"] = {"type": "auto"}
    elif len(tools) > 1:
        body["tool_choice"] = {"type": "any"}
    else:
        body["tool_choice"] = {"type": "tool", "name": STRUCTURED_OUTPUT_TOOL}


def fold_structured_output(
    text: str, tool_calls: list[ToolCall], raw_stop: Any, *, requested: bool = True
) -> tuple[str, list[ToolCall], Any]:
    """Turn a call to the structured-output tool back into the turn's text.

    Without this the react loop sees a tool named `felix_structured_output` that no manifest
    bound, fails to find it, and answers with a tool error — so the whole feature would read as
    broken rather than as unsupported. Folding here is also what makes the two wires
    interchangeable to a caller: on either one, `message.content` is the JSON document and
    `stop_reason` is `end_turn`.

    `requested` is whether this turn actually asked for a schema. The guard against a manifest
    binding a tool by the reserved name lives in `apply_anthropic_output_schema`, which only
    runs when one was — so without this flag, a manifest that bound `felix_structured_output`
    and declared *no* schema had that call silently swallowed: never executed, its
    model-authored arguments returned as the final answer, and the stop reason forced to
    `end_turn`. The guard was on the one branch where the collision was expected.

    A turn that also calls a real tool is the loop continuing rather than answering, so the
    premature structured call is dropped and the text left alone — the schema is asked for
    again on the next turn, which is the one that will end the run.

    What comes out of here is a reply like any other and is screened like one, which means
    reply controls can leave a caller holding a 200 whose body does not parse: PII redaction
    rewrites the JSON in place, and a block replaces it with `PII_BLOCKED_REPLY`. That is the
    right precedence — a control the operator switched on outranks a shape a caller asked for —
    but it is the one case where the contract and the control disagree, and a caller calling
    `json.loads` on every reply should expect it.

    Only a `tool_use` stop becomes `end_turn`. A turn truncated mid-arguments stops for
    `max_tokens`, and `parse_tool_arguments` answers a half-written document with `{}` rather
    than raising — so reporting `end_turn` there would hand the caller a well-formed `"{}"` as
    a finished answer and, worse, silence react's truncation quarantine, which is the thing
    that would otherwise catch it.
    """
    structured = [c for c in tool_calls if c.name == STRUCTURED_OUTPUT_TOOL] if requested else []
    if not structured:
        return text, tool_calls, raw_stop
    remaining = [c for c in tool_calls if c.name != STRUCTURED_OUTPUT_TOOL]
    if remaining:
        return text, remaining, raw_stop
    # The arguments *are* the answer, so they replace any prose rather than joining it: a
    # caller holding a schema calls `json.loads` on this, and a preamble breaks that.
    #
    # `tool_use` with the only tool call consumed would map to a stop reason the loop reads as
    # "run another turn", and there is nothing left to run. Every other stop is the provider
    # saying something about the turn that is still true once the call is folded away.
    stop = "end_turn" if str(raw_stop or "").lower() == "tool_use" else raw_stop
    return json.dumps(structured[-1].args, ensure_ascii=False), [], stop


@dataclass
class AnthropicMessagesClient(HttpModelClient):
    """The Anthropic messages wire format, including thinking blocks and cache points."""

    def _body(
        self,
        messages: list[ChatMessage],
        tools: Sequence[ToolSchema],
        temperature: float,
        max_tokens: int | None,
        *,
        isolate_cache: bool = False,
        output_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        system = ""
        converted: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "system":
                system = (system + "\n" + m.content).strip() if system else m.content
                continue
            if m.role == "tool":
                converted.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": m.tool_call_id,
                                "content": m.content,
                            }
                        ],
                    }
                )
                continue
            if m.role == "assistant" and m.tool_calls:
                # Thinking blocks come first and verbatim: with extended thinking on, the
                # provider rejects a turn that replays a tool call without the signed
                # reasoning that produced it.
                blocks: list[dict[str, Any]] = _anthropic_thinking_blocks(m)
                if m.content:
                    blocks.append({"type": "text", "text": m.content})
                for tc in m.tool_calls:
                    blocks.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.args})
                converted.append({"role": "assistant", "content": blocks})
                continue
            converted.append(_anthropic_user_or_plain(m))

        body: dict[str, Any] = {
            "model": self.route.model,
            "messages": converted,
            "temperature": temperature,
            "max_tokens": max_tokens or _DEFAULT_MAX_TOKENS,
        }
        if system:
            body["system"] = system
        if tools:
            body["tools"] = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": tool_json_schema(t),
                }
                for t in tools
            ]
        apply_anthropic_thinking_cache(body, self.spec, self.route.model, isolate_cache=isolate_cache)
        # After the thinking pass, which is what decides whether a forced tool choice is legal.
        if output_schema:
            apply_anthropic_output_schema(body, output_schema)
        return body

    async def _chat(
        self,
        messages: list[ChatMessage],
        tools: Sequence[ToolSchema],
        temperature: float,
        max_tokens: int | None,
        *,
        isolate_cache: bool = False,
        output_schema: dict[str, Any] | None = None,
    ) -> ModelChatResult:
        body = self._body(
            messages,
            tools,
            temperature,
            max_tokens,
            isolate_cache=isolate_cache,
            output_schema=output_schema,
        )
        headers = self._headers(
            {
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            }
        )
        async with httpx.AsyncClient(timeout=self._timeout()) as client:
            resp = await post_with_retry(
                client,
                f"{self.base_url.rstrip('/')}/v1/messages",
                label="anthropic",
                json=body,
                headers=headers,
            )
            if resp.status_code >= 400:
                raise ModelGatewayError("anthropic", resp.status_code, resp.text)
            data = resp.json()
        content_blocks = data.get("content") or []
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        thinking_blocks: list[dict[str, Any]] = []
        for b in content_blocks:
            if b.get("type") == "text":
                text_parts.append(str(b.get("text") or ""))
            elif b.get("type") == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=str(b.get("id") or ""),
                        name=str(b.get("name") or ""),
                        args=dict(b.get("input") or {}),
                    )
                )
            elif b.get("type") in ("thinking", "redacted_thinking"):
                thinking_blocks.append(dict(b))
        usage_raw = data.get("usage") or {}
        content, tool_calls, raw_stop = fold_structured_output(
            "".join(text_parts), tool_calls, data.get("stop_reason"), requested=output_schema is not None
        )
        stop = map_stop(raw_stop, _ANTHROPIC_STOP, had_tool_calls=bool(tool_calls))
        return ModelChatResult(
            message=ChatMessage(
                role="assistant",
                content=content,
                tool_calls=tool_calls or None,
                thinking=thinking_blocks or None,
            ),
            stop_reason=stop,
            usage=TokenUsage(
                input=int(usage_raw.get("input_tokens") or 0),
                output=int(usage_raw.get("output_tokens") or 0),
                cache_creation=int(usage_raw.get("cache_creation_input_tokens") or 0),
                cache_read=int(usage_raw.get("cache_read_input_tokens") or 0),
            ),
        )

    async def _stream_turn(
        self,
        messages: list[ChatMessage],
        tools: Sequence[ToolSchema],
        temperature: float,
        max_tokens: int | None,
        *,
        isolate_cache: bool = False,
        output_schema: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamDelta | ModelChatResult]:
        body = self._body(
            messages,
            tools,
            temperature,
            max_tokens,
            isolate_cache=isolate_cache,
            output_schema=output_schema,
        )
        body["stream"] = True
        headers = self._headers(
            {
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            }
        )

        text_parts: list[str] = []
        thinking_by_index: dict[int, dict[str, Any]] = {}
        tools_by_index: dict[int, dict[str, Any]] = {}
        usage = TokenUsage()
        raw_stop: str | None = None

        async with (
            httpx.AsyncClient(timeout=self._timeout()) as client,
            client.stream(
                "POST",
                f"{self.base_url.rstrip('/')}/v1/messages",
                json=body,
                headers=headers,
            ) as resp,
        ):
            if resp.status_code >= 400:
                raw = await resp.aread()
                raise ModelGatewayError("anthropic", resp.status_code, raw.decode("utf-8", errors="replace"))
            async for data in iter_sse_json(resp):
                kind = data.get("type")

                if kind == "message_start":
                    raw_usage = (data.get("message") or {}).get("usage") or {}
                    usage.input = int(raw_usage.get("input_tokens") or 0)
                    usage.cache_creation = int(raw_usage.get("cache_creation_input_tokens") or 0)
                    usage.cache_read = int(raw_usage.get("cache_read_input_tokens") or 0)
                elif kind == "content_block_start":
                    index = int(data.get("index") or 0)
                    block = data.get("content_block") or {}
                    btype = block.get("type")
                    if btype == "tool_use":
                        tools_by_index[index] = {
                            "id": str(block.get("id") or ""),
                            "name": str(block.get("name") or ""),
                            "json": "",
                        }
                    elif btype in ("thinking", "redacted_thinking"):
                        thinking_by_index[index] = dict(block)
                    elif btype == "text" and block.get("text"):
                        text_parts.append(str(block["text"]))
                        yield StreamDelta(kind="text", text=str(block["text"]))
                elif kind == "content_block_delta":
                    index = int(data.get("index") or 0)
                    delta = data.get("delta") or {}
                    dtype = delta.get("type")
                    if dtype == "text_delta" and delta.get("text"):
                        text_parts.append(str(delta["text"]))
                        yield StreamDelta(kind="text", text=str(delta["text"]))
                    elif dtype == "thinking_delta" and delta.get("thinking"):
                        block = thinking_by_index.setdefault(index, {"type": "thinking", "thinking": ""})
                        block["thinking"] = str(block.get("thinking") or "") + str(delta["thinking"])
                        yield StreamDelta(kind="thinking", text=str(delta["thinking"]))
                    elif dtype == "signature_delta" and delta.get("signature"):
                        block = thinking_by_index.setdefault(index, {"type": "thinking", "thinking": ""})
                        block["signature"] = str(block.get("signature") or "") + str(delta["signature"])
                    elif dtype == "input_json_delta":
                        entry = tools_by_index.setdefault(index, {"id": "", "name": "", "json": ""})
                        entry["json"] = str(entry["json"]) + str(delta.get("partial_json") or "")
                elif kind == "message_delta":
                    raw_stop = (data.get("delta") or {}).get("stop_reason") or raw_stop
                    out = (data.get("usage") or {}).get("output_tokens")
                    if out is not None:
                        usage.output = int(out)

        tool_calls = [
            ToolCall(
                id=entry["id"] or f"call_{uuid.uuid4().hex[:12]}",
                name=str(entry["name"]),
                args=parse_tool_arguments(entry["json"]),
            )
            for _, entry in sorted(tools_by_index.items())
            if entry.get("name")
        ]
        thinking = [thinking_by_index[i] for i in sorted(thinking_by_index)]
        # The structured answer streams as `input_json_delta` on a tool block, so nothing here
        # yielded a text delta for it. Everything downstream that renders a stream forwards
        # text deltas only — react re-emits `text_delta`, and `/v1` streaming filters to
        # `REPLY_TEXT_EVENTS` — so without the delta below a client streaming a structured
        # agent received `[DONE]` and no content at all.
        #
        # It arrives whole rather than incrementally on purpose: a half-parsed JSON document
        # is not an answer, and a caller holding a schema is going to `json.loads` it.
        before = "".join(text_parts)
        content, tool_calls, raw_stop = fold_structured_output(
            before, tool_calls, raw_stop, requested=output_schema is not None
        )
        if content != before:
            yield StreamDelta(kind="text", text=content)
        yield ModelChatResult(
            message=ChatMessage(
                role="assistant",
                content=content,
                tool_calls=tool_calls or None,
                thinking=thinking or None,
            ),
            stop_reason=map_stop(raw_stop, _ANTHROPIC_STOP, had_tool_calls=bool(tool_calls)),
            usage=usage,
        )


__all__ = [
    "STRUCTURED_OUTPUT_TOOL",
    "AnthropicMessagesClient",
    "apply_anthropic_output_schema",
    "apply_anthropic_thinking_cache",
    "fold_structured_output",
]
