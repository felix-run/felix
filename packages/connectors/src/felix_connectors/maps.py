"""Maps: places from a name, a name from a point, places near a point, a route, and a time zone.

The geocoder and the router each sit behind an open registry (`register_geocoder`,
`register_router`), selected by `FELIX_MAPS_GEOCODER` and `FELIX_MAPS_ROUTER`, so a deployment
with a Google Maps or Apple Maps key -- or its own Nominatim -- registers one and never touches
this file. The bundled backends need no key: OpenStreetMap's Nominatim for places, the FOSSGIS
OSRM instances for routes (the OSRM project's own demo answers every profile with a car route,
which would hand a walker a motorway), and Open-Meteo for a point's time zone.

Both public instances ask for at most one request a second and a User-Agent that names the
application, so each host is paced (`http.pace_host`) and sent `FELIX_CONNECTORS_CONTACT`; a
backend pointed at an operator's own instance is not paced. Every answer names its source, which
for OpenStreetMap data is also the attribution the ODbL requires -- and a reminder that the names
and addresses in it are text anyone can edit, so a manifest offering these tools should screen
what they return (`content_screening`).

The weather tools take coordinates, not a place name: looking the name up there would make them
reach the geocoder's host too, and refuse to compile under an `allow_hosts` that names only the
forecast's. The model chains `connectors__geocode` into them instead.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from typing import Literal, Protocol, runtime_checkable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from felix.config import Settings
from felix.security.host_allowlist import url_host
from felix.tools.types import Tool
from pydantic import BaseModel, ConfigDict, Field

from felix_connectors.http import Upstream, allow_http, pace_host, user_agent
from felix_connectors.registry import BackendRegistry
from felix_connectors.tools import Point, connector_tool, number, one_line
from felix_connectors.weather import OPEN_METEO_URL

Profile = Literal["driving", "walking", "cycling"]
MAX_PLACES = 10
MAX_STEPS = 15
MAX_RADIUS_KM = 50.0
_KM_PER_DEGREE = 111.32
_MI_PER_KM = 0.621371
NOMINATIM_URL = "https://nominatim.openstreetmap.org"
FOSSGIS_URL = "https://routing.openstreetmap.de"
# Their usage policies: at most one request a second, from each client.
pace_host(url_host(NOMINATIM_URL), 1.0)
pace_host(url_host(FOSSGIS_URL), 1.0)


@dataclass(frozen=True, slots=True)
class Place:
    name: str
    address: str
    latitude: float
    longitude: float
    #: What the place is, as the upstream classes it: `tourism/museum`, `highway/residential`.
    kind: str = ""


@dataclass(frozen=True, slots=True)
class Step:
    instruction: str
    distance_m: float


@dataclass(frozen=True, slots=True)
class Route:
    source: str
    profile: Profile
    distance_m: float
    duration_s: float
    steps: Sequence[Step] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class Near:
    latitude: float
    longitude: float
    radius_km: float


@runtime_checkable
class Geocoder(Protocol):
    """Places for a query, and the place at a point. `hosts` is every host it connects to: the
    tools declare them, and the compile holds them to the agent's `network.allow_hosts`."""

    name: str
    source: str
    hosts: frozenset[str]

    async def search(
        self, query: str, *, limit: int, near: Near | None = None, countries: Sequence[str] = ()
    ) -> list[Place]: ...

    async def reverse(self, latitude: float, longitude: float) -> Place | None: ...


class NoRoute(Exception):
    """The router found no way between the points. Part of the `Router` contract: the route tool
    answers it as a finding, where any other exception reads to the model as an outage."""


@runtime_checkable
class Router(Protocol):
    """A route between two points by `profile`; `NoRoute` when there is none."""

    name: str
    source: str
    hosts: frozenset[str]

    async def route(
        self, origin: tuple[float, float], destination: tuple[float, float], *, profile: Profile
    ) -> Route: ...


@runtime_checkable
class TimezoneSource(Protocol):
    """A point's IANA zone name. Not checked against this host's tz database: `render_zone` says
    when it cannot show the local time."""

    source: str
    hosts: frozenset[str]

    async def zone_at(self, latitude: float, longitude: float) -> str: ...


