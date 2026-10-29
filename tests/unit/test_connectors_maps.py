"""`felix_connectors` maps tools, against a loopback server through the real egress guard.

As with weather, each backend is pointed at `tests.support.loopback_http` under the development
`allow_http` exemption, so what is checked is the request production sends and the answer as the
model reads it. The production path -- `FELIX_MAPS_*` through `get_settings` into the registries,
the terms each factory passes, and the per-host pacing the public instances' policies ask for --
is pinned separately. The pacer is driven on a fake clock, so nothing here waits on time.
"""

from __future__ import annotations

import asyncio
import json
import math
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.support.factories import make_settings
from tests.support.loopback_http import Request, respond, serve
from tests.support.optional_deps import require_optional

MUSEUM = {
    "lat": "33.4667444",
    "lon": "-112.0721030",
    "category": "tourism",
    "type": "museum",
    "name": "Phoenix Art Museum",
    "display_name": "Phoenix Art Museum, 1625, North Central Avenue, Phoenix, Arizona, 85004, United States",
}


def _route(steps: list[Any], *, distance: Any = 2450.5, duration: Any = 1830.0) -> dict[str, Any]:
    return {
        "code": "Ok",
        "routes": [{"distance": distance, "duration": duration, "legs": [{"steps": steps}]}],
    }


ROUTE = _route(
    [
        {"name": "", "distance": 22.8, "maneuver": {"type": "depart", "modifier": "left"}},
        {
            "name": "North Central Avenue",
            "distance": 2300.0,
            "maneuver": {"type": "turn", "modifier": "right"},
        },
        {"name": "", "distance": 40, "maneuver": {"type": "roundabout", "exit": 2}},
        {"name": "Roosevelt Street", "distance": 0, "maneuver": {"type": "arrive"}},
    ]
)
A_TO_B = {
    "origin": {"latitude": 33.4672, "longitude": -112.0727},
    "destination": {"latitude": 33.4484, "longitude": -112.074},
}
NOMINATIM_UNUSABLE = (
    "[tool error/provider_error] OpenStreetMap Nominatim (© OpenStreetMap contributors, ODbL) "
    "gave an unusable answer"
)


@pytest.fixture(autouse=True)
def _plugin() -> Any:
    return require_optional("felix_connectors", "connectors")


def _json_responder(payload: Any, seen: list[Request], status: str = "200 OK") -> Any:
    def responder(request: Request, writer: Any) -> None:
        seen.append(request)
        respond(writer, json.dumps(payload).encode(), status=status, ctype="application/json")

    return responder


def _query(request: Request) -> dict[str, list[str]]:
    return parse_qs(urlsplit(request.path).query)


def _tools(base: str) -> dict[str, Any]:
    from felix_connectors.maps import NominatimGeocoder, OpenMeteoTimezone, OsrmRouter, maps_tools

    ua = "felix-test me@example.com"
    tools = maps_tools(
        geocoder=NominatimGeocoder(base_url=base, user_agent=ua, allow_http=True),
        router=OsrmRouter(base_url=base, user_agent=ua, allow_http=True),
        timezone=OpenMeteoTimezone(base_url=base, user_agent=ua, allow_http=True),
    )
    return {t.name: t for t in tools}


async def _call(
    payload: Any, tool: str, args: dict[str, Any], *, status: str = "200 OK"
) -> tuple[Any, list[Request]]:
    seen: list[Request] = []
    async with serve(_json_responder(payload, seen, status=status)) as base:
        out = await _tools(base)[tool].executor.execute(args)
    return out, seen


def _error(out: Any) -> tuple[str | None, str]:
    from felix.tools.errors import read_tool_error_code
    from felix.tools.types import tool_output_content

    return read_tool_error_code(out), tool_output_content(out)


# --- places ---------------------------------------------------------------------------------


