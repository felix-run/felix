"""`spec.network.allow_hosts`: the hosts an agent's tools may reach, enforced where the model
picks the destination and checked against every URL the manifest names."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.manifests.loader import ManifestParseError, parse_manifest
from felix.manifests.schema import HttpFetchToolRef
from felix.security.egress import safe_async_client
from felix.security.host_allowlist import (
    EGRESS_CEILINGS_EXTRA,
    EgressBound,
    HostAllowlist,
    normalise_host_pattern,
    restore_ceilings,
    url_host,
)
from felix.security.ssrf import EgressBlocked
from felix.tools.http_fetch import tools_from_http_fetch_refs

from tests.support.factories import make_settings
from tests.support.loopback_http import Request
from tests.support.loopback_http import respond as _respond
from tests.support.loopback_http import serve as _serve

BLOCKED = "http_fetch_error: egress_blocked: destination not permitted"


@pytest.mark.parametrize(
    ("raw", "stored"),
    [
        ("API.GitHub.com", "api.github.com"),
        ("api.github.com.", "api.github.com"),
        ("*.github.com", "*.github.com"),
        ("203.0.113.7", "203.0.113.7"),
        ("[2001:db8::1]", "2001:db8::1"),
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
        ("evilgithub.com", False),
        ("github.com.evil.com", False),
        ("203.0.113.7", True),
        ("203.0.113.8", False),
        ("", False),
    ],
)
def test_a_host_is_allowed_only_by_a_pattern_that_names_it(host: str, allowed: bool) -> None:
    patterns = ("api.github.com", "*.github.com", "203.0.113.7")
    assert HostAllowlist(patterns).allows(host) is allowed


def test_a_bound_allows_only_what_every_list_in_it_allows() -> None:
    unrestricted = EgressBound()
    assert unrestricted.allows("anywhere.example") and unrestricted.under(None) == unrestricted
    parent = unrestricted.under(["*.example.com"])
    child = parent.under(["api.example.com", "other.org"])
    assert child.allows("api.example.com")
    assert not child.allows("other.org"), "the parent's list still binds the child"
    assert not child.allows("www.example.com"), "and so does the child's own"
    assert parent.under([]).allows("api.example.com") is False, "an empty list allows nothing"


async def test_a_runs_carried_lists_bind_every_bound_inside_it() -> None:
    """A background child: its parents' lists ride the durable run, not the compile."""
    ctx = RequestContext(settings=make_settings(), auth=AuthContext())
    ctx.extras[EGRESS_CEILINGS_EXTRA] = [["api.example.com"]]
    async with async_run_with_context(ctx):
        assert EgressBound().allows("api.example.com")
        assert not EgressBound().allows("other.org")
        assert EgressBound().under(["other.org"]).as_state() == [["other.org"], ["api.example.com"]]


@pytest.mark.parametrize("raw", ["x", [["ok.com"], "x"], [["https://ok.com"]]])
def test_a_stored_run_list_is_revalidated(raw: Any) -> None:
    with pytest.raises(ValueError):
        restore_ceilings(raw)
    assert restore_ceilings([["OK.com."]]) == [["ok.com"]]
    assert restore_ceilings(None) == []


def test_url_host_is_the_punycode_a_request_carries() -> None:
    assert url_host("https://münchen.de/x") == "xn--mnchen-3ya.de"
    assert url_host("http://[::1]:8080/") == "::1"
    assert url_host("not a url") == ""


# --- write-time: the manifest's own URLs ---------------------------------------------


def _manifest(name: str = "net", **spec: Any) -> Any:
    return parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": name},
            "spec": {"pattern": "react", **spec},
        }
    )


@pytest.mark.parametrize(
    "spec",
    [
        {"mcp_servers": [{"name": "gh", "url": "https://mcp.elsewhere.io/sse"}]},
        {"peers": [{"name": "p", "url": "https://peer.elsewhere.io"}]},
        {"http_tools": [{"name": "f", "path_prefix": "https://docs.elsewhere.io/"}]},
    ],
)
def test_a_declared_url_outside_the_list_is_refused_at_write(spec: dict[str, Any]) -> None:
    with pytest.raises(ManifestParseError, match=r"elsewhere\.io"):
        _manifest(network={"allow_hosts": ["*.example.com"]}, **spec)


def test_declared_urls_inside_the_list_and_unlisted_manifests_are_left_alone() -> None:
    mcp = {"mcp_servers": [{"name": "gh", "url": "https://mcp.example.com/sse"}]}
    _manifest(network={"allow_hosts": ["*.example.com"]}, **mcp)
    _manifest(mcp_servers=[{"name": "gh", "url": "https://mcp.elsewhere.io/sse"}])


# --- enforcement: the guarded transport and http_fetch ------------------------------


async def test_the_transport_refuses_a_host_outside_the_bound() -> None:
    async with _serve(lambda req, w: _respond(w, b"OK")) as base:
        port = base.rsplit(":", 1)[1]
        bound = EgressBound().under(["127.0.0.1"])
        async with safe_async_client(timeout=5, allow_http=True, egress=bound) as client:
            assert (await client.get(f"{base}/")).text == "OK"
            with pytest.raises(EgressBlocked):
                await client.get(f"http://localhost:{port}/")


