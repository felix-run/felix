"""Spill large tool outputs to the object store, and read them back.

The spill has always worked; nothing ever read it. `object_store.get` appeared
nowhere in the harness, so an oversized tool result was written to a store no
route, CLI command or client method could reach — the model saw a preview and a
marker naming an object that could not be fetched by anyone.

The HTTP route fixed that for clients and left the model where it was: holding a
200-character preview of a file it had asked to read, with no call that could fetch
the rest. `read_artifact` is that call. Without it, turning the spill on saves tokens
by discarding what the agent needed, which is why no bundled manifest enabled it.

Nothing collected the prefix either, and once five bundled manifests spilled by default
that stopped being an opt-in cost. The `ObjectStore` Protocol has no `list`, so each spill
is recorded in a ledger beside the bytes (`ArtifactRow`, the same shape as uploads'), and
the nightly retention sweep drops what is older than `FELIX_ARTIFACT_RETENTION_DAYS`.
"""

from __future__ import annotations

import logging
import posixpath
import re
import secrets
import time
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete as sa_delete
from sqlalchemy import select as sa_select

from felix.config import Settings
from felix.db.session import _use_memory
from felix.logging_setup import loggable
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


def _owner_key(tenant_id: str, manifest_id: str, artifact_id: str) -> str:
    """Which conversation spilled it, beside the object itself. Read only by the model's tool."""
    return f"artifacts/{tenant_id}/{manifest_id}/{artifact_id}.owner"


def _conversation(ctx: ToolInvocationCtx | None) -> str | None:
    """The conversation a tool call belongs to, as the spill and the reader both name it.

    The key scopes an artifact to a tenant and a manifest, and nothing narrower: every caller
    of a manifest shares its prefix, so an id alone would reach another user's run. The HTTP
    route answers that with `artifacts:read`; the model has no scope to check, so its reader is
    held to the conversation instead — a read is exactly as private as the history it came
    from. A turn with no thread still has a request, and a token kept on that request's
    context lets it read what it spilled itself and nothing else.
    """
    from felix.context import try_get_context

    if ctx is not None and ctx.thread_id:
        return f"thread:{ctx.thread_id}"
    request = try_get_context()
    if request is None:
        return None
    if request.thread_id:
        return f"thread:{request.thread_id}"
    return "request:" + request.extras.setdefault("artifact_request_token", secrets.token_hex(16))


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


# --- the ledger -------------------------------------------------------------------------

#: `(tenant_id, manifest_id, artifact_id) -> row`, the in-memory twin of `artifacts`.
_ledger_rows: dict[tuple[str, str, str], dict[str, Any]] = {}


def now_ms() -> int:
    """Wall clock in epoch ms, as a module attribute so the retention tests can hold it still."""
    return int(time.time() * 1000)


def clear_memory_ledger() -> None:
    """Drop the in-memory ledger. Test seam, matching the other `memory://` stores."""
    _ledger_rows.clear()


async def record_artifact(
    settings: Settings, *, tenant_id: str, manifest_id: str, artifact_id: str, size_bytes: int
) -> None:
    """Note that a spill exists, so the sweep can find it. Written before the bytes are."""
    row = {
        "tenant_id": tenant_id,
        "manifest_id": manifest_id,
        "artifact_id": artifact_id,
        "size_bytes": int(size_bytes),
        "created_at": now_ms(),
    }
    if _use_memory(settings):
        _ledger_rows[(tenant_id, manifest_id, artifact_id)] = row
        return

    from felix.db.models import ArtifactRow
    from felix.db.session import tenant_session

    async with tenant_session(settings, tenant_id) as db:
        await db.merge(ArtifactRow(**row))
        await db.commit()


async def forget_artifact(settings: Settings, *, tenant_id: str, manifest_id: str, artifact_id: str) -> None:
    """Drop the ledger row for one spill. Idempotent."""
    if _use_memory(settings):
        _ledger_rows.pop((tenant_id, manifest_id, artifact_id), None)
        return

    from felix.db.models import ArtifactRow
    from felix.db.session import tenant_session

    async with tenant_session(settings, tenant_id) as db:
        await db.execute(
            sa_delete(ArtifactRow).where(
                ArtifactRow.tenant_id == tenant_id,
                ArtifactRow.manifest_id == manifest_id,
                ArtifactRow.artifact_id == artifact_id,
            )
        )
        await db.commit()


async def expired_artifacts(
    settings: Settings, *, older_than_ms: int, limit: int = 1000
) -> list[tuple[str, str, str]]:
    """`(tenant_id, manifest_id, artifact_id)` for spills older than a cutoff, across tenants.

    Crosses tenants because the sweep does, so it reads under `rls_bypass`; the delete
    re-derives each key under the tenant it is handed, as `expired_attachments` does.
    """
    if _use_memory(settings):
        return [
            key
            for key, r in sorted(_ledger_rows.items(), key=lambda kv: kv[1]["created_at"])
            if int(r["created_at"]) < older_than_ms
        ][:limit]

    from felix.db.models import ArtifactRow
    from felix.db.session import get_session_factory, rls_bypass

    factory = get_session_factory(settings=settings)
    with rls_bypass():
        async with factory() as db:
            rows = await db.execute(
                sa_select(ArtifactRow.tenant_id, ArtifactRow.manifest_id, ArtifactRow.artifact_id)
                .where(ArtifactRow.created_at < older_than_ms)
                .order_by(ArtifactRow.created_at)
                .limit(limit)
            )
            return [(str(t), str(m), str(a)) for t, m, a in rows.all()]