async def test_geocode_asks_nominatim_and_renders_each_place() -> None:
    args = {"query": "Phoenix Art Museum", "limit": 2, "country_codes": ["US"]}
    out, seen = await _call([MUSEUM], "connectors__geocode", args)
    assert urlsplit(seen[0].path).path == "/search"
    assert _query(seen[0]) == {
        "q": ["Phoenix Art Museum"],
        "limit": ["2"],
        "countrycodes": ["us"],
        "format": ["jsonv2"],
    }
    assert seen[0].headers["user-agent"] == "felix-test me@example.com"
    assert out.splitlines() == [
        "source: OpenStreetMap Nominatim (© OpenStreetMap contributors, ODbL)",
        "1. Phoenix Art Museum · 33.46674,-112.07210 · tourism/museum",
        "   Phoenix Art Museum, 1625, North Central Avenue, Phoenix, Arizona, 85004, United States",
    ]


async def test_a_country_code_cannot_add_codes_to_the_list() -> None:
    # The codes are joined with commas into one parameter; a code carrying a comma would add more.
    args = {"query": "x", "country_codes": ["us,ru", "c", "éa", "ca"]}
    out, seen = await _call([], "connectors__geocode", args)
    assert _query(seen[0])["countrycodes"] == ["ca"]
    assert out.splitlines()[-1] == "no places found"


async def test_entries_that_are_not_places_are_skipped_and_the_rest_numbered() -> None:
    # The limit is applied to what the upstream sent, then the unusable entries dropped.
    junk = [42, {"name": "no coordinates"}, MUSEUM, {**MUSEUM, "name": "Fourth"}]
    out, _ = await _call(junk, "connectors__geocode", {"query": "x", "limit": 3})
    assert out.splitlines()[1:] == [
        "1. Phoenix Art Museum · 33.46674,-112.07210 · tourism/museum",
        f"   {MUSEUM['display_name']}",
    ]


async def test_reverse_geocode_sends_the_point() -> None:
    _, seen = await _call(
        MUSEUM, "connectors__reverse_geocode", {"latitude": 33.4672, "longitude": -112.0727}
    )
    assert urlsplit(seen[0].path).path == "/reverse"
    assert _query(seen[0]) == {"lat": ["33.467200"], "lon": ["-112.072700"], "format": ["jsonv2"]}


async def test_a_point_nominatim_cannot_place_reads_as_nothing_found() -> None:
    sea = {"error": "Unable to geocode"}
    out, _ = await _call(sea, "connectors__reverse_geocode", {"latitude": 0, "longitude": -30})
    assert out.splitlines()[-1] == "no places found"


async def test_an_unnamed_place_is_labelled_by_its_address_once() -> None:
    door = {**MUSEUM, "name": "", "category": "amenity", "type": "vending_machine"}
    out, _ = await _call(door, "connectors__reverse_geocode", {"latitude": 33.4672, "longitude": -112.0727})
    assert out.splitlines()[1:] == [
        f"1. {MUSEUM['display_name']} · 33.46674,-112.07210 · amenity/vending_machine"
    ]


@pytest.mark.parametrize(
    ("tool", "args", "payload"),
    [
        ("connectors__geocode", {"query": "x"}, "a string, not a list"),
        ("connectors__reverse_geocode", {"latitude": 1, "longitude": 2}, []),
    ],
)
async def test_an_answer_of_the_wrong_shape_is_unusable_not_empty(tool: str, args: Any, payload: Any) -> None:
    out, _ = await _call(payload, tool, args)
    assert _error(out) == ("provider_error", NOMINATIM_UNUSABLE)


async def test_places_search_is_bounded_to_the_radius_around_the_point() -> None:
    args = {"query": "museum", "latitude": 33.4484, "longitude": -112.074, "radius_km": 2, "limit": 4}
    out, seen = await _call([MUSEUM], "connectors__places_search", args)
    q = _query(seen[0])
    assert {k: v for k, v in q.items() if k != "viewbox"} == {
        "q": ["museum"],
        "limit": ["4"],
        "bounded": ["1"],
        "format": ["jsonv2"],
    }
    left, top, right, bottom = map(float, q["viewbox"][0].split(","))
    assert (top - bottom) / 2 == pytest.approx(2 / 111.32, rel=1e-3)
    assert (right - left) / 2 == pytest.approx(2 / (111.32 * math.cos(math.radians(33.4484))), rel=1e-3)
    assert (left + right) / 2 == pytest.approx(-112.074)
    assert (top + bottom) / 2 == pytest.approx(33.4484)
    assert out.splitlines()[1].endswith("· 2.0 km away")