async def test_http_fetch_refuses_a_host_and_a_redirect_hop_outside_the_bound() -> None:
    """`localhost` and `127.0.0.1` are the same server: only the host name differs."""
    async with _serve(lambda req, w: _respond(w, b"SECRET")) as other:
        port = other.rsplit(":", 1)[1]

        def _redirect(req: Request, writer: asyncio.StreamWriter) -> None:
            _respond(writer, b"", status="302 Found", extra=f"location: http://localhost:{port}/x\r\n")

        async with _serve(_redirect) as allowed:
            (tool,) = tools_from_http_fetch_refs(
                [HttpFetchToolRef(name="f", allow_any_host=True)],
                allow_http=True,
                egress=EgressBound().under(["127.0.0.1"]),
            )
            direct = await tool.executor.execute({"url": f"http://localhost:{port}/x"})
            hop = await tool.executor.execute({"url": f"{allowed}/start"})
    assert direct == BLOCKED
    assert "SECRET" not in hop and BLOCKED in hop


# --- compile: children answer to their parents' lists --------------------------------


async def _compile(root: Any, children: dict[str, Any]) -> Any:
    from felix.manifests.builder import BuildDeps, build_agent
    from felix.tools.provider import InMemoryToolProvider

    deps = BuildDeps(tools=InMemoryToolProvider(), settings=make_settings())

    async def build_child(name: str) -> Any:
        return await build_agent(children[name], deps=deps)

    deps.sub_agent_builder = build_child
    await build_agent(root, deps=deps)
    return deps


def _fetcher(name: str, **spec: Any) -> Any:
    return _manifest(name, http_tools=[{"name": "fetch", "allow_any_host": True}], **spec)


def _lead(name: str, children: list[str], hosts: list[str] | None) -> Any:
    network = {"network": {"allow_hosts": hosts}} if hosts is not None else {}
    return _manifest(
        name, delegation={"agents": [{"name": c, "description": "x"} for c in children]}, **network
    )


def _fetch_tool(agent: Any) -> Any:
    [tool] = [t for t in agent.tools if t.name == "fetch"]
    return tool


async def test_a_childs_url_outside_its_parents_list_is_refused_at_compile() -> None:
    child = _manifest("helper", http_tools=[{"name": "fetch", "path_prefix": "https://docs.elsewhere.io/"}])
    with pytest.raises(ValueError, match=r"elsewhere\.io"):
        await _compile(_lead("lead", ["helper"], ["*.example.com"]), {"helper": child})


async def test_a_child_shared_by_two_parents_is_bound_by_each_ones_list() -> None:
    """The compile memo once handed whichever parent compiled the child first its tools."""
    root = _lead("root", ["open", "closed"], None)
    deps = await _compile(
        root,
        {
            "open": _lead("open", ["helper"], None),
            "closed": _lead("closed", ["helper"], ["api.example.com"]),
            "helper": _fetcher("helper"),
        },
    )
    ctx = RequestContext(settings=make_settings(), auth=AuthContext())
    async with async_run_with_context(ctx):
        out = await _fetch_tool(deps.compiled["helper"]).executor.execute({"url": "https://elsewhere.io/"})
    assert str(out) == BLOCKED, "the last compile of the shared child is the closed parent's"
    assert deps.compiled_egress["helper"] == EgressBound().under(["api.example.com"])


# --- the browser: the navigation and every request the page makes --------------------


async def test_the_browser_refuses_a_page_request_outside_the_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_check_egress` is what the page's route guard runs for every subresource and redirect."""
    from felix.manifests.schema import BrowserToolRef
    from felix.security import ssrf
    from felix.tools.browser import tools_from_browser_refs

    monkeypatch.setattr(ssrf, "resolve_host", lambda host: ["93.184.216.34"])
    ref = BrowserToolRef(name="browse", binding="chromium")
    (tool,) = tools_from_browser_refs([ref], egress=EgressBound().under(["docs.example.com"]))
    await tool.executor._check_egress("https://docs.example.com/page")
    with pytest.raises(ValueError, match="egress_blocked"):
        await tool.executor._check_egress("https://tracker.elsewhere.io/pixel.gif")
    out = await tool.executor.execute({"url": "https://elsewhere.io/"})
    assert out == "browser_error: egress_blocked: destination not permitted"


async def test_an_internationalised_host_is_matched_by_its_punycode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patterns are punycode, which is what a request carries; httpx's `host` is decoded."""
    from felix.security import ssrf

    monkeypatch.setattr(ssrf, "resolve_host", lambda host: ["127.0.0.1"])
    async with _serve(lambda req, w: _respond(w, b"OK")) as base:
        port = base.rsplit(":", 1)[1]
        bound = EgressBound().under(["xn--mnchen-3ya.de"])
        async with safe_async_client(timeout=5, allow_http=True, egress=bound) as client:
            assert (await client.get(f"http://münchen.de:{port}/")).text == "OK"
