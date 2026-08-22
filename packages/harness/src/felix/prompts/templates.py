"""Prompt template expansion — ``$1``, ``$@``, ``${1:-default}``."""

from __future__ import annotations

import re
from typing import Any

# ${n:-default} | $n | $@
_TOKEN_RE = re.compile(r"\$\{(\d+)(?::-([^}]*))?\}|\$(\d+)|\$@")


def expand_template(body: str, args: list[str] | None = None) -> str:
    """Expand positional placeholders in a prompt template body."""
    args = list(args or [])

    def _repl(match: re.Match[str]) -> str:
        whole = match.group(0)
        if whole == "$@":
            return " ".join(args)
        if match.group(3) is not None:
            idx = int(match.group(3)) - 1
            return args[idx] if 0 <= idx < len(args) else ""
        idx = int(match.group(1)) - 1
        default = match.group(2) if match.group(2) is not None else ""
        if 0 <= idx < len(args) and args[idx] != "":
            return args[idx]
        return default

    return _TOKEN_RE.sub(_repl, body)


def find_prompt_spec(manifest: Any, name: str) -> Any | None:
    """Return a PromptTemplateSpec-like object from ``spec.prompts`` by name."""
    if not name:
        return None
    spec = getattr(manifest, "spec", None)
    prompts = getattr(spec, "prompts", None) or []
    needle = name.strip().lower()
    for p in prompts:
        if str(getattr(p, "name", "") or "").strip().lower() == needle:
            return p
    return None


async def resolve_prompt_body(
    prompt_spec: Any,
    *,
    object_store: Any | None = None,
    workspace_root: Any | None = None,
    tenant_id: str = "default",
) -> str:
    """Resolve inline body or load from ``file`` key (object store / workspace)."""
    body = str(getattr(prompt_spec, "body", "") or "")
    file_key = getattr(prompt_spec, "file", None)
    if body and not file_key:
        return body
    if file_key:
        from felix.context_files import load_instruction_files

        parts = await load_instruction_files(
            file_keys=[str(file_key)],
            object_store=object_store,
            workspace_root=workspace_root,
            tenant_id=tenant_id,
        )
        if parts:
            # Strip the [context:key] prefix wrapper for template bodies.
            text = parts[0]
            nl = text.find("\n")
            return text[nl + 1 :] if nl >= 0 else text
    return body


async def expand_named_prompt(
    manifest: Any,
    name: str,
    args: list[str] | None = None,
    *,
    object_store: Any | None = None,
    workspace_root: Any | None = None,
    tenant_id: str = "default",
) -> str:
    """Look up a named prompt on the manifest and expand it."""
    prompt_spec = find_prompt_spec(manifest, name)
    if prompt_spec is None:
        raise LookupError(f"unknown_prompt:{name}")
    body = await resolve_prompt_body(
        prompt_spec,
        object_store=object_store,
        workspace_root=workspace_root,
        tenant_id=tenant_id,
    )
    if not body:
        raise LookupError(f"empty_prompt:{name}")
    return expand_template(body, args)


__all__ = [
    "expand_named_prompt",
    "expand_template",
    "find_prompt_spec",
    "resolve_prompt_body",
]
