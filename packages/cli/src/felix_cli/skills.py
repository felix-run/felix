"""`felix skills` — browse a GitHub repository's skills, import one into a server's library, and
keep it current: `outdated` lists imported skills against their origins, `diff` shows what one's
origin changed against the live version, `update` re-imports it as a draft.

Each talks to a running server (`/skill-library/-/browse`, `/-/import`, `/-/upstream`,
`/{name}/-/upstream`, `/{name}/-/update`) through
`FelixClient`, authenticated the way `felix ingest-docs` is: `--api-key`/`FELIX_API_KEY`, else the
token `felix login --save` kept for that server. The server does the fetching -- pinned to one
commit, through its egress guard, within its `FELIX_SKILL_IMPORT_SOURCES` -- so nothing here
reaches GitHub. An import or an update is a draft: this prints how to publish it once someone has
read it.

Every string that came from a repository is stripped of control characters before it reaches the
terminal: a skill's name or description is the third party's text, and an escape sequence in it
would otherwise be the third party's control of the operator's terminal. A diff is cleaned line by
line (`clean_text`), keeping its newlines and nothing else.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any

import typer

skills_app = typer.Typer(
    name="skills", help="Browse, import and update Agent Skills from GitHub.", no_args_is_help=True
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
# on its own in terminals that read 8-bit controls. Then bidi embeddings, overrides and isolates
# (U+202A-202E, U+2066-2069), which reorder what an operator reads -- a source that displays as
# one repository and is another -- and zero-width characters (U+200B-200F, U+FEFF) that hide text.
_CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")


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
        f"{clean(listing['source'])} @ {clean(listing['ref'])} ({clean(listing['commit'])}), "
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
    where = f"{clean(row.get('origin_source'))} @ {clean(row.get('origin_commit'))}"
    if row.get("unchanged"):
        typer.echo(f"{name}@{version} is already {where}; nothing saved.")
        return
    typer.echo(f"{name}@{version} saved as a draft from {where}.")
    for path in row.get("dropped_files") or []:
        typer.echo(f"dropped {clean(path)}", err=True)
    _review_hint(name, version)


def _review_hint(name: str, version: str) -> None:
    typer.echo(
        f"Review it (GET /skill-library/{name}/versions/{version}/preview), then publish with "
        f"POST /skill-library/{name}/versions/{version}/publish.",
        err=True,
    )


def clean_text(value: Any) -> str:
    """``value`` cleaned line by line (`clean`), its newlines kept: a diff read in a terminal."""
    return "\n".join(clean(line) for line in str(value if value is not None else "").split("\n"))


def _short(commit: Any) -> str:
    return clean(commit)[:12] if commit else "-"


def _print_diff(diff: dict[str, Any]) -> None:
    """Each changed file, and its unified diff -- third-party text, cleaned of control characters."""
    files = diff.get("files") or []
    if not files:
        typer.echo(f"no file differs from {clean(diff.get('compared_with') or 'nothing')}", err=True)
        return
    for item in files:
        sizes = f"{item.get('old_size') if item.get('old_size') is not None else '-'} -> "
        sizes += f"{item.get('new_size') if item.get('new_size') is not None else '-'} bytes"
        typer.echo(f"{clean(item['change'])}\t{clean(item['path'])}\t{sizes}")
        if item.get("diff"):
            typer.echo(clean_text(item["diff"]).rstrip("\n"))
        elif not item.get("binary"):
            typer.echo("(diff left out: past the answer's diff budget)")
    if diff.get("diff_truncated"):
        typer.echo("…some diffs were cut short or left out.", err=True)


@skills_app.command("outdated")
def outdated_cmd(
    cached: bool = typer.Option(
        False, "--cached", help="Show the last recorded checks; ask GitHub nothing (and spend no budget)."
    ),
    url: str = _URL,
    api_key: str | None = _API_KEY,
) -> None:
    """List the library's imported skills against their origins, and which have an update waiting.

    Follows every page; each refreshed page spends GitHub calls from the server's budget."""
    cursor: str | None = None
    found = False
    while True:
        page = _call(
            url, api_key, lambda c, after=cursor: c.list_skill_upstreams(cursor=after, refresh=not cached)
        )
        for item in page["items"]:
            found = True
            typer.echo(
                f"{clean(item['name'])}\t{clean(item['version'])}\t{clean(item['origin_source'])} @ "
                f"{clean(item['origin_ref'])}\t{_short(item.get('origin_commit'))} -> "
                f"{_short(item.get('upstream_commit'))}\t{_upstream_state(item)}"
            )
        if page.get("stopped"):
            typer.echo(f"stopped early ({clean(page['stopped'])}); run it again to check the rest.", err=True)
        cursor = page.get("next_cursor")
        if not cursor or page.get("stopped"):
            break
    if not found:
        typer.echo("no imported skills", err=True)


def _upstream_state(item: dict[str, Any]) -> str:
    if item.get("error"):
        return f"check failed: {clean(item['error'])}"
    if item.get("update_available"):
        if not item.get("eligible") and item.get("eligible_at"):
            return f"update available, too recent to update until {_date(item['eligible_at'])}"
        return "update available"
    return "up to date" if item.get("upstream_commit") else "never checked"


@skills_app.command("diff")
def diff_cmd(
    name: str = typer.Argument(..., help="The library skill's name."),
    ref: str | None = typer.Option(
        None, "--ref", help="Another branch, tag or commit; the stored ref if omitted."
    ),
    url: str = _URL,
    api_key: str | None = _API_KEY,
) -> None:
    """Show what an imported skill's origin holds now against the live version, file by file."""
    found = _call(url, api_key, lambda c: c.check_skill_upstream(name, ref=ref))
    current, now = found["current"], found["upstream"]
    typer.echo(
        f"{clean(found['name'])}@{clean(current['version'])} from {clean(now['source'])} @ "
        f"{clean(now['ref'])}: {_short(current.get('commit'))} -> {_short(now['commit'])}",
        err=True,
    )
    if not found["update_available"]:
        typer.echo("up to date: the origin's files are the newest version's.", err=True)
    elif not now.get("eligible", True):
        typer.echo(f"too recent to update until {_date(now['eligible_at'])}.", err=True)
    _print_diff(found["diff"])


@skills_app.command("update")
def update_cmd(
    name: str = typer.Argument(..., help="The library skill's name."),
    ref: str | None = typer.Option(
        None, "--ref", help="Another branch, tag or commit; the stored ref if omitted."
    ),
    url: str = _URL,
    api_key: str | None = _API_KEY,
) -> None:
    """Re-import an imported skill from its origin as a draft. Never published here: read the
    draft, then publish it with the route this prints."""
    row = _call(url, api_key, lambda c: c.update_skill(name, ref=ref))
    skill, version = clean(row["name"]), clean(row["version"])
    where = f"{clean(row.get('origin_source'))} @ {clean(row.get('origin_commit'))}"
    if row.get("unchanged"):
        typer.echo(f"{skill}@{version} is already {where}; nothing saved.")
        return
    typer.echo(f"{skill}@{version} saved as a draft from {where}.")
    for path in row.get("dropped_files") or []:
        typer.echo(f"dropped {clean(path)}", err=True)
    _print_diff(row.get("diff") or {})
    _review_hint(skill, version)


__all__ = ["clean", "clean_text", "skills_app"]
