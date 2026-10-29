"""`felix_connectors` market and exchange-rate tools, against a loopback server.

As with weather and maps, each backend is pointed at `tests.support.loopback_http` under the
development `allow_http` exemption, so what is checked is the request production sends -- where
the key travels included -- and the answer as the model reads it. The production path, keyed by
`FELIX_MARKET_API_KEY`, is pinned through `default_tool_provider` and `get_settings`.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.support.factories import make_settings
from tests.support.loopback_http import Request, respond, serve
from tests.support.optional_deps import require_optional

KEY = "k3y-for-tests"
FINNHUB_QUOTE = {
    "c": 253.22,
    "d": 2.97,
    "dp": 1.1868,
    "h": 253.85,
    "l": 247.67,
    "o": 248.49,
    "pc": 250.25,
    "t": 1761681600,
}
AV_QUOTE = {
    "Global Quote": {
        "01. symbol": "IBM",
        "02. open": "248.4900",
        "03. high": "253.8500",
        "04. low": "247.6700",
        "05. price": "253.2200",
        "06. volume": "3706461",
        "07. latest trading day": "2026-10-28",
        "08. previous close": "250.2500",
        "09. change": "2.9700",
        "10. change percent": "1.1868%",
    }
}


def _bar(o: str, c: str) -> dict[str, str]:
    return {"1. open": o, "2. high": c, "3. low": o, "4. close": c, "5. volume": "1000"}


AV_DAILY = {
    "Meta Data": {"1. Information": "Daily Prices", "2. Symbol": "IBM"},
    "Time Series (Daily)": {
        "2026-10-28": _bar("248.49", "253.22"),
        "2026-10-24": _bar("240", "241.5"),
        "2026-10-27": _bar("251.93", "250.25"),
        "not a day": _bar("1", "1"),
        "2026-10-23": {"1. open": "1"},
        "2026-10-22": "junk",
    },
}


@pytest.fixture(autouse=True)
def _plugin() -> Any:
    return require_optional("felix_connectors", "connectors")


def _json_responder(payload: Any, seen: list[Request], status: str = "200 OK") -> Any:
    def responder(request: Request, writer: Any) -> None:
        seen.append(request)
        respond(writer, json.dumps(payload).encode(), status=status, ctype="application/json")

    return responder


def _query(request: Request) -> dict[str, list[str]]:
    return parse_qs(urlsplit(request.path).query, keep_blank_values=True)


def _market(base: str, which: str) -> dict[str, Any]:
    from felix_connectors.markets import AlphaVantageBackend, FinnhubBackend, market_tools

    cls = FinnhubBackend if which == "finnhub" else AlphaVantageBackend
    backend = cls(api_key=KEY, base_url=base, user_agent="felix-test", allow_http=True)
    return {t.name: t for t in market_tools(backend=backend)}


def _fx(base: str) -> Any:
    from felix_connectors.fx import FrankfurterBackend, fx_tools

    return fx_tools(backend=FrankfurterBackend(base_url=base, user_agent="felix-test", allow_http=True))[0]


async def _call(which: str, tool: str, args: dict[str, Any], payload: Any, status: str = "200 OK") -> Any:
    seen: list[Request] = []
    async with serve(_json_responder(payload, seen, status=status)) as base:
        if which == "fx":
            out = await _fx(base).executor.execute(args)
        else:
            out = await _market(base, which)[tool].executor.execute(args)
    return out, seen


def _error(out: Any) -> tuple[str | None, str]:
    from felix.tools.errors import read_tool_error_code
    from felix.tools.types import tool_output_content

    return read_tool_error_code(out), tool_output_content(out)


# --- Finnhub --------------------------------------------------------------------------------


async def test_a_finnhub_quote_sends_the_key_in_a_header_never_the_url() -> None:
    out, seen = await _call("finnhub", "connectors__quote", {"symbol": "AAPL"}, FINNHUB_QUOTE)
    assert urlsplit(seen[0].path).path == "/quote"
    assert _query(seen[0]) == {"symbol": ["AAPL"]}
    assert KEY not in seen[0].path
    assert seen[0].headers["x-finnhub-token"] == KEY
    lines = out.splitlines()
    assert lines[:3] == [
        "source: Finnhub",
        "AAPL: 253.22 (+2.97, +1.19%)",
        "open 248.49 · high 253.85 · low 247.67 · previous close 250.25",
    ]
    assert lines[3].startswith("as of: 2025-10-28 20:00 UTC (")
    assert lines[3].endswith("ago); delayed or real-time depends on the data plan")


@pytest.mark.parametrize(
    "payload",
    [
        {"c": 0, "d": None, "dp": None, "h": 0, "l": 0, "o": 0, "pc": 0, "t": 0},
        # A price with no time would render as 1970's.
        {"c": 253.22, "t": 0},
        {"c": 0, "t": 1761681600},
    ],
)
async def test_a_symbol_finnhub_does_not_know_is_no_data_not_a_price_of_zero(payload: Any) -> None:
    out, _ = await _call("finnhub", "connectors__quote", {"symbol": "NOPE"}, payload)
    assert _error(out)[0] is None
    assert out.splitlines() == ["source: Finnhub", "no data for NOPE"]


async def test_finnhub_history_says_there_is_none_without_a_request() -> None:
    out, seen = await _call("finnhub", "connectors__quote_history", {"symbol": "AAPL"}, {})
    assert seen == []
    assert _error(out)[0] is None
    assert out.splitlines() == [
        "source: Finnhub",
        "no daily history from this deployment's market data backend",
    ]


async def test_a_finnhub_search_renders_each_match() -> None:
    found = {
        "count": 2,
        "result": [{"description": "APPLE INC", "symbol": "AAPL", "type": "Common Stock"}, "junk"],
    }
    out, seen = await _call("finnhub", "connectors__symbol_search", {"query": "apple"}, found)
    assert _query(seen[0]) == {"q": ["apple"]}
    assert out.splitlines() == ["source: Finnhub", "1. AAPL · APPLE INC · Common Stock"]


# --- Alpha Vantage --------------------------------------------------------------------------


async def test_an_alpha_vantage_quote_is_a_trading_days_close() -> None:
    out, seen = await _call("alphavantage", "connectors__quote", {"symbol": "IBM"}, AV_QUOTE)
    assert urlsplit(seen[0].path).path == "/query"
    assert _query(seen[0]) == {"function": ["GLOBAL_QUOTE"], "symbol": ["IBM"], "apikey": [KEY]}
    lines = out.splitlines()
    assert lines[:3] == [
        "source: Alpha Vantage",
        "IBM: 253.22 (+2.97, +1.19%)",
        "open 248.49 · high 253.85 · low 247.67 · previous close 250.25",
    ]
    assert lines[3].startswith("as of: the trading day 2026-10-28 (")
    assert lines[3].endswith("); not an intraday price")


@pytest.mark.parametrize("note", ["Information", "Note"])
async def test_a_spent_alpha_vantage_quota_is_rate_limited_not_data(note: str) -> None:
    out, _ = await _call("alphavantage", "connectors__quote", {"symbol": "IBM"}, {note: "25 requests a day"})
    assert _error(out) == (
        "rate_limited",
        "[tool error/rate_limited] Alpha Vantage's request quota is spent for now",
    )


@pytest.mark.parametrize("payload", [{"Error Message": "Invalid API call."}, {"Global Quote": {}}])
async def test_an_alpha_vantage_symbol_it_does_not_know_is_no_data(payload: Any) -> None:
    out, _ = await _call("alphavantage", "connectors__quote", {"symbol": "NOPE"}, payload)
    assert out.splitlines() == ["source: Alpha Vantage", "no data for NOPE"]


async def test_an_alpha_vantage_error_for_a_search_is_no_matches_not_an_outage() -> None:
    out, _ = await _call(
        "alphavantage", "connectors__symbol_search", {"query": "x"}, {"Error Message": "Invalid"}
    )
    assert out.splitlines() == ["source: Alpha Vantage", "no matching symbols"]


async def test_alpha_vantage_history_is_the_newest_days_oldest_first() -> None:
    out, seen = await _call(
        "alphavantage", "connectors__quote_history", {"symbol": "IBM", "days": 2}, AV_DAILY
    )
    assert _query(seen[0]) == {
        "function": ["TIME_SERIES_DAILY"],
        "symbol": ["IBM"],
        "outputsize": ["compact"],
        "apikey": [KEY],
    }
    assert out.splitlines() == [
        "source: Alpha Vantage",
        "IBM: daily, oldest first (date: open high low close volume)",
        "2026-10-27: 251.93 250.25 251.93 250.25 1,000",
        "2026-10-28: 248.49 253.22 248.49 253.22 1,000",
    ]


async def test_alpha_vantage_search_renders_region_and_currency() -> None:
    found = {
        "bestMatches": [
            {
                "1. symbol": "TSCO.LON",
                "2. name": "Tesco PLC",
                "3. type": "Equity",
                "4. region": "United Kingdom",
                "8. currency": "GBX",
            }
        ]
    }
    out, seen = await _call("alphavantage", "connectors__symbol_search", {"query": "tesco"}, found)
    assert _query(seen[0])["keywords"] == ["tesco"]
    assert out.splitlines()[1] == "1. TSCO.LON · Tesco PLC · Equity · United Kingdom · GBX"


@pytest.mark.parametrize("symbol", ["BRK.B", "TSCO.LON", "^GSPC", "EURUSD=X"])
async def test_a_symbol_in_any_exchange_spelling_reaches_the_upstream_as_written(symbol: str) -> None:
    _, seen = await _call("finnhub", "connectors__quote", {"symbol": symbol}, FINNHUB_QUOTE)
    assert _query(seen[0])["symbol"] == [symbol]


@pytest.mark.parametrize(
    ("which", "payload"),
    [
        ("finnhub", {"result": [{"symbol": "A", "description": "a"}, {"symbol": "B", "description": "b"}]}),
        ("alphavantage", {"bestMatches": [{"1. symbol": "A"}, {"1. symbol": "B"}]}),
    ],
)
async def test_search_returns_at_most_the_limit(which: str, payload: Any) -> None:
    out, _ = await _call(which, "connectors__symbol_search", {"query": "a", "limit": 1}, payload)
    assert [line[:2] for line in out.splitlines()[1:]] == ["1."]


async def test_a_key_alpha_vantage_refuses_is_the_operators_problem_not_a_missing_symbol() -> None:
    refusal = {"Error Message": "the parameter apikey is invalid or missing. Please claim your free API key"}
    out, _ = await _call("alphavantage", "connectors__quote", {"symbol": "IBM"}, refusal)
    assert _error(out) == (
        "permission_denied",
        "[tool error/permission_denied] Alpha Vantage refused this deployment's key",
    )


async def test_a_series_with_no_usable_day_says_so() -> None:
    junk = {"Time Series (Daily)": {"not a day": _bar("1", "1"), "2026-10-22": "junk"}}
    out, _ = await _call("alphavantage", "connectors__quote_history", {"symbol": "IBM"}, junk)
    assert out.splitlines()[-1] == "(no trading days reported)"


@pytest.mark.parametrize("symbol", ["AAPL&apikey=x", "A B", "", "X" * 25])
async def test_a_symbol_outside_the_ticker_grammar_never_reaches_the_upstream(symbol: str) -> None:
    out, seen = await _call("alphavantage", "connectors__quote", {"symbol": symbol}, AV_QUOTE)
    assert seen == []
    assert _error(out)[0] == "invalid_arguments"


@pytest.mark.parametrize(
    ("age", "text"),
    [
        (timedelta(minutes=20), "(20 min ago)"),
        (timedelta(hours=5), "(5 h ago)"),
        (timedelta(days=4), "(4 days ago)"),
    ],
)
def test_an_instant_says_how_long_ago_it_was(age: timedelta, text: str) -> None:
    from felix_connectors.markets import as_of_line

    now = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
    assert text in as_of_line(now - age, now=now)


@pytest.mark.parametrize(
    ("day", "text"), [(date(2026, 10, 29), "(today)"), (date(2026, 10, 28), "(1 day ago)")]
)
def test_a_trading_day_says_how_long_ago_it_was(day: date, text: str) -> None:
    from felix_connectors.markets import as_of_line

    assert text in as_of_line(day, now=datetime(2026, 10, 29, 12, 0, tzinfo=UTC))


# --- the production path --------------------------------------------------------------------


@pytest.fixture
def keyed(monkeypatch: pytest.MonkeyPatch) -> Any:
    from felix.config import get_settings

    def use(**env: str) -> None:
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        get_settings.cache_clear()

    yield use
    get_settings.cache_clear()


def test_a_stock_tool_without_a_key_fails_by_the_setting_it_needs(keyed: Any) -> None:
    from felix.tools.builtins import default_tool_provider

    keyed(FELIX_MARKET_API_KEY="", FELIX_MARKET_BACKEND="finnhub", FELIX_FX_BACKEND="frankfurter")
    provider = default_tool_provider()
    with pytest.raises(
        ValueError, match="FELIX_MARKET_API_KEY is not set; the Finnhub market backend needs one"
    ):
        provider.get("connectors__quote")
    # Exchange rates are their own family, so they stay available without one.
    assert provider.get("connectors__fx_rate").egress_hosts == {"api.frankfurter.dev"}


@pytest.mark.parametrize(
    ("backend", "host"), [("finnhub", "finnhub.io"), ("alphavantage", "www.alphavantage.co")]
)
def test_the_market_tools_are_untrusted_read_only_and_declare_their_host(
    keyed: Any, backend: str, host: str
) -> None:
    from felix.manifests.builder import _is_untrusted_tool
    from felix.tools.builtins import default_tool_provider
    from felix_connectors.markets import TOOL_NAMES

    keyed(FELIX_MARKET_API_KEY=KEY, FELIX_MARKET_BACKEND=backend)
    provider = default_tool_provider()
    for name in TOOL_NAMES:
        tool = provider.get(name)
        assert _is_untrusted_tool(tool), name
        assert tool.read_only and tool.replay_safe, name
        assert tool.egress_hosts == {host}, name


@pytest.mark.parametrize(
    ("overrides", "insecure"),
    [({"allow_insecure": False}, False), ({"environment": "development", "allow_insecure": True}, True)],
)
def test_the_built_backends_carry_the_operators_terms(overrides: dict[str, Any], insecure: bool) -> None:
    from felix_connectors.fx import build_fx_backend
    from felix_connectors.http import USER_AGENT
    from felix_connectors.markets import build_market_backend

    ua = f"{USER_AGENT} ops@example.com"
    common = {"connectors_contact": "ops@example.com", "market_api_key": KEY, **overrides}
    finnhub = build_market_backend(make_settings(**common))
    alpha = build_market_backend(make_settings(market_backend="alphavantage", **common))
    fx = build_fx_backend(make_settings(**common))
    for built in (finnhub, alpha, fx):
        assert built._up.user_agent == ua, built.name  # type: ignore[attr-defined]
        assert built._up.allow_http is insecure, built.name  # type: ignore[attr-defined]
    assert finnhub._up.headers == {"x-finnhub-token": KEY}  # type: ignore[attr-defined]
    assert alpha._key == KEY  # type: ignore[attr-defined]
    # The key is in no repr a log line or a traceback could print.
    assert KEY not in repr(finnhub._up)  # type: ignore[attr-defined]


# --- exchange rates -------------------------------------------------------------------------

RATES = {
    "amount": 100.0,
    "base": "USD",
    "date": "2026-10-29",
    "rates": {"JPY": 15806, "EUR": 89.847, "BAD": "x"},
}


async def test_fx_asks_frankfurter_for_the_latest_fixing() -> None:
    out, seen = await _call("fx", "", {"base": "usd", "symbols": ["jpy", "EUR"], "amount": 100}, RATES)
    assert urlsplit(seen[0].path).path == "/v1/latest"
    assert _query(seen[0]) == {"base": ["USD"], "amount": ["100"], "symbols": ["JPY,EUR"]}
    assert out.splitlines() == [
        "source: Frankfurter (European Central Bank reference rates)",
        "fixing day: 2026-10-29 (a daily reference rate, not a live price)",
        "100 USD = 89.847 EUR",
        "100 USD = 15,806 JPY",
    ]


async def test_a_day_without_a_fixing_names_the_day_it_got() -> None:
    weekend = {"amount": 1.0, "base": "EUR", "date": "2026-10-23", "rates": {"USD": 1.1193}}
    out, seen = await _call("fx", "", {"base": "EUR", "symbols": ["USD"], "day": "2026-10-25"}, weekend)
    assert urlsplit(seen[0].path).path == "/v1/2026-10-25"
    assert out.splitlines()[1] == "fixing day: 2026-10-23 (none was published for 2026-10-25)"


async def test_a_code_frankfurter_drops_from_its_answer_is_named() -> None:
    partial = {"amount": 1.0, "base": "USD", "date": "2026-10-29", "rates": {"EUR": 0.89847}}
    out, _ = await _call("fx", "", {"base": "USD", "symbols": ["EUR", "XYZ"]}, partial)
    assert out.splitlines()[2:] == ["1 USD = 0.89847 EUR", "no rate for XYZ"]


@pytest.mark.parametrize(
    ("amount", "sent", "line"),
    [
        (1234567.89, "1234567.89", "1,234,567.89 USD = 1,109,225 EUR"),
        (1e12, "1000000000000", "1,000,000,000,000 USD = 1,109,225 EUR"),
        (0.0000001, "0.0000001", "0.0000001 USD = 1,109,225 EUR"),
    ],
)
async def test_an_amount_is_sent_and_shown_in_every_digit(amount: float, sent: str, line: str) -> None:
    payload = {"base": "USD", "date": "2026-10-29", "rates": {"EUR": 1109225}}
    out, seen = await _call("fx", "", {"base": "USD", "symbols": ["EUR"], "amount": amount}, payload)
    assert _query(seen[0])["amount"] == [sent]
    # The upstream echoed no amount here, so what is shown is what was asked.
    assert out.splitlines()[2] == line


async def test_no_symbols_asks_for_every_rate_and_an_empty_answer_says_so() -> None:
    out, seen = await _call(
        "fx", "", {"base": "USD"}, {"amount": 1, "base": "USD", "date": "2026-10-29", "rates": {}}
    )
    assert "symbols" not in _query(seen[0])
    assert out.splitlines()[-1] == "no rates reported"


async def test_a_currency_frankfurter_does_not_quote_is_an_answer() -> None:
    out, _ = await _call("fx", "", {"base": "XYZ"}, {"message": "not found"}, status="404 Not Found")
    assert _error(out)[0] is None
    assert out.splitlines()[-1] == "no rates for one of XYZ"


@pytest.mark.parametrize(
    "args",
    [
        {"base": "US"},
        {"base": "U$D"},
        {"base": "USD", "symbols": ["EUR,GBP"]},
        {"base": "USD", "day": "1998-12-31"},
        {"base": "USD", "amount": 0},
        {"base": "USD", "symbols": ["EUR"] * 11},
    ],
)
async def test_fx_arguments_out_of_grammar_never_reach_the_upstream(args: dict[str, Any]) -> None:
    out, seen = await _call("fx", "", args, RATES)
    assert seen == []
    assert _error(out)[0] == "invalid_arguments"


async def test_a_day_past_utc_today_is_refused_and_today_is_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    from felix_connectors import fx

    class Frozen(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Any:
            # 23:30 in Phoenix, already the next day in UTC.
            return datetime(2026, 10, 30, 6, 30, tzinfo=UTC)

    monkeypatch.setattr(fx, "datetime", Frozen)
    out, seen = await _call("fx", "", {"base": "USD", "day": "2026-10-31"}, RATES)
    assert seen == []
    assert out.splitlines()[-1] == "no fixing for a future day (2026-10-31)"
    _, seen = await _call("fx", "", {"base": "USD", "day": "2026-10-30"}, RATES)
    assert len(seen) == 1


# --- the key, out of logs and into masking --------------------------------------------------


async def test_a_key_in_the_url_never_reaches_a_log_line(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    from felix.logging_setup import configure_logging

    configure_logging(make_settings())
    try:
        with caplog.at_level(logging.DEBUG):
            out, seen = await _call("alphavantage", "connectors__quote", {"symbol": "IBM"}, AV_QUOTE)
        assert KEY in seen[0].path
        assert out.startswith("source: Alpha Vantage")
        assert KEY not in caplog.text
    finally:
        configure_logging(make_settings())


def test_the_market_key_is_hydrated_masked_and_header_safe() -> None:
    from felix.secrets import _HYDRATE_MAP, collected_secret_values

    assert "market_api_key" in _HYDRATE_MAP
    assert "mk-supersecret-1234" in collected_secret_values(
        make_settings(market_api_key="mk-supersecret-1234")
    )
    # A trailing CRLF from a `.env` or secret file goes; whitespace inside is refused at load.
    assert make_settings(market_api_key="k3y\r\n").market_api_key == "k3y"
    with pytest.raises(ValueError, match="FELIX_MARKET_API_KEY must be printable ASCII"):
        make_settings(market_api_key="k 3y")
