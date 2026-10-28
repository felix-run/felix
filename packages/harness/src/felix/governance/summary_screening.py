"""Screening the summaries a session strategy writes, before they are stored.

A summary is a model's rewrite of a transcript that carried tool output, and once stored it is
replayed into every later turn of the thread. Its *input* is fenced as data
(`compaction.summariser_transcript`), but a summariser that followed an injection anyway writes
the injection into the summary, and from there it reaches every turn the thread has left. So the
summary itself is held to the rule a user turn is (`inbound.screen_text_verdict`): the injection
markers, then `content_screening.model` and the decider when the manifest sets them.

The seam is the summariser model. Every path that summarises -- a turn's render, a forced pass
on overflow, `POST /chat/compact`, `summarizing:N` -- is handed the model to summarise with, by
`render` options or `compact_now`'s `model`, so wrapping that model on the strategy covers each
of them, including one added later. A flagged summary, or one that could not be screened, makes
the call raise `SummaryWithheld`, which every summariser already treats as a failed summary:
nothing is stored, and the thread renders as it was, its recent window kept.
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


class SummaryWithheld(Exception):
    """The summary was flagged by content screening, or could not be screened, and is not kept."""


class _ScreenedSummarizer:
    """A summariser model whose every reply is screened before it is returned."""

    def __init__(self, inner: Any, manifest: Manifest, settings: Settings) -> None:
        self._inner = inner
        self._manifest = manifest
        self._settings = settings

    async def chat(self, messages: list[ChatMessage], tools: Any, opts: Any = None) -> Any:
        result = await self._inner.chat(messages, tools, opts)
        text = str(result.message.content or "")
        if text.strip():
            action = await self._verdict(text)
            if action is not None:
                # Metered here because the raise skips the caller's own metering, and the call was
                # paid for whatever became of its answer.
                from felix.session.compaction import meter_summarizer

                meter_summarizer(result, self._inner, kind="summary_withheld", reason=action)
                raise SummaryWithheld(f"summary_withheld: {action}")
        return result

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
        return getattr(self._inner, name)


class SummaryScreeningStrategy:
    """A session strategy whose summariser model is screened (`_ScreenedSummarizer`)."""

    def __init__(self, inner: Any, manifest: Manifest, settings: Settings) -> None:
        self._inner = inner
        self._manifest = manifest
        self._settings = settings

    def _screened(self, model: Any) -> Any:
        if model is None or isinstance(model, _ScreenedSummarizer):
            return model
        return _ScreenedSummarizer(model, self._manifest, self._settings)

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


__all__ = ["SummaryScreeningStrategy", "SummaryWithheld", "screen_summaries"]
