"""Sync a directory of Markdown / MDX pages into a Felix deployment's document corpus.

Each page becomes one document whose `source` is the page's public URL (`site_url` plus its
slugified path, or its frontmatter `slug:`, the way Starlight and most static-site generators
route it). That is what lets an agent quote a hit and then read the whole page: `support` pairs
`search_docs` with `fetch_docs`, which is confined to the same site.

A sync is safe to repeat. The server keys a document on `(source, title)`, so a page sent again
replaces itself. With `prune`, documents under `site_url` that no page produced any more —
deleted, moved or retitled — are removed, so the corpus does not keep answering from a page the
site no longer has. Prune deletes, so it is fenced three ways: `site_url` must be a real http(s)
origin (an unset variable must not widen the prefix to every URL), it runs only after every page
went in and only from a listing that shows the whole corpus, and it deletes nothing at all when
more than `max_prune` documents would go — a `site_url` broader than the pages synced looks like
exactly that.

`.mdx` is reduced to the prose a reader sees: frontmatter supplies the title and description,
and ESM import/export statements, component tags (single- or multi-line; a `title=`/`label=`
value is kept) and `:::` directive fences are dropped. `.md` is plain Markdown, where such lines
are prose, so only the directive fences go. Code fences are kept verbatim, matched as CommonMark
does (same character, at least as long), so a fence shown inside a fence does not flip the state.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import httpx

if TYPE_CHECKING:
    from felix_client.client import FelixClient

PAGE_SUFFIXES = (".md", ".mdx")
# Pages a site has that answer nothing: Starlight's (and most generators') not-found page.
SKIP_STEMS = frozenset({"404"})
# `GET /documents` returns at most this many, with no cursor. A listing that comes back full may
# be hiding documents, so prune refuses rather than deciding from a partial view.
LIST_CEILING = 500
# More deletions than this in one sync is more likely a `site_url` wider than the pages synced
# than a site that really dropped that many pages. Raise it deliberately when the site did.
DEFAULT_MAX_PRUNE = 10

_FENCE = re.compile(r"^ {0,3}(?P<marker>`{3,}|~{3,})(?P<info>.*)$")
_ESM_START = re.compile(r"^(import|export)\b")
# A component tag, which in MDX is capitalised: `<Aside type="note">`, `</Steps>`, `<Card`.
_TAG_START = re.compile(r"^\s*</?[A-Z][\w.]*")
_TAG_LABEL = re.compile(r"""\b(?:title|label)=(?:"([^"]*)"|'([^']*)')""")
_DIRECTIVE = re.compile(r"^\s*:::\s*(\w+)?(?:\[(?P<label>[^\]]*)\])?\s*$")
_SLUG_DROP = re.compile(r"[^\w\-]")


class SiteUrlError(ValueError):
    """`site_url` is not an http(s) origin a prune could safely be scoped to."""


@dataclass(frozen=True, slots=True)
class Page:
    path: Path
    title: str
    source: str
    text: str


@dataclass(slots=True)
class SyncResult:
    ingested: list[tuple[str, int]] = field(default_factory=list)  # (source, chunks)
    pruned: list[str] = field(default_factory=list)  # sources
    skipped: list[tuple[Path, str]] = field(default_factory=list)  # (path, why)
    failed: list[tuple[str, str]] = field(default_factory=list)  # (source, error)


def check_site_url(site_url: str) -> str:
    """`site_url` without its trailing slash, or `SiteUrlError`.

    Every page's source and the prune prefix are built from it, so a degenerate value is a
    data-loss bug, not a typo: `https://` (an unset `$DOCS_HOST`) makes the prefix `https:/`,
    which every https document starts with.
    """
    parts = urlsplit(site_url.strip())
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise SiteUrlError(f"--site-url must be an http(s) URL with a host, got {site_url!r}")
    if parts.query or parts.fragment:
        raise SiteUrlError(f"--site-url may not carry a query or fragment, got {site_url!r}")
    return site_url.strip().rstrip("/")


