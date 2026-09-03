"""Fetch tools from ``spec.http_tools`` — read a model-supplied URL, SSRF-guarded.

Distinct from `HttpExecutor` in `tools/transports.py`, which is the other direction: that
one posts a tool's *arguments* to a URL fixed by the manifest, so the destination is
operator-chosen and the payload is the model's. Here the destination is the model's, which
is what makes this the higher-risk shape and why every knob below is bounded by the
manifest rather than by the caller.

Egress is not re-implemented. `safe_async_client` resolves once, validates every answer,
and dials one of the approved addresses, so the address that was checked is the address
that is used — including on each redirect hop, which re-enters the guarded transport.
That is enforcement rather than advice, so unlike the browser tool this path needs no
separate resolving pre-check.

The transport is ``http``, which is deliberately absent from `_TRUSTED_TRANSPORTS` in
`manifests/builder.py`: a fetched page is attacker-controlled input and must reach content
screening like any other untrusted tool output.
"""

from __future__ import annotations

import asyncio
import logging
from html.parser import HTMLParser
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from felix.manifests.schema import HttpFetchToolRef
from felix.security.egress import safe_async_client
from felix.security.ssrf import EgressBlocked, assert_safe_outbound_url
from felix.timeouts import DEFAULT_CONNECT_TIMEOUT_S
from felix.tools.types import (
    Tool,
    ToolInput,
    ToolInvocationCtx,
    ToolOutput,
    define_tool_with_executor,
)

logger = logging.getLogger("felix.tools.http_fetch")

DEFAULT_FETCH_TIMEOUT_MS = 15_000
DEFAULT_MAX_BYTES = 100_000

# A redirect chain is bounded rather than followed indefinitely. Each hop re-enters the
# guarded transport and is validated on the same terms as the first, so this is a cost
# bound, not a security one.
MAX_REDIRECTS = 5

# Every egress refusal returns this exact line, whatever the reason and whichever layer
# raised it. Distinguishing "does not resolve" from "resolves into private space" from
# "blocked by scheme" would hand the model — and whatever is steering it — a probe for
# internal addressing, and this string goes straight into the transcript. It mirrors
# `felix.security.egress._BLOCKED_MESSAGE`; the tests assert on equality with it, so a
# refusal cannot be confused with an unrelated failure that merely happens to be an error.
_BLOCKED = "egress_blocked: destination not permitted"

# What is worth handing to a model as text. Anything else returns a description instead of
# its bytes: a model cannot use a JPEG, and base64 of one would spend the context window to
# say nothing.
_TEXTUAL_TYPES = frozenset(
    {
        "application/json",
        "application/ld+json",
        "application/xml",
        "application/xhtml+xml",
        "application/javascript",
        "application/x-ndjson",
        "image/svg+xml",
    }
)

_HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})

# Content inside these never renders, so it is noise at best. `script` is also the most
# likely place for a page to address the model directly.
_NON_RENDERED = frozenset({"script", "style", "noscript", "template", "svg"})

_BLOCK_LEVEL = frozenset(
    {
        "p", "div", "br", "li", "tr", "section", "article", "header", "footer",
        "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "table", "ul", "ol",
    }
)  # fmt: skip


class _PrefixRefused(ValueError):
    """The URL was outside the manifest's `path_prefix`.

    Separate from `EgressBlocked` because the two refusals differ in what may be said out
    loud: the prefix is operator configuration the model has already been told about, while
    an egress refusal must not describe what it refused.
    """


class HttpFetchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, description="The http(s) URL to fetch.")