def test_the_viewbox_stays_on_the_globe_at_its_edges() -> None:
    from felix_connectors.maps import Near, _viewbox

    left, _, right, _ = map(float, _viewbox(Near(0, 179.95, 50)).split(","))
    assert right == 180.0
    assert left < 179.95
    edges = list(map(float, _viewbox(Near(89.99, 0, 10)).split(",")))
    assert edges[1] == 90.0
    assert all(math.isfinite(v) for v in edges)


# --- routes ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("profile", "instance"),
    [("driving", "routed-car"), ("walking", "routed-foot"), ("cycling", "routed-bike")],
)
async def test_each_profile_goes_to_its_own_instance_longitude_first(profile: str, instance: str) -> None:
    out, seen = await _call(ROUTE, "connectors__route", {**A_TO_B, "profile": profile})
    assert urlsplit(seen[0].path).path == (
        f"/{instance}/route/v1/driving/-112.072700,33.467200;-112.074000,33.448400"
    )
    assert _query(seen[0]) == {"overview": ["false"], "steps": ["true"]}
    assert out.splitlines() == [
        "source: OSRM on FOSSGIS (© OpenStreetMap contributors, ODbL)",
        f"{profile}: 2.5 km (1.5 mi), about 30 min",
        "1. start left (23 m)",
        "2. turn right onto North Central Avenue (2.3 km (1.4 mi))",
        "3. take exit 2 at the roundabout (40 m)",
        "4. arrive at Roosevelt Street (0 m)",
    ]


async def test_every_kind_of_step_reads_as_an_instruction() -> None:
    odd = _route(
        [
            {"name": "Van Buren Street", "distance": 10, "maneuver": {"type": "new name"}},
            {"name": "", "distance": 10, "maneuver": {"type": "rotary", "modifier": "right"}},
            {"name": "", "distance": 10, "maneuver": {"type": "roundabout", "exit": "2"}},
            {"name": "1st Avenue", "distance": 10, "maneuver": "not an object"},
            {"name": "", "distance": 10},
            "not a step",
        ]
    )
    odd["routes"][0]["legs"].append("not a leg")
    out, _ = await _call(odd, "connectors__route", A_TO_B)
    assert out.splitlines()[2:] == [
        "1. continue onto Van Buren Street (10 m)",
        "2. enter the roundabout (10 m)",
        "3. enter the roundabout (10 m)",
        "4. continue onto 1st Avenue (10 m)",
        "5. continue (10 m)",
    ]


async def test_no_route_is_an_answer_not_an_error() -> None:
    out, _ = await _call({"code": "NoRoute", "routes": []}, "connectors__route", A_TO_B)
    assert _error(out)[0] is None
    assert out.splitlines()[-1] == "no driving route between these points"


@pytest.mark.parametrize(
    "payload", [_route([], distance=None), _route([], duration="soon"), {"code": "Ok", "routes": [{}]}]
)
async def test_a_route_without_its_length_is_unusable_not_zero(payload: Any) -> None:
    out, _ = await _call(payload, "connectors__route", A_TO_B)
    assert _error(out)[0] == "provider_error"


async def test_a_long_route_is_capped_in_steps() -> None:
    from felix_connectors.maps import MAX_STEPS

    step = {"name": "Main Street", "distance": 100, "maneuver": {"type": "continue"}}
    out, _ = await _call(_route([step] * 40), "connectors__route", A_TO_B)
    lines = out.splitlines()
    assert len(lines) == 2 + MAX_STEPS + 1
    assert lines[-1] == f"(+{40 - MAX_STEPS} more steps)"


@pytest.mark.parametrize(
    ("distance", "duration", "line"),
    [
        (150_000, 5400, "driving: 150.0 km (93.2 mi), about 1 h 30 min"),
        (0, 0, "driving: 0 m, about 1 min"),
    ],
)
def test_route_lengths_read_in_hours_and_never_as_no_time(
    distance: float, duration: float, line: str
) -> None:
    from felix_connectors.maps import Route, render_route

    assert render_route(Route("S", "driving", distance, duration)).splitlines()[1] == line


