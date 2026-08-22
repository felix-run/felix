"""Spill large tool outputs to the object store and return a preview."""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from felix.manifests.schema import ArtifactsSpec
from felix.tools.executor import wrap_tool
from felix.tools.types import Tool, ToolInvocationCtx, ToolOutput, tool_output_content

logger = logging.getLogger("felix.artifacts")


def apply_artifact_spill(
    tools: list[Tool],
    spec: ArtifactsSpec,
    *,
    object_store: Any | None,
    tenant_id: str,
    manifest_id: str,
) -> list[Tool]:
    """Wrap tools so oversized outputs are stored and replaced with a preview."""
    if not spec.enabled or object_store is None:
        return tools

    threshold = spec.threshold_chars
    preview = spec.preview_chars

    def wrap_one(tool: Tool) -> Tool:
        inner = tool.executor

        async def execute(
            args: dict[str, Any],
            ctx: ToolInvocationCtx | None = None,
            _inner: Any = inner,
        ) -> ToolOutput:
            result = await _inner.execute(args, ctx)
            content = tool_output_content(result)
            if len(content) <= threshold:
                return result
            artifact_id = uuid.uuid4().hex
            key = f"artifacts/{tenant_id}/{manifest_id}/{artifact_id}.txt"
            try:
                await object_store.put(
                    key, content.encode("utf-8"), content_type="text/plain; charset=utf-8"
                )
            except Exception:
                logger.debug("artifact spill failed; returning truncated output", exc_info=True)
                head = content[:preview]
                return (
                    f"{head}\n\n…[truncated {len(content) - preview} chars; "
                    f"artifact store write failed]"
                )
            head = content[:preview]
            return (
                f"{head}\n\n…[artifact:{artifact_id} key={key} "
                f"chars={len(content)} spilled_at={int(time.time())}]"
            )

        return wrap_tool(tool, execute)

    return [wrap_one(t) for t in tools]


__all__ = ["apply_artifact_spill"]
