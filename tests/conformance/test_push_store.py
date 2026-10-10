"""One contract for the push subscription store, run against both backends.

The properties here are the ones a dict and a `SELECT` can quietly disagree on: that a browser
re-subscribing replaces its own row rather than adding one, that the same endpoint under two
tenants is two rows neither can touch, and that the per-tenant cap refuses a *new* browser
without refusing one that is already subscribed.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.push import store as push

BACKENDS = ["memory", "postgres"]
parametrized = pytest.mark.parametrize("store_settings", BACKENDS, indirect=True)

ENDPOINT = "https://web.push.apple.com/conformance/device"


async def _subscribe(
    settings: Any, tenant: str = "acme", endpoint: str = ENDPOINT, key: str = "k1"
) -> dict[str, Any]:
    return await push.upsert(settings, tenant, endpoint=endpoint, p256dh=key, auth="a1", principal_subj="ops")


@parametrized
async def test_resubscribing_replaces_the_browser_s_own_row(store_settings: Any) -> None:
    first = await _subscribe(store_settings, key="k1")
    again = await _subscribe(store_settings, key="k2")

    rows = await push.list_for_tenant(store_settings, "acme")
    assert [r["id"] for r in rows] == [first["id"]] == [again["id"]]
    assert rows[0]["p256dh"] == "k2"
    assert rows[0]["created_at"] == first["created_at"]


@parametrized
async def test_resubscribing_clears_failures_and_keeps_the_last_delivery(store_settings: Any) -> None:
    row = await _subscribe(store_settings, key="k1")
    await push.record_outcomes(store_settings, "acme", delivered=[row["id"]], failed=[], gone=[])
    await push.record_outcomes(store_settings, "acme", delivered=[], failed=[row["id"]], gone=[])
    (before,) = await push.list_for_tenant(store_settings, "acme")

    await _subscribe(store_settings, key="k2")

    (after,) = await push.list_for_tenant(store_settings, "acme")
    assert after["failures"] == 0
    assert after["last_ok_at"] == before["last_ok_at"] is not None


@parametrized
async def test_a_reused_approval_announces_once_on_either_backend(
    store_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `create_pending` announces from one place after both arms; this holds the Postgres arm
    # to the same once-per-row rule the unit test holds the memory arm to.
    from felix.approvals import store as approvals
    from felix.push import notify

    calls: list[str] = []
    monkeypatch.setattr(notify, "approval_pending", lambda settings, row: calls.append(row["id"]))
    kw = {"tool_name": "write_file", "call_signature": "sig", "manifest_id": "m", "ttl_seconds": 60}
    first = await approvals.create_pending(store_settings, "acme", **kw)
    again = await approvals.create_pending(store_settings, "acme", **kw)
    assert again["id"] == first["id"]
    assert calls == [first["id"]]


@parametrized
async def test_the_same_endpoint_under_two_tenants_is_two_rows(store_settings: Any) -> None:
    await _subscribe(store_settings, "acme")
    await _subscribe(store_settings, "globex")

    assert await push.remove(store_settings, "globex", ENDPOINT) is True
    assert len(await push.list_for_tenant(store_settings, "acme")) == 1
    assert await push.list_for_tenant(store_settings, "globex") == []
    assert await push.remove(store_settings, "globex", ENDPOINT) is False


@parametrized
async def test_outcomes_clear_count_and_drop(store_settings: Any) -> None:
    ok = await _subscribe(store_settings, endpoint=f"{ENDPOINT}/ok")
    flaky = await _subscribe(store_settings, endpoint=f"{ENDPOINT}/flaky")
    left = await _subscribe(store_settings, endpoint=f"{ENDPOINT}/left")

    await push.record_outcomes(
        store_settings, "acme", delivered=[ok["id"]], failed=[flaky["id"]], gone=[left["id"]]
    )
    rows = {r["id"]: r for r in await push.list_for_tenant(store_settings, "acme")}
    assert set(rows) == {ok["id"], flaky["id"]}
    assert isinstance(rows[ok["id"]]["last_ok_at"], int)
    # One failure is a count, not a removal.
    assert rows[flaky["id"]]["failures"] == 1

    for _ in range(push.MAX_CONSECUTIVE_FAILURES - 1):
        await push.record_outcomes(store_settings, "acme", delivered=[], failed=[flaky["id"]], gone=[])
    assert [r["id"] for r in await push.list_for_tenant(store_settings, "acme")] == [ok["id"]]


@parametrized
async def test_a_delivery_resets_the_failure_count(store_settings: Any) -> None:
    row = await _subscribe(store_settings)
    for _ in range(push.MAX_CONSECUTIVE_FAILURES - 1):
        await push.record_outcomes(store_settings, "acme", delivered=[], failed=[row["id"]], gone=[])
    await push.record_outcomes(store_settings, "acme", delivered=[row["id"]], failed=[], gone=[])
    await push.record_outcomes(store_settings, "acme", delivered=[], failed=[row["id"]], gone=[])
    (after,) = await push.list_for_tenant(store_settings, "acme")
    assert after["failures"] == 1


@parametrized
async def test_the_cap_refuses_a_new_browser_but_not_a_known_one(
    store_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(push, "MAX_SUBSCRIPTIONS_PER_TENANT", 2)
    await _subscribe(store_settings, endpoint=f"{ENDPOINT}/1")
    await _subscribe(store_settings, endpoint=f"{ENDPOINT}/2")

    with pytest.raises(push.TooManySubscriptions):
        await _subscribe(store_settings, endpoint=f"{ENDPOINT}/3")
    await _subscribe(store_settings, endpoint=f"{ENDPOINT}/2", key="rotated")
    assert len(await push.list_for_tenant(store_settings, "acme")) == 2
