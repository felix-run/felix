"""Output styles (`spec.output_styles`): how the agent writes, chosen per conversation.

A manifest declares named styles, each a block of prose, and optionally a `default`. A
conversation picks one with `POST /chat/style`, stored in the thread's metadata as `output_style`; the next
compile of that thread's agent appends the chosen style -- or the default -- to its system prompt.

The system prompt, not a per-run message: the text is the manifest author's own, like
`append_system_md`, and it changes only when the person changes style, so the cached prefix stays
whole between turns. Only the agent the request names gets it; a sub-agent or `task` child -- in
the compile or run in the background -- answers its parent, not the person, and keeps its own
prompt. A pattern whose own prompt does not write the answer (a router) refuses the field at
compile (`honours_output_styles`). A stored style the agent does not declare -- the thread moved to
another manifest -- reads as the default.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from felix.context import try_get_context

if TYPE_CHECKING:
    from felix.manifests.schema import Manifest, OutputStyle

logger = logging.getLogger("felix.output_styles")


def _style_block(style: OutputStyle) -> str:
    """The style as the system prompt carries it: a heading naming it, then the author's prose."""
    return f"# Output style: {style.name}\n\n{style.prompt}"


async def chosen_style(manifest: Manifest, *, top_level: bool) -> OutputStyle | None:
    """The style this compile of `manifest` writes in, or None for the prompt as written.
    `top_level` is false for a sub-agent or delegate, which keeps its own prompt."""
    spec = manifest.spec.output_styles
    if not spec.styles or not top_level:
        return None
    from felix.durability.runs import BACKGROUND_CHILD_EXTRA

    req = try_get_context()
    if req is not None and req.extras.get(BACKGROUND_CHILD_EXTRA):
        # A background `task` child compiles in the worker as a run of its own, so the compile
        # stack cannot say it is a child; the run does.
        return None
    stored = None
    if req is not None and req.thread_id:
        from felix.session.thread_state import get_thread_meta

        try:
            meta = await get_thread_meta(
                settings=req.settings, tenant_id=req.auth.tenant_id, thread_id=req.thread_id
            )
            stored = meta.get("output_style")
        except Exception:
            # A style is presentation, not a control: an unreadable one is the default.
            logger.warning("output_style_unreadable", exc_info=True)
    return spec.find(stored if isinstance(stored, str) else None) or spec.find(spec.default)


async def apply_output_style(system_prompt: str, manifest: Manifest, *, top_level: bool) -> str:
    style = await chosen_style(manifest, top_level=top_level)
    if style is None:
        return system_prompt
    return f"{system_prompt}\n\n---\n\n{_style_block(style)}" if system_prompt else _style_block(style)


__all__ = ["apply_output_style", "chosen_style"]
