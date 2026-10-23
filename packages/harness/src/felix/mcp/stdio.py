"""MCP over stdio: the SDK's `stdio_client`, spawned only under Felix's stdio policy.

The SDK owns the transport -- newline-delimited JSON-RPC over the child's stdin/stdout, the
handshake, and terminating the process. Felix owns whether the process may exist at all: the
command must be on `FELIX_MCP_STDIO_ALLOWED_COMMANDS`, checked here because this is the one place
the process is created, and the child gets a scrubbed environment, never the API's own.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, TextIO

from mcp.client.stdio import StdioServerParameters, stdio_client

from felix.manifests.schema import McpServerRef
from felix.security.stdio_policy import git_disarmed, resolve_stdio_command, stdio_child_env


def stdio_parameters(ref: McpServerRef, *, settings: Any | None = None) -> StdioServerParameters:
    """What the SDK spawns for ``ref``, after the allowlist has said it may.

    The command is spawned by the absolute path the allowlisted name resolves to on the API's
    own PATH, and the child's PATH is the API's too (a manifest may not set it), so neither the
    binary nor an interpreter it starts through `/usr/bin/env` comes from a directory the
    manifest chose. The SDK merges its own short list of inherited variables (`HOME`, `USER` and
    the like, none of them credentials) under ``env``; the API's model keys, database URL and
    cloud credentials are in neither.
    """
    if not ref.command:
        raise RuntimeError("stdio MCP ref has no command")
    # Defense in depth: the manifest was already checked on write and at compile.
    if settings is None:
        from felix.config import get_settings

        settings = get_settings()
    command = resolve_stdio_command(ref.command, settings)
    return StdioServerParameters(
        command=command,
        args=list(ref.args),
        # Git disarmed as the shell tool's is: a server pointed at a repository runs whatever that
        # repository's `.git/config` names (`core.fsmonitor`, hooks) on its first `git status`.
        env=git_disarmed(stdio_child_env(ref.env)),
        cwd=ref.cwd or None,
    )


def _devnull() -> TextIO:
    return open(os.devnull, "w", encoding="utf-8")


@asynccontextmanager
async def stdio_transport(ref: McpServerRef, *, settings: Any | None = None) -> AsyncIterator[Any]:
    """The SDK transport for ``ref``: the child's streams, closed and the process reaped on exit.

    The child's stderr goes nowhere, as it always has: it is the server's to write, and an
    operator's log is not the place for whatever a manifest-named process prints.
    """
    params = stdio_parameters(ref, settings=settings)
    errlog = await asyncio.to_thread(_devnull)
    try:
        async with stdio_client(params, errlog=errlog) as streams:
            yield streams
    finally:
        errlog.close()


__all__ = ["stdio_parameters", "stdio_transport"]
