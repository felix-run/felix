"""What a compile remembers between requests, and what makes it forget.

The agent is compiled per request. Each test here pins one answer that used to be fetched on
every compile — the resolver's "not in the store", a server's MCP tool list, a cloud secret —
and the event that has to make the next compile fetch it again.
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest
from felix.manifests.resolver import invalidate_active, resolve_manifest
from felix.manifests.schema import McpServerRef


class _EmptyStore:
    """A manifest store with nothing in it, counting how often it is asked."""

    def __init__(self) -> None:
        self.asked = 0

    async def get_active(self, tenant_id: str, name: str) -> Any:
        self.asked += 1
        return None

    async def get_version(self, tenant_id: str, name: str, version: int) -> Any:
        return None


async def test_a_bundled_manifest_is_looked_up_in_the_store_once() -> None:
    store = _EmptyStore()
    for _ in range(3):
        resolved = await resolve_manifest("acme", "quick", manifest_store=store)
        assert resolved.source == "bundled"
    assert store.asked == 1


async def test_activation_makes_the_store_be_asked_again() -> None:
    store = _EmptyStore()
    await resolve_manifest("acme", "quick", manifest_store=store)
    invalidate_active("acme", "quick")
    await resolve_manifest("acme", "quick", manifest_store=store)
    assert store.asked == 2


def _ref(name: str, auth: str = "") -> McpServerRef:
    return McpServerRef(name=name, url=f"https://{name}.example.com/mcp", auth=auth)


async def test_mcp_discovery_is_remembered_per_server_and_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    from felix.mcp import client

    asked: list[tuple[str, str]] = []

    async def fake_list(ref: McpServerRef, *, allow_http: bool = False, handshake: Any = None) -> list:
        asked.append((ref.name, ref.auth))
        handshake["serverInfo"] = {"name": ref.name}
        return [{"name": "search", "description": "d"}]

    monkeypatch.setattr(client, "list_remote_tools", fake_list)
    first = await client.tools_from_mcp_servers([_ref("docs", "token-a")])
    again = await client.tools_from_mcp_servers([_ref("docs", "token-a")])
    other_caller = await client.tools_from_mcp_servers([_ref("docs", "token-b")])

    assert [t.name for t in first] == [t.name for t in again] == ["docs__search"]
    assert [t.name for t in other_caller] == ["docs__search"]
    # A different credential is a different caller, and is asked for itself.
    assert asked == [("docs", "token-a"), ("docs", "token-b")]


async def test_a_failed_discovery_is_not_remembered(monkeypatch: pytest.MonkeyPatch) -> None:
    from felix.mcp import client

    calls = 0

    async def flaky(ref: McpServerRef, *, allow_http: bool = False, handshake: Any = None) -> list:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("down")
        return [{"name": "search"}]

    monkeypatch.setattr(client, "list_remote_tools", flaky)
    assert await client.tools_from_mcp_servers([_ref("docs")]) == []
    assert [t.name for t in await client.tools_from_mcp_servers([_ref("docs")])] == ["docs__search"]


async def test_mcp_servers_are_asked_concurrently_and_bound_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each fake server waits until both are in flight; asked one at a time, this deadlocks."""
    from felix.mcp import client

    both_in_flight = asyncio.Barrier(2)

    async def fake_list(ref: McpServerRef, *, allow_http: bool = False, handshake: Any = None) -> list:
        await both_in_flight.wait()
        return [{"name": "t"}]

    monkeypatch.setattr(client, "list_remote_tools", fake_list)
    tools = await asyncio.wait_for(client.tools_from_mcp_servers([_ref("b"), _ref("a")]), timeout=2)
    assert [t.name for t in tools] == ["b__t", "a__t"]


class _FakeAws:
    class exceptions:
        class ResourceNotFoundException(Exception):
            pass

    def __init__(self) -> None:
        self.fetched: list[str] = []
        self.threads: set[int] = set()

    def get_secret_value(self, SecretId: str) -> dict[str, str]:
        self.fetched.append(SecretId)
        self.threads.add(threading.get_ident())
        if SecretId == "missing":
            raise self.exceptions.ResourceNotFoundException()
        return {"SecretString": f"value-of-{SecretId}"}


