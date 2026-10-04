"""`felix skills` — browse a GitHub repository's skills and import one into a server's library.

Both talk to a running server (`/skill-library/-/browse`, `/skill-library/-/import`) through
`FelixClient`, authenticated the way `felix ingest-docs` is: `--api-key`/`FELIX_API_KEY`, else the
token `felix login --save` kept for that server. The server does the fetching -- pinned to one
commit, through its egress guard, within its `FELIX_SKILL_IMPORT_SOURCES` -- so nothing here
reaches GitHub. An import is a draft: this prints how to publish it once someone has read it.

Every string that came from a repository is stripped of control characters before it reaches the
terminal: a skill's name or description is the third party's text, and an escape sequence in it
would otherwise be the third party's control of the operator's terminal.
"""

from __future__ import annotations

import asyncio
import re
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

# C0 (but tab), DEL and C1: ESC opens every terminal escape sequence, and C1's CSI (U+009B) is one
# on its own in terminals that read 8-bit controls.
_CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f-\x9f]")


def clean(value: Any) -> str:
    """``value`` as text with every control character removed; tabs become spaces."""
    return _CONTROL.sub("", str(value if value is not None else "")).replace("\t", " ")


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
        typer.echo(
            f"{clean(code or exc.response.status_code)}: {clean(message or exc.response.text[:200])}",
            err=True,
        )
        raise typer.Exit(1) from exc
    except httpx.HTTPError as exc:
        typer.echo(f"could not reach {url}: {type(exc).__name__}", err=True)
        raise typer.Exit(1) from exc


def _date(ms: int) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ms / 1000, tz=UTC).date().isoformat()


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
        f"{clean(listing['source'])} @ {clean(listing['ref'])} ({clean(listing['commit'])[:12]}), "
        f"license {clean(listing.get('license') or 'not declared')}",
        err=True,
    )
    for item in listing["items"]:
        description = " ".join(clean(item.get("description")).split())
        waiting = ""
        if not item.get("eligible", True) and item.get("eligible_at"):
            waiting = f"\t(too recent to import until {_date(item['eligible_at'])})"
        typer.echo(f"{clean(item['source'])}\t{clean(item['name'])}\t{description[:100]}{waiting}")
    if listing.get("truncated"):
        typer.echo("…more skills than one listing holds; name a path to narrow it.", err=True)
    if not listing["items"]:
        typer.echo("no skills found", err=True)


@skills_app.command("add")
def add_cmd(
    source: str = typer.Argument(..., help="github:owner/repo/path — the directory holding SKILL.md"),
    ref: str | None = _REF,
    url: str = _URL,
    api_key: str | None = _API_KEY,
) -> None:
    """Import one skill into the server's library as a draft, pinned to the commit `--ref` names.

    Never published here: read the draft, then publish it with the route this prints."""
    row = _call(url, api_key, lambda c: c.import_skill(source, ref=ref))
    name, version = clean(row["name"]), clean(row["version"])
    where = f"{clean(row.get('origin_source'))} @ {clean(row.get('origin_commit'))[:12]}"
    if row.get("unchanged"):
        typer.echo(f"{name}@{version} is already {where}; nothing saved.")
        return
    typer.echo(f"{name}@{version} saved as a draft from {where}.")
    for path in row.get("dropped_files") or []:
        typer.echo(f"dropped {clean(path)}", err=True)
    typer.echo(
        f"Review it (GET /skill-library/{name}/versions/{version}/preview), then publish with "
        f"POST /skill-library/{name}/versions/{version}/publish.",
        err=True,
    )


__all__ = ["clean", "skills_app"]