class _HtmlTextExtractor(HTMLParser):
    """HTML to readable text, using only the standard library.

    A regex that strips tags is wrong on exactly the documents that matter — a `<` inside a
    script, an unclosed tag, an attribute containing markup — and pulling in a parser
    dependency for this would put a wheel behind the lean-install rule for one tool.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._suppress = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in _NON_RENDERED:
            self._suppress += 1
        elif tag in _BLOCK_LEVEL:
            self._parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: Any) -> None:
        """`<script/>` must not re-open as `start` + immediate `end`.

        HTMLParser dispatches a self-closing tag here, and the default implementation calls
        `handle_starttag` then `handle_endtag` — so suppression opened and closed at once and
        the text after it was emitted. A browser does not honour the slash on `script`: it
        treats the tag as *open* and hides what follows. That difference lets one page read
        one way to a human reviewer and another to the model, so the slash is ignored for
        exactly the tags whose content never renders.
        """
        if tag in _NON_RENDERED:
            self._suppress += 1
            return
        super().handle_startendtag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in _NON_RENDERED:
            # Clamped at zero: a stray closing tag with no opener is ordinary broken HTML,
            # and letting it go negative would un-suppress the rest of a real script body.
            self._suppress = max(0, self._suppress - 1)
        elif tag in _BLOCK_LEVEL:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._suppress == 0:
            self._parts.append(data)

    def text(self) -> str:
        joined = "".join(self._parts)
        # Collapse the runs of blank lines the block-level newlines above produce, without
        # collapsing intentional paragraph breaks into one wall of text.
        lines = [ln.strip() for ln in joined.splitlines()]
        out: list[str] = []
        for line in lines:
            if line or (out and out[-1]):
                out.append(line)
        return "\n".join(out).strip()


NO_READABLE_TEXT = "(no readable text)"


def html_to_text(html: str) -> str:
    """Best-effort readable text. Malformed input yields what parsed, never an exception.

    A page with no visible text returns the marker, **never the source**. Falling back to the
    raw document handed back precisely what suppression had just removed — an SPA shell whose
    whole body is `<div id="root"></div><script>…</script>` is the ordinary case, not a
    contrived one, so the fallback leaked script bodies on exactly the pages most likely to
    contain something addressed to the model.
    """
    parser = _HtmlTextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # pragma: no cover - HTMLParser is lenient; belt and braces
        logger.debug("html parse ended early", exc_info=True)
    return parser.text() or NO_READABLE_TEXT


def _within_prefix(url: str, prefix: str) -> bool:
    """Is `url` inside `prefix`, comparing origins as origins rather than as text?

    A raw `startswith` gets the host wrong in both directions. `https://docs.felix.run` (the
    schema now appends the slash, but a bare `startswith` would still be doing string work on
    a structured value) matched `https://docs.felix.run.evil.com/`, and `HTTPS://Docs.Felix.Run/x`
    — same origin by every rule that matters — matched nothing. Host and scheme are
    case-insensitive per RFC 3986; the path is not.
    """
    from urllib.parse import urlsplit

    u, p = urlsplit(url), urlsplit(prefix)
    if u.scheme.lower() != p.scheme.lower():
        return False
    # `netloc` carries userinfo and port; comparing hostname and port separately keeps
    # `https://user@docs.felix.run/` from being treated as a different origin, and keeps
    # `https://evil.com@docs.felix.run/` from being read as the prefix host by a text match.
    if (u.hostname or "").lower() != (p.hostname or "").lower() or u.port != p.port:
        return False
    return u.path.startswith(p.path)


def _media_type(content_type: str) -> str:
    return content_type.split(";", 1)[0].strip().lower()


def _is_textual(media_type: str) -> bool:
    return media_type.startswith("text/") or media_type in _TEXTUAL_TYPES


class _HttpFetchExecutor:
    transport = "http"

    def __init__(
        self,
        *,
        path_prefix: str = "",
        allow_http: bool = False,
        timeout_ms: int = DEFAULT_FETCH_TIMEOUT_MS,
        max_bytes: int = DEFAULT_MAX_BYTES,
        as_text: bool = True,
    ) -> None:
        self._path_prefix = path_prefix
        self._allow_http = allow_http
        self._timeout_s = timeout_ms / 1000
        self._max_bytes = max_bytes
        self._as_text = as_text

    def _check_url(self, url: str) -> None:
        """Cheap, non-resolving checks so an obviously bad URL fails before a client opens.

        The resolving half is the transport's job and is enforcement rather than advice, so
        this deliberately does not resolve: doing it here would add a blocking lookup on the
        event loop and still not be the check that matters.
        """
        try:
            assert_safe_outbound_url(url, allow_http=self._allow_http, resolve=False)
        except ValueError as exc:
            # `assert_safe_outbound_url` names the address it objected to, and this string
            # is returned to the model. That turns a refusal into a one-bit oracle for
            # internal addressing, which is the reason `EgressBlocked` carries a fixed
            # message at dial time; the syntactic check needs the same treatment. Operator
            # gets the detail in the log, caller gets the same line either way.
            logger.warning("http_fetch refused url=%s reason=%s", url, exc)
            raise EgressBlocked(_BLOCKED) from None
        if self._path_prefix and not _within_prefix(url, self._path_prefix):
            # Not a secret: the prefix is operator-set and is already in the tool
            # description, so telling the model is what lets it correct itself.
            raise _PrefixRefused(f"url must start with {self._path_prefix!r}")

    async def _read_capped(self, resp: Any) -> tuple[bytes, bool]:
        """Body bytes up to the cap, and whether there was more.

        Streamed rather than `resp.read()`: the response length is chosen by the far end,
        and a model-supplied URL can name an endless one. Reading one byte past the cap is
        what distinguishes "exactly this long" from "truncated".
        """
        chunks: list[bytes] = []
        total = 0
        truncated = False
        async for chunk in resp.aiter_bytes():
            chunks.append(chunk)
            total += len(chunk)
            if total > self._max_bytes:
                truncated = True
                break
        body = b"".join(chunks)[: self._max_bytes]
        return body, truncated

    async def _follow(self, client: Any, url: str) -> str:
        """Walk the redirect chain by hand, checking and capping every hop.

        `follow_redirects=True` was wrong here in two ways that httpx cannot know about.
        `path_prefix` is re-checked nowhere but this method, so a single `302` from a page
        inside the allowed prefix walked the agent straight out of it — the exfiltration
        shape the prefix exists to prevent, reachable from injected content on any allowed
        page. And httpx `aread()`s each interim body in full before building the next
        request, so a 40 MB redirect body cost 80 MB of resident memory on a fetch capped at
        one kilobyte. Exiting the stream context without reading is what keeps the cap real.

        The egress guard re-checks each hop on its own, in the transport; that half was never
        the problem, and this does not replace it.
        """
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            async with client.stream("GET", current) as resp:
                location = resp.headers.get("location", "") if resp.is_redirect else ""
                if not location:
                    body, truncated = await self._read_capped(resp)
                    return self._render(resp, body, truncated)
                # Resolved against the *current* URL so a relative Location works.
                nxt = str(resp.url.join(location))
            # Outside the context: the interim body is discarded unread, and a refusal here
            # is raised with the connection already closed.
            self._check_url(nxt)
            current = nxt
        return f"http_fetch_error: more than {MAX_REDIRECTS} redirects"

    async def execute(self, args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
        import httpx

        _ = ctx
        url = str(args.get("url") or "").strip()
        if not url:
            return "http_fetch_error: url is required"

        timeout = httpx.Timeout(self._timeout_s, connect=DEFAULT_CONNECT_TIMEOUT_S)
        try:
            self._check_url(url)
            # A whole-call deadline, not four independent per-operation ones. `httpx.Timeout`
            # bounds each read, so a server dribbling a byte just inside it held the call —
            # and a worker — open indefinitely. Nothing above catches that: `check_budgets`
            # evaluates `max_wall_clock_seconds` before dispatch and between turns, never
            # during a call, so `timeout_ms` is the only ceiling this path has.
            async with (
                asyncio.timeout(self._timeout_s),
                safe_async_client(
                    allow_http=self._allow_http,
                    timeout=timeout,
                    follow_redirects=False,
                ) as client,
            ):
                return await self._follow(client, url)
        except _PrefixRefused as exc:
            return f"http_fetch_error: {exc}"
        except EgressBlocked as exc:
            # Already carries the one-message-for-every-refusal string; do not add detail.
            return f"http_fetch_error: {exc}"
        except ValueError as exc:
            # `GuardedAsyncTransport.handle_async_request` re-checks each hop with
            # `assert_safe_outbound_url`, which raises a *detailed* `ValueError` rather than
            # `EgressBlocked`. Uncaught it propagated out of the executor entirely — past
            # secret masking, content screening, guardrails and artifact spill, none of which
            # wrap a raise — and `fatal: true` would have ended the run on a bad redirect.
            logger.warning("http_fetch refused a hop from url=%s reason=%s", url, exc)
            return f"http_fetch_error: {_BLOCKED}"
        except TimeoutError:
            logger.warning("http_fetch timed out url=%s after %.1fs", url, self._timeout_s)
            return "http_fetch_error: timed out"
        except httpx.HTTPError as exc:
            # The type is useful to the model; the message is not, and asyncio embeds the
            # dialled address in `ConnectError`.
            logger.warning("http_fetch failed url=%s error=%s", url, exc)
            return f"http_fetch_error: {type(exc).__name__}"

    def _render(self, resp: Any, body: bytes, truncated: bool) -> str:
        media_type = _media_type(resp.headers.get("content-type", ""))
        final_url = str(resp.url)
        head = [f"status: {resp.status_code}", f"url: {final_url}"]
        if media_type:
            head.append(f"content-type: {media_type}")

        # A non-2xx is reported, not raised. The body of a 404 or a 429 is often the useful
        # part — an API error message, a rate-limit explanation — and a model that is told
        # only "it failed" will retry the same URL.
        if not body:
            head.append("(empty body)")
            return "\n".join(head)

        if not _is_textual(media_type):
            head.append(f"({len(body)} bytes of {media_type or 'unknown type'}; not text)")
            return "\n".join(head)

        encoding = resp.charset_encoding or "utf-8"
        try:
            text = body.decode(encoding, errors="replace")
        except LookupError:
            # A server may name an encoding Python does not have; that is not a fetch failure.
            text = body.decode("utf-8", errors="replace")

        if self._as_text and media_type in _HTML_TYPES:
            text = html_to_text(text)

        if truncated:
            head.append(f"(truncated at {self._max_bytes} bytes)")
        return "\n".join(head) + "\n\n" + text


def tools_from_http_fetch_refs(
    refs: list[HttpFetchToolRef],
    *,
    allow_http: bool = False,
) -> list[Tool]:
    out: list[Tool] = []
    for ref in refs:
        if ref.allow_any_host:
            # The schema makes this deliberate; this makes it *visible*. An operator reading
            # logs should be able to see that an agent can reach anywhere the egress guard
            # permits, in the same way `_warn_untrusted_tools_are_unscreened` surfaces a
            # missing screener — a control nobody can observe is one nobody reviews.
            logger.warning(
                "http tool %r is bound with allow_any_host: it may fetch any address the "
                "egress guard permits, and the response reaches the model",
                ref.name,
            )
        executor = _HttpFetchExecutor(
            path_prefix=ref.path_prefix,
            allow_http=allow_http,
            timeout_ms=int(ref.timeout_ms or DEFAULT_FETCH_TIMEOUT_MS),
            max_bytes=int(ref.max_bytes or DEFAULT_MAX_BYTES),
            as_text=ref.format == "text",
        )
        desc = ref.description or "Fetch a URL over HTTP(S) and return its contents."
        if ref.path_prefix:
            desc = f"{desc} Only URLs starting with {ref.path_prefix} are permitted."
        out.append(
            define_tool_with_executor(
                name=ref.name,
                description=desc,
                args=HttpFetchArgs,
                executor=executor,
                source="http",
                fatal=ref.fatal,
                # Not replay-safe, on reflection. "A GET is nominally read-only" is the call
                # the workspace read tools make, and it holds there because the target is a
                # local path under a root the operator set. Here the *model* names the
                # endpoint, and a GET that mutates is a normal thing to find on the open web,
                # so a resumed run re-issuing one is a side effect the harness cannot see.
                replay_safe=False,
            )
        )
    return out


__all__ = ["HttpFetchArgs", "html_to_text", "tools_from_http_fetch_refs"]