def _frontmatter(raw: str) -> tuple[dict[str, str], str]:
    """Top-level `key: value` pairs of a leading `---` block, and the body after it."""
    if not raw.startswith("---"):
        return {}, raw
    end = raw.find("\n---", 3)
    if end == -1:
        return {}, raw
    fields: dict[str, str] = {}
    for line in raw[3:end].splitlines():
        if not line or line[0].isspace() or ":" not in line:
            continue  # nested keys (sidebar.order) and blanks
        key, _, value = line.partition(":")
        value = value.strip()
        if value[:1] in {"|", ">"}:
            continue  # a YAML block scalar; its text is on the lines below, which are not read
        fields[key.strip()] = value.strip("'\"")
    return fields, raw[end + 4 :].lstrip("\n")


class _Reducer:
    """Line-by-line state for `mdx_to_text`: inside a code fence, an ESM statement, or a tag."""

    def __init__(self, *, mdx: bool) -> None:
        self.mdx = mdx
        self.fence: str | None = None  # the opening marker while inside a code fence
        self.esm_depth = 0  # unclosed `{` of an import/export statement spanning lines
        self.in_tag = False  # inside a component tag whose `>` has not arrived yet

    def feed(self, line: str) -> list[str]:
        if self.fence is not None:
            m = _FENCE.match(line)
            closes = m and m.group("marker")[0] == self.fence[0] and len(m.group("marker")) >= len(self.fence)
            if closes and m and not m.group("info").strip():
                self.fence = None
            return [line]
        if m := _FENCE.match(line):
            self.fence = m.group("marker")
            return [line]
        if self.mdx and (self.esm_depth > 0 or _ESM_START.match(line)):
            self.esm_depth = max(0, self.esm_depth + line.count("{") - line.count("}"))
            return []
        if self.mdx and (self.in_tag or _TAG_START.match(line)):
            self.in_tag = ">" not in line
            return [f"{a or b}:" for a, b in _TAG_LABEL.findall(line) if a or b]
        if directive := _DIRECTIVE.match(line):
            return [f"{label}:"] if (label := directive.group("label")) else []
        return [line]


def mdx_to_text(raw: str, *, mdx: bool = True) -> tuple[str | None, str, str | None]:
    """`(title, text, slug)` for one page: frontmatter title and slug (or None), and the page as
    a reader sees it. `mdx=False` treats the body as plain Markdown."""
    meta, body = _frontmatter(raw)
    out: list[str] = []
    if description := meta.get("description"):
        out += [description, ""]
    reducer = _Reducer(mdx=mdx)
    for line in body.splitlines():
        out += reducer.feed(line)
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    return meta.get("title") or None, text, meta.get("slug") or None


def _slug(segment: str) -> str:
    return _SLUG_DROP.sub("", re.sub(r"\s+", "-", segment.strip().lower()))


def page_url(site_url: str, root: Path, path: Path, *, slug: str | None = None) -> str:
    """The page's public URL: slugified path segments, `index` as its directory, and a trailing
    `/`. A frontmatter `slug:` replaces the path, as the generators do."""
    base = site_url.rstrip("/")
    if slug is not None:
        parts = [_slug(p) for p in slug.strip("/").split("/") if p]
    else:
        parts = [_slug(p) for p in path.relative_to(root).with_suffix("").parts]
        if parts and parts[-1] == "index":
            parts = parts[:-1]
    joined = "/".join(p for p in parts if p)
    return f"{base}/{joined}/" if joined else f"{base}/"


def _hidden(root: Path, path: Path) -> bool:
    return any(part.startswith((".", "_")) for part in path.relative_to(root).parts)


