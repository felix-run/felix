"""Screening the summaries a session strategy writes, before they are stored.

A summary is a model's rewrite of a transcript that carried tool output, and once stored it is
replayed into every later turn of the thread. Its *input* is fenced as data
(`compaction.summariser_transcript`), but a summariser that followed an injection anyway writes
the injection into the summary, and from there it reaches every turn the thread has left. So the
summary itself is held to the rule a user turn is (`inbound.screen_text_verdict`): the injection
markers, then `content_screening.model` and the decider when the manifest sets them.

The seam is the summariser model (`screen_summarizer`). A strategy is handed the model it
summarises with -- by `render` options or `compact_now`'s `model` -- on a turn's render, a forced
pass on overflow, `POST /chat/compact` and `summarizing:N`, so `SummaryScreeningStrategy` wraps it
there. A summary written outside a strategy must wrap its model itself. The branch summary a rewind
writes does not: it is never rendered into context (`session.types._LLM_SKIP_KINDS`).

A **flagged** summary is replaced by `WITHHELD_SUMMARY`, and the strategy stores that for the range
it covers. Storing nothing instead left the range to be summarised again on every turn over budget,
each attempt paying the summariser and the screener: text planted in the history, or a harmless
false positive, became a standing cost. A summary that **could not be screened** raises
`SummaryWithheld`, which every summariser treats as a failure -- nothing is stored, and a later turn
tries again once the screener is back.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from felix.observability.metrics import record_counter

if TYPE_CHECKING:
    from felix.config import Settings
    from felix.manifests.schema import Manifest
    from felix.patterns.types import ChatMessage

logger = logging.getLogger("felix.governance.summary_screening")

#: What a flagged summary is stored as: the range it covers is settled, and says why it is empty.
WITHHELD_SUMMARY = "[summary withheld: content screening flagged it]"

# A summariser's model calls other than `chat`. Each would return text no screen has read, so the
# wrapper refuses them rather than handing them through (`_ScreenedSummarizer.__getattr__`).
_UNSCREENED_CALLS = frozenset({"stream", "stream_turn"})


class SummaryWithheld(Exception):
    """The summary could not be screened, and is not kept."""


class _ScreenedSummarizer:
    """A summariser model whose every reply is screened before it is returned."""

    def __init__(self, inner: Any, manifest: Manifest, settings: Settings) -> None:
        self._inner = inner
        self._manifest = manifest
        self._settings = settings

    async def chat(self, messages: list[ChatMessage], tools: Any, opts: Any = None) -> Any:
        from felix.patterns.types import ChatMessage

        result = await self._inner.chat(messages, tools, opts)
        text = str(result.message.content or "")
        if not text.strip():
            return result
        action = await self._verdict(text)
        if action is None:
            return result
        if action == "flagged":
            # Returned rather than raised, so the caller meters it and stores it as it would any
            # summary: the range is settled, and is not summarised and screened again next turn.
            return replace(result, message=ChatMessage(role="assistant", content=WITHHELD_SUMMARY))
        # Metered here because the raise skips the caller's own metering, and the call was paid
        # for whatever became of its answer.
        from felix.session.compaction import meter_summarizer

        meter_summarizer(result, self._inner, kind="summary_withheld", reason=action)
        raise SummaryWithheld(f"summary_withheld: {action}")

    async def _verdict(self, text: str) -> str | None:
        """None for a summary that may be kept, else the screening action it earned."""
        from felix.audit.emit import emit_agent_audit
        from felix.governance.inbound import screen_text_verdict
        from felix.governance.screening import screening_decider

        name = self._manifest.metadata.name
        try:
            decider = screening_decider(self._manifest, self._settings)
            verdict = await screen_text_verdict(self._manifest, text, self._settings, decider)
        except Exception:
            # A screener that cannot run has not cleared anything.
            logger.warning("summary screening could not run; the summary is not kept", exc_info=True)
            action: str | None = "unavailable"
        else:
            action = "flagged" if verdict.flagged else "unavailable" if verdict.unavailable else None
        if action is not None:
            record_counter("felix_summary_withheld", {"manifest_id": name, "reason": action})
            emit_agent_audit("summary_screening", status=action, payload={}, manifest_id=name)
        return action

    def __getattr__(self, name: str) -> Any:
        # `model_id`, `route` and the rest are read by name: pricing reads the wire model off them.
        if name == "_inner":
            raise AttributeError(name)
        if name in _UNSCREENED_CALLS:
            raise AttributeError(f"{name} is not screened; a summariser calls chat")
        return getattr(self._inner, name)


def screen_summarizer(model: Any, manifest: Manifest, settings: Settings) -> Any:
    """`model` with every summary it writes screened under `manifest`'s content screening.

    Stacked rather than collapsed: a sub-agent is compiled with the strategy its router already
    wrapped, so the child's screen wraps a model the router's will wrap again, and each screen adds
    to the other's. Only a second wrap under the same manifest is skipped.
    """
    if model is None or (isinstance(model, _ScreenedSummarizer) and model._manifest is manifest):
        return model
    return _ScreenedSummarizer(model, manifest, settings)


class SummaryScreeningStrategy:
    """A session strategy whose summariser model is screened (`screen_summarizer`)."""

    def __init__(self, inner: Any, manifest: Manifest, settings: Settings) -> None:
        self._inner = inner
        self._manifest = manifest
        self._settings = settings

    def _screened(self, model: Any) -> Any:
        return screen_summarizer(model, self._manifest, self._settings)

    async def render(self, session: Any, incoming: list[ChatMessage], opts: Any) -> list[ChatMessage]:
        if isinstance(opts, dict):
            opts = {**opts, "model": self._screened(opts.get("model"))}
        elif getattr(opts, "model", None) is not None:
            opts = replace(opts, model=self._screened(opts.model))
        return await self._inner.render(session, incoming, opts)

    def __getattr__(self, name: str) -> Any:
        # `context_window_tokens`, `reserve_tokens` and the rest are read by name. `compact_now`
        # is here rather than a method because its *presence* is the signal callers read
        # (`getattr(strategy, "compact_now", None)`): this wrapper has it only when the inner does.
        if name == "_inner":
            raise AttributeError(name)
        attr = getattr(self._inner, name)
        if name != "compact_now":
            return attr

        async def compact_now(session: Any, *, model: Any | None = None, **kwargs: Any) -> Any:
            return await attr(session, model=self._screened(model), **kwargs)

        return compact_now


def screen_summaries(strategy: Any, manifest: Manifest, settings: Settings | None) -> Any:
    """`strategy` with the summaries it writes screened, or `strategy` itself when the manifest
    does not screen content (`content_screening.enabled`) or there is no strategy."""
    if strategy is None or not manifest.spec.content_screening.enabled:
        return strategy
    if settings is None:
        from felix.config import get_settings

        settings = get_settings()
    return SummaryScreeningStrategy(strategy, manifest, settings)


__all__ = [
    "WITHHELD_SUMMARY",
    "SummaryScreeningStrategy",
    "SummaryWithheld",
    "screen_summaries",
    "screen_summarizer",
]
