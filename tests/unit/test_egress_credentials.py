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
from felix.manifests.schema import HttpFetchToolRef
from felix.security import ssrf
from felix.security.egress import safe_async_client
from felix.security.egress_credentials import (
    CredentialSet,
    EgressCredentialError,
    bind_credentials,
    parse_egress_credentials,
)
from felix.security.host_allowlist import EgressBound
from felix.tools.http_fetch import tools_from_http_fetch_refs

from tests.support.factories import make_settings
from tests.support.loopback_http import Request
from tests.support.loopback_http import respond as _respond
from tests.support.loopback_http import serve as _serve

# Not shaped like any real provider's key: the masking test needs a value, not a lookalike.
VALUE = "felix-test-credential-value-0001"
OTHER = "felix-test-credential-value-0002"


@pytest.fixture(autouse=True)
def _loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssrf, "resolve_host", lambda host: ["127.0.0.1"] if host.endswith(".test") else [])


def _entry(**spec: Any) -> dict[str, Any]:
    return {"host": "api.test", "secret": VALUE, "scheme": "Bearer", "tenants": ["default"]} | spec


def _settings(**spec: Any) -> Any:
    return make_settings(egress_credentials=json.dumps({"api": _entry(**spec)}))


def _port(base: str) -> int:
    return int(base.rsplit(":", 1)[1])


def _echo(header: str = "authorization") -> Any:
    def respond(req: Request, writer: asyncio.StreamWriter) -> None:
        _respond(writer, f"{header}=[{req.headers.get(header, '')}]".encode())

    return respond


async def _bind(settings: Any, tenant: str = "default", hosts: list[str] | None = None) -> CredentialSet:
    creds = await bind_credentials(["api"], settings, tenant, allow_hosts=hosts or ["api.test"])
    assert creds is not None
    return creds


def _fetch(creds: CredentialSet, *hosts: str) -> Any:
    (tool,) = tools_from_http_fetch_refs(
        [HttpFetchToolRef(name="f", allow_any_host=True)],
        allow_http=True,
        egress=EgressBound().under(["api.test", *hosts]),
        credentials=creds,
    )
    return tool.executor


# --- the registry -------------------------------------------------------------------------


def test_a_registered_credential_is_parsed_with_its_defaults() -> None:
    [cred] = parse_egress_credentials(_settings()).values()
    assert (cred.host, cred.port, cred.path_prefix, cred.header) == ("api.test", None, "/", "Authorization")
    assert cred.allows("default") and not cred.allows("other")
    [open_to_all] = parse_egress_credentials(_settings(tenants="*")).values()
    assert open_to_all.allows("default") and open_to_all.allows("anyone")


@pytest.mark.parametrize(
    ("spec", "field"),
    [
        ({"host": "*.api.test"}, "not a wildcard"),  # it would reach every name under it
        ({"host": "203.0.113.7"}, "not an IP address"),
        ({"host": "https://api.test"}, "allow_hosts entry"),
        ({"port": 0}, "`port`"),
        ({"port": True}, "`port`"),
        ({"port": 65536}, "`port`"),
        ({"path_prefix": "repos/"}, "`path_prefix`"),
        ({"path_prefix": "/repos/#x"}, "`path_prefix`"),
        ({"header": "Host"}, "`header`"),
        ({"header": "Cookie"}, "`header`"),
        ({"header": "Bad Header"}, "`header`"),
        ({"scheme": "Bearer token"}, "`scheme`"),
        ({"secret": ""}, "`secret`"),
        ({"tenants": None}, "`tenants`"),
        ({"tenants": []}, "`tenants`"),
    ],
)
def test_a_malformed_credential_fails_the_boot(spec: dict[str, Any], field: str) -> None:
    with pytest.raises(RuntimeError, match="FELIX_EGRESS_CREDENTIALS") as raised:
        _settings(**spec).validate_runtime()
    assert field in str(raised.value)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("{not json", "not valid JSON"),
        ("[]", "must be a JSON object"),
        ('{"Bad Id": {}}', "must match"),
        ('{"api": "x"}', "must be an object"),
    ],
)
def test_a_malformed_registry_fails_the_boot(raw: str, message: str) -> None:
    with pytest.raises(RuntimeError, match=message):
        make_settings(egress_credentials=raw).validate_runtime()


# --- the manifest --------------------------------------------------------------------------


def _manifest(fetch: bool = True, **network: Any) -> Any:
    spec: dict[str, Any] = {"pattern": "react", "network": network}
    if fetch:
        spec["http_tools"] = [{"name": "fetch", "allow_any_host": True}]
    return parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": "keyed"}, "spec": spec}
    )


