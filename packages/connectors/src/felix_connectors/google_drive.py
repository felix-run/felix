"""Google Drive: find the person's files by what they say, and read one as text.

Acts as the person through their vault (`google_common`), with `drive_read` only. Google Docs,
Sheets and Slides are exported as text; a plain-text file is read as is; anything else is named
and left alone. Every file is text someone could have shared with the person: screen it.
"""

from __future__ import annotations

from functools import partial
from typing import Any

from felix.config import Settings
from felix.tools.types import Tool
from pydantic import Field

from felix_connectors.google_common import AccountArgs, GoogleApis, Id
from felix_connectors.person import answered, capped
from felix_connectors.tools import connector_tool, one_line

SERVICE = "Google Drive"
MAX_FILES = 10
READ = ("drive_read",)
# Docs, Sheets and Slides have no bytes of their own; they are exported.
EXPORTS = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}


def drive_query(text: str) -> str:
    """`text` as a Drive search clause. Drive's `q` is a grammar of its own: a quote or a
    backslash in the words would end the string and start a clause of the model's choosing."""
    escaped = text.replace("\\", "\\\\").replace("'", "\\'")
    return f"fullText contains '{escaped}' and trashed = false"


def file_line(f: dict[str, Any]) -> str:
    return (
        f"- {one_line(f.get('name', '?'), 160)} · {one_line(f.get('mimeType', ''), 80)} · "
        f"modified {one_line(f.get('modifiedTime', '?'), 40)} · id {one_line(f.get('id', ''), 128)}"
    )


class SearchDriveArgs(AccountArgs):
    query: str = Field(min_length=1, max_length=200, description="Words the file's text or name contains.")
    limit: int = Field(default=5, ge=1, le=MAX_FILES)


class ReadDriveArgs(AccountArgs):
    file_id: Id


#: Every tool `build_tools` returns, registered by name before any is built.
TOOL_NAMES = ("connectors__drive_search", "connectors__drive_read")


def build_tools(settings: Settings) -> list[Tool]:
    """The family's tools under `settings`; `ValueError` without the `google` extra."""
    return drive_tools(GoogleApis.from_settings(settings))


def drive_tools(apis: GoogleApis) -> list[Tool]:
    hosts = apis.googleapis_hosts

    def files() -> Any:
        return apis.service("drive", "v3").files()

    async def search(args: SearchDriveArgs) -> str:
        request = files().list(
            q=drive_query(args.query), pageSize=args.limit, fields="files(id,name,mimeType,modifiedTime)"
        )
        found = (await apis.json(request, READ, args.account, hosts)).get("files") or []
        lines = [f"source: {SERVICE}", *(file_line(f) for f in found[: args.limit] if isinstance(f, dict))]
        if len(lines) == 1:
            lines.append("no files")
        return "\n".join(lines)

    async def read(args: ReadDriveArgs) -> str:
        meta = await apis.json(
            files().get(fileId=args.file_id, fields="id,name,mimeType"), READ, args.account, hosts
        )
        kind, name = str(meta.get("mimeType", "")), one_line(meta.get("name", ""), 160)
        if kind in EXPORTS:
            request = files().export(fileId=args.file_id, mimeType=EXPORTS[kind])
        elif kind.startswith("text/") or kind == "application/json":
            request = files().get_media(fileId=args.file_id)
        else:
            return f"source: {SERVICE}\n{name} is {one_line(kind, 80)}, which is not text"
        data = await apis.call(request, READ, args.account, hosts)
        return f"source: {SERVICE}\n{name}\n\n{capped(data.decode('utf-8', 'replace'))}"

    tool = partial(connector_tool, family="google_drive", service=SERVICE, hosts=hosts)
    return [
        tool(
            name="connectors__drive_search",
            args=SearchDriveArgs,
            run=answered(SERVICE, search),
            description="Search the person's Google Drive by the words in a file.",
        ),
        tool(
            name="connectors__drive_read",
            args=ReadDriveArgs,
            run=answered(SERVICE, read),
            description="One of the person's Drive files as text, by id (Docs, Sheets, Slides, text).",
        ),
    ]


__all__ = ["EXPORTS", "TOOL_NAMES", "build_tools", "drive_query", "drive_tools", "file_line"]