class NominatimGeocoder:
    """OpenStreetMap's Nominatim. No key; one request a second on the public instance."""

    name = "nominatim"
    source = "OpenStreetMap Nominatim (© OpenStreetMap contributors, ODbL)"

    def __init__(self, *, base_url: str = NOMINATIM_URL, user_agent: str, allow_http: bool = False):
        self._up = Upstream(base_url, user_agent, allow_http)
        self.hosts = self._up.hosts

    async def _get(self, path: str, params: dict[str, str]) -> object:
        return await self._up.get(path, {**params, "format": "jsonv2"})

    async def search(
        self, query: str, *, limit: int, near: Near | None = None, countries: Sequence[str] = ()
    ) -> list[Place]:
        params = {"q": query, "limit": str(limit)}
        if countries:
            params["countrycodes"] = ",".join(countries)
        if near is not None:
            params |= {"viewbox": _viewbox(near), "bounded": "1"}
        payload = await self._get("/search", params)
        if not isinstance(payload, list):
            raise ValueError("search answer is not a list")
        return [p for p in (_place(raw) for raw in payload[:limit]) if p is not None]

    async def reverse(self, latitude: float, longitude: float) -> Place | None:
        payload = await self._get("/reverse", {"lat": f"{latitude:.6f}", "lon": f"{longitude:.6f}"})
        if not isinstance(payload, dict):
            raise ValueError("reverse answer is not an object")
        # Nominatim answers a point in the sea with `{"error": "Unable to geocode"}` and a 200,
        # which has no coordinates, so it is no place.
        return _place(payload)


def _viewbox(near: Near) -> str:
    dlat = near.radius_km / _KM_PER_DEGREE
    # A degree of longitude shrinks toward the poles; held at a tenth of the equator's so the
    # box stays finite near one.
    dlon = near.radius_km / (_KM_PER_DEGREE * max(math.cos(math.radians(near.latitude)), 0.1))
    left, right = max(near.longitude - dlon, -180.0), min(near.longitude + dlon, 180.0)
    top, bottom = min(near.latitude + dlat, 90.0), max(near.latitude - dlat, -90.0)
    return f"{left:.6f},{top:.6f},{right:.6f},{bottom:.6f}"


def _place(raw: object) -> Place | None:
    if not isinstance(raw, dict):
        return None
    lat, lon = number(raw.get("lat")), number(raw.get("lon"))
    if lat is None or lon is None:
        return None
    category, kind = str(raw.get("category") or ""), str(raw.get("type") or "")
    return Place(
        name=str(raw.get("name") or ""),
        address=str(raw.get("display_name") or ""),
        latitude=lat,
        longitude=lon,
        kind=f"{category}/{kind}" if category and kind else category or kind,
    )


_FOSSGIS_PATHS: dict[str, str] = {"driving": "routed-car", "walking": "routed-foot", "cycling": "routed-bike"}


class OsrmRouter:
    """OSRM, as FOSSGIS runs it: one instance per profile at `/routed-{car,foot,bike}`."""

    name = "osrm"
    source = "OSRM on FOSSGIS (© OpenStreetMap contributors, ODbL)"

    def __init__(self, *, base_url: str = FOSSGIS_URL, user_agent: str, allow_http: bool = False):
        self._up = Upstream(base_url, user_agent, allow_http)
        self.hosts = self._up.hosts

    async def route(
        self, origin: tuple[float, float], destination: tuple[float, float], *, profile: Profile
    ) -> Route:
        # OSRM orders a coordinate longitude first. Both are floats the arguments bounded, so
        # nothing the model wrote reaches the path but digits, a sign and a point.
        points = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in (origin, destination))
        path = f"/{_FOSSGIS_PATHS[profile]}/route/v1/driving/{points}"
        payload = await self._up.get(path, {"overview": "false", "steps": "true"})
        if not isinstance(payload, dict):
            raise ValueError("route answer is not an object")
        routes = payload.get("routes")
        if payload.get("code") != "Ok" or not isinstance(routes, list) or not routes:
            raise NoRoute(str(payload.get("code") or ""))
        best = routes[0] if isinstance(routes[0], dict) else {}
        distance, duration = number(best.get("distance")), number(best.get("duration"))
        if distance is None or duration is None:
            # Not a route of 0 m: a plausible answer is worse than an unusable one.
            raise ValueError("route has no distance or duration")
        return Route(
            source=self.source,
            profile=profile,
            distance_m=distance,
            duration_s=duration,
            steps=_steps(best.get("legs")),
        )


def _steps(legs: object) -> tuple[Step, ...]:
    out: list[Step] = []
    for leg in legs if isinstance(legs, list) else []:
        for raw in leg.get("steps", []) if isinstance(leg, dict) else []:
            if isinstance(raw, dict):
                out.append(Step(_instruction(raw), number(raw.get("distance")) or 0.0))
    return tuple(out)


def _instruction(step: dict) -> str:
    raw = step.get("maneuver")
    maneuver: dict = raw if isinstance(raw, dict) else {}
    kind = str(maneuver.get("type") or "continue")
    modifier = str(maneuver.get("modifier") or "")
    road = str(step.get("name") or "")
    if kind == "depart":
        verb = "start"
    elif kind == "arrive":
        return "arrive" + (f" at {road}" if road else "")
    elif kind in {"roundabout", "rotary"}:
        exit_no = maneuver.get("exit")
        verb = "enter the roundabout"
        if isinstance(exit_no, int):
            verb = f"take exit {exit_no} at the roundabout"
        modifier = ""
    elif kind == "new name":
        verb = "continue"
    else:
        verb = kind
    words = " ".join(w for w in (verb, modifier) if w)
    return words + (f" onto {road}" if road else "")


