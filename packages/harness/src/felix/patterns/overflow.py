"""Recognising a context overflow in a provider's rejection.

Compaction is threshold-driven: it fires when the estimated token count crosses
`context_window - reserve_tokens`. Estimation is a character heuristic anchored on the
last reported usage, so it runs slightly behind the truth — and a manifest can be
configured with a window larger than the model really has. When the estimate is wrong in
the optimistic direction the provider rejects the request, and a rejection was a hard
failure for the run rather than a reason to compact and try once more.

Providers disagree on how they say it, so the phrases are a table rather than a guess.
Two vendors do not raise at all, which is worse: one accepts the request and reports more
input tokens than the window holds, another truncates the input and returns a
length-stop having produced nothing. Both are handled as `is_silent_overflow`.

The exclusion list matters as much as the marker list. Throttling messages mention tokens
too ("rate limit reached for tokens"), and treating one as an overflow would compact a
conversation that was never too long — discarding history to fix a problem that a retry
would have solved on its own.
"""

from __future__ import annotations

from typing import Any

# Phrases that mean "this request does not fit". Matched case-insensitively against the
# provider's response body.
_OVERFLOW_MARKERS: tuple[str, ...] = (
    "context length",
    "context_length_exceeded",
    "context window",
    "maximum context",
    "prompt is too long",
    "input is too long",
    "exceed context limit",
    "exceeds the context",
    "reduce the length of the messages",
    "reduce the length of your prompt",
    "too many tokens in the prompt",
    "string too long",
)

# Phrases that mean "you are sending too fast", which also mention tokens and limits.
# Checked first: a match here disqualifies the response regardless of the markers above.
_THROTTLING_MARKERS: tuple[str, ...] = (
    "rate limit",
    "rate_limit",
    "tokens per min",
    "requests per min",
    "tpm",
    "rpm",
    "quota",
    "too many requests",
    "overloaded",
    "capacity",
)


def is_context_overflow(error: Any) -> bool:
    """True when a provider rejection means the request did not fit the context window."""
    body = ""
    for attr in ("body", "text"):
        try:
            raw = getattr(error, attr, "") or ""
        except Exception:
            raw = ""
        if raw:
            body = str(raw)
            break
    haystack = f"{body} {error}".lower()
    if any(marker in haystack for marker in _THROTTLING_MARKERS):
        return False
    return any(marker in haystack for marker in _OVERFLOW_MARKERS)


def is_silent_overflow(
    *,
    stop_reason: str | None,
    tokens_input: int,
    tokens_output: int,
    context_window: int,
) -> bool:
    """True when a turn overflowed without the provider saying so.

    Two shapes, both observed in the wild and neither raising:

    * The request is accepted and the reported input exceeds the window the model
      advertises. The provider silently dropped or refused part of the prompt.
    * The turn stops on the output limit having produced **no** output at all. A model
      that emits nothing before hitting its ceiling did not have room to answer, so the
      input consumed the budget.
    """
    if context_window > 0 and tokens_input > context_window:
        return True
    return stop_reason == "max_tokens" and tokens_output == 0 and tokens_input > 0


__all__ = ["is_context_overflow", "is_silent_overflow"]
