"""Credentials the model never holds: an operator's header, attached in transit to one host.

Every network case runs against a loopback server that echoes back the header it received, under
`.test` names a patched `ssrf.resolve_host` sends there -- so "was the key sent" is read off the
wire, and no case touches real DNS.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.manifests.loader import ManifestParseError, parse_manifest
from felix.security import ssrf
from felix.security.egress import safe_async_client
from felix.security.egress_credentials import bind_credentials, parse_egress_credentials
from felix.security.host_allowlist import EgressBound

from tests.support.factories import make_settings
from tests.support.loopback_http import Request
from tests.support.loopback_http import respond as _respond
from tests.support.loopback_http import serve as _serve

# Not shaped like any real provider's key: the masking test needs a value, not a lookalike.
VALUE = "felix-test-credential-value-0001"


@pytest.fixture(autouse=True)
def _loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssrf, "resolve_host", lambda host: ["127.0.0.1"] if host.endswith(".test") else [])


def _registry(**spec: Any) -> str:
    base = {"host": "api.test", "secret": VALUE, "scheme": "Bearer", "tenants": ["default"]}
    return json.dumps({"api": base | spec})


def _settings(**spec: Any) -> Any:
    return make_settings(egress_credentials=_registry(**spec))


def _port(base: str) -> int:
    return int(base.rsplit(":", 1)[1])


async def _echo(req: Request, writer: asyncio.StreamWriter) -> None:
    _respond(writer, f"auth=[{req.headers.get('authorization', '')}]".encode())


# --- the registry --------------------------------------------------------------------------


def test_a_registered_credential_is_parsed_with_its_defaults() -> None:
    [cred] = parse_egress_credentials(_settings()).values()
    assert (cred.host, cred.port, cred.path_prefix, cred.header) == ("api.test", None, "/", "Authorization")
    assert cred.allows("default") and not cred.allows("other")


@pytest.mark.parametrize(
    "spec",
    [
        {"host": "*.api.test"},  # a wildcard would send the key to every name under it
        {"host": "https://api.test"},
        {"port": 0},
        {"port": "443"},
        {"path_prefix": "repos/"},
        {"path_prefix": "/repos/?x=1"},
        {"header": "Host"},
        {"header": "Bad Header"},
        {"scheme": "Bearer token"},
        {"secret": ""},
        {"tenants": None},
        {"tenants": []},
    ],
)
def test_a_malformed_credential_fails_the_boot(spec: dict[str, Any]) -> None:
    with pytest.raises(RuntimeError, match="FELIX_EGRESS_CREDENTIALS"):
        _settings(**spec).validate_runtime()


# --- the manifest ----------------------------------------------------------------------------


def _manifest(**network: Any) -> Any:
    spec = {"pattern": "react", "network": network, "http_tools": [{"name": "fetch", "allow_any_host": True}]}
    return parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": "keyed"}, "spec": spec}
    )


def test_credentials_need_a_host_list() -> None:
    """What an agent reads with a key may go only where its list allows."""
    with pytest.raises(ManifestParseError, match="allow_hosts"):
        _manifest(credentials=["api"])
    with pytest.raises(ManifestParseError, match="credential id"):
        _manifest(allow_hosts=["api.test"], credentials=["Not An Id"])
    _manifest(allow_hosts=["api.test"], credentials=["api"])


@pytest.mark.parametrize(
    ("tenant", "allow_hosts", "error"),
    [
        ("other", ["api.test"], "unknown egress credential: api"),
        ("default", ["*.test"], "must name exactly"),  # covered by a wildcard is not named
        ("default", ["elsewhere.test"], "must name exactly"),
    ],
)
def test_a_credential_the_agent_may_not_use_refuses_the_compile(
    tenant: str, allow_hosts: list[str], error: str
) -> None:
    with pytest.raises(ValueError, match=error):
        bind_credentials(["api"], _settings(), tenant, allow_hosts=allow_hosts, allow_http=True)
    with pytest.raises(ValueError, match="unknown egress credential: nope"):
        bind_credentials(["nope"], _settings(), "default", allow_hosts=["api.test"], allow_http=True)


# --- the transport: what is sent where ----------------------------------------------------------


async def _sent(url: str, *, allow_http: bool = True, **spec: Any) -> str:
    creds = bind_credentials(
        ["api"], _settings(**spec), "default", allow_hosts=["api.test"], allow_http=allow_http
    )
    bound = EgressBound().under(["api.test", "other.test"])
    async with safe_async_client(timeout=5, allow_http=True, egress=bound, credentials=creds) as client:
        return (await client.get(url)).text


async def test_the_key_goes_to_its_host_port_and_prefix_and_nowhere_else() -> None:
    async with _serve(_echo) as base:
        port = _port(base)
        inside = await _sent(f"http://api.test:{port}/repos/acme/x", port=port, path_prefix="/repos/acme/")
        outside = await _sent(f"http://api.test:{port}/repos/other/x", port=port, path_prefix="/repos/acme/")
        walked = await _sent(
            f"http://api.test:{port}/repos/acme/../other", port=port, path_prefix="/repos/acme/"
        )
        other_host = await _sent(
            f"http://other.test:{port}/repos/acme/x", port=port, path_prefix="/repos/acme/"
        )
        other_port = await _sent(f"http://api.test:{port}/repos/acme/x", port=port + 1 if port < 65535 else 1)
    assert inside == f"auth=[Bearer {VALUE}]"
    assert outside == walked == other_host == other_port == "auth=[]"


async def test_the_key_is_never_sent_in_plain_text_outside_development() -> None:
    async with _serve(_echo) as base:
        port = _port(base)
        assert await _sent(f"http://api.test:{port}/", allow_http=False, port=port) == "auth=[]"


async def test_a_redirect_off_the_host_leaves_the_key_behind() -> None:
    async with _serve(_echo) as other:

        def _redirect(req: Request, writer: asyncio.StreamWriter) -> None:
            _respond(
                writer, b"", status="302 Found", extra=f"location: http://other.test:{_port(other)}/x\r\n"
            )

        async with _serve(_redirect) as api:
            creds = bind_credentials(
                ["api"], _settings(port=_port(api)), "default", allow_hosts=["api.test"], allow_http=True
            )
            bound = EgressBound().under(["api.test", "other.test"])
            async with safe_async_client(
                timeout=5, allow_http=True, egress=bound, credentials=creds, follow_redirects=True
            ) as client:
                landed = await client.get(f"http://api.test:{_port(api)}/start")
    assert landed.text == "auth=[]"
    assert landed.history and landed.history[0].request.headers.get("authorization") == f"Bearer {VALUE}"


def test_a_credential_covers_only_https_unless_plain_text_is_allowed() -> None:
    [cred] = parse_egress_credentials(_settings()).values()
    assert cred.covers(httpx.URL("https://api.test/x"), allow_http=False)
    assert not cred.covers(httpx.URL("http://api.test/x"), allow_http=False)
    assert not cred.covers(httpx.URL("https://api.test:8443/x"), allow_http=False)
    assert not cred.covers(httpx.URL("https://API.test.evil.test/x"), allow_http=False)


# --- end to end: the compiled agent's fetch tool -------------------------------------------------


async def test_the_model_sees_the_answer_and_never_the_key() -> None:
    """A server that echoes the header back hands the model `[REDACTED]`, not the key."""
    from felix.manifests.builder import BuildDeps, build_agent
    from felix.tools.provider import InMemoryToolProvider

    async with _serve(_echo) as base:
        settings = _settings(port=_port(base))
        manifest = _manifest(allow_hosts=["api.test"], credentials=["api"])
        agent = await build_agent(manifest, deps=BuildDeps(tools=InMemoryToolProvider(), settings=settings))
        [fetch] = [t for t in agent.tools if t.name == "fetch"]
        ctx = RequestContext(settings=settings, auth=AuthContext())
        async with async_run_with_context(ctx):
            out = await fetch.executor.execute({"url": f"http://api.test:{_port(base)}/x"})
    text = out if isinstance(out, str) else str(getattr(out, "content", out))
    assert "auth=[Bearer [REDACTED]]" in text
    assert VALUE not in text