async def test_cloud_secrets_are_fetched_off_the_loop_and_remembered(monkeypatch: pytest.MonkeyPatch) -> None:
    from felix import secrets

    fake = _FakeAws()
    monkeypatch.setattr(secrets, "_aws_client", lambda region: fake)
    provider = secrets.AwsSecretsManager(region="us-east-1")

    assert await provider.get("db") == "value-of-db"
    assert await provider.get("db") == "value-of-db"
    assert await provider.get("missing") is None
    assert await provider.get("missing") is None

    # The hit is not refetched; a miss always is, so a newly created secret is seen at once.
    assert fake.fetched == ["db", "missing", "missing"]
    assert threading.get_ident() not in fake.threads


async def test_a_remembered_absence_lapses_with_the_pointer_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """Another replica's activation reaches this one within `ACTIVE_TTL_MS`, absent or not."""
    import time

    from felix.manifests import resolver

    store = _EmptyStore()
    await resolve_manifest("acme", "quick", manifest_store=store)
    later = time.time() + resolver.ACTIVE_TTL_MS / 1000 + 1
    monkeypatch.setattr(resolver, "time", SimpleNamespace(time=lambda: later))
    await resolve_manifest("acme", "quick", manifest_store=store)
    assert store.asked == 2


def _after(monkeypatch: pytest.MonkeyPatch, seconds: float) -> None:
    """Move every `BoundedCache` clock — and only theirs — `seconds` ahead."""
    from felix import bounded_cache

    later = time.monotonic() + seconds
    monkeypatch.setattr(bounded_cache, "time", SimpleNamespace(monotonic=lambda: later))


async def test_mcp_discovery_lapses_after_its_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    from felix.mcp import client

    asked = 0

    async def fake_list(ref: McpServerRef, *, allow_http: bool = False, handshake: Any = None) -> list:
        nonlocal asked
        asked += 1
        handshake["serverInfo"] = {"name": ref.name}
        return [{"name": "search"}]

    monkeypatch.setattr(client, "list_remote_tools", fake_list)
    await client.tools_from_mcp_servers([_ref("docs")])
    _after(monkeypatch, client.DISCOVERY_TTL_S + 1)
    await client.tools_from_mcp_servers([_ref("docs")])
    assert asked == 2


async def test_a_cached_discovery_keeps_the_servers_instructions(monkeypatch: pytest.MonkeyPatch) -> None:
    """The handshake is cached with the tool list; a hit that lost it would drop the guidance."""
    from felix.mcp import client

    async def fake_list(ref: McpServerRef, *, allow_http: bool = False, handshake: Any = None) -> list:
        handshake["instructions"] = "Search before you answer."
        return [{"name": "search"}]

    monkeypatch.setattr(client, "list_remote_tools", fake_list)
    ref = McpServerRef(name="docs", url="https://docs.example.com/mcp", use_instructions=True)
    first = await client.tools_from_mcp_servers([ref])
    again = await client.tools_from_mcp_servers([ref])
    assert first[0].prompt_guidance
    assert again[0].prompt_guidance == first[0].prompt_guidance


async def test_a_cache_hit_is_a_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    from felix.mcp import client

    async def fake_list(ref: McpServerRef, *, allow_http: bool = False, handshake: Any = None) -> list:
        handshake["serverInfo"] = {"name": ref.name}
        return [{"name": "search", "inputSchema": {"type": "object", "properties": {}}}]

    monkeypatch.setattr(client, "list_remote_tools", fake_list)
    await client._discover(_ref("docs"), allow_http=False)  # the miss that fills the cache
    hit, shake = await client._discover(_ref("docs"), allow_http=False)
    hit[0]["inputSchema"]["properties"]["planted"] = {"type": "string"}
    shake["serverInfo"]["name"] = "planted"
    again, again_shake = await client._discover(_ref("docs"), allow_http=False)
    assert again[0]["inputSchema"]["properties"] == {}
    assert again_shake["serverInfo"]["name"] == "docs"


async def test_cloud_secrets_lapse_and_are_keyed_by_region(monkeypatch: pytest.MonkeyPatch) -> None:
    from felix import secrets

    fake = _FakeAws()
    monkeypatch.setattr(secrets, "_aws_client", lambda region: fake)
    await secrets.AwsSecretsManager(region="us-east-1").get("db")
    await secrets.AwsSecretsManager(region="eu-west-1").get("db")
    _after(monkeypatch, secrets.CLOUD_SECRET_TTL_S + 1)
    await secrets.AwsSecretsManager(region="us-east-1").get("db")
    assert fake.fetched == ["db", "db", "db"]


