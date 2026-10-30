"""Screening for text an agent sends out of the harness under the deployment's name.

`notify` (`push/agent_tool.py`) and `post` (`destinations/tool.py`) carry text a model wrote --
possibly under a prompt injection -- to a person's lock screen or a team's channel. Neither can
quarantine what it sends the way the screening wrapper quarantines a tool's output, so both refuse
instead, and both screen the same way: always the free marker scan (`content_screening.py`), then,
when the manifest's content screening is on and covers the tool, its model and decider as the
screening wrapper runs them. A screener that cannot run refuses too: unscreened is not clean.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from felix.tools.refusal import ToolRefused

if TYPE_CHECKING:
    from felix.manifests.schema import Manifest

logger = logging.getLogger("felix.governance.outbound_screening")


async def screen_outbound(
    settings: Any, manifest: Manifest, tool_name: str, text: str, *, flagged: str, unavailable: str
) -> None:
    """Raise `ToolRefused("screened", ...)` -- `flagged` when a screener flags `text`, `unavailable`
    when one that should run cannot -- or return when it is clean. The messages are the tool's own."""
    from felix.governance.content_screening import screen_content

    verdict = await screen_content(text, settings=settings, block_on_injection=True, redact_pii=False)
    if verdict.denied:
        raise ToolRefused("screened", flagged)
    screening = manifest.spec.content_screening
    if not screening.enabled:
        return
    from felix.manifests.tool_match import matches_any

    if screening.model_tools and not matches_any(screening.model_tools, tool_name):
        return
    from felix.governance.screening import screening_decider
    from felix.governance.tool_screening import screen_tool_output

    try:
        decider = screening_decider(manifest, settings)
    except Exception:
        logger.warning(
            "%s screening decider unavailable manifest=%s", tool_name, manifest.metadata.name, exc_info=True
        )
        raise ToolRefused("screened", unavailable) from None
    model_id = screening.model.strip()
    if not (model_id or decider is not None):
        return
    result = await screen_tool_output(settings, text, model_id, decider)
    if result.unavailable:
        raise ToolRefused("screened", unavailable)
    if result.flagged:
        raise ToolRefused("screened", flagged)


__all__ = ["screen_outbound"]
