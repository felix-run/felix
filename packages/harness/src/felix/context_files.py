"""Load AGENTS.md / SYSTEM.md / instruction files from object store or local paths."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger("felix.context_files")

CONTEXT_FILENAMES = ("AGENTS.md", "AGENTS.override.md", "CLAUDE.md")

# Discovery order for the AGENTS layer — override wins, which is not the order
# CONTEXT_FILENAMES happens to be written in.
_AGENTS_MD_PRECEDENCE = ("AGENTS.override.md", "AGENTS.md", "CLAUDE.md")

# A local context file is read on every agent build, from a directory the agent's own code can
# write, and all of it goes into the system prompt. 256 KB is where `search_files` stops reading
# a file, and far past any instruction file worth sending a model every turn. A file over it is
# ignored with a warning rather than cut short: half an instruction file is a different one.
MAX_CONTEXT_FILE_BYTES = 256_000


async def _get_text(store: Any | None, key: str) -> str | None:
    if store is None:
        return None
    try:
        data = await store.get(key)
    except Exception:
        logger.debug("context file get failed for %s", key, exc_info=True)
        return None
    if not data:
        return None
    # Decode inside the guard: this used to raise UnicodeDecodeError out through
    # `build_agent`, so one bad object broke every chat request for that tenant.
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        logger.warning("context file %s is not valid utf-8; ignored", key)
        return None


def _read_local(root: Path, key: str) -> str | None:
    """Read ``key`` from under ``root``, or None if it escapes.

    This took a prebuilt `root / key`, which is not containment: `Path("/srv/ws") /
    "/etc/passwd"` is `/etc/passwd`, and `../` was never normalised. The keys are
    manifest-supplied strings, so the workspace tools' own walk is the right gate — same
    rule, one implementation, and the same refusal of a symlink at any component, since the
    workspace is writable by the code the agent runs.
    """
    from felix.tools.workspace import open_regular, open_workspace_parent

    try:
        with open_workspace_parent(root, key) as (parent, leaf, rel):
            if leaf is None:
                return None
            fd = open_regular(parent, leaf, os.O_RDONLY, rel)
            with os.fdopen(fd, "rb") as fh:
                # The fstat refuses without reading; the bounded read, one byte past the cap,
                # refuses a file that grew after it.
                too_big = os.fstat(fd).st_size > MAX_CONTEXT_FILE_BYTES
                data = b"" if too_big else fh.read(MAX_CONTEXT_FILE_BYTES + 1)
    except ValueError:
        logger.warning("context file key %r escapes the workspace root or is not a file; ignored", key)
        return None
    except OSError:
        logger.debug("local context file read failed for %s", key, exc_info=True)
        return None
    if too_big or len(data) > MAX_CONTEXT_FILE_BYTES:
        logger.warning("context file %s exceeds %d bytes; ignored", key, MAX_CONTEXT_FILE_BYTES)
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        logger.warning("context file %s is not valid utf-8; ignored", key)
        return None


def _tenant_key(tenant_id: str, key: str) -> str:
    """Rewrite a manifest-supplied key into the caller's own workspace prefix.

    The loaders used to try `key` as-is first and fall back to this. That let a
    manifest name `workspace/<other-tenant>/notes.md` and have the contents read
    into its system prompt, because the as-is attempt hit. `system_prompt.files`,
    `system_md` and `append_system_md` are unvalidated strings controlled by anyone
    with `manifests:write`, so the prefix is applied rather than offered.
    """
    if not tenant_id or "/" in tenant_id or tenant_id in {".", ".."}:
        # The prefix is the whole boundary; an empty or multi-segment tenant collapses
        # it. Unreachable from HTTP today (auth coerces to a real tenant), but the
        # `or "default"` chains upstream are one refactor from reintroducing it.
        raise ValueError(f"tenant_id {tenant_id!r} is not a single path segment")
    segments = [seg for seg in key.strip("/").split("/") if seg]
    if not segments or any(seg in {".", ".."} for seg in segments):
        # Normalising `..` away would silently change which object is meant, so a
        # traversal segment is refused instead. `FilesystemObjectStore._path` rejects
        # these too, and S3/GCS treat them as literal key text — but the guarantee
        # here must not depend on which backend an operator configured.
        raise ValueError(f"context file key {key!r} is not a plain key")

    prefix = ["workspace", tenant_id]
    if segments[: len(prefix)] == prefix:
        return "/".join(segments)
    return "/".join(prefix + segments)


async def load_instruction_files(
    *,
    file_keys: list[str],
    object_store: Any | None = None,
    workspace_root: Path | str | None = None,
    tenant_id: str = "default",
) -> list[str]:
    """Load instruction file contents for ``system_prompt.files`` keys.

    Keys resolve only under ``workspace/{tenant}/`` in the object store, then under
    ``workspace_root`` when provided. Neither accepts a key that points outside the
    caller's own tenant.
    """
    parts: list[str] = []
    root = Path(workspace_root) if workspace_root else None
    for key in file_keys:
        try:
            scoped = _tenant_key(tenant_id, key)
        except ValueError:
            logger.warning("context file key %r rejected; ignored", key)
            continue
        text = await _get_text(object_store, scoped)
        if text is None and root is not None:
            # The *scoped* key, not the raw one. Scoping only the object-store lookup
            # left the local fallback as an unscoped read of the whole workspace root:
            # `files: ["workspace/victim/x"]` missed in the store and then read the
            # victim's file off disk, and `files: [".env"]` put credentials into the
            # system prompt, which secret masking never sees (it wraps tool output).
            text = _read_local(root, scoped)
        if text:
            parts.append(f"[context:{key}]\n{text.strip()}")
    return parts


async def load_agents_md_layer(
    *,
    object_store: Any | None = None,
    workspace_root: Path | str | None = None,
    tenant_id: str = "default",
    enabled: bool = True,
) -> str:
    """Discover AGENTS.md / CLAUDE.md (override wins) from store or workspace root."""
    if not enabled:
        return ""
    root = Path(workspace_root) if workspace_root else None
    # Two passes, not tenant-then-shared per name. Interleaving them meant a shared
    # `AGENTS.override.md` beat a tenant's own `AGENTS.md`, because override is
    # earlier in the tuple — the tenant's file was never consulted.
    for name in _AGENTS_MD_PRECEDENCE:
        text = await _get_text(object_store, f"workspace/{tenant_id}/{name}")
        if text is None and root is not None:
            text = _read_local(root, f"workspace/{tenant_id}/{name}")
        if text:
            return f"[context:{name}]\n{text.strip()}"

    # Only then the shared operator layer, and only in the object store. These names
    # are fixed rather than manifest-supplied, and no route lets a tenant write a
    # bare object key — `artifacts.py` is the harness's only writer and it prefixes
    # every key. The local root is deliberately excluded here: it is one shared
    # directory with no tenant component, so any tenant whose manifest binds
    # `write_file` could drop an `AGENTS.override.md` into another tenant's system
    # prompt. A shared layer on disk needs a directory agents cannot write to.
    for name in _AGENTS_MD_PRECEDENCE:
        text = await _get_text(object_store, name)
        if text:
            return f"[context:{name}]\n{text.strip()}"
    return ""


async def load_system_md(
    key: str | None,
    *,
    object_store: Any | None = None,
    workspace_root: Path | str | None = None,
    tenant_id: str = "default",
) -> str | None:
    if not key:
        return None
    try:
        scoped = _tenant_key(tenant_id, key)
    except ValueError:
        logger.warning("system_md key %r rejected; ignored", key)
        return None
    text = await _get_text(object_store, scoped)
    if text is None and workspace_root:
        text = _read_local(Path(workspace_root), scoped)
    return text.strip() if text else None


__all__ = [
    "CONTEXT_FILENAMES",
    "load_agents_md_layer",
    "load_instruction_files",
    "load_system_md",
]