async def test_gcp_secrets_are_keyed_by_project(monkeypatch: pytest.MonkeyPatch) -> None:
    from felix import secrets

    asked: list[str] = []

    class _Payload:
        def __init__(self, path: str) -> None:
            self.data = path.encode()

    class _FakeGcp:
        def access_secret_version(self, request: dict[str, str]) -> Any:
            asked.append(request["name"])
            return SimpleNamespace(payload=_Payload(request["name"]))

    monkeypatch.setattr(secrets, "_gcp_client", lambda: _FakeGcp())
    a = await secrets.GcpSecretManager("proj-a").get("db")
    b = await secrets.GcpSecretManager("proj-b").get("db")
    await secrets.GcpSecretManager("proj-a").get("db")
    assert (a, b) == (
        "projects/proj-a/secrets/db/versions/latest",
        "projects/proj-b/secrets/db/versions/latest",
    )
    assert len(asked) == 2


async def test_a_degraded_discovery_is_not_remembered(monkeypatch: pytest.MonkeyPatch) -> None:
    """Discovery degrades rather than raising: no tools from a failed stdio spawn, no handshake
    from a failed `initialize`. Neither may stand for the server for a whole TTL."""
    from felix.mcp import client

    answers = [([], {}), ([{"name": "search"}], {}), ([{"name": "search"}], {"instructions": "x"})]
    asked = 0

    async def degrading(ref: McpServerRef, *, allow_http: bool = False, handshake: Any = None) -> list:
        nonlocal asked
        remotes, shake = answers[min(asked, len(answers) - 1)]
        asked += 1
        handshake.update(shake)
        return list(remotes)

    monkeypatch.setattr(client, "list_remote_tools", degrading)
    for _ in range(4):
        await client.tools_from_mcp_servers([_ref("docs")])
    # Asked again after each partial answer; the first whole one is the last fetch.
    assert asked == 3


async def test_mcp_discovery_concurrency_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    from felix.mcp import client

    in_flight = peak = 0

    async def fake_list(ref: McpServerRef, *, allow_http: bool = False, handshake: Any = None) -> list:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0)
        in_flight -= 1
        return [{"name": "t"}]

    monkeypatch.setattr(client, "list_remote_tools", fake_list)
    refs = [_ref(f"s{i}") for i in range(client._DISCOVERY_CONCURRENCY * 3)]
    await client.tools_from_mcp_servers(refs)
    assert peak == client._DISCOVERY_CONCURRENCY


async def test_two_compiles_share_one_mcp_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Through `build_agent`, with the credential resolved from a `secret:` ref as production does.

    The discovery key is the ref *after* secret resolution, so a field that differed per compile
    would make every compile a miss while each unit test above stayed green.
    """
    from felix.config import Settings
    from felix.manifests.builder import BuildDeps, build_agent
    from felix.mcp import client
    from felix.storage import MemoryObjectStore
    from felix.tools.provider import InMemoryToolProvider

    monkeypatch.setenv("FELIX_TEST_MCP_TOKEN", "resolved-token")
    asked: list[str] = []

    async def fake_list(ref: McpServerRef, *, allow_http: bool = False, handshake: Any = None) -> list:
        asked.append(ref.auth)
        handshake["serverInfo"] = {"name": ref.name}
        return [{"name": "search", "description": "Search the docs."}]

    monkeypatch.setattr(client, "list_remote_tools", fake_list)
    settings = Settings(database_url="memory://compile-cache", object_store="memory")
    manifest = {
        "apiVersion": "felix/v1",
        "kind": "Agent",
        "metadata": {"name": "mcp-cache"},
        "spec": {
            "pattern": "react",
            "mcp_servers": [
                {"name": "docs", "url": "https://docs.example.com/mcp", "auth": "secret:FELIX_TEST_MCP_TOKEN"}
            ],
        },
    }
    names = []
    for _ in range(2):
        agent = await build_agent(
            manifest,
            deps=BuildDeps(
                tools=InMemoryToolProvider(),
                settings=settings,
                tenant_id="acme",
                object_store=MemoryObjectStore(),
            ),
            settings=settings,
        )
        names.append(sorted(t.name for t in agent.tools if t.name.startswith("docs__")))

    assert names == [["docs__search"], ["docs__search"]]
    assert asked == ["resolved-token"]
