"""Search tools from ``spec.search_tools`` — a query in, ranked links out.

The counterpart to `tools/http_fetch.py`, and deliberately the milder of the two: there the
model chooses an outbound destination, here it supplies only a query and the endpoint is
whatever the operator configured. So this needs no `path_prefix`, no per-call address
validation and no redirect walking — the backend's own URL is checked by the egress guard
like any other outbound call, once, where it is built.

What it does share is the output. Titles and snippets are written by whoever ranked for the
query, which makes them attacker-influenced text arriving in the transcript. The transport is
``search``, absent from `_TRUSTED_TRANSPORTS`, so content screening covers it.

Results are rendered as numbered `title / url / snippet` blocks rather than JSON. The next
thing an agent does with a result is fetch its URL, and a bare URL on its own line is
markedly easier for a model to lift than one nested in an object.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from felix.manifests.schema import SearchToolRef
from felix.search import MAX_QUERY_CHARS, SearchBackend, SearchResult
from felix.tools.types import (
    Tool,
    ToolInput,
    ToolInvocationCtx,
    ToolOutput,
    define_tool_with_executor,
)

logger = logging.getLogger("felix.tools.web_search")

DEFAULT_MAX_RESULTS = 5

# Snippets are the untrusted half and they are also the verbose half. Truncating per result
# keeps one long-winded hit from crowding out the other nine.
MAX_SNIPPET_CHARS = 400

NOT_CONFIGURED = (
    "search_error: no search backend is configured; an operator must set "
    "FELIX_SEARCH_BACKEND (and FELIX_SEARCH_URL)"
)


class WebSearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=MAX_QUERY_CHARS, description="What to search for.")


MAX_TITLE_CHARS = 200
MAX_URL_CHARS = 500


def _one_line(value: str, cap: int) -> str:
    """Collapse to a single line and cap it.

    The rendered block below *is a grammar*: `\\n` separates fields and `\\n\\n` separates
    records. Every field in it is attacker-influenced — a title and snippet are written by
    whoever ranked for the query — so interpolating them raw let one result forge as many
    more as it liked, complete with URLs, in text the model reads as harness output. It could
    equally emit a line shaped like `search_error:` or `[quarantined]`.

    That is this repo's own rule: validating a value for one grammar does not validate it for
    the next. `str(...).strip()` made these fields safe as JSON, which says nothing about the
    block format they are about to enter. Control characters go, and every field is capped —
    previously only `snippet` was, so an unbounded title could crowd out the results the cap
    on `snippet` existed to protect.
    """
    # `\r\n\t` are kept by the filter so the replacement can see them: excluding them as
    # non-printable *deleted* them instead, so "a\\nb" became "ab" and joined two unrelated
    # words into one token.
    flat = "".join(" " if ch in "\r\n\t" else ch for ch in value if ch.isprintable() or ch in " \t\r\n")
    flat = " ".join(flat.split())
    return flat[:cap] + "…" if len(flat) > cap else flat


def render_results(results: list[SearchResult]) -> str:
    if not results:
        return "(no results)"
    blocks: list[str] = []
    for i, r in enumerate(results, 1):
        url = _one_line(r.url, MAX_URL_CHARS)
        lines = [f"{i}. {_one_line(r.title, MAX_TITLE_CHARS) or '(untitled)'}", f"   {url}"]
        snippet = _one_line(r.snippet, MAX_SNIPPET_CHARS)
        if snippet:
            lines.append(f"   {snippet}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


class _WebSearchExecutor:
    transport = "search"

    def __init__(self, *, backend: SearchBackend, max_results: int = DEFAULT_MAX_RESULTS) -> None:
        self._backend = backend
        self._max_results = max_results

    async def execute(self, args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
        _ = ctx
        query = str(args.get("query") or "").strip()
        if not query:
            return "search_error: query is required"
        if not self._backend.enabled:
            # Reported rather than silently empty: "no backend" and "nothing matched" are
            # different answers, and a model told the latter will rephrase and retry forever.
            return NOT_CONFIGURED
        try:
            results = list(await self._backend.search(query[:MAX_QUERY_CHARS], limit=self._max_results))
        except Exception as exc:
            # The backend URL is operator configuration; naming it or the failure detail in a
            # tool result would put deployment topology in the transcript.
            logger.warning("search failed backend=%s error=%s", type(self._backend).__name__, exc)
            return f"search_error: {type(exc).__name__}"
        return render_results(results[: self._max_results])


def tools_from_search_refs(refs: list[SearchToolRef], *, backend: Any) -> list[Tool]:
    out: list[Tool] = []
    for ref in refs:
        if not getattr(backend, "enabled", False):
            # Bound anyway, returning `NOT_CONFIGURED`. Skipping the binding instead would
            # make `spec.search_tools` silently do nothing, which is the inert-field shape
            # this repo keeps finding; the warning is how the operator learns.
            logger.warning(
                "search tool %r is bound with no search backend configured; every call will "
                "report that FELIX_SEARCH_BACKEND is unset",
                ref.name,
            )
        executor = _WebSearchExecutor(
            backend=backend,
            max_results=int(ref.max_results or DEFAULT_MAX_RESULTS),
        )
        out.append(
            define_tool_with_executor(
                name=ref.name,
                description=ref.description or "Search the web and return ranked links.",
                args=WebSearchArgs,
                executor=executor,
                source="search",
                fatal=ref.fatal,
                # A query has no side effect on the caller's side, and the endpoint is the
                # operator's own rather than one the model named — the distinction that made
                # `http_fetch` unsafe to replay does not apply.
                replay_safe=True,
            )
        )
    return out


__all__ = ["WebSearchArgs", "render_results", "tools_from_search_refs"]
