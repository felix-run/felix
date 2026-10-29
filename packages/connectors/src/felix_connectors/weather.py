"""Weather: current conditions and a daily forecast for a point, and US alerts.

The forecast sits behind an open registry (`register_weather_backend`), selected by
`FELIX_WEATHER_BACKEND`, so a deployment that pays for WeatherKit or Tomorrow.io registers one
and never touches this file. The bundled backend is Open-Meteo, which needs no key -- the same
posture as the rest of the default install. Alerts come from the US National Weather Service,
also keyless, and say so when a point is outside its coverage rather than reporting calm.

Every answer names its source, and current conditions carry the time the upstream observed
them, so an agent cannot present a stale reading as the current one.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from felix.config import Settings
from felix.security.host_allowlist import url_host
from felix.tools.types import Tool
from pydantic import BaseModel, ConfigDict, Field

from felix_connectors.http import allow_http, get_json, user_agent
from felix_connectors.registry import BackendRegistry
from felix_connectors.tools import connector_tool, one_line

logger = logging.getLogger("felix_connectors.weather")

Units = Literal["metric", "imperial"]
MAX_FORECAST_DAYS = 16
MAX_ALERTS = 5
#: An alert's prose, per alert: enough for what to do, not the whole bulletin.
MAX_ALERT_TEXT = 600

# WMO weather interpretation codes, as Open-Meteo reports them.
_WMO = {
    0: "clear",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "freezing fog",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    56: "freezing drizzle",
    57: "heavy freezing drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    66: "freezing rain",
    67: "heavy freezing rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    77: "snow grains",
    80: "light showers",
    81: "showers",
    82: "violent showers",
    85: "snow showers",
    86: "heavy snow showers",
    95: "thunderstorm",
    96: "thunderstorm with hail",
    99: "thunderstorm with heavy hail",
}


def condition(code: object) -> str:
    return _WMO.get(code, "unknown") if isinstance(code, int) else "unknown"


@dataclass(frozen=True, slots=True)
class UnitLabels:
    temperature: str
    speed: str
    precipitation: str

    @classmethod
    def of(cls, units: Units) -> UnitLabels:
        return cls("°F", "mph", "in") if units == "imperial" else cls("°C", "km/h", "mm")


@dataclass(frozen=True, slots=True)
class Current:
    time: str
    temperature: float | None
    feels_like: float | None
    humidity: float | None
    precipitation: float | None
    wind_speed: float | None
    wind_direction: float | None
    condition: str


@dataclass(frozen=True, slots=True)
class Day:
    date: str
    condition: str
    high: float | None
    low: float | None
    precipitation: float | None
    precipitation_chance: float | None
    wind_max: float | None


@dataclass(frozen=True, slots=True)
class Forecast:
    source: str
    latitude: float
    longitude: float
    timezone: str
    units: Units
    current: Current | None
    days: Sequence[Day] = field(default_factory=tuple)


@runtime_checkable
class WeatherBackend(Protocol):
    """Answers for a point. `hosts` is every host it connects to: the tools declare them, and the
    compile holds them to the agent's `network.allow_hosts`."""

    name: str
    #: The upstream, as an answer and a failure name it.
    source: str
    hosts: frozenset[str]

    async def forecast(self, latitude: float, longitude: float, *, days: int, units: Units) -> Forecast: ...


