"""Load AGENTS.md / SYSTEM.md / instruction files from object store or local paths."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("felix.context_files")

CONTEXT_FILENAMES = ("AGENTS.md", "AGENTS.override.md", "CLAUDE.md")


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
    return data.decode("utf-8")


def _read_local(path: Path) -> str | None:
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8")
    except OSError:
        logger.debug("local context file read failed for %s", path, exc_info=True)
    return None


async def load_instruction_files(
    *,
    file_keys: list[str],
    object_store: Any | None = None,
    workspace_root: Path | str | None = None,
    tenant_id: str = "default",
) -> list[str]:
    """Load instruction file contents for ``system_prompt.files`` keys.

    Keys are tried as object-store keys (as-is, then under ``workspace/{tenant}/``),
    then as paths relative to ``workspace_root`` when provided.
    """
    parts: list[str] = []
    root = Path(workspace_root) if workspace_root else None
    for key in file_keys:
        text = await _get_text(object_store, key)
        if text is None:
            text = await _get_text(object_store, f"workspace/{tenant_id}/{key}")
        if text is None and root is not None:
            text = _read_local(root / key)
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
    # Prefer override
    for name in ("AGENTS.override.md", "AGENTS.md", "CLAUDE.md"):
        text = await _get_text(object_store, name)
        if text is None:
            text = await _get_text(object_store, f"workspace/{tenant_id}/{name}")
        if text is None and root is not None:
            text = _read_local(root / name)
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
    text = await _get_text(object_store, key)
    if text is None:
        text = await _get_text(object_store, f"workspace/{tenant_id}/{key}")
    if text is None and workspace_root:
        text = _read_local(Path(workspace_root) / key)
    return text.strip() if text else None


__all__ = [
    "CONTEXT_FILENAMES",
    "load_agents_md_layer",
    "load_instruction_files",
    "load_system_md",
]
