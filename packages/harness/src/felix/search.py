"""Web search behind a swappable backend.

The seam, not the tool: `felix/tools/web_search.py` binds this into a governed tool the way
`memory/embedder.py` is bound into recall. Split for the same reason — the thing that talks
to a vendor and the thing the harness calls should be replaceable independently.

Two properties this inherits from the rest of the harness rather than reinventing.

*Protocols, not vendors.* `SearchBackend` is a Protocol and the registry is open, so a
deployment that wants Brave, Tavily or an internal index registers one and never touches
core. Nothing here enumerates a closed set of providers.

*The destination is the operator's.* Unlike `spec.http_tools`, the model does not choose
where this call goes — an operator configures one endpoint, and the model supplies only a
query. That makes the risk profile MCP's rather than the fetch tool's: the egress target is
fixed and trusted, and it is the *results* that are untrusted, because their titles and
snippets are written by whoever ranked for the query.

The one bundled backend is SearXNG, which is self-hostable and needs no API key — the same
posture as the rest of the default install. It needs nothing beyond httpx, so unlike the
embeddings backends it sits behind no extra.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from felix.config import Settings
from felix.timeouts import DEFAULT_CONNECT_TIMEOUT_S

logger = logging.getLogger("felix.search")

DEFAULT_SEARCH_TIMEOUT_S = 15.0

# A query is a model-supplied string that lands in a URL query parameter. The cap is not a
# security boundary — httpx encodes it — but an unbounded one is a way to make an operator's
# search endpoint do arbitrary work, and no useful query is this long.
MAX_QUERY_CHARS = 512

# Ceiling on a search response. Generous for a page of results and far below what would
# trouble the process; the point is that a backend cannot choose an unbounded number.
MAX_RESPONSE_BYTES = 2_000_000


@dataclass(slots=True, frozen=True)
class SearchResult:
    """One hit. `snippet` is attacker-controlled text; see the module docstring."""

    title: str
    url: str
    snippet: str = ""


@runtime_checkable
class SearchBackend(Protocol):
    """Answers a query with ranked results.

    ``enabled`` is part of the contract rather than an implementation detail, mirroring
    `Embedder`: callers branch on it to tell "no backend configured" from "backend returned
    nothing", which are different things to report to a model.
    """

    enabled: bool

    async def search(self, query: str, *, limit: int) -> Sequence[SearchResult]: ...


class NullSearchBackend:
    """The default. Declaring a search tool without configuring a backend is a
    misconfiguration, so this reports rather than pretending to have searched."""

    enabled = False

    async def search(self, query: str, *, limit: int) -> Sequence[SearchResult]:
        return []


class SearxngBackend:
    """A self-hosted SearXNG instance over its JSON API.

    Chosen as the bundled backend because it is the only mainstream option an operator can
    run themselves without an account, which is the same reason `FELIX_OBJECT_STORE=fs` is
    the default. `url` is operator-supplied and goes through the same egress guard as every
    other outbound call, so pointing it at internal space is refused.
    """

    enabled = True

    def __init__(
        self,
        *,
        url: str,
        api_key: str = "",
        timeout_s: float = DEFAULT_SEARCH_TIMEOUT_S,
        allow_http: bool = False,
    ):
        self._url = url.rstrip("/") + "/search"
        self._api_key = api_key
        self._timeout_s = timeout_s
        # A self-hosted SearXNG on `http://localhost:8888` is the ordinary way to try this,
        # and without the same development exemption every other integration gets, the
        # bundled backend would be unusable on the machine most likely to run it. Off
        # outside development, so a production deployment cannot reach one over plaintext.
        self._allow_http = allow_http

    async def _read_capped(self, resp: Any) -> bytes:
        """Body bytes up to `MAX_RESPONSE_BYTES`, streamed.

        `client.get()` followed by `resp.json()` buffers whatever the endpoint sends and then
        parses it on the event loop. The endpoint is operator-configured rather than
        model-chosen, so this is a smaller hole than the fetch tool's — but "operator
        configured it" is not "operator controls what it returns", and a proxying metasearch
        instance returns whatever its upstreams do.
        """
        chunks: list[bytes] = []
        total = 0
        async for chunk in resp.aiter_bytes():
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                raise ValueError(f"search response exceeded {MAX_RESPONSE_BYTES} bytes")
        return b"".join(chunks)

    async def search(self, query: str, *, limit: int) -> Sequence[SearchResult]:
        import json

        import httpx

        from felix.security.egress import safe_async_client

        headers = {"authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        params = {"q": query, "format": "json"}
        timeout = httpx.Timeout(self._timeout_s, connect=DEFAULT_CONNECT_TIMEOUT_S)
        # A whole-call deadline, not four per-operation ones — the same fix `http_fetch`
        # carries, for the same reason: `httpx.Timeout` bounds each read, so a server
        # dribbling a byte just inside it holds the call, and a worker, open indefinitely.
        # `check_budgets` never runs during a call, so this is the only ceiling on this path.
        async with asyncio.timeout(self._timeout_s):
            async with safe_async_client(timeout=timeout, allow_http=self._allow_http) as client:
                async with client.stream("GET", self._url, params=params, headers=headers) as resp:
                    resp.raise_for_status()
                    body = await self._read_capped(resp)
        payload = json.loads(body)

        rows = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return []
        out: list[SearchResult] = []
        for row in rows[:limit]:
            if not isinstance(row, dict):
                continue
            url = str(row.get("url") or "").strip()
            if not url:
                # A result with no link is not actionable and cannot be fetched, so it is
                # noise in a context window rather than a partial answer.
                continue
            out.append(
                SearchResult(
                    title=str(row.get("title") or "").strip(),
                    url=url,
                    snippet=str(row.get("content") or "").strip(),
                )
            )
        return out


# `Settings | None` rather than `Settings`: `build_agent` passes `deps.settings`, which
# is optional, and every factory reads through `getattr` so a `None` resolves to the null
# backend. Typing it as required made the signature disagree with the documented
# behaviour, and `ty` was right to say so.
SearchBackendFactory = Callable[[Settings | None], Any]

_backends: dict[str, SearchBackendFactory] = {}


def register_search_backend(name: str, factory: SearchBackendFactory) -> None:
    _backends[name] = factory


def list_search_backends() -> list[str]:
    return sorted(_backends)


def _build_none(settings: Settings | None) -> SearchBackend:
    return NullSearchBackend()


def _build_searxng(settings: Settings | None) -> SearchBackend:
    url = str(getattr(settings, "search_url", "") or "").strip()
    if not url:
        logger.warning("FELIX_SEARCH_BACKEND=searxng but FELIX_SEARCH_URL is unset; search is off")
        return NullSearchBackend()
    return SearxngBackend(
        url=url,
        api_key=str(getattr(settings, "search_api_key", "") or ""),
        timeout_s=float(getattr(settings, "search_timeout_seconds", 0) or DEFAULT_SEARCH_TIMEOUT_S),
        # The same rule `build_agent` applies to every other outbound integration.
        allow_http=(
            getattr(settings, "environment", "") == "development"
            and bool(getattr(settings, "allow_insecure", False))
        ),
    )


register_search_backend("none", _build_none)
register_search_backend("searxng", _build_searxng)


def build_search_backend(settings: Settings | None) -> SearchBackend:
    """The configured backend, or a disabled one.

    An unknown name degrades with a warning rather than failing startup, matching
    `build_embedder`: `validate_runtime` already refuses an unregistered value at boot, so
    reaching here with one means a backend was unregistered at runtime, and losing search is
    a better outcome than losing the service.
    """
    name = str(getattr(settings, "search_backend", "none") or "none")
    factory = _backends.get(name)
    if factory is None:
        logger.warning(
            "unknown FELIX_SEARCH_BACKEND %r; search is disabled (registered: %s)",
            name,
            ", ".join(list_search_backends()),
        )
        return NullSearchBackend()
    return factory(settings)


__all__ = [
    "DEFAULT_SEARCH_TIMEOUT_S",
    "MAX_QUERY_CHARS",
    "NullSearchBackend",
    "SearchBackend",
    "SearchBackendFactory",
    "SearchResult",
    "SearxngBackend",
    "build_search_backend",
    "list_search_backends",
    "register_search_backend",
]