async def delete_artifact(
    object_store: Any | None, *, tenant_id: str, manifest_id: str, artifact_id: str, settings: Settings
) -> bool:
    """Remove one spill — its text, its owner record, then its ledger row.

    Bytes before the row, the mirror of the write: interrupted here, the row outlives its
    objects and the next sweep deletes absent keys, which is a no-op on every backend.
    Dropping the row first would leave objects nothing can name.
    """
    if object_store is None:
        return False
    text_key = artifact_key(tenant_id, manifest_id, artifact_id)
    if not valid_artifact_ref(manifest_id, artifact_id) or not _contained(tenant_id, text_key):
        # A row no valid key can come from names no object, so there is nothing to delete and
        # the row goes. Kept, it would head every sweep batch forever: the ledger reads oldest
        # first, and a row that is never deleted never stops being oldest.
        await forget_artifact(settings, tenant_id=tenant_id, manifest_id=manifest_id, artifact_id=artifact_id)
        return False
    try:
        await object_store.delete(text_key)
        await object_store.delete(_owner_key(tenant_id, manifest_id, artifact_id))
    except Exception:
        logger.warning("artifact delete failed id=%s", loggable(artifact_id, limit=64), exc_info=True)
        return False
    await forget_artifact(settings, tenant_id=tenant_id, manifest_id=manifest_id, artifact_id=artifact_id)
    return True


# --- the spill -------------------------------------------------------------------------


def apply_artifact_spill(
    tools: list[Tool],
    spec: ArtifactsSpec,
    *,
    object_store: Any | None,
    tenant_id: str,
    manifest_id: str,
    settings: Settings,
) -> list[Tool]:
    """Wrap tools so oversized outputs are stored and replaced with a preview.

    `settings` is required rather than defaulted: it is where the ledger lives, and a spill
    with no ledger row is bytes the sweep can never find.
    """
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
            encoded = content.encode("utf-8")
            try:
                # Row first, so an interruption leaves a row whose bytes may not exist (the
                # sweep deletes absent keys harmlessly) rather than bytes no row names.
                await record_artifact(
                    settings,
                    tenant_id=tenant_id,
                    manifest_id=manifest_id,
                    artifact_id=artifact_id,
                    size_bytes=len(encoded),
                )
                await object_store.put(key, encoded, content_type="text/plain; charset=utf-8")
                owner = _conversation(ctx)
                if owner is not None:
                    await object_store.put(
                        _owner_key(tenant_id, manifest_id, artifact_id),
                        owner.encode("utf-8"),
                        content_type="text/plain; charset=utf-8",
                    )
            except Exception:
                logger.debug("artifact spill failed; returning truncated output", exc_info=True)
                head = content[:preview]
                return f"{head}\n\n…[truncated {len(content) - preview} chars; artifact store write failed]"
            head = content[:preview]
            # The marker must stay the last thing in the output, in exactly this shape:
            # clients (`felix-protocol`'s `parseArtifactMarker`, the terminal's `/artifact`)
            # anchor it to the end and take everything before it as the preview. How to
            # read on is in `read_artifact`'s description, not appended here.
            return (
                f"{head}\n\n…[artifact:{artifact_id} key={key} "
                f"chars={len(content)} spilled_at={int(time.time())}]"
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


async def _spilled_here(
    object_store: Any, tenant_id: str, manifest_id: str, artifact_id: str, ctx: ToolInvocationCtx | None
) -> bool:
    """Whether this conversation spilled the artifact. Validated before any key is built."""
    conversation = _conversation(ctx)
    if conversation is None or not valid_artifact_ref(manifest_id, artifact_id):
        return False
    try:
        recorded = await object_store.get(_owner_key(tenant_id, manifest_id, artifact_id))
    except Exception:
        logger.debug("artifact owner read failed", exc_info=True)
        return False
    return recorded is not None and recorded.decode("utf-8", errors="replace") == conversation


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

    Within that, it reads only what this conversation spilled (`_conversation`). An
    artifact of another thread, or one spilled with no conversation to record, is reported
    exactly as a missing one, so the answer does not say whether the id exists.
    """
    default_window = spec.default_window_chars
    max_window = spec.max_window_chars

    async def handler(args: ReadArtifactArgs, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
        content = None
        if await _spilled_here(object_store, tenant_id, manifest_id, args.artifact_id, ctx):
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
    "clear_memory_ledger",
    "delete_artifact",
    "expired_artifacts",
    "forget_artifact",
    "make_read_artifact_tool",
    "read_artifact",
    "record_artifact",
    "valid_artifact_ref",
]
