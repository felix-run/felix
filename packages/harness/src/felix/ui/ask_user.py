"""`ask_user` — the model's way to put a question to the person watching the run.

`request_ui` has carried select / confirm / input prompts over the stream since the clients
learned to render them, and nothing in the harness could reach it: no tool exposed it, so
the only callers were tests and the banner both web clients draw never appeared. This is
that tool.

It asks only when someone can answer. A prompt reaches a person through the live SSE stream
alone -- a non-streaming `POST /chat`, a durable run in the worker, `/v1` and A2A have no
consumer for the side channel -- and without this check the model would block for the whole
timeout and then be told nobody answered, a five-minute stall that reads as a hang. The
streaming route marks its request `LIVE_STREAM_EXTRA`; anywhere else the tool refuses at
once, in words the model can act on.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from felix.tools.types import ToolInvocationCtx
from felix.ui.prompts import request_ui

# Set on a request's `extras` by the route that streams the run to a person.
LIVE_STREAM_EXTRA = "live_stream"

DEFAULT_TIMEOUT_SECONDS = 300
MAX_TIMEOUT_SECONDS = 900


class AskUserArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2000, description="What to ask, as a full sentence.")
    kind: Literal["input", "confirm", "select"] = Field(
        default="input",
        description="`confirm` for yes/no, `select` to choose one of `options`, `input` for free text.",
    )
    options: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="The choices for `select`. Giving options implies `select`.",
    )
    timeout_seconds: int = Field(
        default=DEFAULT_TIMEOUT_SECONDS,
        ge=10,
        le=MAX_TIMEOUT_SECONDS,
        description="How long to wait for an answer before carrying on without one.",
    )


def _live() -> bool:
    from felix.context import try_get_context

    ctx = try_get_context()
    return bool(ctx is not None and ctx.extras.get(LIVE_STREAM_EXTRA))


def _result(**fields: Any) -> str:
    return json.dumps(fields)


async def ask_user_handler(args: AskUserArgs, ctx: ToolInvocationCtx | None = None) -> str:
    """Ask, wait, and say what happened -- including when nobody could be asked."""
    if not _live():
        return _result(
            answered=False,
            reason="no_one_watching",
            guidance=(
                "This run is not being watched live, so no one can answer a question now. "
                "Make a reasonable choice and state it, or ask in your reply."
            ),
        )
    thread_id = ctx.thread_id if ctx is not None else None
    if not thread_id:
        return _result(answered=False, reason="no_thread", guidance="Ask in your reply instead.")

    kind = "select" if args.options and args.kind == "input" else args.kind
    if kind == "select" and not args.options:
        return _result(answered=False, reason="no_options", guidance="Give `options` for `select`.")

    response = await request_ui(
        thread_id,
        kind,
        prompt=args.question,
        options=list(args.options) if kind == "select" else None,
        timeout=float(args.timeout_seconds),
    )
    if response.cancelled:
        # `timeout` and a person dismissing the prompt are different answers: one means
        # nobody saw it, the other that somebody chose not to answer.
        return _result(
            answered=False,
            reason=response.note or "cancelled",
            guidance="Carry on without an answer and say what you assumed.",
        )
    return _result(answered=True, value=response.value)


__all__ = ["LIVE_STREAM_EXTRA", "AskUserArgs", "ask_user_handler"]
