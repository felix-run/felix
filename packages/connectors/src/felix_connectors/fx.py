"""Exchange rates: the European Central Bank's reference rates, through Frankfurter.

Keyless, and its own family so it works where the stock tools (`markets`) have no key. The rates
are the ECB's daily reference fixings, published once a working day around 16:00 CET -- not a
market price -- and every answer says which day's they are, so an agent quoting one cannot present
yesterday's fixing as a live rate. The backend sits behind an open registry
(`register_fx_backend`), selected by `FELIX_FX_BACKEND`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from functools import partial
from typing import Annotated, Protocol, runtime_checkable

from felix.config import Settings
from felix.tools.types import Tool
from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from felix_connectors.http import Upstream, UpstreamError, allow_http, user_agent
from felix_connectors.registry import BackendRegistry
from felix_connectors.tools import connector_tool, iso_day, number, one_line

FRANKFURTER_URL = "https://api.frankfurter.dev"
MAX_SYMBOLS = 10
#: The ECB's series starts on 1999-01-04.
FIRST_DAY = date(1999, 1, 4)


def _currency(value: str) -> str:
    code = value.strip().upper()
    if len(code) != 3 or not code.isascii() or not code.isalpha():
        raise ValueError("a currency is a three-letter ISO 4217 code, e.g. USD")
    return code


Currency = Annotated[str, AfterValidator(_currency)]


@dataclass(frozen=True, slots=True)
class Rates:
    base: str
    amount: float
    #: The fixing's day: the latest working day's, or the one before a date asked for.
    day: date | None
    rates: Mapping[str, float] = field(default_factory=dict)


class UnknownCurrency(Exception):
    """A code the backend does not quote. Part of the `FxBackend` contract: answered as a finding."""


@runtime_checkable
class FxBackend(Protocol):
    name: str
    source: str
    hosts: frozenset[str]

    async def rates(self, base: str, symbols: Sequence[str], *, amount: float, on: date | None) -> Rates: ...


class FrankfurterBackend:
    """api.frankfurter.dev: ECB reference rates, no key. A date with no fixing (a weekend, a
    holiday) answers with the working day before it, which the answer names."""

    name = "frankfurter"
    source = "Frankfurter (European Central Bank reference rates)"

    def __init__(self, *, base_url: str = FRANKFURTER_URL, user_agent: str, allow_http: bool = False):
        self._up = Upstream(base_url, user_agent, allow_http)
        self.hosts = self._up.hosts

    async def rates(self, base: str, symbols: Sequence[str], *, amount: float, on: date | None) -> Rates:
        params = {"base": base, "amount": exact(amount)}
        if symbols:
            params["symbols"] = ",".join(symbols)
        try:
            raw = await self._up.get(f"/v1/{on.isoformat() if on else 'latest'}", params)
        except UpstreamError as exc:
            # Frankfurter answers a code it does not quote with a 404.
            if exc.status == 404:
                raise UnknownCurrency(base, *symbols) from None
            raise
        if not isinstance(raw, dict) or not isinstance(raw.get("rates"), dict):
            raise ValueError("answer has no rates")
        rates = {str(k): v for k, raw_v in raw["rates"].items() if (v := number(raw_v)) is not None}
        amount_back = number(raw.get("amount"))
        return Rates(base=base, amount=amount_back or amount, day=iso_day(raw.get("date")), rates=rates)


# --- the registry ---------------------------------------------------------------------------

BACKENDS: BackendRegistry[FxBackend] = BackendRegistry("FELIX_FX_BACKEND")
register_fx_backend = BACKENDS.register
BACKENDS.register(
    "frankfurter",
    lambda settings: FrankfurterBackend(user_agent=user_agent(settings), allow_http=allow_http(settings)),
)


def build_fx_backend(settings: Settings) -> FxBackend:
    return BACKENDS.build(settings, settings.fx_backend)


# --- rendering ------------------------------------------------------------------------------


def exact(value: float) -> str:
    """`value` in plain digits, every one the float holds: `:g` keeps six, so 1234567.89 went out
    as `1.23457e+06` and a different amount was converted."""
    text = format(Decimal(repr(value)), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _grouped(value: float) -> str:
    whole, _, frac = exact(value).partition(".")
    return f"{int(whole):,}" + (f".{frac}" if frac else "")


def render_rates(r: Rates, *, source: str, asked: date | None = None, wanted: Sequence[str] = ()) -> str:
    if r.day is None:
        day = "fixing day: not reported"
    elif asked is not None and r.day != asked:
        # Earlier for a weekend or a holiday, and the latest for a day not yet published.
        day = f"fixing day: {r.day.isoformat()} (none was published for {asked.isoformat()})"
    else:
        day = f"fixing day: {r.day.isoformat()} (a daily reference rate, not a live price)"
    lines = [f"source: {source}", day]
    lines += [
        f"{_grouped(r.amount)} {one_line(r.base, 3)} = {_grouped(value)} {one_line(code, 3)}"
        for code, value in sorted(r.rates.items())
    ]
    # Frankfurter drops a code it does not quote and answers for the rest.
    missing = [c for c in wanted if c not in r.rates]
    if missing:
        lines.append(f"no rate for {', '.join(missing)}")
    elif not r.rates:
        lines.append("no rates reported")
    return "\n".join(lines)


# --- the tools ------------------------------------------------------------------------------


class FxArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base: Currency = Field(description="The currency to convert from, e.g. USD.")
    symbols: list[Currency] = Field(
        default_factory=list, max_length=MAX_SYMBOLS, description="Currencies to convert to; all if empty."
    )
    amount: float = Field(default=1.0, gt=0, le=1e12)
    day: date | None = Field(
        default=None, ge=FIRST_DAY, description="A past day's fixing; the latest if omitted."
    )


#: Every tool `build_tools` returns, registered by name before any is built.
TOOL_NAMES = ("connectors__fx_rate",)


def build_tools(settings: Settings) -> list[Tool]:
    """The family's tools under `settings`: the interface `felix_connectors.register` reads."""
    return fx_tools(backend=build_fx_backend(settings))


def fx_tools(*, backend: FxBackend) -> list[Tool]:
    async def rate(args: FxArgs) -> str:
        # The ECB publishes in Frankfurt; a day past UTC's has certainly not been.
        if args.day is not None and args.day > datetime.now(UTC).date():
            return f"source: {backend.source}\nno fixing for a future day ({args.day.isoformat()})"
        try:
            found = await backend.rates(args.base, args.symbols, amount=args.amount, on=args.day)
        except UnknownCurrency:
            asked = ", ".join([args.base, *args.symbols])
            return f"source: {backend.source}\nno rates for one of {asked}"
        return render_rates(found, source=backend.source, asked=args.day, wanted=args.symbols)

    tool = partial(connector_tool, family="fx", service=backend.source, hosts=backend.hosts)
    return [
        tool(
            name="connectors__fx_rate",
            args=FxArgs,
            run=rate,
            description="Convert an amount between currencies at the ECB's daily reference rate.",
        )
    ]


__all__ = [
    "BACKENDS",
    "FRANKFURTER_URL",
    "TOOL_NAMES",
    "FrankfurterBackend",
    "FxBackend",
    "Rates",
    "UnknownCurrency",
    "build_fx_backend",
    "build_tools",
    "exact",
    "fx_tools",
    "register_fx_backend",
    "render_rates",
]
