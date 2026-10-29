"""One contract for each person's stored connections, run against both backends.

The properties a dict and a `SELECT` can quietly disagree on: that connecting again replaces the
person's own row and keeps when they first connected, that one person in two tenants is two rows
neither can see, that callers racing a refresh spend it once (the Postgres arm takes an advisory
lock; the memory arm an asyncio lock) and store the rotated token, that a removal waits for a
refresh in flight, that a refusal leaves the row `revoked` and an outage leaves it alone with its
lock released, and that the operator's listing is newest first with ties broken on the key in
code point order whatever the database's collation.
"""

from __future__ import annotations

import asyncio
import base64
import os
from typing import Any

import pytest
from felix.auth import connections
from felix.auth.connections import OAuthGrant

BACKENDS = ["memory", "postgres"]
parametrized = pytest.mark.parametrize("store_settings", BACKENDS, indirect=True)
ME = "felix-self|google:1"
ROW = ("acme", ME, "conformance", "me@example.com")


class Provider:
    name = "conformance"

    def __init__(self) -> None:
        self.refreshes: list[str] = []
        self.revoked: list[str] = []
        self.refuse = False
        self.fail: Exception | None = None
        self.gate: asyncio.Event | None = None

    async def refresh(self, settings: Any, refresh_token: str) -> OAuthGrant:
        self.refreshes.append(refresh_token)
        await asyncio.sleep(0.01)
        if self.gate is not None:
            await self.gate.wait()
        if self.refuse:
            raise connections.ConnectionRefused("refused")
        if self.fail is not None:
            raise self.fail
        n = len(self.refreshes)
        return OAuthGrant(access_token=f"access-{n}", expires_in=3600, refresh_token=f"refresh-{n + 1}")

    async def revoke(self, settings: Any, token: str) -> None:
        self.revoked.append(token)


@pytest.fixture
def provider() -> Any:
    fake = Provider()
    connections.register_connection_provider(fake)
    yield fake
    connections.unregister_connection_provider(fake.name)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    now = [1_000_000]
    monkeypatch.setattr(connections, "now_ms", lambda: now[-1])
    return now


def _keyed(settings: Any) -> Any:
    return settings.model_copy(update={"connection_token_key": base64.b64encode(os.urandom(32)).decode()})


async def _connect(
    settings: Any, tenant: str = "acme", account: str = "me@example.com", label: str = "me", person: str = ME
) -> Any:
    grant = OAuthGrant(access_token="access-0", expires_in=3600, refresh_token="refresh-1")
    return await connections.save_oauth(
        settings, tenant, person=person, provider="conformance", account=account, grant=grant, label=label
    )


async def _status(settings: Any) -> str:
    return (await connections.get_connection(settings, *ROW) or {}).get("status", "gone")


@parametrized
async def test_connecting_again_replaces_the_row_and_keeps_its_first_connection(
    store_settings: Any, clock: list[int]
) -> None:
    settings = _keyed(store_settings)
    await _connect(settings, label="first")
    clock.append(2_000_000)
    again = await _connect(settings, label="again")
    rows = await connections.list_for_person(settings, "acme", ME)
    assert [r["label"] for r in rows] == ["again"]
    assert (again["created_at"], again["updated_at"]) == (1_000_000, 2_000_000)


@parametrized
async def test_one_person_in_two_tenants_is_two_rows(store_settings: Any) -> None:
    settings = _keyed(store_settings)
    await _connect(settings, "acme")
    await _connect(settings, "globex", person="felix-self|google:9")
    await _connect(settings, "globex")
    assert [r["person"] for r in await connections.list_connections(settings, "acme")] == [ME]
    assert await connections.remove_connection(settings, *ROW) is True
    assert await connections.get_connection(settings, *ROW) is None
    assert (await connections.get_connection(settings, "globex", *ROW[1:]) or {})["status"] == "active"


@parametrized
async def test_racing_callers_refresh_once_and_store_the_rotated_token(
    store_settings: Any, provider: Provider
) -> None:
    settings = _keyed(store_settings)
    await _connect(settings)
    await connections._force_expiry_for_tests(settings, ROW)
    tokens = await asyncio.gather(*(connections.access_token(settings, *ROW) for _ in range(4)))
    assert tokens == ["access-1"] * 4
    assert provider.refreshes == ["refresh-1"]
    await connections._force_expiry_for_tests(settings, ROW)
    assert await connections.access_token(settings, *ROW) == "access-2"
    assert provider.refreshes == ["refresh-1", "refresh-2"]