async def test_an_upstream_error_names_the_maps_service() -> None:
    out, _ = await _call({}, "connectors__route", A_TO_B, status="503 Unavailable")
    assert _error(out)[1] == (
        "[tool error/provider_error] OSRM on FOSSGIS (© OpenStreetMap contributors, ODbL) answered 503"
    )


# --- time zones -----------------------------------------------------------------------------


async def test_timezone_at_asks_open_meteo_for_the_zone_of_the_point() -> None:
    args = {"latitude": 48.85, "longitude": 2.35}
    out, seen = await _call({"timezone": "Europe/Paris"}, "connectors__timezone_at", args)
    assert urlsplit(seen[0].path).path == "/v1/forecast"
    assert _query(seen[0]) == {"latitude": ["48.8500"], "longitude": ["2.3500"], "timezone": ["auto"]}
    assert out.splitlines()[:2] == ["source: Open-Meteo", "zone: Europe/Paris"]


@pytest.mark.parametrize(
    ("zone", "line"),
    [
        ("America/Phoenix", "local time: 2026-07-01 05:00 (UTC-07:00)"),
        ("Asia/Kolkata", "local time: 2026-07-01 17:30 (UTC+05:30)"),
        ("Mars/Olympus", "local time: n/a (zone not in this server's time zone database)"),
        # The upstream's string, not ours: zoneinfo refuses a path with ValueError, not NotFound.
        ("../etc/localtime", "local time: n/a (zone not in this server's time zone database)"),
    ],
)
def test_the_local_time_is_rendered_in_the_zone(zone: str, line: str) -> None:
    from felix_connectors.maps import render_zone

    noon = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    assert render_zone(zone, source="S", now=noon).splitlines()[-1] == line


async def test_a_reply_with_no_zone_is_unusable() -> None:
    out, _ = await _call({"latitude": 1}, "connectors__timezone_at", {"latitude": 1, "longitude": 2})
    assert _error(out)[1] == "[tool error/provider_error] Open-Meteo gave an unusable answer"


# --- pacing ---------------------------------------------------------------------------------


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """`time.monotonic` held still and `asyncio.sleep` recorded, inside `felix_connectors.http`."""
    from felix_connectors import http

    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(http.time, "monotonic", lambda: 1000.0)
    monkeypatch.setattr(http.asyncio, "sleep", sleep)
    return slept


async def test_concurrent_calls_queue_one_interval_apart(clock: list[float]) -> None:
    from felix_connectors.http import Pacer

    pacer = Pacer(0.2, max_wait_s=1)
    await asyncio.gather(*(pacer.wait() for _ in range(3)))
    assert clock == pytest.approx([0.2, 0.4])


async def test_a_refused_call_reserves_nothing(clock: list[float]) -> None:
    from felix_connectors.http import Busy, Pacer

    pacer = Pacer(1.0, max_wait_s=1.0)
    await asyncio.gather(pacer.wait(), pacer.wait())
    reserved = pacer._next
    for _ in range(3):
        with pytest.raises(Busy):
            await pacer.wait()
    assert pacer._next == reserved


async def test_a_cancelled_call_gives_its_slot_back(monkeypatch: pytest.MonkeyPatch) -> None:
    from felix_connectors import http

    monkeypatch.setattr(http.time, "monotonic", lambda: 1000.0)
    pacer = http.Pacer(1.0)
    await pacer.wait()
    waiting = asyncio.ensure_future(pacer.wait())
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert pacer._next == 1001.0


@pytest.mark.parametrize(
    ("tool", "args", "payload"),
    [("connectors__geocode", {"query": "x"}, []), ("connectors__route", A_TO_B, ROUTE)],
)
async def test_a_paced_host_answers_busy_past_the_longest_wait(
    monkeypatch: pytest.MonkeyPatch, tool: str, args: dict[str, Any], payload: Any
) -> None:
    from felix_connectors import http

    monkeypatch.setattr(http, "_PACERS", {"127.0.0.1": http.Pacer(60.0, max_wait_s=1.0)})
    seen: list[Request] = []
    async with serve(_json_responder(payload, seen)) as base:
        run = _tools(base)[tool].executor
        await run.execute(args)
        out = await run.execute(args)
    assert len(seen) == 1
    code, text = _error(out)
    assert code == "rate_limited"
    assert text.endswith("is busy; try again shortly")


