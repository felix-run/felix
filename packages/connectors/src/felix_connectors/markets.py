"""Markets: a stock quote, its daily history, and symbol search.

The backend sits behind an open registry (`register_market_backend`), selected by
`FELIX_MARKET_BACKEND`. Unlike weather and maps there is no keyless default worth shipping --
every free stock API needs a key -- so `FELIX_MARKET_API_KEY` must be set, and a manifest listing
one of these tools without it fails its compile by name rather than binding a tool that answers
every call with an error. Exchange rates, which need no key, are their own family (`fx`), so they
work without one.

Two backends ship. Finnhub (the default) gives quotes and search on its free plan, but not daily
history, and says so when asked for it. Alpha Vantage gives all three on a free plan of 25
requests a day, and answers a spent quota, a refused key or an unknown symbol with a 200 and a
note, which is read for what it is rather than parsed as data.

A price is only as good as its timestamp: every answer carries the source and the time the
upstream reports for it, and how long ago that was, and never calls itself real-time -- whether
a plan's prices are delayed is the plan's business, not something the response says.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from functools import partial
from typing import Annotated, Any, Protocol, runtime_checkable

from felix.config import Settings
from felix.tools.types import Tool
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from felix_connectors.http import KeyRefused, QuotaSpent, Upstream, allow_http, user_agent
from felix_connectors.registry import BackendRegistry
from felix_connectors.tools import connector_tool, iso_day, number, one_line

MAX_HISTORY_DAYS = 100
MAX_MATCHES = 10
FINNHUB_URL = "https://finnhub.io/api/v1"
ALPHAVANTAGE_URL = "https://www.alphavantage.co"

#: A ticker as exchanges write one: `AAPL`, `BRK.B`, `TSCO.LON`, `^GSPC`, `EURUSD=X`.
Symbol = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9.\-^=:]{1,24}$")]


@dataclass(frozen=True, slots=True)
class Quote:
    symbol: str
    price: float
    change: float | None
    change_percent: float | None
    open: float | None
    high: float | None
    low: float | None
    previous_close: float | None
    #: When the upstream says the price is from: an instant, or only a trading day.
    as_of: datetime | date | None


@dataclass(frozen=True, slots=True)
class Bar:
    day: date
    open: float | None
    high: float | None
    low: float | None
    close: float
    volume: float | None


@dataclass(frozen=True, slots=True)
class Match:
    symbol: str
    name: str
    kind: str = ""
    region: str = ""
    currency: str = ""


@dataclass(frozen=True, slots=True)
class History:
    symbol: str
    bars: Sequence[Bar] = field(default_factory=tuple)


class UnknownSymbol(Exception):
    """The backend has nothing for that symbol. Part of the `MarketBackend` contract: the tools
    answer it as a finding, where any other exception reads to the model as an outage."""


class HistoryUnavailable(Exception):
    """The backend offers no daily history (Finnhub's free plan). Answered as a finding."""


@runtime_checkable
class MarketBackend(Protocol):
    """Quotes, daily bars and search. `UnknownSymbol` from any of them for a symbol it does not
    know; `HistoryUnavailable` from `history` when it has none to give."""

    name: str
    source: str
    hosts: frozenset[str]

    async def quote(self, symbol: str) -> Quote: ...

    async def history(self, symbol: str, *, days: int) -> History: ...

    async def search(self, query: str, *, limit: int) -> list[Match]: ...


class FinnhubBackend:
    """finnhub.io: the key travels in a header, never the URL."""

    name = "finnhub"
    source = "Finnhub"

    def __init__(
        self, *, api_key: str, base_url: str = FINNHUB_URL, user_agent: str, allow_http: bool = False
    ):
        self._up = Upstream(base_url, user_agent, allow_http, headers={"x-finnhub-token": api_key})
        self.hosts = self._up.hosts

    async def quote(self, symbol: str) -> Quote:
        raw = await self._up.get("/quote", {"symbol": symbol})
        if not isinstance(raw, dict):
            raise ValueError("quote is not an object")
        price, stamp = number(raw.get("c")), number(raw.get("t"))
        # An unknown symbol is a 200 of zeroes with no time; a price with no time is no quote.
        if not price or not stamp:
            raise UnknownSymbol(symbol)
        return Quote(
            symbol=symbol,
            price=price,
            change=number(raw.get("d")),
            change_percent=number(raw.get("dp")),
            open=number(raw.get("o")),
            high=number(raw.get("h")),
            low=number(raw.get("l")),
            previous_close=number(raw.get("pc")),
            as_of=datetime.fromtimestamp(stamp, UTC),
        )

    async def history(self, symbol: str, *, days: int) -> History:
        raise HistoryUnavailable(symbol)

    async def search(self, query: str, *, limit: int) -> list[Match]:
        raw = await self._up.get("/search", {"q": query})
        results = raw.get("result") if isinstance(raw, dict) else None
        if not isinstance(results, list):
            raise ValueError("search answer has no result list")
        out = []
        for r in results[:limit]:
            if isinstance(r, dict) and r.get("symbol"):
                out.append(
                    Match(
                        symbol=str(r["symbol"]),
                        name=str(r.get("description") or ""),
                        kind=str(r.get("type") or ""),
                    )
                )
        return out


class AlphaVantageBackend:
    """alphavantage.co: everything through `/query?function=…`, the key a query parameter (the
    API takes it no other way), and errors as a 200 with a note in place of data."""

    name = "alphavantage"
    source = "Alpha Vantage"

    def __init__(
        self, *, api_key: str, base_url: str = ALPHAVANTAGE_URL, user_agent: str, allow_http: bool = False
    ):
        self._up = Upstream(base_url, user_agent, allow_http)
        self._key = api_key
        self.hosts = self._up.hosts

    async def _query(self, function: str, **params: str) -> dict[str, Any]:
        raw = await self._up.get("/query", {"function": function, **params, "apikey": self._key})
        if not isinstance(raw, dict):
            raise ValueError("answer is not an object")
        # A spent quota ("Information", "Note"), said with a 200.
        if "Information" in raw or "Note" in raw:
            raise QuotaSpent(function)
        # The same field says "bad symbol" and "bad key"; the key is the operator's to fix, and
        # must not read to the model as a symbol to try again with.
        if "apikey" in str(raw.get("Error Message", "")).lower():
            raise KeyRefused(function)
        return raw

    async def quote(self, symbol: str) -> Quote:
        answer = await self._query("GLOBAL_QUOTE", symbol=symbol)
        raw = answer.get("Global Quote")
        price = number(raw.get("05. price")) if isinstance(raw, dict) else None
        # An unknown symbol is `{"Global Quote": {}}`, or an "Error Message".
        if not isinstance(raw, dict) or price is None:
            raise UnknownSymbol(symbol)
        pct = str(raw.get("10. change percent") or "").rstrip("%")
        return Quote(
            symbol=str(raw.get("01. symbol") or symbol),
            price=price,
            change=number(raw.get("09. change")),
            change_percent=number(pct),
            open=number(raw.get("02. open")),
            high=number(raw.get("03. high")),
            low=number(raw.get("04. low")),
            previous_close=number(raw.get("08. previous close")),
            as_of=iso_day(raw.get("07. latest trading day")),
        )

    async def history(self, symbol: str, *, days: int) -> History:
        raw = (await self._query("TIME_SERIES_DAILY", symbol=symbol, outputsize="compact")).get(
            "Time Series (Daily)"
        )
        if not isinstance(raw, dict) or not raw:
            raise UnknownSymbol(symbol)
        bars = []
        for key, row in raw.items():
            day = iso_day(key)
            close = number(row.get("4. close")) if isinstance(row, dict) else None
            if day is None or close is None:
                continue
            bars.append(
                Bar(
                    day=day,
                    open=number(row.get("1. open")),
                    high=number(row.get("2. high")),
                    low=number(row.get("3. low")),
                    close=close,
                    volume=number(row.get("5. volume")),
                )
            )
        # Days are unique keys, so the newest `days` are unambiguous.
        bars.sort(key=lambda b: b.day)
        return History(symbol=symbol, bars=tuple(bars[-days:]))

    async def search(self, query: str, *, limit: int) -> list[Match]:
        answer = await self._query("SYMBOL_SEARCH", keywords=query)
        if "Error Message" in answer:
            return []
        raw = answer.get("bestMatches")
        if not isinstance(raw, list):
            raise ValueError("search answer has no match list")
        return [
            Match(
                symbol=str(m["1. symbol"]),
                name=str(m.get("2. name") or ""),
                kind=str(m.get("3. type") or ""),
                region=str(m.get("4. region") or ""),
                currency=str(m.get("8. currency") or ""),
            )
            for m in raw[:limit]
            if isinstance(m, dict) and m.get("1. symbol")
        ]


# --- the registry ---------------------------------------------------------------------------


class _KeyedBackend(Protocol):
    source: str

    def __call__(self, *, api_key: str, user_agent: str, allow_http: bool) -> MarketBackend: ...


def _requires_key(cls: _KeyedBackend) -> Callable[[Settings], MarketBackend]:
    def build(settings: Settings) -> MarketBackend:
        if not settings.market_api_key:
            # At the first compile that lists a stock tool, by name -- not a tool that fails each call.
            raise ValueError(f"FELIX_MARKET_API_KEY is not set; the {cls.source} market backend needs one")
        return cls(
            api_key=settings.market_api_key, user_agent=user_agent(settings), allow_http=allow_http(settings)
        )

    return build


BACKENDS: BackendRegistry[MarketBackend] = BackendRegistry("FELIX_MARKET_BACKEND")
register_market_backend = BACKENDS.register
BACKENDS.register("finnhub", _requires_key(FinnhubBackend))
BACKENDS.register("alphavantage", _requires_key(AlphaVantageBackend))


def build_market_backend(settings: Settings) -> MarketBackend:
    return BACKENDS.build(settings, settings.market_backend)


# --- rendering ------------------------------------------------------------------------------


def _price(value: float | None) -> str:
    return "n/a" if value is None else f"{value:,.4f}".rstrip("0").rstrip(".")


def _signed(value: float | None, suffix: str = "") -> str:
    return "n/a" if value is None else f"{value:+,.2f}{suffix}"


def as_of_line(as_of: datetime | date | None, *, now: datetime) -> str:
    """When the price is from, and how long ago -- the part a stale answer must not lose."""
    if as_of is None:
        return "as of: not reported"
    if isinstance(as_of, datetime):
        hours = (now - as_of).total_seconds() / 3600
        if hours < 1:
            ago = f"{hours * 60:.0f} min ago"
        elif hours < 48:
            ago = f"{hours:.0f} h ago"
        else:
            ago = f"{hours / 24:.0f} days ago"
        return f"as of: {as_of:%Y-%m-%d %H:%M} UTC ({ago}); delayed or real-time depends on the data plan"
    days = (now.date() - as_of).days
    ago = "today" if days == 0 else "1 day ago" if days == 1 else f"{days} days ago"
    return f"as of: the trading day {as_of.isoformat()} ({ago}); not an intraday price"


def render_quote(q: Quote, *, source: str, now: datetime | None = None) -> str:
    return "\n".join(
        [
            f"source: {source}",
            f"{one_line(q.symbol, 24)}: {_price(q.price)} "
            f"({_signed(q.change)}, {_signed(q.change_percent, '%')})",
            f"open {_price(q.open)} · high {_price(q.high)} · low {_price(q.low)} · "
            f"previous close {_price(q.previous_close)}",
            as_of_line(q.as_of, now=now or datetime.now(UTC)),
        ]
    )


def render_history(h: History, *, source: str) -> str:
    lines = [
        f"source: {source}",
        f"{one_line(h.symbol, 24)}: daily, oldest first (date: open high low close volume)",
    ]
    for b in h.bars:
        volume = "n/a" if b.volume is None else f"{b.volume:,.0f}"
        prices = " ".join(_price(p) for p in (b.open, b.high, b.low, b.close))
        lines.append(f"{b.day.isoformat()}: {prices} {volume}")
    if not h.bars:
        lines.append("(no trading days reported)")
    return "\n".join(lines)


def render_matches(matches: list[Match], *, source: str) -> str:
    lines = [f"source: {source}"]
    for i, m in enumerate(matches, 1):
        extra = " · ".join(one_line(x, 40) for x in (m.kind, m.region, m.currency) if x)
        lines.append(
            f"{i}. {one_line(m.symbol, 24)} · {one_line(m.name, 120)}" + (f" · {extra}" if extra else "")
        )
    if not matches:
        lines.append("no matching symbols")
    return "\n".join(lines)


# --- the tools ------------------------------------------------------------------------------


class QuoteArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: Symbol = Field(description="Ticker symbol, e.g. AAPL; find one with connectors__symbol_search.")


class HistoryArgs(QuoteArgs):
    days: int = Field(default=30, ge=1, le=MAX_HISTORY_DAYS, description="Trading days, newest last.")


class SearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=100, description="A company name or part of a ticker.")
    limit: int = Field(default=5, ge=1, le=MAX_MATCHES)


