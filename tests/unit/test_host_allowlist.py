"""`spec.network.allow_hosts`: the hosts an agent's tools may reach, enforced where the model
picks the destination and checked against every URL the manifest names.

Nothing here touches real DNS. Names under `.test` resolve, through a patched
`ssrf.resolve_host`, to a loopback server, so `allowed.test` and `other.test` are the same server
under two names and only the allowlist tells them apart -- and a test that asserts a refusal
cannot pass because a lookup failed.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.manifests.loader import ManifestParseError, parse_manifest
from felix.manifests.schema import BrowserToolRef, HttpFetchToolRef
from felix.security import ssrf
from felix.security.egress import safe_async_client
from felix.security.host_allowlist import (
    EGRESS_CEILINGS_EXTRA,
    EgressBound,
    HostAllowlist,
    normalise_host_pattern,
    url_host,
)
from felix.security.ssrf import EgressBlocked
from felix.tools.browser import tools_from_browser_refs
from felix.tools.http_fetch import tools_from_http_fetch_refs

from tests.support.factories import make_settings
from tests.support.loopback_http import Request
from tests.support.loopback_http import respond as _respond
from tests.support.loopback_http import serve as _serve

BLOCKED = "http_fetch_error: egress_blocked: destination not permitted"


@pytest.fixture
def lookups(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every name resolved; each `.test` name answers loopback, anything else nothing."""
    seen: list[str] = []

    def resolve(host: str) -> list[str]:
        seen.append(host)
        return ["127.0.0.1"] if host.endswith(".test") else []

    monkeypatch.setattr(ssrf, "resolve_host", resolve)
    return seen


def _port(base: str) -> str:
    return base.rsplit(":", 1)[1]


# --- patterns and bounds ------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "stored"),
    [
        ("API.GitHub.com", "api.github.com"),
        ("api.github.com.", "api.github.com"),
        ("*.github.com", "*.github.com"),
        ("203.0.113.7", "203.0.113.7"),
        ("[2001:db8::1]", "2001:db8::1"),
        ("2001:DB8:0:0::1", "2001:db8::1"),
        ("xn--mnchen-3ya.de", "xn--mnchen-3ya.de"),
    ],
)
def test_a_pattern_is_stored_normalised(raw: str, stored: str) -> None:
    assert normalise_host_pattern(raw) == stored


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "https://api.github.com",
        "api.github.com:443",
        "api.github.com/repos",
        "*.com",
        "a.*.com",
        "münchen.de",
        "*.0.0.1",  # a wildcard over an address is not a name
    ],
)
def test_a_pattern_in_another_grammar_is_refused(raw: str) -> None:
    with pytest.raises(ValueError):
        normalise_host_pattern(raw)


@pytest.mark.parametrize(
    ("host", "allowed"),
    [
        ("api.github.com", True),
        ("API.GITHUB.COM.", True),
        ("raw.githubusercontent.com", False),
        ("uploads.github.com", True),  # `*.github.com`
        ("a.b.github.com", True),
        ("github.com", False),  # the wildcard is subdomains only
        (".github.com", False),  # and needs a label before the suffix
        ("evilgithub.com", False),
        ("github.com.evil.com", False),
        ("203.0.113.7", True),
        ("203.0.113.8", False),
        ("2001:db8:0:0::1", True),  # the same address as the stored `2001:db8::1`
        ("[2001:db8::1]", True),
        ("", False),
    ],
)
def test_a_host_is_allowed_only_by_a_pattern_that_names_it(host: str, allowed: bool) -> None:
    patterns = ("api.github.com", "*.github.com", "203.0.113.7", "2001:db8::1")
    assert HostAllowlist(patterns).allows(host) is allowed


def test_a_bound_allows_only_what_every_list_in_it_allows() -> None:
    unrestricted = EgressBound()
    assert unrestricted.allows("anywhere.example") and unrestricted.under(None) == unrestricted
    parent = unrestricted.under(["*.example.com"])
    child = parent.under(["api.example.com", "other.org"])
    assert child.allows("api.example.com")
    assert not child.allows("other.org"), "the parent's list still binds the child"
    assert not child.allows("www.example.com"), "and so does the child's own"
    assert not parent.under([]).allows("api.example.com"), "an empty list allows nothing"


