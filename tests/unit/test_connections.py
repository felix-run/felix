"""`felix.auth.connections`: each person's stored connections, and the routes over them.

The store runs on its in-memory twin here; `tests/conformance/test_connections_store.py` runs the
properties a dict and a `SELECT` can disagree on against both backends. A fake provider stands in
for Google: it counts refreshes and revocations, and refuses or fails on demand. The clock is the
module's `now_ms`, patched where a test is about time.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from typing import Any

import pytest
from felix.audit import store as audit_store
from felix.auth import connections
from felix.auth.connections import OAuthGrant

from tests.support.factories import app_client, make_settings

KEY = base64.b64encode(os.urandom(32)).decode()
ME = "felix-self|google:1"
ROW = ("acme", ME, "fakeprov", "me@example.com")


class FakeProvider:
    name = "fakeprov"

    def __init__(self) -> None:
        self.refreshed: list[str] = []
        self.revoked: list[str] = []
        self.refuse = False
        self.fail: Exception | None = None
        self.rotate = True
        self.scopes = "calendar mail"
        self.refresh_expires_in = 0

    async def refresh(self, settings: Any, refresh_token: str) -> OAuthGrant:
        self.refreshed.append(refresh_token)
        await asyncio.sleep(0)
        if self.refuse:
            raise connections.ConnectionRefused("revoked by the person")
        if self.fail is not None:
            raise self.fail
        n = len(self.refreshed)
        return OAuthGrant(
            access_token=f"access-{n}",
            expires_in=3600,
            refresh_token=f"refresh-{n + 1}" if self.rotate else "",
            refresh_expires_in=self.refresh_expires_in,
            scopes=self.scopes,
        )

    async def revoke(self, settings: Any, token: str) -> None:
        if self.fail is not None:
            raise self.fail
        self.revoked.append(token)


@pytest.fixture
def provider() -> Any:
    fake = FakeProvider()
    connections.register_connection_provider(fake)
    yield fake
    connections.unregister_connection_provider(fake.name)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """`now_ms` reads the last value in the list; append to move time."""
    now = [1_000_000]
    monkeypatch.setattr(connections, "now_ms", lambda: now[-1])
    return now


def _settings(**kw: Any) -> Any:
    return make_settings(connection_token_key=KEY, **kw)


async def _connect(
    settings: Any,
    *,
    person: str = ME,
    account: str = "me@example.com",
    expires_in: int = 3600,
    tenant: str = "acme",
    refresh_expires_in: int = 0,
    access_token: str = "access-0",
) -> dict[str, Any]:
    grant = OAuthGrant(
        access_token=access_token,
        expires_in=expires_in,
        refresh_token="refresh-1",
        refresh_expires_in=refresh_expires_in,
        scopes="calendar",
    )
    return await connections.save_oauth(
        settings, tenant, person=person, provider="fakeprov", account=account, grant=grant, label=account
    )


async def _token(settings: Any, account: str | None = None) -> str:
    return await connections.access_token(settings, "acme", ME, "fakeprov", account)


async def _status(settings: Any, key: tuple[str, str, str, str] = ROW) -> str:
    return (await connections.get_connection(settings, *key) or {}).get("status", "gone")


async def _audit(settings: Any) -> list[tuple[str, str, dict[str, Any]]]:
    await audit_store.flush_pending(settings)
    items, _ = await audit_store.query(settings, "acme", event_type="connection")
    items = sorted(items, key=lambda e: e["ts"])

    def payload(e: dict[str, Any]) -> dict[str, Any]:
        raw = e["payload_json"]
        return json.loads(raw) if isinstance(raw, str) else raw

    return [(e["status"], e["principal_subj"], payload(e)) for e in items]


# --- storing ------------------------------------------------------------------------------


async def test_what_is_stored_and_shown_holds_no_credential(clock: list[int]) -> None:
    settings = _settings()
    shown = await _connect(settings, refresh_expires_in=7200)
    assert shown == {
        "person": ME,
        "provider": "fakeprov",
        "account": "me@example.com",
        "label": "me@example.com",
        "kind": "oauth",
        "status": "active",
        "scopes": ["calendar"],
        "created_at": 1_000_000,
        "updated_at": 1_000_000,
        "expires_at": 1_000_000 + 7_200_000,
    }
    stored = json.dumps(connections._memory[ROW])
    assert "refresh-1" not in stored and "access-0" not in stored


@pytest.mark.parametrize(
    ("target", "column"),
    [
        (("acme", ME, "fakeprov", "b@example.com"), "access_sealed"),  # another account
        (("globex", ME, "fakeprov", "me@example.com"), "access_sealed"),  # another tenant
        (("acme", "felix-self|google:2", "fakeprov", "me@example.com"), "access_sealed"),  # another person
        (ROW, "secret_sealed"),  # another column of the same row
    ],
)
async def test_a_sealed_value_opens_only_where_it_was_sealed(target: Any, column: str) -> None:
    settings = _settings()
    await _connect(settings)
    await _connect(settings, account="b@example.com")
    await _connect(settings, tenant="globex")
    await _connect(settings, person="felix-self|google:2")
    connections._memory[target]["access_sealed"] = connections._memory[ROW][column]
    with pytest.raises(connections.SealError):
        await connections.access_token(settings, *target)


@pytest.mark.parametrize(
    ("person", "provider", "account", "message"),
    [
        ("", "fakeprov", "a", "names none"),
        ("google:1", "fakeprov", "a", "names none"),  # a bare subject: no issuer
        ("|google:1", "fakeprov", "a", "names none"),
        ("api_key|api_key", "fakeprov", "a", "names none"),  # a sub-less API key, shared by all
        ("felix-self|cron", "fakeprov", "a", "names none"),
        ("felix-self|trigger:abc", "fakeprov", "a", "names none"),
        ("felix-self|github-actions:org/repo:wf", "fakeprov", "a", "names none"),
        ("felix-self|" + "x" * 600, "fakeprov", "a", "names none"),
        (ME, "Fake Prov", "a", "provider name"),
        (ME, "fakeprov", "", "an account is"),
        (ME, "fakeprov", "a" * 321, "an account is"),
        (ME, "fakeprov", "a\nb", "an account is"),
    ],
)
async def test_a_connection_needs_one_person_a_provider_and_an_account(
    person: str, provider: str, account: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        await connections.save_secret(
            _settings(), "acme", person=person, provider=provider, account=account, secret="pw"
        )


def test_a_provider_name_is_checked_when_registered() -> None:
    bad = FakeProvider()
    bad.name = "Not A Name"
    with pytest.raises(ValueError, match="provider name"):
        connections.register_connection_provider(bad)


async def test_a_grant_without_a_refresh_token_is_not_kept() -> None:
    with pytest.raises(ValueError, match="no refresh token"):
        await connections.save_oauth(
            _settings(),
            "acme",
            person=ME,
            provider="fakeprov",
            account="a",
            grant=OAuthGrant(access_token="x"),
        )
    assert connections._memory == {}


async def test_connecting_again_replaces_the_row_and_keeps_when_it_was_first_made(clock: list[int]) -> None:
    settings = _settings()
    await _connect(settings)
    clock.append(2_000_000)
    again = await _connect(settings)
    assert (again["created_at"], again["updated_at"]) == (1_000_000, 2_000_000)
    assert len(connections._memory) == 1


# --- the key ------------------------------------------------------------------------------


def test_the_key_falls_back_to_the_github_token_key() -> None:
    other = base64.b64encode(os.urandom(32)).decode()
    assert connections.connection_key(make_settings(github_token_key=other)) == base64.b64decode(other)
    assert connections.connection_key(make_settings(connection_token_key=KEY, github_token_key=other)) == (
        base64.b64decode(KEY)
    )


@pytest.mark.parametrize(
    ("kw", "message"),
    [
        ({}, "FELIX_CONNECTION_TOKEN_KEY is not set"),
        ({"connection_token_key": "c2hvcnQ="}, "FELIX_CONNECTION_TOKEN_KEY decodes to 5 bytes"),
        ({"github_token_key": "!!!"}, "FELIX_GITHUB_TOKEN_KEY is not base64"),
    ],
)
def test_an_unusable_key_is_named(kw: dict[str, str], message: str) -> None:
    settings = make_settings(**kw)
    with pytest.raises(ValueError, match=message):
        connections.connection_key(settings)
    assert connections.store_enabled(settings) is False


def test_the_key_is_a_hydrated_and_masked_secret() -> None:
    from felix.secrets import _HYDRATE_MAP, collected_secret_values

    assert "connection_token_key" in _HYDRATE_MAP
    assert KEY in collected_secret_values(_settings())


# --- using a connection -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expires_in", "access", "refreshed"),
    [
        (600, "access-0", []),  # ten minutes left: used as is
        (60, "access-1", ["refresh-1"]),  # inside the five-minute margin: refreshed
        (0, "access-0", []),  # the provider said nothing: used until it is refused
    ],
)
async def test_the_refresh_margin(
    provider: FakeProvider, expires_in: int, access: str, refreshed: list[str]
) -> None:
    settings = _settings()
    await _connect(settings, expires_in=expires_in)
    assert await _token(settings) == access
    assert provider.refreshed == refreshed


async def test_a_grant_that_came_without_an_access_token_is_refreshed_on_first_use(
    provider: FakeProvider,
) -> None:
    settings = _settings()
    await _connect(settings, access_token="")
    assert await _token(settings) == "access-1"


async def test_a_refresh_stores_the_rotated_token_its_expiry_and_new_scopes(
    provider: FakeProvider, clock: list[int]
) -> None:
    settings = _settings()
    await _connect(settings)
    provider.refresh_expires_in = 60
    await connections._force_expiry_for_tests(settings, ROW)
    clock.append(5_000_000)
    assert await _token(settings) == "access-1"
    await connections._force_expiry_for_tests(settings, ROW)
    assert await _token(settings) == "access-2"
    assert provider.refreshed == ["refresh-1", "refresh-2"]
    shown = (await connections.list_for_person(settings, "acme", ME))[0]
    assert (shown["scopes"], shown["expires_at"], shown["updated_at"]) == (
        ["calendar", "mail"],
        5_060_000,
        5_000_000,
    )


async def test_a_refresh_that_grants_no_scopes_keeps_the_stored_ones(provider: FakeProvider) -> None:
    provider.scopes = ""
    settings = _settings()
    await _connect(settings)
    await connections._force_expiry_for_tests(settings, ROW)
    await _token(settings)
    assert (await connections.list_for_person(settings, "acme", ME))[0]["scopes"] == ["calendar"]


async def test_a_provider_that_keeps_its_refresh_token_keeps_it_stored(provider: FakeProvider) -> None:
    provider.rotate = False
    settings = _settings()
    await _connect(settings)
    for _ in range(2):
        await connections._force_expiry_for_tests(settings, ROW)
        await _token(settings)
    assert provider.refreshed == ["refresh-1", "refresh-1"]


async def test_two_callers_at_once_refresh_once(provider: FakeProvider) -> None:
    settings = _settings()
    await _connect(settings)
    await connections._force_expiry_for_tests(settings, ROW)
    tokens = await asyncio.gather(*(_token(settings) for _ in range(3)))
    assert tokens == ["access-1"] * 3
    assert provider.refreshed == ["refresh-1"]


async def test_a_refused_refresh_revokes_the_connection_for_good(provider: FakeProvider) -> None:
    settings = _settings()
    await _connect(settings)
    await connections._force_expiry_for_tests(settings, ROW)
    provider.refuse = True
    with pytest.raises(connections.ConnectionRevoked):
        await _token(settings, "me@example.com")
    assert await _status(settings) == "revoked"
    provider.refuse = False
    with pytest.raises(connections.ConnectionRevoked):
        await _token(settings, "me@example.com")
    # Unnamed, an only account that is revoked says "reconnect", not "never connected".
    with pytest.raises(connections.ConnectionRevoked):
        await _token(settings)
    assert provider.refreshed == ["refresh-1"]


async def test_an_outage_at_the_provider_revokes_nothing_and_carries_none_of_its_words(
    provider: FakeProvider,
) -> None:
    settings = _settings()
    await _connect(settings)
    await connections._force_expiry_for_tests(settings, ROW)
    provider.fail = RuntimeError("POST https://oauth.example/token?refresh_token=refresh-1 failed")
    with pytest.raises(connections.ProviderFailed) as failed:
        await _token(settings)
    assert str(failed.value) == "fakeprov could not refresh this connection just now"
    assert failed.value.__cause__ is None and failed.value.__suppress_context__
    assert await _status(settings) == "active"
    provider.fail = None
    assert await _token(settings) == "access-2"


@pytest.mark.parametrize("call", ["access_token", "secret"])
async def test_a_store_failure_carries_none_of_the_databases_words(
    monkeypatch: pytest.MonkeyPatch, call: str
) -> None:
    from sqlalchemy.exc import OperationalError

    async def broken(*args: Any) -> Any:
        raise OperationalError(
            "SELECT pg_advisory_xact_lock(:key)", {"key": f'["acme","{ME}"]'}, Exception("timeout")
        )

    monkeypatch.setattr(connections, "_rows_for", broken)
    with pytest.raises(connections.StoreUnavailable) as failed:
        await getattr(connections, call)(_settings(), "acme", ME, "fakeprov")
    assert str(failed.value) == "the connection store could not be reached just now"
    assert failed.value.__suppress_context__


async def test_a_provider_that_hangs_is_cut_off(
    provider: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(connections, "REFRESH_TIMEOUT_S", 0.05)

    async def hang(settings: Any, refresh_token: str) -> OAuthGrant:
        # Answers in the end: only the deadline makes this a failure.
        await asyncio.sleep(1)
        return OAuthGrant(access_token="too-late", expires_in=3600)

    monkeypatch.setattr(provider, "refresh", hang)
    settings = _settings()
    await _connect(settings)
    await connections._force_expiry_for_tests(settings, ROW)
    with pytest.raises(connections.ProviderFailed):
        await _token(settings)


async def test_an_expired_refresh_token_revokes_without_asking(provider: FakeProvider) -> None:
    settings = _settings()
    await _connect(settings, refresh_expires_in=1)
    await connections._force_expiry_for_tests(settings, ROW)
    connections._memory[ROW]["secret_expires_at"] = 1
    with pytest.raises(connections.ConnectionRevoked):
        await _token(settings)
    assert provider.refreshed == []
    assert await _status(settings) == "revoked"


async def test_a_provider_no_longer_installed_is_unavailable_not_revoked() -> None:
    settings = _settings()
    await _connect(settings)
    await connections._force_expiry_for_tests(settings, ROW)
    with pytest.raises(connections.ProviderUnavailable):
        await _token(settings)
    assert await _status(settings) == "active"


async def test_which_account_a_call_means(provider: FakeProvider) -> None:
    settings = _settings()
    with pytest.raises(connections.NotConnected):
        await _token(settings)
    await _connect(settings, account="b@example.com")
    assert await _token(settings) == "access-0"
    await _connect(settings, account="a@example.com")
    with pytest.raises(connections.AmbiguousConnection) as which:
        await _token(settings)
    assert which.value.accounts == ["a@example.com", "b@example.com"]
    assert await _token(settings, "b@example.com") == "access-0"
    with pytest.raises(connections.NotConnected):
        await _token(settings, "c@example.com")


async def test_a_secret_connection_opens_only_as_a_secret() -> None:
    settings = _settings()
    await connections.save_secret(
        settings, "acme", person=ME, provider="icloud", account="me@icloud.com", secret="abcd-efgh"
    )
    assert await connections.secret(settings, "acme", ME, "icloud") == "abcd-efgh"
    with pytest.raises(connections.NotConnected):
        await connections.secret(settings, "acme", ME, "icloud", "other@icloud.com")
    with pytest.raises(ValueError, match="use secret"):
        await connections.access_token(settings, "acme", ME, "icloud")
    await _connect(settings)
    with pytest.raises(ValueError, match="use access_token"):
        await connections.secret(settings, "acme", ME, "fakeprov")
    connections._memory[("acme", ME, "icloud", "me@icloud.com")]["status"] = "revoked"
    with pytest.raises(connections.ConnectionRevoked):
        await connections.secret(settings, "acme", ME, "icloud", "me@icloud.com")


async def test_connections_are_per_person_and_per_tenant() -> None:
    settings = _settings()
    await _connect(settings)
    # The same subject under another issuer is another person.
    assert await connections.list_for_person(settings, "acme", "api_key|google:1") == []
    assert await connections.list_for_person(settings, "globex", ME) == []
    with pytest.raises(connections.NotConnected):
        await connections.access_token(settings, "acme", "api_key|google:1", "fakeprov")


async def test_a_persons_connections_list_by_provider_then_account() -> None:
    settings = _settings()
    await _connect(settings, account="b@example.com")
    await connections.save_secret(settings, "acme", person=ME, provider="alpha", account="z", secret="pw")
    await _connect(settings, account="a@example.com")
    shown = [(r["provider"], r["account"]) for r in await connections.list_for_person(settings, "acme", ME)]
    assert shown == [("alpha", "z"), ("fakeprov", "a@example.com"), ("fakeprov", "b@example.com")]


# --- removing, and what the audit log says ------------------------------------------------


async def test_removing_forgets_the_row_and_withdraws_the_grant(provider: FakeProvider) -> None:
    settings = _settings()
    await _connect(settings)
    assert await connections.remove_connection(settings, *ROW) is True
    assert provider.revoked == ["refresh-1"]
    assert await connections.list_for_person(settings, "acme", ME) == []
    assert await connections.remove_connection(settings, *ROW) is False


async def test_a_withdrawal_that_fails_still_forgets_the_row(provider: FakeProvider) -> None:
    settings = _settings()
    await _connect(settings)
    provider.fail = ConnectionError("provider down")
    assert await connections.remove_connection(settings, *ROW) is True
    assert await connections.list_for_person(settings, "acme", ME) == []


async def test_a_revoked_connection_is_not_withdrawn_again(provider: FakeProvider) -> None:
    settings = _settings()
    await _connect(settings)
    await connections._force_expiry_for_tests(settings, ROW)
    provider.refuse = True
    with pytest.raises(connections.ConnectionRevoked):
        await _token(settings)
    await connections.remove_connection(settings, *ROW)
    assert provider.revoked == []


async def test_a_removal_waits_for_a_refresh_in_flight(
    provider: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A refresh holding the row's lock finishes before the removal reads the row, so the grant
    # withdrawn is the one the refresh stored -- never an orphan Felix no longer knows about.
    gate = asyncio.Event()
    real = provider.refresh

    async def slow(settings: Any, refresh_token: str) -> OAuthGrant:
        await gate.wait()
        return await real(settings, refresh_token)

    monkeypatch.setattr(provider, "refresh", slow)
    settings = _settings()
    await _connect(settings)
    await connections._force_expiry_for_tests(settings, ROW)
    refreshing = asyncio.ensure_future(_token(settings))
    await asyncio.sleep(0)
    removing = asyncio.ensure_future(connections.remove_connection(settings, *ROW))
    await asyncio.sleep(0)
    gate.set()
    assert await refreshing == "access-1"
    assert await removing is True
    assert provider.revoked == ["refresh-2"]


async def test_every_change_is_audited_to_whoever_made_it_without_a_credential(
    provider: FakeProvider,
) -> None:
    settings = _route_settings()
    await _connect(settings, person=KEYED)
    await connections._force_expiry_for_tests(settings, ("acme", KEYED, "fakeprov", "me@example.com"))
    provider.refuse = True
    with pytest.raises(connections.ConnectionRevoked):
        await connections.access_token(settings, "acme", KEYED, "fakeprov")
    body = {"person": KEYED, "provider": "fakeprov", "account": "me@example.com"}
    async with app_client(settings) as client:
        assert (
            await client.post("/connections/tenant/revoke", json=body, headers=_h("sk-admin"))
        ).status_code == 204
    events = await _audit(settings)
    # Events in one millisecond share a timestamp, so their order is not the point; who is.
    assert sorted((status, actor) for status, actor, _ in events) == [
        ("refresh_refused", KEYED),
        ("removed", "ops"),
        ("stored", KEYED),
    ]
    assert all(
        payload == {"person": KEYED, "provider": "fakeprov", "account": "me@example.com", "kind": "oauth"}
        for _, _, payload in events
    )


async def test_an_expiry_is_audited() -> None:
    settings = _settings()
    await _connect(settings, refresh_expires_in=1)
    await connections._force_expiry_for_tests(settings, ROW)
    connections._memory[ROW]["secret_expires_at"] = 1
    with pytest.raises(connections.ConnectionRevoked):
        await _token(settings)
    assert sorted(s for s, _, _ in await _audit(settings)) == ["refresh_expired", "stored"]


# --- the routes ---------------------------------------------------------------------------

ROUTE_KEYS = json.dumps(
    {
        "sk-person": {"tenant_id": "acme", "sub": "google:1", "scopes": ["chat"]},
        "sk-other": {"tenant_id": "acme", "sub": "google:2", "scopes": ["chat"]},
        "sk-shared": {"tenant_id": "acme", "scopes": ["chat"]},
        "sk-admin": {"tenant_id": "acme", "sub": "ops", "scopes": ["connections:admin"]},
        "sk-globex": {"tenant_id": "globex", "sub": "ops", "scopes": ["connections:admin"]},
    }
)
# An API key's principal is issued by `api_key`, so its person key says so.
KEYED = "api_key|google:1"


def _h(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _route_settings() -> Any:
    return _settings(auth_mode="api_key", auth_api_keys=ROUTE_KEYS)


async def test_a_person_sees_and_removes_only_their_own(provider: FakeProvider) -> None:
    settings = _route_settings()
    await _connect(settings, person=KEYED)
    # A Felix sign-in with the same subject is someone else to an API key.
    await _connect(settings, person=ME, account="signed-in@example.com")
    async with app_client(settings) as client:
        mine = (await client.get("/connections", headers=_h("sk-person"))).json()
        theirs = (await client.get("/connections", headers=_h("sk-other"))).json()
        refused = await client.delete("/connections/fakeprov/me@example.com", headers=_h("sk-other"))
        removed = await client.delete("/connections/fakeprov/me@example.com", headers=_h("sk-person"))
        not_mine = await client.delete("/connections/fakeprov/signed-in@example.com", headers=_h("sk-person"))
    assert [c["account"] for c in mine["items"]] == ["me@example.com"]
    assert "refresh-1" not in json.dumps(mine) and "access-0" not in json.dumps(mine)
    assert theirs == {"items": []}
    assert refused.status_code == 404
    assert removed.status_code == 204
    assert not_mine.status_code == 404
    assert provider.revoked == ["refresh-1"]
    assert await _status(settings, ("acme", ME, "fakeprov", "signed-in@example.com")) == "active"


async def test_a_credential_many_callers_share_is_nobodys_person() -> None:
    async with app_client(_route_settings()) as client:
        listed = await client.get("/connections", headers=_h("sk-shared"))
    assert (listed.status_code, listed.json()["detail"]) == (403, "not_a_person")


async def test_with_auth_off_nobody_is_a_person_with_connections() -> None:
    settings = _settings(auth_mode="none")
    await connections.save_secret(settings, "default", person=ME, provider="icloud", account="a", secret="pw")
    async with app_client(settings) as client:
        listed = await client.get("/connections")
        removed = await client.delete("/connections/icloud/a")
    assert (listed.status_code, listed.json()["detail"]) == (403, "not_a_person")
    assert removed.status_code == 403


async def test_the_tenant_listing_and_revocation_need_the_admin_scope_and_stay_in_the_tenant(
    provider: FakeProvider,
) -> None:
    settings = _route_settings()
    await _connect(settings)
    await _connect(settings, tenant="globex", person="felix-self|google:9")
    body = {"person": ME, "provider": "fakeprov", "account": "me@example.com"}
    async with app_client(settings) as client:
        denied = await client.get("/connections/tenant", headers=_h("sk-person"))
        denied_revoke = await client.post("/connections/tenant/revoke", json=body, headers=_h("sk-person"))
        listing = (await client.get("/connections/tenant", headers=_h("sk-admin"))).json()
        globex = (await client.get("/connections/tenant", headers=_h("sk-globex"))).json()
        elsewhere = await client.post("/connections/tenant/revoke", json=body, headers=_h("sk-globex"))
        revoked = await client.post("/connections/tenant/revoke", json=body, headers=_h("sk-admin"))
    assert denied.status_code == 403
    assert denied_revoke.status_code == 403
    assert [(c["person"], c["account"]) for c in listing["items"]] == [(ME, "me@example.com")]
    assert [c["person"] for c in globex["items"]] == ["felix-self|google:9"]
    assert elsewhere.status_code == 404
    assert revoked.status_code == 204
    assert provider.revoked == ["refresh-1"]


def test_a_builtin_provider_is_registered_on_first_lookup_however_often() -> None:
    # A worker resuming a run may refresh a Google grant before anything imported Google's module.
    from felix.auth.google_connect import GoogleConnectionProvider

    for _ in range(2):
        connections.unregister_connection_provider("google")
        assert isinstance(connections._provider("google"), GoogleConnectionProvider)