class OpenMeteoTimezone:
    """A point's IANA zone, from Open-Meteo's forecast endpoint asked for nothing but the zone.
    The same host as the default weather backend, but not tied to it: under another
    `FELIX_WEATHER_BACKEND` this still needs `api.open-meteo.com` allowed."""

    source = "Open-Meteo"

    def __init__(self, *, base_url: str = OPEN_METEO_URL, user_agent: str, allow_http: bool = False):
        self._up = Upstream(base_url, user_agent, allow_http)
        self.hosts = self._up.hosts

    async def zone_at(self, latitude: float, longitude: float) -> str:
        payload = await self._up.get(
            "/v1/forecast",
            {"latitude": f"{latitude:.4f}", "longitude": f"{longitude:.4f}", "timezone": "auto"},
        )
        zone = payload.get("timezone") if isinstance(payload, dict) else None
        if not isinstance(zone, str) or not zone:
            raise ValueError("no time zone in the answer")
        return zone


# --- the registries -------------------------------------------------------------------------

GEOCODERS: BackendRegistry[Geocoder] = BackendRegistry("FELIX_MAPS_GEOCODER")
ROUTERS: BackendRegistry[Router] = BackendRegistry("FELIX_MAPS_ROUTER")
register_geocoder = GEOCODERS.register
register_router = ROUTERS.register
GEOCODERS.register(
    "nominatim",
    lambda settings: NominatimGeocoder(user_agent=user_agent(settings), allow_http=allow_http(settings)),
)
ROUTERS.register(
    "osrm", lambda settings: OsrmRouter(user_agent=user_agent(settings), allow_http=allow_http(settings))
)


def build_geocoder(settings: Settings) -> Geocoder:
    return GEOCODERS.build(settings, settings.maps_geocoder)


def build_router(settings: Settings) -> Router:
    return ROUTERS.build(settings, settings.maps_router)


def build_timezone(settings: Settings) -> TimezoneSource:
    return OpenMeteoTimezone(user_agent=user_agent(settings), allow_http=allow_http(settings))


# --- rendering ------------------------------------------------------------------------------


def _distance(metres: float) -> str:
    km = metres / 1000
    return f"{km:.1f} km ({km * _MI_PER_KM:.1f} mi)" if km >= 1 else f"{metres:.0f} m"


def _duration(seconds: float) -> str:
    minutes = round(seconds / 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if hours else f"{max(minutes, 1)} min"


def _haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(min(h, 1.0)))


def _place_line(i: int, p: Place, *, origin: tuple[float, float] | None = None) -> str:
    label = one_line(p.name, 120) or one_line(p.address, 120) or "(unnamed)"
    parts = [f"{i}. {label}", f"{p.latitude:.5f},{p.longitude:.5f}"]
    if p.kind:
        parts.append(one_line(p.kind, 60))
    if origin is not None:
        parts.append(f"{_haversine_km(origin, (p.latitude, p.longitude)):.1f} km away")
    line = " · ".join(parts)
    # An unnamed place is labelled by its address already.
    if p.name and p.address and p.address != p.name:
        line += f"\n   {one_line(p.address, 240)}"
    return line


def render_places(places: list[Place], *, source: str, origin: tuple[float, float] | None = None) -> str:
    lines = [f"source: {source}"]
    lines += [_place_line(i, p, origin=origin) for i, p in enumerate(places, 1)]
    if not places:
        lines.append("no places found")
    return "\n".join(lines)


def render_route(r: Route) -> str:
    lines = [
        f"source: {r.source}",
        f"{r.profile}: {_distance(r.distance_m)}, about {_duration(r.duration_s)}",
    ]
    for i, s in enumerate(r.steps[:MAX_STEPS], 1):
        lines.append(f"{i}. {one_line(s.instruction, 120)} ({_distance(s.distance_m)})")
    if len(r.steps) > MAX_STEPS:
        lines.append(f"(+{len(r.steps) - MAX_STEPS} more steps)")
    return "\n".join(lines)


def render_zone(zone: str, *, source: str, now: datetime | None = None) -> str:
    head = f"source: {source}\nzone: {one_line(zone, 60)}"
    try:
        tz = ZoneInfo(zone)
    except ZoneInfoNotFoundError, ValueError:
        # A name this host's tz database lacks: the zone is still the answer, the clock is not.
        return f"{head}\nlocal time: n/a (zone not in this server's time zone database)"
    local = (now or datetime.now(UTC)).astimezone(tz)
    offset = local.strftime("%z")
    return f"{head}\nlocal time: {local:%Y-%m-%d %H:%M} (UTC{offset[:3]}:{offset[3:]})"


