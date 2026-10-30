"""Felix connectors: tools an agent calls to reach everyday services: weather, maps, markets, FX,
and a person's own Google Calendar, Gmail and Drive.

Installed, this plugin registers its tools with every tool provider; a manifest still names each
one in `spec.tools` before an agent holds it:

    tools: [connectors__geocode, connectors__weather_forecast, connectors__route]

Each tool reaches a host fixed by the operator's configuration, never one the model names, and
declares it (`Tool.egress_hosts`), so an agent's `network.allow_hosts` refuses a tool it does not
cover at compile, and each request is held to them. The transport is `connectors`, which
governance does not trust, so a manifest that enables content screening has what comes back
screened. The defaults need no key: Open-Meteo for the forecast and a point's time zone, the US
National Weather Service for alerts, OpenStreetMap's Nominatim for places, FOSSGIS's OSRM for
routes and Frankfurter for exchange rates. Stock quotes need `FELIX_MARKET_API_KEY` (Finnhub or
Alpha Vantage). The Google tools act as the person the run acts for, with the grant they
connected (`felix.auth.google_connect`), and need the `google` extra.

A family is a module exporting `TOOL_NAMES` and `build_tools(settings)`, listed in `FAMILIES`.

Core never names this package; it arrives through the `felix.plugins` entry point.
"""

from __future__ import annotations

from collections.abc import Callable
from types import ModuleType
from typing import Any

from felix_connectors import fx, gmail, google_calendar, google_drive, maps, markets, weather

FAMILIES: tuple[ModuleType, ...] = (weather, maps, markets, fx, google_calendar, gmail, google_drive)


def _build(family: ModuleType, name: str) -> Any:
    """`name` from its family, built when a provider first resolves it. The provider keeps the
    tool, so a changed setting takes effect at the next process start."""
    from felix.config import get_settings

    for tool in family.build_tools(get_settings()):
        if tool.name == name:
            return tool
    raise KeyError(f"{family.__name__} lists {name!r} in TOOL_NAMES but did not build it")


class ConnectorsPlugin:
    name = "connectors"

    def register_tools(self, register: Callable[[str, Callable[[], Any]], None]) -> None:
        for family in FAMILIES:
            for tool_name in family.TOOL_NAMES:
                register(tool_name, lambda f=family, n=tool_name: _build(f, n))


def register(registry: Any) -> None:
    """Entry point, called once at startup in the API and the worker."""
    registry.register_plugin(ConnectorsPlugin())


__all__ = ["FAMILIES", "ConnectorsPlugin", "register"]