def read_pages(root: Path, *, site_url: str) -> tuple[list[Page], list[tuple[Path, str]]]:
    """Every page under `root`, and the files skipped with why.

    Hidden and underscore paths (`.git`, `_drafts`) are not pages. Two files that land on the
    same `(source, title)` would overwrite each other on the server, so the later one is skipped
    and named rather than reported as ingested.
    """
    base = check_site_url(site_url)
    pages: list[Page] = []
    skipped: list[tuple[Path, str]] = []
    seen: dict[tuple[str, str], Path] = {}
    candidates = sorted(p for p in root.rglob("*") if p.suffix in PAGE_SUFFIXES and p.is_file())
    for path in candidates:
        if _hidden(root, path):
            continue
        if path.stem.lower() in SKIP_STEMS:
            skipped.append((path, "not-found page"))
            continue
        title, text, slug = mdx_to_text(path.read_text(encoding="utf-8-sig"), mdx=path.suffix == ".mdx")
        if not text:
            skipped.append((path, "no text after stripping markup"))
            continue
        page = Page(
            path=path,
            title=title or path.stem.replace("-", " ").title(),
            source=page_url(base, root, path, slug=slug),
            text=text,
        )
        if (first := seen.get((page.source, page.title))) is not None:
            skipped.append((path, f"same page as {first.relative_to(root)} ({page.source})"))
            continue
        seen[(page.source, page.title)] = path
        pages.append(page)
    return pages, skipped


def _error(exc: httpx.HTTPError) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}: {exc.response.text[:200]}"
    return f"{type(exc).__name__}: {exc}"


async def _prune(
    client: FelixClient,
    prefix: str,
    kept: set[tuple[str, str]],
    result: SyncResult,
    *,
    dry_run: bool,
    max_prune: int,
) -> None:
    """Delete documents under `prefix` that this sync did not produce, recording into `result`."""
    try:
        items = (await client.list_documents(limit=LIST_CEILING)).get("items", [])
    except httpx.HTTPError as exc:
        result.failed.append((prefix, f"listing the corpus to prune: {_error(exc)}"))
        return
    if len(items) >= LIST_CEILING:
        result.failed.append(
            (prefix, f"the corpus lists {LIST_CEILING}+ documents; not pruning from a partial view")
        )
        return
    stale = [
        item
        for item in items
        if str(item.get("source", "")).startswith(prefix)
        and (str(item.get("source", "")), str(item.get("title", ""))) not in kept
    ]
    if len(stale) > max_prune:
        result.failed.append(
            (
                prefix,
                f"{len(stale)} documents would be pruned, over the limit of {max_prune}; nothing was "
                "deleted. Check --site-url covers exactly these pages, then raise --max-prune",
            )
        )
        return
    for item in stale:
        source = str(item["source"])
        if not dry_run:
            try:
                await client.delete_document(str(item["doc_id"]))
            except httpx.HTTPError as exc:
                result.failed.append((source, _error(exc)))
                continue
        result.pruned.append(source)


async def sync_pages(
    client: FelixClient,
    pages: Iterable[Page],
    *,
    site_url: str,
    prune: bool = False,
    dry_run: bool = False,
    max_prune: int = DEFAULT_MAX_PRUNE,
) -> SyncResult:
    """Ingest `pages`; with `prune`, delete documents under `site_url` that no page produced.

    A page that fails is recorded and the rest continue, so one bad page does not leave the
    corpus half-synced. Prune runs only when every page went in: deleting on the strength of a
    sync that partly failed could remove a document whose replacement was the one that failed.
    """
    prefix = check_site_url(site_url) + "/"
    result = SyncResult()
    kept: set[tuple[str, str]] = set()
    for page in pages:
        kept.add((page.source, page.title))
        if dry_run:
            result.ingested.append((page.source, 0))
            continue
        try:
            answer = await client.ingest_document(page.title, page.text, source=page.source)
        except httpx.HTTPError as exc:
            result.failed.append((page.source, _error(exc)))
            continue
        result.ingested.append((page.source, int(answer.get("chunks", 0))))
    if not prune or result.failed:
        return result
    await _prune(client, prefix, kept, result, dry_run=dry_run, max_prune=max_prune)
    return result


__all__ = [
    "DEFAULT_MAX_PRUNE",
    "Page",
    "SiteUrlError",
    "SyncResult",
    "check_site_url",
    "mdx_to_text",
    "page_url",
    "read_pages",
    "sync_pages",
]