@parametrized
async def test_a_removal_waits_for_a_refresh_in_flight(store_settings: Any, provider: Provider) -> None:
    settings = _keyed(store_settings)
    await _connect(settings)
    await connections._force_expiry_for_tests(settings, ROW)
    provider.gate = asyncio.Event()
    refreshing = asyncio.ensure_future(connections.access_token(settings, *ROW))
    await asyncio.sleep(0.05)
    removing = asyncio.ensure_future(connections.remove_connection(settings, *ROW))
    await asyncio.sleep(0.05)
    assert not removing.done()
    provider.gate.set()
    assert await refreshing == "access-1"
    assert await removing is True
    # What was withdrawn is the grant the refresh stored, not the one it spent.
    assert provider.revoked == ["refresh-2"]


@parametrized
async def test_a_refused_refresh_is_stored_as_revoked(store_settings: Any, provider: Provider) -> None:
    settings = _keyed(store_settings)
    await _connect(settings)
    await connections._force_expiry_for_tests(settings, ROW)
    provider.refuse = True
    with pytest.raises(connections.ConnectionRevoked):
        await connections.access_token(settings, *ROW)
    assert await _status(settings) == "revoked"


@parametrized
async def test_an_outage_or_a_missing_provider_revokes_nothing_and_frees_the_lock(
    store_settings: Any,
) -> None:
    settings = _keyed(store_settings)
    await _connect(settings)
    await connections._force_expiry_for_tests(settings, ROW)
    with pytest.raises(connections.ProviderUnavailable):
        await connections.access_token(settings, *ROW)
    assert await _status(settings) == "active"
    provider = Provider()
    provider.fail = TimeoutError("down")
    connections.register_connection_provider(provider)
    try:
        with pytest.raises(connections.ProviderFailed):
            await connections.access_token(settings, *ROW)
        assert await _status(settings) == "active"
        provider.fail = None
        # The same refresh token again, and no wait on a lock left held.
        assert await asyncio.wait_for(connections.access_token(settings, *ROW), 5) == "access-2"
        assert provider.refreshes == ["refresh-1", "refresh-1"]
    finally:
        connections.unregister_connection_provider(provider.name)


@parametrized
async def test_the_operators_listing_is_newest_first_ending_on_the_key(
    store_settings: Any, clock: list[int]
) -> None:
    settings = _keyed(store_settings)
    # Mixed case on purpose: a database collation orders `B` and `a` differently than code points.
    for account in ("b@example.com", "B@example.com", "a@example.com"):
        await _connect(settings, account=account)
    clock.append(2_000_000)
    await _connect(settings, account="c@example.com")
    rows = await connections.list_connections(settings, "acme")
    assert [r["account"] for r in rows] == [
        "c@example.com",
        "B@example.com",
        "a@example.com",
        "b@example.com",
    ]


@parametrized
async def test_a_persons_listing_is_by_provider_then_account(store_settings: Any) -> None:
    settings = _keyed(store_settings)
    await _connect(settings, account="b@example.com")
    await connections.save_secret(settings, "acme", person=ME, provider="alpha", account="z", secret="pw")
    await _connect(settings, account="a@example.com")
    rows = await connections.list_for_person(settings, "acme", ME)
    assert [(r["provider"], r["account"]) for r in rows] == [
        ("alpha", "z"),
        ("conformance", "a@example.com"),
        ("conformance", "b@example.com"),
    ]
    with pytest.raises(connections.AmbiguousConnection) as which:
        await connections.access_token(settings, "acme", ME, "conformance")
    assert which.value.accounts == ["a@example.com", "b@example.com"]


@parametrized
async def test_a_secret_round_trips_and_reads_only_as_one(store_settings: Any) -> None:
    settings = _keyed(store_settings)
    await connections.save_secret(
        settings, "acme", person=ME, provider="icloud", account="me@icloud.com", secret="abcd-efgh"
    )
    assert await connections.secret(settings, "acme", ME, "icloud") == "abcd-efgh"
    assert [r["kind"] for r in await connections.list_for_person(settings, "acme", ME)] == ["secret"]
