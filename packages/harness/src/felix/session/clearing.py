"""Clear stale tool results (`spec.session.clear_tool_results`): old outputs stubbed in the render.

A long agent run accumulates tool results -- file reads, search hits, command output, screenshots
-- that it needed once and never reads again, and every later model call pays for them. This wraps
a session strategy so that each render replaces an old tool result with a one-line stub when it is
larger than `min_chars` or carries an image. The log is untouched: a rewind, a fork or an export
still has every result, and so does a compaction summary, which is written from the log.

- **Old means before the newest `keep_turns` user messages**, counted in what the strategy
  rendered. A steer or a compaction summary is a user message too; counting it only ever keeps
  more. Transient guidance is not counted.
- **The cut-off moves in steps.** It advances `keep_turns` user messages at a time, so the newest
  `keep_turns` to `2 * keep_turns - 1` are always whole and the prefix a provider caches changes
  once a step rather than on every call (for a strategy that appends; a windowed or semantic
  render changes its prefix anyway).
- **The call stays paired.** The result's `tool_call_id` and the assistant's call are kept; only
  the content goes, so every wire still sees a result for every call.
- **A spilled result keeps its handle**: the stub names the artifact id from its spill marker, and
  nothing else from the result, so `read_artifact` still reaches the full output.
- **What the caller sent is theirs.** A message in the incoming turn -- a `/v1` client re-sends
  the whole conversation as one -- is never stubbed.

Clearing runs inside replay image screening, so a stubbed screenshot is never screened again.
`model_view_strategy` composes the two for every site that renders history for a model
(`build_agent`, `side_question`).
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from felix_ai.types import IMAGE_PART_TYPES

if TYPE_CHECKING:
    from felix_ai.types import ChatMessage

    from felix.config import Settings
    from felix.manifests.schema import Manifest, ToolResultClearing

STUB_PREFIX = "[cleared to save context:"
# Governance's own notices: short, and a signal the model should keep seeing.
_NEVER_STUBBED = (STUB_PREFIX, "[quarantined]")


def _has_image(message: ChatMessage) -> bool:
    return bool(message.attachments) or any(b.type in IMAGE_PART_TYPES for b in message.content_blocks or ())


def _stub(message: ChatMessage) -> ChatMessage:
    from felix.artifacts import spilled_artifact_id

    content = message.content or ""
    images = " and an image" if _has_image(message) else ""
    note = (
        f"{STUB_PREFIX} an earlier tool result of {len(content)} characters{images}. If you need it "
        "again, re-run a tool that only reads; do not repeat an action to see what it returned."
    )
    artifact_id = spilled_artifact_id(content)
    if artifact_id:
        note = f"{note} Its full output is artifact {artifact_id} (read_artifact)."
    return replace(message, content=note, content_blocks=None, attachments=None)


def _stale(message: ChatMessage, min_chars: int) -> bool:
    content = message.content or ""
    if message.role != "tool" or content.startswith(_NEVER_STUBBED):
        return False
    return len(content) > min_chars or _has_image(message)


def clear_stale_tool_results(
    messages: list[ChatMessage], *, keep_turns: int, min_chars: int, keep: set[int] | None = None
) -> list[ChatMessage]:
    """`messages` with each stale tool result before the cut-off stubbed. The cut-off is the
    user message that leaves the newest `keep_turns` to `keep_turns * 2 - 1` of them whole: it
    moves `keep_turns` at a time. Messages whose `id` is in `keep` (the incoming turn) are never
    touched."""
    users = [i for i, m in enumerate(messages) if m.role == "user" and not m.transient]
    # User messages behind the cut-off: the whole steps of `keep_turns` beyond the newest ones.
    cleared = max(0, (len(users) - keep_turns) // keep_turns) * keep_turns
    if cleared == 0:
        return messages
    cut = users[cleared]
    keep = keep or set()
    return [
        _stub(m) if i < cut and id(m) not in keep and _stale(m, min_chars) else m
        for i, m in enumerate(messages)
    ]


class ClearingSessionStrategy:
    """A session strategy whose renders stub stale tool results (`clear_stale_tool_results`)."""

    def __init__(self, inner: Any, *, keep_turns: int, min_chars: int) -> None:
        self._inner = inner
        self._keep_turns = keep_turns
        self._min_chars = min_chars

    async def render(self, session: Any, incoming: list[ChatMessage], opts: Any) -> list[ChatMessage]:
        rendered = await self._inner.render(session, incoming, opts)
        return clear_stale_tool_results(
            rendered,
            keep_turns=self._keep_turns,
            min_chars=self._min_chars,
            keep={id(m) for m in incoming},
        )

    def __getattr__(self, name: str) -> Any:
        # `compact_now`, `context_window_tokens` and the rest are read by name, with a default:
        # hiding them would quietly turn compaction off.
        if name == "_inner":
            raise AttributeError(name)
        return getattr(self._inner, name)


def clear_session_strategy(strategy: Any, spec: ToolResultClearing | None) -> Any:
    """`strategy` with stale tool results cleared from its renders, or itself when off."""
    if strategy is None or spec is None:
        return strategy
    return ClearingSessionStrategy(strategy, keep_turns=spec.keep_turns, min_chars=spec.min_chars)


def model_view_strategy(strategy: Any, manifest: Manifest, settings: Settings | None) -> Any:
    """`strategy` as one compile renders history for its model: stale tool results cleared, then
    replayed images screened. Every site that renders for a model calls this, so a next turn and a
    side question about the thread read it the same way."""
    from felix.governance.image_screening import screen_session_strategy
    from felix.governance.inbound import replay_screener

    cleared = clear_session_strategy(strategy, manifest.spec.session.clear_tool_results)
    return screen_session_strategy(cleared, replay_screener(manifest, settings))


__all__ = [
    "STUB_PREFIX",
    "ClearingSessionStrategy",
    "clear_session_strategy",
    "clear_stale_tool_results",
    "model_view_strategy",
]
