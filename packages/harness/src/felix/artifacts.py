"""Spill large tool outputs to the object store, and read them back.

The spill has always worked; nothing ever read it. `object_store.get` appeared
nowhere in the harness, so an oversized tool result was written to a store no
route, CLI command or client method could reach — the model saw a preview and a
marker naming an object that could not be fetched by anyone.
"""

from __future__ import annotations

import logging
import posixpath
import re
import time
import uuid
from typing import Any

from felix.manifests.schema import ArtifactsSpec
from felix.tools.executor import wrap_tool
from felix.tools.types import Tool, ToolInvocationCtx, ToolOutput, tool_output_content

logger = logging.getLogger("felix.artifacts")

# A spilled id is a uuid4 hex. A manifest id is looser, so it is bounded here rather
# than trusted: both land in an object key, and a segment that is not what it looks
# like addresses something other than what the caller named.
#
# The leading character is not decoration. An earlier spelling allowed `.` anywhere,
# so `..` passed — a segment with no slash in it, which every traversal case tested
# alongside it had. No backend would have leaked: the filesystem store rejects a `..`
# part outright, and S3/GCS/memory treat the key as a literal rather than normalising
# it, so the escape only ever produced a 500 on one backend and a miss on the rest.
# It is closed here anyway, because a reference format whose safety depends on what
# each backend happens to do with a bad key is not a reference format.
_ID = re.compile(r"\A[0-9a-f]{32}\Z")
_MANIFEST_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def artifact_key(tenant_id: str, manifest_id: str, artifact_id: str) -> str:
    """Where a spilled output lives. The one definition, used to write and to read."""
    return f"artifacts/{tenant_id}/{manifest_id}/{artifact_id}.txt"


def valid_artifact_ref(manifest_id: str, artifact_id: str) -> bool:
    """Whether these are safe to build a key from.

    Two independent checks, deliberately. The patterns say what a reference may look
    like; `_contained` says what the result must be regardless — that the key still
    resolves under this tenant's prefix once normalised. A charset is an argument
    that traversal is impossible, and that argument has already been wrong once.

    The containment check is this layer's own guarantee, not a restatement of the
    backend's. The filesystem store makes the same check about its root and the
    others never normalise at all; none of them knows what a tenant is.
    """
    if not (_MANIFEST_ID.match(manifest_id) and _ID.match(artifact_id)):
        return False
    return _contained("t", artifact_key("t", manifest_id, artifact_id))


def _contained(tenant_id: str, key: str) -> bool:
    """Whether a built key still lives under its tenant's prefix."""
    prefix = f"artifacts/{tenant_id}/"
    return posixpath.normpath(key).startswith(prefix) and ".." not in key.split("/")


async def read_artifact(
    object_store: Any | None,
    *,
    tenant_id: str,
    manifest_id: str,
    artifact_id: str,
) -> str | None:
    """The full text of a spilled output, or None if it is not there.

    The tenant is the caller's own, never a path segment, so one tenant cannot
    name another's artifact however the rest of the reference is spelled.
    """
    if object_store is None or not valid_artifact_ref(manifest_id, artifact_id):
        return None
    key = artifact_key(tenant_id, manifest_id, artifact_id)
    # Checked again against the real tenant. `valid_artifact_ref` proves the reference
    # cannot climb out of *a* prefix; this is the one it actually landed in.
    if not _contained(tenant_id, key):
        return None
    raw = await object_store.get(key)
    if raw is None:
        return None
    return raw.decode("utf-8", errors="replace")


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

        # `inner` by closure, like all eight wrappers in manifests/builder.py. It used to be
        # a third parameter defaulted to `inner`, which existed only to satisfy the old
        # arity-probe-by-exception in `wrap_executor` — and made this the one wrapper whose
        # `execute` dispatched on the probe's first call, so a `TypeError` from anywhere in
        # the governance chain below re-ran the whole chain.
        async def execute(
            args: dict[str, Any],
            ctx: ToolInvocationCtx | None = None,
        ) -> ToolOutput:
            result = await inner.execute(args, ctx)
            content = tool_output_content(result)
            if len(content) <= threshold:
                return result
            artifact_id = uuid.uuid4().hex
            key = artifact_key(tenant_id, manifest_id, artifact_id)
            try:
                await object_store.put(key, content.encode("utf-8"), content_type="text/plain; charset=utf-8")
            except Exception:
                logger.debug("artifact spill failed; returning truncated output", exc_info=True)
                head = content[:preview]
                return f"{head}\n\n…[truncated {len(content) - preview} chars; artifact store write failed]"
            head = content[:preview]
            return (
                f"{head}\n\n…[artifact:{artifact_id} key={key} "
                f"chars={len(content)} spilled_at={int(time.time())}]"
            )

        return wrap_tool(tool, execute)

    return [wrap_one(t) for t in tools]


__all__ = ["apply_artifact_spill", "artifact_key", "read_artifact", "valid_artifact_ref"]