def test_credentials_need_a_host_list_and_a_client() -> None:
    """What an agent reads with a key may go only where its list allows; and named without a
    client that sends them, they would compile and do nothing."""
    with pytest.raises(ManifestParseError, match="allow_hosts"):
        _manifest(credentials=["api"])
    with pytest.raises(ManifestParseError, match="credential id"):
        _manifest(allow_hosts=["api.test"], credentials=["Not An Id"])
    with pytest.raises(ManifestParseError, match="http_tools"):
        _manifest(fetch=False, allow_hosts=["api.test"], credentials=["api"])
    _manifest(allow_hosts=["api.test"], credentials=["api"])


@pytest.mark.parametrize(
    ("tenant", "allow_hosts", "error"),
    [
        ("other", ["api.test"], "unknown egress credential: api"),
        ("default", ["*.test"], "must name exactly"),  # covered by a wildcard is not named
        ("default", ["elsewhere.test"], "must name exactly"),
    ],
)
async def test_a_credential_the_agent_may_not_use_refuses_the_compile(
    tenant: str, allow_hosts: list[str], error: str
) -> None:
    with pytest.raises(EgressCredentialError, match=error):
        await bind_credentials(["api"], _settings(), tenant, allow_hosts=allow_hosts)


async def test_an_unregistered_id_reads_as_another_tenants() -> None:
    """Which ids exist is not this tenant's to learn: the same words either way."""
    with pytest.raises(EgressCredentialError, match=r"^unknown egress credential: nope$"):
        await bind_credentials(["nope"], _settings(), "default", allow_hosts=["api.test"])


async def test_a_secret_ref_resolves_and_a_missing_one_refuses_the_compile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EGRESS_TEST_KEY", OTHER)
    creds = await _bind(_settings(secret="secret:EGRESS_TEST_KEY"))
    request = httpx.Request("GET", "https://api.test/x")
    creds.attach(request, allow_http=False)
    assert request.headers["authorization"] == f"Bearer {OTHER}"
    with pytest.raises(EgressCredentialError, match="could not be resolved"):
        await _bind(_settings(secret="secret:EGRESS_TEST_ABSENT"))


@pytest.mark.parametrize("value", [f"{VALUE}\n", f" {VALUE}", "short", "has\x00control-chars"])
async def test_a_value_that_would_leak_or_escape_masking_refuses_the_compile(value: str) -> None:
    """A trailing newline reached h11, which refused it in an error naming the whole value."""
    with pytest.raises(EgressCredentialError, match="resolved to a value") as raised:
        await _bind(_settings(secret=value))
    assert value.strip() not in str(raised.value)


# --- what is sent where --------------------------------------------------------------------------


async def test_the_key_goes_to_its_host_port_and_prefix_and_nowhere_else() -> None:
    async with _serve(_echo()) as base:
        port = _port(base)
        fetch = _fetch(await _bind(_settings(port=port, path_prefix="/repos/acme/")), "other.test")
        got = {
            name: await fetch.execute({"url": url})
            for name, url in {
                "inside": f"http://api.test:{port}/repos/acme/x",
                "outside": f"http://api.test:{port}/repos/other/x",
                "walked": f"http://api.test:{port}/repos/acme/../other",
                "double-encoded": f"http://api.test:{port}/repos/acme/%252e%252e/other",
                "other host": f"http://other.test:{port}/repos/acme/x",
            }.items()
        }
    assert got["inside"].endswith(f"authorization=[Bearer {VALUE}]")
    for name in ("outside", "walked", "double-encoded", "other host"):
        assert got[name].endswith("authorization=[]"), name


async def test_another_port_on_the_host_gets_no_key() -> None:
    async with _serve(_echo()) as base:
        port = _port(base)
        fetch = _fetch(await _bind(_settings(port=port + 1 if port < 65535 else 1)))
        assert (await fetch.execute({"url": f"http://api.test:{port}/x"})).endswith("authorization=[]")


async def test_a_custom_header_is_sent_as_configured() -> None:
    async with _serve(_echo("x-api-key")) as base:
        fetch = _fetch(await _bind(_settings(port=_port(base), header="X-Api-Key", scheme="")))
        out = await fetch.execute({"url": f"http://api.test:{_port(base)}/x"})
    assert out.endswith(f"x-api-key=[{VALUE}]")


async def test_two_credentials_on_one_host_each_keep_to_their_prefix() -> None:
    async with _serve(_echo()) as base:
        port = _port(base)
        registry = {
            "acme": _entry(port=port, path_prefix="/acme/"),
            "beta": _entry(port=port, path_prefix="/beta/", secret=OTHER),
        }
        settings = make_settings(egress_credentials=json.dumps(registry))
        creds = await bind_credentials(["acme", "beta"], settings, "default", allow_hosts=["api.test"])
        assert creds is not None
        fetch = _fetch(creds)
        acme = await fetch.execute({"url": f"http://api.test:{port}/acme/x"})
        beta = await fetch.execute({"url": f"http://api.test:{port}/beta/x"})
        neither = await fetch.execute({"url": f"http://api.test:{port}/gamma/x"})
    assert acme.endswith(f"[Bearer {VALUE}]") and beta.endswith(f"[Bearer {OTHER}]")
    assert neither.endswith("authorization=[]")