def test_a_stored_bound_round_trips_and_is_revalidated() -> None:
    bound = EgressBound().under(["*.example.com"]).under([])
    restored = EgressBound.from_state(bound.as_state())
    assert restored == bound and restored.restricted
    assert not restored.allows("api.example.com"), "an empty list survives the round trip"
    assert EgressBound.from_state([["OK.com."]]) == EgressBound().under(["ok.com"])
    assert EgressBound.from_state(None) == EgressBound()


@pytest.mark.parametrize("raw", ["x", [["ok.com"], "x"], [["https://ok.com"]]])
def test_a_tampered_stored_bound_is_refused(raw: Any) -> None:
    with pytest.raises(ValueError):
        EgressBound.from_state(raw)


def test_url_host_is_the_punycode_a_request_carries() -> None:
    assert url_host("https://münchen.de/x") == "xn--mnchen-3ya.de"
    assert url_host("http://[::1]:8080/") == "::1"
    assert url_host("not a url") == ""


# --- write: the manifest's own URLs ---------------------------------------------------


def _manifest(name: str = "net", **spec: Any) -> Any:
    return parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": name},
            "spec": {"pattern": "react", **spec},
        }
    )


DECLARED = [
    {"mcp_servers": [{"name": "gh", "url": "https://mcp.elsewhere.io/sse"}]},
    {"peers": [{"name": "p", "url": "https://peer.elsewhere.io"}]},
    {"containers": [{"name": "c", "gateway_url": "https://gw.elsewhere.io", "image": "x"}]},
    {"http_tools": [{"name": "f", "path_prefix": "https://docs.elsewhere.io/"}]},
]
DECLARED_IDS = ["mcp", "peer", "container", "http_tools"]


@pytest.mark.parametrize("spec", DECLARED, ids=DECLARED_IDS)
def test_a_declared_url_outside_the_list_is_refused_at_write(spec: dict[str, Any]) -> None:
    with pytest.raises(ManifestParseError, match=r"elsewhere\.io"):
        _manifest(network={"allow_hosts": ["*.example.com"]}, **spec)
    _manifest(**spec)  # no list, no check


def test_declared_urls_inside_the_list_are_accepted() -> None:
    _manifest(
        network={"allow_hosts": ["*.example.com"]},
        mcp_servers=[{"name": "gh", "url": "https://mcp.example.com/sse"}],
    )


# --- enforcement: the guarded transport, http_fetch and the browser --------------------


async def test_the_transport_refuses_a_host_outside_the_bound(lookups: list[str]) -> None:
    async with _serve(lambda req, w: _respond(w, b"OK")) as base:
        port = _port(base)
        async with safe_async_client(
            timeout=5, allow_http=True, egress=EgressBound().under(["allowed.test"])
        ) as client:
            assert (await client.get(f"http://allowed.test:{port}/")).text == "OK"
            with pytest.raises(EgressBlocked):
                await client.get(f"http://other.test:{port}/")
    assert "other.test" not in lookups, "a refused name is never looked up"


async def test_http_fetch_refuses_a_host_and_a_redirect_hop_outside_the_bound(lookups: list[str]) -> None:
    async with _serve(lambda req, w: _respond(w, b"SECRET")) as other:
        port = _port(other)

        def _redirect(req: Request, writer: asyncio.StreamWriter) -> None:
            _respond(writer, b"", status="302 Found", extra=f"location: http://other.test:{port}/x\r\n")

        async with _serve(_redirect) as hopper:
            (tool,) = tools_from_http_fetch_refs(
                [HttpFetchToolRef(name="f", allow_any_host=True)],
                allow_http=True,
                egress=EgressBound().under(["allowed.test"]),
            )
            allowed = await tool.executor.execute({"url": f"http://allowed.test:{port}/x"})
            direct = await tool.executor.execute({"url": f"http://other.test:{port}/x"})
            hop = await tool.executor.execute({"url": f"http://allowed.test:{_port(hopper)}/start"})
    assert "SECRET" in allowed, "the listed name reaches the same server"
    assert direct == BLOCKED
    assert "SECRET" not in hop and BLOCKED in hop
    assert "other.test" not in lookups


@pytest.mark.parametrize(
    "url",
    ["https://evil.test\\@allowed.test/", "https://allowed.test/ x", "https://allowed.test/\x00"],
)
async def test_the_browser_refuses_a_url_its_parsers_would_read_differently(
    url: str, lookups: list[str]
) -> None:
    (tool,) = tools_from_browser_refs([BrowserToolRef(name="b", binding="chromium")])
    out = await tool.executor.execute({"url": url})
    assert "must not contain backslashes, whitespace or control characters" in out
    assert lookups == []


