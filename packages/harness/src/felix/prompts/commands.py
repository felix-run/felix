"""Slash commands: a user turn written `/name args` runs the manifest's prompt `name`.

`spec.prompts` were reachable only by a request's `template` field, which a person typing into a
client has no way to set. A user turn whose text is `/review auth.py perf` now expands as the
prompt `review` would with `template_args: ["auth.py", "perf"]`, in place of the turn.

Only a declared name is a command. Anything else that starts with a slash -- `/etc/hosts is
missing`, `/shrug` -- is sent as written, so a manifest with no prompts behaves as it always did.
Arguments split like a shell's (`"two words"` is one), falling back to whitespace when the quoting
does not close. The expansion is the user's own turn, so a route expands it *before* inbound
screening: it is screened, hooked and stored as that, exactly as the text the person would
otherwise have pasted.
"""

from __future__ import annotations

import dataclasses
import re
import shlex
from typing import TYPE_CHECKING, Any

from felix.prompts.templates import (
    PromptUnavailable,
    expand_template,
    find_prompt_spec,
    resolve_prompt_body,
)

if TYPE_CHECKING:
    from felix_ai.types import ChatMessage

    from felix.manifests.schema import PromptTemplateSpec

# A name a person can type after the slash: no spaces, and nothing a path would carry.
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_COMMAND_RE = re.compile(rf"^/({_NAME.pattern})(?:\s+(.*))?$", re.DOTALL)
# `shlex` builds each token a character at a time, quadratic in its length: a megabyte-long
# argument held the event loop for seconds. Past this, arguments split on whitespace only.
MAX_SHELL_SPLIT_CHARS = 16_000
# Expansions per request. A stateless `/v1` client resends every earlier command each turn; a body
# padded with thousands of them would otherwise multiply the prompt body by as many.
MAX_COMMANDS_PER_REQUEST = 16


def parse_command(text: str) -> tuple[str, str] | None:
    """`(name, rest)` for text written as a command, else `None`. Says nothing about whether the
    manifest declares `name`, and does not split `rest`: that waits until it does."""
    match = _COMMAND_RE.match(text.strip())
    if match is None:
        return None
    return match.group(1), (match.group(2) or "").strip()


def split_args(rest: str) -> list[str]:
    """A command's arguments: shell-style, else on whitespace when the quoting does not close or
    the text is too long to tokenise cheaply."""
    if len(rest) > MAX_SHELL_SPLIT_CHARS:
        return rest.split()
    try:
        return shlex.split(rest)
    except ValueError:
        return rest.split()


def runnable_commands(manifest: Any) -> list[PromptTemplateSpec]:
    """The manifest's prompts a person can run as `/name`: those whose name they can type."""
    prompts = getattr(getattr(manifest, "spec", None), "prompts", None) or []
    return [p for p in prompts if _NAME.fullmatch(p.name)]


async def expand_commands(
    manifest: Any,
    messages: list[ChatMessage],
    *,
    settings: Any,
    tenant_id: str,
    object_store: Any | None = None,
) -> list[ChatMessage]:
    """`messages` with every user turn written as a declared command replaced by its expansion.

    Every turn, not only the newest: a `/v1` client resends its history, and an earlier command
    left raw would sit in front of the answer to its expansion. Each prompt body is read once per
    call. A turn sent as content parts is left alone: the command would have to say which part it
    replaces. `PromptUnavailable` when a declared prompt expands to nothing, or past
    `MAX_COMMANDS_PER_REQUEST`.
    """
    bodies: dict[str, str] = {}
    expanded = 0
    out = []
    for message in messages:
        command = parse_command(message.content) if _is_user_text(message) else None
        spec = find_prompt_spec(manifest, command[0]) if command else None
        if command is None or spec is None:
            out.append(message)
            continue
        expanded += 1
        if expanded > MAX_COMMANDS_PER_REQUEST:
            raise PromptUnavailable(f"too_many_commands:{MAX_COMMANDS_PER_REQUEST}")
        if spec.name not in bodies:
            bodies[spec.name] = await resolve_prompt_body(
                spec,
                object_store=object_store,
                workspace_root=getattr(settings, "workspace_root", None),
                tenant_id=tenant_id,
            )
        if not bodies[spec.name]:
            raise PromptUnavailable(f"empty_prompt:{command[0]}")
        text = expand_template(bodies[spec.name], split_args(command[1]))
        out.append(dataclasses.replace(message, content=text))
    return out


def _is_user_text(message: ChatMessage) -> bool:
    return message.role == "user" and message.content_blocks is None


__all__ = [
    "MAX_COMMANDS_PER_REQUEST",
    "MAX_SHELL_SPLIT_CHARS",
    "expand_commands",
    "parse_command",
    "runnable_commands",
    "split_args",
]
