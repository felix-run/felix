"""Wikipedia: find an article, and read its lead, in any language edition. Keyless.

Through the MediaWiki Action API (`/w/api.php`) of the language's edition, not the REST summary
endpoint: that one answers a redirected title ("NYC") with a 302 to the article, and a connector
never follows a redirect, while the Action API resolves it in the answer (`redirects=1`) and says
which title it landed on. Every answer names the article's URL, so a claim taken from it can be
cited, and says when the title is a disambiguation page rather than an article.

The edition is the model's choice, held to a language code, so the host is always
`<code>.wikipedia.org` -- what the tools declare (`*.wikipedia.org`). Wikipedia is written by
anyone: what comes back is untrusted text, screened where the manifest screens. Wikimedia asks
for a User-Agent that identifies the client (`FELIX_CONNECTORS_CONTACT` adds the operator's).
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from functools import partial
from typing import Annotated, Any

from felix.config import Settings
from felix.tools.types import Tool
from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from felix_connectors.http import allow_http, get_json, user_agent
from felix_connectors.tools import connector_tool, one_line

SOURCE = "Wikipedia"
WIKIPEDIA_URL = "https://{lang}.wikipedia.org"
WIKIPEDIA_HOSTS = frozenset({"*.wikipedia.org"})
MAX_RESULTS = 10
#: The lead of a long article runs to a few thousand characters; this keeps the gist.
MAX_EXTRACT = 3000
#: `en`, `pt`, `zh-yue`, `be-tarask`, `zh-classical`, `simple`: one subdomain label, never a dot
#: or a slash, so the host is always `<code>.wikipedia.org`.
_LANG = re.compile(r"[a-z]{2,3}(-[a-z]{2,12}){0,2}|simple", re.ASCII)
#: Labels the pattern takes that are no edition: `www` answers a redirect.
_NOT_EDITIONS = frozenset({"www"})
_TAG = re.compile(r"<[^>]*>")


def _lang(value: str) -> str:
    code = value.strip().lower()
    if not _LANG.fullmatch(code) or code in _NOT_EDITIONS:
        raise ValueError("a Wikipedia edition is a language code, e.g. en, de, ja, zh-yue")
    return code


Lang = Annotated[str, AfterValidator(_lang)]


def _title(value: str) -> str:
    # `titles=` takes a list, split on `|` (or on U+001F after a leading one): one title holding
    # either would fetch several pages and describe whichever came first. Neither is legal in one.
    if "|" in value or "\x1f" in value:
        raise ValueError("a title holds no | -- ask for one article at a time")
    return value


Title = Annotated[str, AfterValidator(_title)]


def _text(value: object, cap: int, *, markup: bool = False) -> str:
    """A field Wikipedia sent, as plain text on one line, every line break collapsed. `markup`
    for a search snippet, which marks its matches in `<span>`: its tags go and its entities are
    read, where a plain description keeps a `<` it means."""
    text = html.unescape(_TAG.sub("", str(value or ""))) if markup else str(value or "")
    return one_line(text, cap)


@dataclass(frozen=True, slots=True)
class Wikipedia:
    """Where the editions are: `url` is formatted with the language (`https://{lang}...`), and
    `hosts` is what that reaches. One value per deployment. Not an `http.Upstream`, which derives
    its host from one base URL: here the host is a template, one per edition, so the two are set
    together and a changed `url` must change `hosts` with it."""

    user_agent: str
    url: str = WIKIPEDIA_URL
    hosts: frozenset[str] = WIKIPEDIA_HOSTS
    allow_http: bool = False

    @classmethod
    def from_settings(cls, settings: Settings) -> Wikipedia:
        return cls(user_agent=user_agent(settings), allow_http=allow_http(settings))

    async def query(self, lang: str, params: dict[str, str]) -> Any:
        """One Action API `query`, in `formatversion=2` JSON."""
        return await get_json(
            self.url.format(lang=lang) + "/w/api.php",
            params={"action": "query", "format": "json", "formatversion": "2", **params},
            headers={"user-agent": self.user_agent, "accept": "application/json"},
            hosts=self.hosts,
            allow_http=self.allow_http,
        )


class SearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=300, description="What to look for.")
    lang: Lang = Field(default="en", description="The edition's language code, e.g. en, de, ja.")
    limit: int = Field(default=5, ge=1, le=MAX_RESULTS)


class ArticleArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Title = Field(
        min_length=1, max_length=255, description="The article's title, as search found it or as known."
    )
    lang: Lang = Field(default="en", description="The edition's language code, e.g. en, de, ja.")


def _query(raw: Any) -> dict[str, Any]:
    query = raw.get("query") if isinstance(raw, dict) else None
    return query if isinstance(query, dict) else {}


def _listed(query: dict[str, Any], key: str) -> list[dict[str, Any]]:
    found = query.get(key)
    return [item for item in found if isinstance(item, dict)] if isinstance(found, list) else []


async def search(wiki: Wikipedia, args: SearchArgs) -> str:
    raw = await wiki.query(
        args.lang,
        {
            "list": "search",
            "srsearch": args.query,
            "srlimit": str(args.limit),
            "srprop": "snippet",
        },
    )
    lines = [f"source: {SOURCE} ({args.lang})"]
    for hit in _listed(_query(raw), "search"):
        if hit.get("title"):
            lines.append(f"- {_text(hit['title'], 200)}: {_text(hit.get('snippet'), 300, markup=True)}")
    if len(lines) == 1:
        lines.append("no articles found")
    return "\n".join(lines)


async def article(wiki: Wikipedia, args: ArticleArgs) -> str:
    raw = await wiki.query(
        args.lang,
        {
            "titles": args.title,
            "redirects": "1",
            "prop": "extracts|description|info|pageprops",
            "exintro": "1",
            "explaintext": "1",
            "inprop": "url",
            "ppprop": "disambiguation",
        },
    )
    lines = [f"source: {SOURCE} ({args.lang})"]
    query = _query(raw)
    pages = _listed(query, "pages")
    page = pages[0] if pages else {}
    if not page and _listed(query, "interwiki"):
        lines.append(
            f"{one_line(args.title, 255)} names another wiki; for another language's edition, "
            "give the title alone with its lang"
        )
        return "\n".join(lines)
    if page.get("special") or (isinstance(page.get("ns"), int) and page["ns"] < 0):
        lines.append(f"{one_line(args.title, 255)} is a special page, not an article")
        return "\n".join(lines)
    if not page or page.get("missing") or page.get("invalid"):
        lines.append(f"no article titled {one_line(args.title, 255)}")
        return "\n".join(lines)
    lines.append(f"title: {_text(page.get('title'), 255)}")
    for hop in _listed(query, "redirects"):
        # Only a hop that landed on this page: a title is one article, but say so of no other.
        if hop.get("from") and hop.get("to") == page.get("title"):
            lines.append(f"redirected from: {_text(hop['from'], 255)}")
    if page.get("fullurl"):
        lines.append(f"url: {one_line(page['fullurl'], 500)}")
    if page.get("description"):
        lines.append(f"description: {_text(page['description'], 300)}")
    if page.get("touched"):
        lines.append(f"last changed: {one_line(page['touched'], 40)}")
    if isinstance(page.get("pageprops"), dict) and "disambiguation" in page["pageprops"]:
        lines.append("this is a disambiguation page: search for the meaning you want")
    extract = str(page.get("extract") or "")  # paragraphs, made one line by `one_line`
    if extract:
        lines.append(f"lead: {one_line(extract, MAX_EXTRACT)}")
    return "\n".join(lines)


TOOL_NAMES = ("connectors__wikipedia_search", "connectors__wikipedia_article")


def build_tools(settings: Settings) -> list[Tool]:
    """The family's tools under `settings`: the interface `felix_connectors.register` reads."""
    return wikipedia_tools(Wikipedia.from_settings(settings))


def wikipedia_tools(wiki: Wikipedia) -> list[Tool]:
    tool = partial(connector_tool, family="wikipedia", service=SOURCE, hosts=wiki.hosts)
    return [
        tool(
            name="connectors__wikipedia_search",
            args=SearchArgs,
            run=partial(search, wiki),
            description="Search Wikipedia for articles: each title with a snippet of where it matched.",
        ),
        tool(
            name="connectors__wikipedia_article",
            args=ArticleArgs,
            run=partial(article, wiki),
            description=(
                "Read the lead of a Wikipedia article by title, with its URL to cite and when it "
                "last changed; follows a redirected title to its article."
            ),
        ),
    ]


__all__ = [
    "MAX_EXTRACT",
    "TOOL_NAMES",
    "WIKIPEDIA_HOSTS",
    "ArticleArgs",
    "SearchArgs",
    "Wikipedia",
    "article",
    "build_tools",
    "search",
    "wikipedia_tools",
]