@pytest.mark.parametrize(
    "url", ["https://docs.example.com.evil.test/", "https://docs.example.com@evil.test/"]
)
async def test_the_browser_prefix_compares_origins(url: str, lookups: list[str]) -> None:
    # Without the trailing slash, as an author may write it: a text match then took both.
    ref = BrowserToolRef(name="b", binding="chromium", path_prefix="https://docs.example.com")
    (tool,) = tools_from_browser_refs([ref], allow_http=True)
    assert "must start with" in await tool.executor.execute({"url": url})


# --- compile: the builder hands the bound on, and children answer to their parents ------


async def _compile(
    root: Any, children: dict[str, Any] | None = None, *, built: list[str] | None = None
) -> Any:
    from felix.manifests.builder import BuildDeps, build_agent
    from felix.tools.provider import InMemoryToolProvider

    deps = BuildDeps(tools=InMemoryToolProvider(), settings=make_settings())

    async def build_child(name: str) -> Any:
        if built is not None:
            built.append(name)
        return await build_agent((children or {})[name], deps=deps)

    deps.sub_agent_builder = build_child
    agent = await build_agent(root, deps=deps)
    return agent, deps


def _lead(name: str, children: list[str], hosts: list[str] | None, **spec: Any) -> Any:
    network = {"network": {"allow_hosts": hosts}} if hosts is not None else {}
    agents = [{"name": c, "description": "x"} for c in children]
    return _manifest(name, delegation={"agents": agents}, **network, **spec)


def _tool(agent: Any, name: str) -> Any:
    [tool] = [t for t in agent.tools if t.name == name]
    return tool


async def _run(tool: Any, args: dict[str, Any], **extras: Any) -> str:
    ctx = RequestContext(settings=make_settings(), auth=AuthContext(), extras=dict(extras))
    async with async_run_with_context(ctx):
        out = await tool.executor.execute(args)
    return out if isinstance(out, str) else str(getattr(out, "content", out))


async def test_the_builder_binds_http_fetch_to_the_list(lookups: list[str]) -> None:
    agent, _ = await _compile(
        _manifest(
            network={"allow_hosts": ["allowed.test"]}, http_tools=[{"name": "fetch", "allow_any_host": True}]
        )
    )
    async with _serve(lambda req, w: _respond(w, b"BODY")) as base:
        allowed = await _run(_tool(agent, "fetch"), {"url": f"http://allowed.test:{_port(base)}/"})
        refused = await _run(_tool(agent, "fetch"), {"url": f"http://other.test:{_port(base)}/"})
    assert "BODY" in allowed
    assert refused == BLOCKED


async def test_a_background_childs_inherited_lists_bind_its_compile(lookups: list[str]) -> None:
    """Its parents' lists ride the run; `build_tenant_agent` seeds the root of its compile."""
    from felix.runtime import build_tenant_agent
    from felix.tools.provider import InMemoryToolProvider

    settings = make_settings()
    ctx = RequestContext(settings=settings, auth=AuthContext())
    ctx.extras[EGRESS_CEILINGS_EXTRA] = EgressBound().under(["allowed.test"])
    fetcher = _manifest("child", http_tools=[{"name": "fetch", "allow_any_host": True}])
    async with async_run_with_context(ctx):
        agent = await build_tenant_agent(
            settings, manifest=fetcher, tools=InMemoryToolProvider(), tenant_id="default", skill_owner=None
        )
    assert await _run(_tool(agent, "fetch"), {"url": "https://other.test/"}) == BLOCKED


@pytest.mark.parametrize("spec", DECLARED, ids=DECLARED_IDS)
async def test_a_childs_url_outside_its_parents_list_is_refused_at_compile(spec: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match=r"elsewhere\.io"):
        await _compile(_lead("lead", ["helper"], ["*.example.com"]), {"helper": _manifest("helper", **spec)})


async def test_a_router_child_answers_to_its_parents_list() -> None:
    child = _manifest("helper", **DECLARED[3])
    router = _manifest(
        "lead", pattern="router", sub_agents=["helper"], network={"allow_hosts": ["*.example.com"]}
    )
    with pytest.raises(ValueError, match=r"elsewhere\.io"):
        await _compile(router, {"helper": child})