@pytest.mark.parametrize("hop", ["/repos/other/x", "http://other.test:{port}/repos/acme/x"])
async def test_a_redirect_out_of_the_prefix_or_off_the_host_leaves_the_key_behind(hop: str) -> None:
    """Through `http_fetch`, which follows each hop as a request of its own."""
    async with _serve(_echo("x-api-key")) as base:
        port = _port(base)

        def _redirect(req: Request, writer: asyncio.StreamWriter) -> None:
            if req.path.startswith("/repos/acme/start"):
                _respond(writer, b"", status="302 Found", extra=f"location: {hop.format(port=port)}\r\n")
            else:
                _echo("x-api-key")(req, writer)

        async with _serve(_redirect) as api:
            api_port = _port(api)
            hop_port = port if "other.test" in hop else api_port
            spec = {"port": api_port, "path_prefix": "/repos/acme/", "header": "X-Api-Key", "scheme": ""}
            fetch = _fetch(await _bind(_settings(**spec)), "other.test")
            out = await fetch.execute({"url": f"http://api.test:{api_port}/repos/acme/start"})
    assert f":{hop_port}/repos/" in out and out.endswith("x-api-key=[]")


def test_a_header_copied_onto_a_request_the_credential_does_not_cover_is_stripped() -> None:
    """httpx copies headers onto a redirect within an origin, and any but `Authorization` across one."""
    creds = CredentialSet(
        tuple(parse_egress_credentials(_settings(path_prefix="/acme/")).values()), {"api": VALUE}
    )
    copied = httpx.Request("GET", "https://api.test/other", headers={"Authorization": f"Bearer {VALUE}"})
    creds.attach(copied, allow_http=False)
    assert "authorization" not in copied.headers


async def test_a_client_carrying_credentials_must_follow_redirects_by_hand() -> None:
    creds = await _bind(_settings())
    with pytest.raises(TypeError, match="follow_redirects=False"):
        safe_async_client(timeout=5, credentials=creds, follow_redirects=True)


def test_a_credential_covers_only_https_unless_plain_text_is_allowed() -> None:
    [cred] = parse_egress_credentials(_settings()).values()
    assert cred.covers(httpx.URL("https://api.test/x"), allow_http=False)
    assert cred.covers(httpx.URL("https://API.TEST:443/x"), allow_http=False)
    assert not cred.covers(httpx.URL("http://api.test/x"), allow_http=False)
    assert not cred.covers(httpx.URL("https://api.test:8443/x"), allow_http=False)
    assert not cred.covers(httpx.URL("https://api.test.evil.test/x"), allow_http=False)


# --- end to end: the compiled agent's fetch tool -------------------------------------------------


async def _compiled_fetch(settings: Any, tenant: str) -> Any:
    from felix.manifests.builder import BuildDeps, build_agent
    from felix.tools.provider import InMemoryToolProvider

    manifest = _manifest(allow_hosts=["api.test"], credentials=["api"])
    deps = BuildDeps(tools=InMemoryToolProvider(), settings=settings, tenant_id=tenant)
    agent = await build_agent(manifest, deps=deps)
    [fetch] = [t for t in agent.tools if t.name == "fetch"]
    return fetch


async def test_the_model_sees_the_answer_and_never_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A server that echoes the header back hands the model `[REDACTED]` -- on the first compile
    in a process, before anything has sent the key, which is when the masking list was missing it."""
    from felix import secrets

    monkeypatch.setattr(secrets, "_resolved_secret_values", [])
    async with _serve(_echo()) as base:
        settings = _settings(port=_port(base), tenants=["acme"])
        fetch = await _compiled_fetch(settings, "acme")
        ctx = RequestContext(settings=settings, auth=AuthContext(tenant_id="acme"))
        async with async_run_with_context(ctx):
            out = await fetch.executor.execute({"url": f"http://api.test:{_port(base)}/x"})
    assert isinstance(out, str)
    assert "authorization=[Bearer [REDACTED]]" in out
    assert VALUE not in out


async def test_the_compile_binds_for_the_requests_tenant() -> None:
    settings = _settings(tenants=["acme"])
    await _compiled_fetch(settings, "acme")
    with pytest.raises(EgressCredentialError, match="unknown egress credential: api"):
        await _compiled_fetch(settings, "other")
