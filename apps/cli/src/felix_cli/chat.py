"""`felix chat` — a line-at-a-time chat with one manifest on a running server.

Authenticated like `felix skills` and `felix ingest-docs`: `--api-key`/`FELIX_API_KEY`, else the
token `felix login --save` kept for `--url`, if one is saved and unexpired, and never a token saved
for another server. Each line is one turn through `FelixClient`: `prompt` (which waits out a
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


def _answer(result: dict[str, Any]) -> str:
    """The reply to print. A durable run that did not complete has no text worth showing: its
    status and error are the answer."""
    status = result.get("status")
    if status and status not in {"completed", "accepted"}:
        return f"[run {status}] {result.get('error') or ''}".rstrip()
    final = result.get("final") or {}
    return str(final.get("content") if isinstance(final, dict) else final)


async def _turn(client: Any, line: str, *, stream: bool) -> None:
    """One turn: printed as it streams, or as the final message once the run ends."""
    if stream:
        print("agent> ", end="", flush=True)
        async for event in client.stream(line):
            print(_delta(event), end="", flush=True)
        print()
        return
    print(f"agent> {_answer(await client.prompt(line))}\n")


def chat(
    url: str = typer.Option("http://localhost:8080", "--url", "--base", help="The Felix server."),
    manifest: str = typer.Option("quick", "--manifest", "-m", help="The agent: a manifest name."),
    api_key: str | None = typer.Option(
        None,
        "--api-key",
        "--token",
        envvar="FELIX_API_KEY",
        help="A key or token for the server. Defaults to the token `felix login --save` kept.",
    ),
    thread: str = typer.Option("", "--thread", help="Thread id suffix, to keep turns in one conversation."),
    model: str = typer.Option("", "--model", help="Model override (allowlisted by the manifest)."),
    stream: bool = typer.Option(False, "--stream", help="Print the reply as it streams."),
) -> None:
    """Chat with a manifest on a running server, one line at a time ('exit' to quit)."""
    import httpx
    from felix_client import FelixClient

    # An explicit key wins; otherwise the login saved for this --url, and never another's.
    client = FelixClient.from_login(url, api_key=api_key or None)
    if client.api_key and not api_key:
        print("using saved GitHub login")
    client.set_manifest(manifest)
    if thread:
        client.set_thread(thread)
    if model:
        client.set_model(model)

    print(f"felix chat → {url}  manifest={manifest}")
    print("Type a message (or 'exit').\n")
    # The prompt is read here, outside the loop, so Ctrl-C at `you>` ends the session at once;
    # one Runner for the session, so every turn shares its event loop.
    with asyncio.Runner() as runner:
        while True:
            try:
                line = input("you> ").strip()
            except EOFError, KeyboardInterrupt:
                print("\nbye")
                return
            if line in {"exit", "quit"}:
                return
            if not line:
                continue
            try:
                runner.run(_turn(client, line, stream=stream))
            except httpx.HTTPStatusError as exc:
                print(f"error: {exc.response.status_code} from {url}\n")
            except httpx.HTTPError as exc:
                print(f"error: could not reach {url} ({type(exc).__name__})\n")