# --- the tools ------------------------------------------------------------------------------


class GeocodeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=300, description="An address, place or landmark.")
    limit: int = Field(default=3, ge=1, le=MAX_PLACES)
    country_codes: list[str] = Field(
        default_factory=list,
        max_length=10,
        description="ISO 3166-1 alpha-2 codes to restrict to, e.g. ['us', 'ca'].",
    )

    def countries(self) -> list[str]:
        # Two letters only: the list is joined into one parameter with commas.
        return [c.lower() for c in self.country_codes if len(c) == 2 and c.isascii() and c.isalpha()]


class PlacesArgs(Point):
    query: str = Field(
        min_length=1, max_length=200, description="What to look for: 'coffee', 'pharmacy', a name."
    )
    radius_km: float = Field(default=5.0, gt=0, le=MAX_RADIUS_KM)
    limit: int = Field(default=5, ge=1, le=MAX_PLACES)


class RouteArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    origin: Point
    destination: Point
    profile: Profile = "driving"


#: Every tool `build_tools` returns, registered by name before any is built.
TOOL_NAMES = (
    "connectors__geocode",
    "connectors__reverse_geocode",
    "connectors__places_search",
    "connectors__route",
    "connectors__timezone_at",
)


def build_tools(settings: Settings) -> list[Tool]:
    """The family's tools under `settings`: the interface `felix_connectors.register` reads."""
    return maps_tools(
        geocoder=build_geocoder(settings), router=build_router(settings), timezone=build_timezone(settings)
    )


def maps_tools(*, geocoder: Geocoder, router: Router, timezone: TimezoneSource) -> list[Tool]:
    async def geocode(args: GeocodeArgs) -> str:
        found = await geocoder.search(args.query, limit=args.limit, countries=args.countries())
        return render_places(found, source=geocoder.source)

    async def reverse(args: Point) -> str:
        place = await geocoder.reverse(args.latitude, args.longitude)
        return render_places([place] if place else [], source=geocoder.source)

    async def places(args: PlacesArgs) -> str:
        near = Near(args.latitude, args.longitude, args.radius_km)
        found = await geocoder.search(args.query, limit=args.limit, near=near)
        return render_places(found, source=geocoder.source, origin=(args.latitude, args.longitude))

    async def route(args: RouteArgs) -> str:
        origin = (args.origin.latitude, args.origin.longitude)
        destination = (args.destination.latitude, args.destination.longitude)
        try:
            found = await router.route(origin, destination, profile=args.profile)
        except NoRoute:
            return f"source: {router.source}\nno {args.profile} route between these points"
        return render_route(found)

    async def zone(args: Point) -> str:
        return render_zone(await timezone.zone_at(args.latitude, args.longitude), source=timezone.source)

    tool = partial(connector_tool, family="maps")
    return [
        tool(
            name="connectors__geocode",
            service=geocoder.source,
            hosts=geocoder.hosts,
            args=GeocodeArgs,
            run=geocode,
            description="Find places by address or name, with coordinates for the weather and route tools.",
        ),
        tool(
            name="connectors__reverse_geocode",
            service=geocoder.source,
            hosts=geocoder.hosts,
            args=Point,
            run=reverse,
            description="The address at a latitude/longitude.",
        ),
        tool(
            name="connectors__places_search",
            service=geocoder.source,
            hosts=geocoder.hosts,
            args=PlacesArgs,
            run=places,
            description="Places matching a query within a radius of a latitude/longitude.",
        ),
        tool(
            name="connectors__route",
            service=router.source,
            hosts=router.hosts,
            args=RouteArgs,
            run=route,
            description="Distance, travel time and turn-by-turn steps between two points "
            "(driving, walking, cycling).",
        ),
        tool(
            name="connectors__timezone_at",
            service=timezone.source,
            hosts=timezone.hosts,
            args=Point,
            run=zone,
            description="The IANA time zone and current local time at a latitude/longitude.",
        ),
    ]


__all__ = [
    "FOSSGIS_URL",
    "GEOCODERS",
    "NOMINATIM_URL",
    "ROUTERS",
    "TOOL_NAMES",
    "Geocoder",
    "Near",
    "NoRoute",
    "NominatimGeocoder",
    "OpenMeteoTimezone",
    "OsrmRouter",
    "Place",
    "Route",
    "Router",
    "Step",
    "TimezoneSource",
    "build_geocoder",
    "build_router",
    "build_timezone",
    "build_tools",
    "maps_tools",
    "register_geocoder",
    "register_router",
    "render_places",
    "render_route",
    "render_zone",
]
