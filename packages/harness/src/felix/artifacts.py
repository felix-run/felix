"""Spill large tool outputs to the object store, and read them back.

The spill has always worked; nothing ever read it. `object_store.get` appeared
nowhere in the harness, so an oversized tool result was written to a store no
route, CLI command or client method could reach — the model saw a preview and a
marker naming an object that could not be fetched by anyone.

The HTTP route fixed that for clients and left the model where it was: holding a
200-character preview of a file it had asked to read, with no call that could fetch
the rest. `read_artifact` is that call. Without it, turning the spill on saves tokens
by discarding what the agent needed, which is why no bundled manifest enabled it.
"""

from __future__ import annotations

import logging
import posixpath
import re
import time
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from felix.manifests.schema import ArtifactsSpec
from felix.tools.errors import tool_error_output
from felix.tools.executor import wrap_tool
from felix.tools.types import Tool, ToolInvocationCtx, ToolOutput, define_tool, tool_output_content

logger = logging.getLogger("felix.artifacts")

READ_ARTIFACT_TOOL = "read_artifact"
# How the spill recognises the reader. By `source` rather than name, so a manifest's own
# tool that happens to be called `read_artifact` is still spilled like any other.
_READER_SOURCE = "artifacts"

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
        # The reader is exempt. Its window may be larger than the threshold, and a window
        # that spilled would hand the model a new preview of the text it had just asked to
        # see — a loop, not a read.
        if tool.source == _READER_SOURCE:
            return tool
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
                f"chars={len(content)} spilled_at={int(time.time())}] "
                f'Call {READ_ARTIFACT_TOOL}(artifact_id="{artifact_id}", offset={len(head)}) to read on.'
            )

        return wrap_tool(tool, execute)

    return [wrap_one(t) for t in tools]


class ReadArtifactArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str = Field(
        description="The id from an `[artifact:<id> …]` marker in an earlier tool result."
    )
    offset: int = Field(default=0, ge=0, description="Character to start from.")
    length: int | None = Field(
        default=None,
        ge=1,
        description="How many characters to return. Omit for the default window; larger requests are capped.",
    )


def make_read_artifact_tool(
    spec: ArtifactsSpec,
    *,
    object_store: Any,
    tenant_id: str,
    manifest_id: str,
) -> Tool:
    """The model's way back to a spilled output, one window at a time.

    Tenant and manifest are fixed at build time, like the spill that wrote the object, so
    the model names only the id. It cannot reach another tenant's artifact or another
    manifest's by any spelling of it — the same guarantee the HTTP route gets from
    credentials, here from never taking the other two segments as input at all.
    """
    default_window = spec.default_window_chars
    max_window = spec.max_window_chars

    async def handler(args: ReadArtifactArgs, _ctx: ToolInvocationCtx | None = None) -> ToolOutput:
        content = await read_artifact(
            object_store, tenant_id=tenant_id, manifest_id=manifest_id, artifact_id=args.artifact_id
        )
        if content is None:
            return tool_error_output("invalid_arguments", f"no artifact {args.artifact_id!r}")
        total = len(content)
        start = min(args.offset, total)
        end = min(start + min(args.length or default_window, max_window), total)
        # Not `[artifact:` — that prefix is the spill marker, and a transcript scanned for
        # spills should not also match every window read back from one.
        header = f"[artifact-window:{args.artifact_id} chars {start}-{end} of {total}"
        header += f"; continue at offset={end}]" if end < total else "; end]"
        return f"{header}\n{content[start:end]}"

    return define_tool(
        name=READ_ARTIFACT_TOOL,
        description=(
            "Read part of a tool result that was too large to show inline. Such a result ends in "
            "an `[artifact:<id> …]` marker; pass that id, and an offset to page through it."
        ),
        args=ReadArtifactArgs,
        handler=handler,
        source=_READER_SOURCE,
        replay_safe=True,
    )


__all__ = [
    "READ_ARTIFACT_TOOL",
    "apply_artifact_spill",
    "artifact_key",
    "make_read_artifact_tool",
    "read_artifact",
    "valid_artifact_ref",
]
