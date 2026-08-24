"""Server-Sent Events plumbing, shared by every streaming route.

Extracted because the header set below is easy to omit and expensive to omit: nginx
buffers proxied responses by default, so a stream without `x-accel-buffering: no`
delivers nothing until it closes. That had already happened twice — the reconnect
route and `/v1/chat/completions` were each built without it — which is what a
constructed-in-three-places response is for.

Framing rules that hold across every stream here:

- a frame is `data: <json>`, and the stream ends with a literal `data: [DONE]`;
- `id:` is optional and carries a *cursor*, the next session sequence to ask for, so
  a client can hand it back as `Last-Event-ID`. Frames without one leave the client's
  `lastEventId` untouched, which is why only structural frames need to carry it;
- `event:` is used for exactly one thing, the error frame, so a client reading only
  `onmessage` never sees an error silently swallowed under an already-sent 200.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi.responses import StreamingResponse

# Emitted by `with_heartbeat` when the upstream stream has been quiet.
HEARTBEAT = object()
SSE_HEARTBEAT_SECONDS = 15.0

# Frames that arrive at token rate. Everything else is structural and gets an `id:`.
#
# A denylist rather than an allowlist on purpose: patterns register themselves through
# an open registry, so core cannot know the full event vocabulary, and an allowlist
# silently withholds `id:` from anything it has not been told about — including the
# approval and client-tool frames, which mark the longest pauses in a stream and so
# the likeliest moment for a connection to drop. This set is short and does not grow
# when someone registers a pattern.
PER_TOKEN_EVENTS = frozenset({"text_delta", "on_chat_model_stream", "session_progress"})


def is_resume_point(event_name: str) -> bool:
    """Whether a frame of this type should carry a resume cursor."""
    return bool(event_name) and event_name not in PER_TOKEN_EVENTS


# How far the upstream run may lead the client. Bounded, so backpressure still exists:
# once the queue is full the pump blocks and the agent loop feels it, exactly as it did
# when the consumer awaited each event directly.
HEARTBEAT_QUEUE_MAXSIZE = 64


async def with_heartbeat(stream: Any, interval: float = SSE_HEARTBEAT_SECONDS) -> Any:
    """Yield upstream events, injecting a heartbeat sentinel during quiet periods.

    A long tool call emits nothing, and proxy idle timeouts are commonly 60s, so a
    perfectly healthy run was being disconnected mid-flight.

    A pump task feeds a bounded queue rather than the consumer awaiting each event
    under a timeout. The timeout is what costs: arming a timer, registering a done
    callback and tearing both down again ran on *every* event, measured at ~46us
    against ~0.14us for raw iteration — a tax paid per token, per open stream, to
    detect a silence that by definition is not happening while tokens arrive. Here the
    timer is armed only when the queue is actually empty, which on a busy stream is
    almost never.
    """
    queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=HEARTBEAT_QUEUE_MAXSIZE)
    # Not named `done`: `task.done()` is a different question forty lines below.
    END = object()
    failure: BaseException | None = None

    async def pump() -> None:
        nonlocal failure
        try:
            async for item in stream:
                await queue.put(item)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            failure = exc
        finally:
            # Non-blocking on purpose. A blocking put deadlocks: if the queue is full
            # because the consumer stopped draining, the consumer's `finally` cancels
            # this task, cancellation lands on `queue.put(item)` above, and then this
            # line blocks forever on the same full queue. Dropping the sentinel is
            # safe because it is only a fast path — the loop below also treats a
            # finished pump as end-of-stream, which is what makes it reliable.
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(END)

    task = asyncio.ensure_future(pump())
    try:
        while True:
            if not queue.empty():
                item = queue.get_nowait()
            elif task.done():
                # Drained everything and the pump has finished. This is the arm that
                # makes a dropped sentinel harmless; without it a full queue at
                # stream end left the consumer emitting heartbeats forever.
                break
            else:
                try:
                    item = await asyncio.wait_for(queue.get(), interval)
                except TimeoutError:
                    yield HEARTBEAT
                    continue
            if item is END:
                break
            yield item
        # An upstream failure must reach the caller: chat.py answers it with an
        # `event: error` frame, and swallowing it here would end the stream under a
        # 200 with no way to tell success from failure.
        if failure is not None:
            raise failure
    finally:
        # Covers the ordinary end of the stream, a consumer break, and client
        # disconnect. Leaving the pump running would keep the agent run burning tokens
        # for a connection nobody is reading.
        if not task.done():
            task.cancel()
            # Both, not one: CancelledError is a BaseException in 3.14, so
            # suppressing Exception alone would let it escape.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


def frame(payload: Any, *, cursor: int | None = None) -> str:
    """One `data:` frame, optionally carrying a resume cursor."""
    prefix = f"id: {cursor}\n" if cursor is not None else ""
    return f"{prefix}data: {json.dumps(payload, default=str)}\n\n"


def error_frame(message: str, *, kind: str = "stream_error") -> str:
    """The one `event:`-typed frame.

    Without it the body simply stopped under an already-sent 200 OK, with no error
    event and no `[DONE]` — a client could not tell success from failure.
    """
    body = json.dumps({"error": {"message": message[:200], "type": kind}})
    return f"event: error\ndata: {body}\n\n"


DONE = "data: [DONE]\n\n"
KEEP_ALIVE = ": keep-alive\n\n"


def sse_response(generator: AsyncIterator[str]) -> StreamingResponse:
    """A streaming response with the headers a proxied SSE stream actually needs."""
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "cache-control": "no-cache",
            "connection": "keep-alive",
            # nginx buffers proxied responses by default, which defeats streaming
            # entirely — the client gets everything at once when the run finishes.
            "x-accel-buffering": "no",
        },
    )


__all__ = [
    "DONE",
    "HEARTBEAT",
    "HEARTBEAT_QUEUE_MAXSIZE",
    "KEEP_ALIVE",
    "PER_TOKEN_EVENTS",
    "SSE_HEARTBEAT_SECONDS",
    "error_frame",
    "frame",
    "is_resume_point",
    "sse_response",
    "with_heartbeat",
]
