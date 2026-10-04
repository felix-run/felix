"""`felix skills` — browse a GitHub repository's skills and import one into a server's library.

Both talk to a running server (`/skill-library/-/browse`, `/skill-library/-/import`) through
`FelixClient`, authenticated the way `felix ingest-docs` is: `--api-key`/`FELIX_API_KEY`, else the
token `felix login --save` kept for that server. The server does the fetching -- pinned to one
commit, through its egress guard, within its `FELIX_SKILL_IMPORT_SOURCES` -- so nothing here
reaches GitHub.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import typer

skills_app = typer.Typer(
    name="skills", help="Browse and import Agent Skills from GitHub.", no_args_is_help=True
)

_URL = typer.Option("http://localhost:8080", "--url", help="The Felix server.")
_API_KEY = typer.Option(
    None,
    "--api-key",
    envvar="FELIX_API_KEY",
    help="A key or token with skills:read (browse) or skills:write (add). "
    "Defaults to the token `felix login --save` kept.",
)
_REF = typer.Option(None, "--ref", help="Branch, tag or commit; the repository's default branch if omitted.")


def _call(url: str, api_key: str | None, call: Callable[[Any], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    """Run one client call, turning a refusal into its code and message on stderr and exit 1."""
    import httpx
    from felix_client import FelixClient

    client = FelixClient.from_login(url, api_key=api_key)
    try:
        return asyncio.run(call(client))
    except httpx.HTTPStatusError as exc:
        try:
            body = exc.response.json()
        except ValueError:
            body = {}
        code = body.get("error") if isinstance(body, dict) else None
        message = body.get("message") if isinstance(body, dict) else None
        typer.echo(f"{code or exc.response.status_code}: {message or exc.response.text[:200]}", err=True)
        raise typer.Exit(1) from exc
    except httpx.HTTPError as exc:
        typer.echo(f"could not reach {url}: {type(exc).__name__}", err=True)
        raise typer.Exit(1) from exc


@skills_app.command("browse")
def browse_cmd(
    source: str = typer.Argument(..., help="github:owner/repo[/path]"),
    ref: str | None = _REF,
    url: str = _URL,
    api_key: str | None = _API_KEY,
) -> None:
    """List the skills a GitHub repository offers, each with the source to `felix skills add` it by."""
    listing = _call(url, api_key, lambda c: c.browse_skills(source, ref=ref))
    typer.echo(
        f"{listing['source']} @ {listing['ref']} ({listing['commit'][:12]}), "
        f"license {listing.get('license') or 'not declared'}",
        err=True,
    )
    for item in listing["items"]:
        description = " ".join(str(item.get("description") or "").split())
        waiting = ""
        if not item.get("eligible", True) and item.get("eligible_at"):
            from datetime import UTC, datetime

            when = datetime.fromtimestamp(item["eligible_at"] / 1000, tz=UTC).date().isoformat()
            waiting = f"\t(too recent to import until {when})"
        typer.echo(f"{item['source']}\t{item['name']}\t{description[:100]}{waiting}")
    if listing.get("truncated"):
        typer.echo("…more skills than one listing holds; name a path to narrow it.", err=True)
    if not listing["items"]:
        typer.echo("no skills found", err=True)


@skills_app.command("add")
def add_cmd(
    source: str = typer.Argument(..., help="github:owner/repo/path — the directory holding SKILL.md"),
    ref: str | None = _REF,
    publish: bool = typer.Option(
        False,
        "--publish",
        help="Also publish it, through the same gate as any publish; the draft stays if refused.",
    ),
    url: str = _URL,
    api_key: str | None = _API_KEY,
) -> None:
    """Import one skill into the server's library as a draft, pinned to the commit `--ref` names."""
    row = _call(url, api_key, lambda c: c.import_skill(source, ref=ref, publish=publish))
    where = f"{row.get('origin_source')} @ {str(row.get('origin_commit') or '')[:12]}"
    if row.get("unchanged"):
        typer.echo(f"{row['name']}@{row['version']} is already {where}; nothing saved.")
        return
    typer.echo(f"{row['name']}@{row['version']} saved as a draft from {where}.")
    for path in row.get("dropped_files") or []:
        typer.echo(f"dropped {path}", err=True)
    if row.get("published"):
        typer.echo(f"{row['name']}@{row['version']} is live.")
    elif publish:
        blocked = row.get("publish_blocked") or {}
        typer.echo(f"not published ({blocked.get('error')}): {blocked.get('message')}", err=True)
        raise typer.Exit(1)


__all__ = ["skills_app"]