def test_the_public_instances_are_paced_at_their_policy_and_nothing_else_is() -> None:
    from felix_connectors import http
    from felix_connectors.maps import FOSSGIS_URL, NOMINATIM_URL

    assert (NOMINATIM_URL, FOSSGIS_URL) == (
        "https://nominatim.openstreetmap.org",
        "https://routing.openstreetmap.de",
    )
    nominatim = http._PACERS["nominatim.openstreetmap.org"]
    fossgis = http._PACERS["routing.openstreetmap.de"]
    assert nominatim is not fossgis
    assert nominatim._interval >= 1.0
    assert fossgis._interval >= 1.0
    # An operator's own instance is theirs to load; Open-Meteo sets no rate this needs to keep.
    assert set(http._PACERS) == {"nominatim.openstreetmap.org", "routing.openstreetmap.de"}


# --- the production path --------------------------------------------------------------------


def test_the_tools_are_untrusted_read_only_and_declare_their_hosts() -> None:
    from felix.manifests.builder import _is_untrusted_tool
    from felix.tools.builtins import default_tool_provider
    from felix_connectors.maps import TOOL_NAMES

    provider = default_tool_provider()
    hosts = {}
    for name in TOOL_NAMES:
        tool = provider.get(name)
        assert _is_untrusted_tool(tool), name
        assert tool.read_only and tool.replay_safe, name
        hosts[name] = tool.egress_hosts
    assert hosts == {
        "connectors__geocode": {"nominatim.openstreetmap.org"},
        "connectors__reverse_geocode": {"nominatim.openstreetmap.org"},
        "connectors__places_search": {"nominatim.openstreetmap.org"},
        "connectors__route": {"routing.openstreetmap.de"},
        "connectors__timezone_at": {"api.open-meteo.com"},
    }


@pytest.mark.parametrize(
    ("overrides", "insecure"),
    [({"allow_insecure": False}, False), ({"environment": "development", "allow_insecure": True}, True)],
)
def test_the_built_backends_carry_the_operators_terms(overrides: dict[str, Any], insecure: bool) -> None:
    from felix_connectors.http import USER_AGENT
    from felix_connectors.maps import build_geocoder, build_router, build_timezone

    settings = make_settings(connectors_contact="ops@example.com", **overrides)
    for build in (build_geocoder, build_router, build_timezone):
        up = build(settings)._up  # type: ignore[attr-defined]
        assert up.user_agent == f"{USER_AGENT} ops@example.com", build.__name__
        assert up.allow_http is insecure, build.__name__


class _ProbeGeocoder:
    name = "probe"
    source = "Probe"
    hosts = frozenset({"probe.test"})

    async def search(self, query: str, *, limit: int, near: Any = None, countries: Any = ()) -> list[Any]:
        return []

    async def reverse(self, latitude: float, longitude: float) -> Any:
        return None


def test_the_plugin_reads_the_operators_geocoder(monkeypatch: pytest.MonkeyPatch) -> None:
    from felix.config import get_settings
    from felix.tools.builtins import default_tool_provider
    from felix_connectors.maps import GEOCODERS

    GEOCODERS.register("probe", lambda settings: _ProbeGeocoder())
    monkeypatch.setenv("FELIX_MAPS_GEOCODER", "probe")
    get_settings.cache_clear()
    try:
        assert default_tool_provider().get("connectors__geocode").egress_hosts == {"probe.test"}
    finally:
        GEOCODERS.unregister("probe")
        get_settings.cache_clear()


def test_an_unregistered_router_fails_the_tool_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    from felix.config import get_settings
    from felix.tools.builtins import default_tool_provider

    monkeypatch.setenv("FELIX_MAPS_ROUTER", "valhalla")
    get_settings.cache_clear()
    try:
        with pytest.raises(ValueError, match="unknown FELIX_MAPS_ROUTER 'valhalla'"):
            default_tool_provider().get("connectors__route")
    finally:
        get_settings.cache_clear()