@pytest.mark.parametrize("order", [["open", "closed"], ["closed", "open"]])
async def test_a_child_shared_by_two_parents_is_built_under_each_ones_list(order: list[str]) -> None:
    """The compile memo once handed a child built under one parent to another with a stricter list."""
    built: list[str] = []
    children = {
        "open": _lead("open", ["helper"], None),
        "closed": _lead("closed", ["helper"], ["api.example.com"]),
        "helper": _manifest("helper", http_tools=[{"name": "fetch", "allow_any_host": True}]),
    }
    _, deps = await _compile(_lead("root", order, None), children, built=built)
    assert built.count("helper") == 2, "rebuilt under the second parent, not reused"
    last = EgressBound().under(["api.example.com"]) if order[-1] == "closed" else EgressBound()
    assert deps.compiled_egress["helper"] == last


async def test_a_restricted_agents_peer_tool_cannot_choose_the_far_agent() -> None:
    peers = {"peers": [{"name": "p", "url": "https://peer.example.com"}]}
    pinned, _ = await _compile(_manifest(network={"allow_hosts": ["peer.example.com"]}, **peers))
    free, _ = await _compile(_manifest(**peers))
    assert "manifest" not in _tool(pinned, "peer__p").args_schema.model_json_schema()["properties"]
    assert "manifest" in _tool(free, "peer__p").args_schema.model_json_schema()["properties"]


async def test_an_internationalised_host_is_matched_by_its_punycode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patterns are punycode, which is what a request carries; httpx's `host` is decoded."""
    monkeypatch.setattr(ssrf, "resolve_host", lambda host: ["127.0.0.1"])
    async with _serve(lambda req, w: _respond(w, b"OK")) as base:
        bound = EgressBound().under(["xn--mnchen-3ya.de"])
        async with safe_async_client(timeout=5, allow_http=True, egress=bound) as client:
            assert (await client.get(f"http://münchen.de:{_port(base)}/")).text == "OK"


# --- the browser: refused under a list, SSRF-guarded everywhere --------------------------


def test_a_browser_cannot_be_held_to_a_list() -> None:
    """A page reaches hosts through channels no interceptor sees: under a list, it would look
    confined and not be. Refused until the browser runs behind a proxy enforcing the bound."""
    browse = {"browser_tools": [{"name": "b", "binding": "chromium"}]}
    with pytest.raises(ManifestParseError, match="browser_tools"):
        _manifest(network={"allow_hosts": ["*.example.com"]}, **browse)
    _manifest(**browse)


async def test_a_child_browser_under_a_parents_list_is_refused_at_compile() -> None:
    child = _manifest("helper", browser_tools=[{"name": "b", "binding": "chromium"}])
    with pytest.raises(ValueError, match="browser_tools"):
        await _compile(_lead("lead", ["helper"], ["*.example.com"]), {"helper": child})
    await _compile(_lead("lead", ["helper"], None), {"helper": child})


class _Socket:
    def __init__(self, url: str) -> None:
        self.url = url
        self.connected = False
        self.closed = False

    def connect_to_server(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.closed = True


async def _socket_handler(*, allow_http: bool) -> Any:
    class _Context:
        async def route(self, _pattern: str, _handler: Any) -> None:
            return None

        async def route_web_socket(self, _pattern: str, handler: Any) -> None:
            self.socket_handler = handler

    (tool,) = tools_from_browser_refs([BrowserToolRef(name="b", binding="chromium")], allow_http=allow_http)
    context = _Context()
    await tool.executor._install_egress_guard(context)
    return context.socket_handler


async def test_a_browser_websocket_passes_the_ssrf_check_as_its_handshake(lookups: list[str]) -> None:
    """The SSRF check speaks http(s): a `wss://` URL handed to it as-is was refused every time.
    Mapped, a socket to loopback is refused outside development and admitted in it."""
    prod, dev = await _socket_handler(allow_http=False), await _socket_handler(allow_http=True)
    for scheme in ("ws", "wss"):
        refused, admitted = _Socket(f"{scheme}://box.test/socket"), _Socket(f"{scheme}://box.test/socket")
        await prod(refused)
        await dev(admitted)
        assert refused.closed and not refused.connected
        assert admitted.connected and not admitted.closed