#: Every tool `build_tools` returns, registered by name before any is built.
TOOL_NAMES = ("connectors__quote", "connectors__quote_history", "connectors__symbol_search")


def build_tools(settings: Settings) -> list[Tool]:
    """The family's tools under `settings`: the interface `felix_connectors.register` reads."""
    return market_tools(backend=build_market_backend(settings))


def market_tools(*, backend: MarketBackend) -> list[Tool]:
    def not_found(symbol: str) -> str:
        return f"source: {backend.source}\nno data for {one_line(symbol, 24)}"

    async def quote(args: QuoteArgs) -> str:
        try:
            return render_quote(await backend.quote(args.symbol), source=backend.source)
        except UnknownSymbol:
            return not_found(args.symbol)

    async def history(args: HistoryArgs) -> str:
        try:
            return render_history(await backend.history(args.symbol, days=args.days), source=backend.source)
        except UnknownSymbol:
            return not_found(args.symbol)
        except HistoryUnavailable:
            return f"source: {backend.source}\nno daily history from this deployment's market data backend"

    async def search(args: SearchArgs) -> str:
        try:
            found = await backend.search(args.query, limit=args.limit)
        except UnknownSymbol:
            found = []
        return render_matches(found, source=backend.source)

    tool = partial(connector_tool, family="markets", service=backend.source, hosts=backend.hosts)
    return [
        tool(
            name="connectors__quote",
            args=QuoteArgs,
            run=quote,
            description="The latest price of a stock or fund, with the time it is from.",
        ),
        tool(
            name="connectors__quote_history",
            args=HistoryArgs,
            run=history,
            description="Daily open, high, low, close and volume for a symbol (up to 100 trading days).",
        ),
        tool(
            name="connectors__symbol_search",
            args=SearchArgs,
            run=search,
            description="Ticker symbols matching a company name.",
        ),
    ]


__all__ = [
    "ALPHAVANTAGE_URL",
    "BACKENDS",
    "FINNHUB_URL",
    "TOOL_NAMES",
    "AlphaVantageBackend",
    "Bar",
    "FinnhubBackend",
    "History",
    "HistoryUnavailable",
    "MarketBackend",
    "Match",
    "Quote",
    "UnknownSymbol",
    "as_of_line",
    "build_market_backend",
    "build_tools",
    "market_tools",
    "register_market_backend",
    "render_history",
    "render_matches",
    "render_quote",
]