def _num(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _at(values: object, i: int) -> object:
    return values[i] if isinstance(values, list) and i < len(values) else None


class OpenMeteoBackend:
    """api.open-meteo.com: no key, global, up to sixteen days."""

    name = "open-meteo"
    source = "Open-Meteo"
    _CURRENT = (
        "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,"
        "weather_code,wind_speed_10m,wind_direction_10m"
    )
    _DAILY = (
        "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,"
        "precipitation_probability_max,wind_speed_10m_max"
    )

    def __init__(
        self, *, base_url: str = "https://api.open-meteo.com", user_agent: str, allow_http: bool = False
    ):
        self._url = base_url.rstrip("/") + "/v1/forecast"
        self._headers = {"user-agent": user_agent, "accept": "application/json"}
        self._allow_http = allow_http
        self.hosts = frozenset({url_host(self._url)})

    async def forecast(self, latitude: float, longitude: float, *, days: int, units: Units) -> Forecast:
        params = {
            "latitude": f"{latitude:.4f}",
            "longitude": f"{longitude:.4f}",
            "current": self._CURRENT,
            "daily": self._DAILY,
            "forecast_days": str(days),
            "timezone": "auto",
        }
        if units == "imperial":
            params |= {
                "temperature_unit": "fahrenheit",
                "wind_speed_unit": "mph",
                "precipitation_unit": "inch",
            }
        payload = await get_json(
            self._url, params=params, headers=self._headers, hosts=self.hosts, allow_http=self._allow_http
        )
        if not isinstance(payload, dict):
            raise ValueError("forecast is not an object")
        return Forecast(
            source=self.source,
            latitude=_num(payload.get("latitude")) or latitude,
            longitude=_num(payload.get("longitude")) or longitude,
            timezone=str(payload.get("timezone") or "UTC"),
            units=units,
            current=self._current(payload.get("current")),
            days=self._days(payload.get("daily")),
        )

    @staticmethod
    def _current(raw: object) -> Current | None:
        if not isinstance(raw, dict):
            return None
        return Current(
            time=str(raw.get("time") or ""),
            temperature=_num(raw.get("temperature_2m")),
            feels_like=_num(raw.get("apparent_temperature")),
            humidity=_num(raw.get("relative_humidity_2m")),
            precipitation=_num(raw.get("precipitation")),
            wind_speed=_num(raw.get("wind_speed_10m")),
            wind_direction=_num(raw.get("wind_direction_10m")),
            condition=condition(raw.get("weather_code")),
        )

    @staticmethod
    def _days(raw: object) -> tuple[Day, ...]:
        if not isinstance(raw, dict) or not isinstance(raw.get("time"), list):
            return ()
        return tuple(
            Day(
                date=str(date),
                condition=condition(_at(raw.get("weather_code"), i)),
                high=_num(_at(raw.get("temperature_2m_max"), i)),
                low=_num(_at(raw.get("temperature_2m_min"), i)),
                precipitation=_num(_at(raw.get("precipitation_sum"), i)),
                precipitation_chance=_num(_at(raw.get("precipitation_probability_max"), i)),
                wind_max=_num(_at(raw.get("wind_speed_10m_max"), i)),
            )
            for i, date in enumerate(raw["time"][:MAX_FORECAST_DAYS])
        )


@dataclass(frozen=True, slots=True)
class Alert:
    event: str
    severity: str
    headline: str
    area: str
    effective: str
    expires: str
    instruction: str


@runtime_checkable
class AlertsSource(Protocol):
    source: str
    hosts: frozenset[str]

    async def active(self, latitude: float, longitude: float) -> list[Alert]: ...


class NwsAlerts:
    """api.weather.gov's active alerts for a point. US states and territories only."""

    source = "US National Weather Service"

    def __init__(
        self, *, base_url: str = "https://api.weather.gov", user_agent: str, allow_http: bool = False
    ):
        self._url = base_url.rstrip("/") + "/alerts/active"
        # The NWS refuses a request without a User-Agent.
        self._headers = {"user-agent": user_agent, "accept": "application/geo+json"}
        self._allow_http = allow_http
        self.hosts = frozenset({url_host(self._url)})

    async def active(self, latitude: float, longitude: float) -> list[Alert]:
        params = {"point": f"{latitude:.4f},{longitude:.4f}"}
        payload = await get_json(
            self._url, params=params, headers=self._headers, hosts=self.hosts, allow_http=self._allow_http
        )
        features = payload.get("features") if isinstance(payload, dict) else None
        out: list[Alert] = []
        for feature in features if isinstance(features, list) else []:
            props = feature.get("properties") if isinstance(feature, dict) else None
            if not isinstance(props, dict):
                continue
            out.append(
                Alert(
                    event=str(props.get("event") or ""),
                    severity=str(props.get("severity") or ""),
                    headline=str(props.get("headline") or ""),
                    area=str(props.get("areaDesc") or ""),
                    effective=str(props.get("effective") or ""),
                    expires=str(props.get("expires") or ""),
                    instruction=str(props.get("instruction") or props.get("description") or ""),
                )
            )
        return out


# --- the registry ---------------------------------------------------------------------------

BACKENDS: BackendRegistry[WeatherBackend] = BackendRegistry("FELIX_WEATHER_BACKEND")
register_weather_backend = BACKENDS.register
BACKENDS.register(
    "open-meteo",
    lambda settings: OpenMeteoBackend(user_agent=user_agent(settings), allow_http=allow_http(settings)),
)


def build_weather_backend(settings: Settings) -> WeatherBackend:
    """The backend `FELIX_WEATHER_BACKEND` names; `ValueError` for one nothing registered."""
    return BACKENDS.build(settings, settings.weather_backend)


def build_alerts(settings: Settings) -> AlertsSource:
    return NwsAlerts(user_agent=user_agent(settings), allow_http=allow_http(settings))


# --- rendering ------------------------------------------------------------------------------


def _fmt(value: float | None, unit: str) -> str:
    return "n/a" if value is None else f"{value:g}{unit}"


def _header(f: Forecast) -> str:
    return f"source: {f.source} · {f.latitude:g},{f.longitude:g} · {one_line(f.timezone, 60)}"


def render_current(f: Forecast) -> str:
    u = UnitLabels.of(f.units)
    head = _header(f)
    c = f.current
    if c is None:
        return f"{head}\n(no current conditions reported)"
    direction = "" if c.wind_direction is None else f" from {c.wind_direction:g}°"
    return "\n".join(
        [
            head,
            f"observed: {one_line(c.time, 40)} (local)",
            f"conditions: {c.condition}",
            f"temperature: {_fmt(c.temperature, u.temperature)}"
            f" (feels like {_fmt(c.feels_like, u.temperature)})",
            f"humidity: {_fmt(c.humidity, '%')}",
            f"precipitation: {_fmt(c.precipitation, ' ' + u.precipitation)}",
            f"wind: {_fmt(c.wind_speed, ' ' + u.speed)}{direction}",
        ]
    )


def render_forecast(f: Forecast) -> str:
    u = UnitLabels.of(f.units)
    lines = [_header(f)]
    for d in f.days:
        chance = "" if d.precipitation_chance is None else f", {d.precipitation_chance:g}% chance"
        lines.append(
            f"{one_line(d.date, 20)}: {d.condition}, high {_fmt(d.high, u.temperature)}, "
            f"low {_fmt(d.low, u.temperature)}, precipitation {_fmt(d.precipitation, ' ' + u.precipitation)}"
            f"{chance}, wind to {_fmt(d.wind_max, ' ' + u.speed)}"
        )
    if not f.days:
        lines.append("(no forecast days reported)")
    return "\n".join(lines)


def render_alerts(alerts: list[Alert], *, source: str = NwsAlerts.source) -> str:
    if not alerts:
        # Not "all clear": the NWS answers the same way for a point it does not cover.
        return f"source: {source}\nno active alerts (coverage: US states and territories)"
    lines = [f"source: {source}"]
    for i, a in enumerate(alerts[:MAX_ALERTS], 1):
        lines.append(
            f"{i}. {one_line(a.event, 80)} ({one_line(a.severity, 20)}): {one_line(a.headline, 240)}"
        )
        lines.append(f"   area: {one_line(a.area, 240)}")
        lines.append(f"   from {one_line(a.effective, 40)} until {one_line(a.expires, 40)}")
        if a.instruction:
            lines.append(f"   {one_line(a.instruction, MAX_ALERT_TEXT)}")
    if len(alerts) > MAX_ALERTS:
        lines.append(f"(+{len(alerts) - MAX_ALERTS} more)")
    return "\n".join(lines)


# --- the tools ------------------------------------------------------------------------------


class _Point(BaseModel):
    model_config = ConfigDict(extra="forbid")

    latitude: float = Field(ge=-90, le=90, description="Latitude in decimal degrees.")
    longitude: float = Field(ge=-180, le=180, description="Longitude in decimal degrees.")


class CurrentArgs(_Point):
    units: Units | None = Field(
        default=None, description="metric or imperial; the operator's default if omitted."
    )


class ForecastArgs(CurrentArgs):
    days: int = Field(default=3, ge=1, le=MAX_FORECAST_DAYS, description="Days ahead, today included.")


def _units(settings: Settings, asked: Units | None) -> Units:
    return asked if asked is not None else settings.weather_units


#: Every tool `build_tools` returns, registered by name before any is built.
TOOL_NAMES = ("connectors__weather_current", "connectors__weather_forecast", "connectors__weather_alerts")


def build_tools(settings: Settings) -> list[Tool]:
    """The family's tools under `settings`: the interface `felix_connectors.register` reads."""
    return weather_tools(settings, backend=build_weather_backend(settings), alerts=build_alerts(settings))


def weather_tools(settings: Settings, *, backend: WeatherBackend, alerts: AlertsSource) -> list[Tool]:
    async def current(args: CurrentArgs) -> str:
        units = _units(settings, args.units)
        return render_current(await backend.forecast(args.latitude, args.longitude, days=1, units=units))

    async def forecast(args: ForecastArgs) -> str:
        units = _units(settings, args.units)
        return render_forecast(
            await backend.forecast(args.latitude, args.longitude, days=args.days, units=units)
        )

    async def active(args: _Point) -> str:
        return render_alerts(await alerts.active(args.latitude, args.longitude), source=alerts.source)

    return [
        connector_tool(
            name="connectors__weather_current",
            family="weather",
            service=backend.source,
            description="Current weather conditions at a latitude/longitude.",
            args=CurrentArgs,
            run=current,
            hosts=backend.hosts,
        ),
        connector_tool(
            name="connectors__weather_forecast",
            family="weather",
            service=backend.source,
            description="Daily weather forecast (up to 16 days) at a latitude/longitude.",
            args=ForecastArgs,
            run=forecast,
            hosts=backend.hosts,
        ),
        connector_tool(
            name="connectors__weather_alerts",
            family="weather",
            service=alerts.source,
            description="Active US National Weather Service alerts at a latitude/longitude (US only).",
            args=_Point,
            run=active,
            hosts=alerts.hosts,
        ),
    ]


__all__ = [
    "BACKENDS",
    "TOOL_NAMES",
    "Alert",
    "AlertsSource",
    "Current",
    "Day",
    "Forecast",
    "NwsAlerts",
    "OpenMeteoBackend",
    "WeatherBackend",
    "build_alerts",
    "build_tools",
    "build_weather_backend",
    "register_weather_backend",
    "render_alerts",
    "render_current",
    "render_forecast",
    "weather_tools",
]
