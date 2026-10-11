"""`felix chat` — a line-at-a-time chat with one manifest on a running server.

Without `--token` it sends the token `felix login --save` kept for `--base`, if one is saved and
unexpired, and never a token saved for another server. Each line is one turn through
`FelixClient`, the SDK every other server-facing command uses: `prompt` (which waits out a
durable manifest's 202 rather than printing the envelope), or `stream` with `--stream`.
`--thread` keeps the turns in one conversation.
"""

from __future__ import annotations

import asyncio
from typing import Any

import typer


def _delta(event: dict[str, Any]) -> str:
    """The text a stream frame carries, whichever shape it arrived in."""
    text = event.get("text") or event.get("delta") or (event.get("data") or {}).get("delta") or ""
    if not text and event.get("event") in {"text_delta", "on_chat_model_stream"}:
        chunk = (event.get("data") or {}).get("chunk") or {}
        text = chunk.get("content") or ""
    return str(text)


async def _turn(client: Any, line: str, *, stream: bool) -> None:
    """One turn: printed as it streams, or as the final message once the run ends."""
    if stream:
        print("agent> ", end="", flush=True)
        async for event in client.stream(line):
            print(_delta(event), end="", flush=True)
        print()
        return
    final = (await client.prompt(line)).get("final") or {}
    content = final.get("content") if isinstance(final, dict) else final
    print(f"agent> {content}\n")


async def _repl(client: Any, *, stream: bool) -> None:
    while True:
        try:
            # In a thread: the prompt blocks, and the loop it would block is the one the turns run on.
            line = (await asyncio.to_thread(input, "you> ")).strip()
        except EOFError, KeyboardInterrupt:
            print("\nbye")
            return
        if line in {"exit", "quit"}:
            return
        if line:
            await _turn(client, line, stream=stream)


def chat(
    base: str = typer.Option("http://localhost:8080", "--base", help="The server to talk to."),
    manifest: str = typer.Option("quick", "--manifest", "-m", help="The agent: a manifest name."),
    token: str = typer.Option("", "--token", help="Bearer token; defaults to a `felix login --save` token."),
    thread: str = typer.Option("", "--thread", help="Thread id suffix, to keep turns in one conversation."),
    model: str = typer.Option("", "--model", help="Model override (allowlisted by the manifest)."),
    stream: bool = typer.Option(False, "--stream", help="Print the reply as it streams."),
) -> None:
    """Chat with a manifest on a running server, one line at a time ('exit' to quit)."""
    from felix_client import FelixClient

    # An explicit --token wins; otherwise the login saved for this --base, and never another's.
    client = FelixClient.from_login(base, api_key=token or None)
    if client.api_key and not token:
        print("using saved GitHub login")
    client.set_manifest(manifest)
    if thread:
        client.set_thread(thread)
    if model:
        client.set_model(model)

    print(f"felix chat → {base}  manifest={manifest}")
    if thread:
        print(f"thread={thread}")
    if model:
        print(f"model={model}")
    print("Type a message (or 'exit').\n")
    asyncio.run(_repl(client, stream=stream))
