"""The usage store contract, run against the in-memory twin and Postgres alike.

`cost_usd` and `wire_model_id` are written at flush time and read back by `query` and
`summary`; the summary's day bucket is a UTC date on both arms (Postgres computes it in
SQL, the twin in Python), and the two have to agree on the same rows.

Add a backend to `BACKENDS` and it inherits every assertion here.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.usage import store as usage_store
from felix.usage.pricing import usage_with_cost

BACKENDS = ["memory", "postgres"]
parametrized = pytest.mark.parametrize("usage_settings", BACKENDS, indirect=True)

TENANT = "conformance"
WIRE = "claude-sonnet-4-5"
DAY_MS = 24 * 60 * 60 * 1000


def _cost(tokens: int) -> float:
    return usage_with_cost({"input": tokens}, model_id=WIRE)["cost"]["total"]


def _record(settings: Any, *, manifest: str, model: str, tokens: int, tenant: str = TENANT) -> None:
    usage_store.record_tokens(
        settings,
        tenant_id=tenant,
        manifest_id=manifest,
        model_id=model,
        wire_model_id=WIRE,
        tokens_input=tokens,
        cost_usd=_cost(tokens),
    )


@parametrized
@pytest.mark.asyncio
async def test_cost_and_wire_id_survive_the_round_trip(usage_settings: Any) -> None:
    _record(usage_settings, manifest="support", model="fast", tokens=1_000_000)
    assert await usage_store.flush_pending(usage_settings) == 1
    items, _ = await usage_store.query(usage_settings, TENANT)
    (row,) = items
    assert row["wire_model_id"] == WIRE
    assert row["model_id"] == "fast"
    assert row["cost_usd"] == pytest.approx(
        usage_with_cost({"input": 1_000_000}, model_id=WIRE)["cost"]["total"]
    )


@parametrized
@pytest.mark.asyncio
async def test_summary_sums_within_the_tenant_and_groups_by_day(usage_settings: Any) -> None:
    _record(usage_settings, manifest="support", model="fast", tokens=1_000_000)
    _record(usage_settings, manifest="support", model="fast", tokens=500_000)
    _record(usage_settings, manifest="deep", model="big", tokens=250_000)
    _record(usage_settings, manifest="support", model="fast", tokens=9_000_000, tenant="someone-else")
    await usage_store.flush_pending(usage_settings)

    out = await usage_store.summary(usage_settings, TENANT)
    by_key = {(i["manifest_id"], i["model_id"]): i for i in out["items"]}
    assert set(by_key) == {("support", "fast"), ("deep", "big")}
    assert by_key[("support", "fast")]["calls"] == 2
    assert by_key[("support", "fast")]["tokens_input"] == 1_500_000
    per_million = usage_with_cost({"input": 1_000_000}, model_id=WIRE)["cost"]["total"]
    assert by_key[("support", "fast")]["cost_usd"] == pytest.approx(1.5 * per_million)
    assert out["totals"]["calls"] == 3
    assert out["totals"]["cost_usd"] == pytest.approx(1.75 * per_million)


@parametrized
@pytest.mark.asyncio
async def test_both_arms_bucket_by_the_same_utc_day(
    usage_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The twin buckets in Python and Postgres in SQL; they have to agree on the date a row
    falls on, in UTC, whatever the session time zone. One row two days back, one now."""
    from datetime import UTC, datetime

    t0 = usage_store.now_ms()
    monkeypatch.setattr(usage_store, "now_ms", lambda: t0 - 2 * DAY_MS)
    _record(usage_settings, manifest="support", model="fast", tokens=1_000_000)
    monkeypatch.setattr(usage_store, "now_ms", lambda: t0)
    _record(usage_settings, manifest="support", model="fast", tokens=500_000)
    await usage_store.flush_pending(usage_settings)

    out = await usage_store.summary(usage_settings, TENANT, since_ms=t0 - 3 * DAY_MS, until_ms=t0 + 1)

    def utc_day(ts: int) -> str:
        return datetime.fromtimestamp(ts / 1000, UTC).strftime("%Y-%m-%d")

    assert [(i["day"], i["tokens_input"]) for i in out["items"]] == [
        (utc_day(t0), 500_000),
        (utc_day(t0 - 2 * DAY_MS), 1_000_000),
    ]


@parametrized
@pytest.mark.asyncio
async def test_summary_window_is_half_open_in_epoch_ms(usage_settings: Any) -> None:
    _record(usage_settings, manifest="support", model="fast", tokens=10)
    await usage_store.flush_pending(usage_settings)
    (row,) = (await usage_store.query(usage_settings, TENANT))[0]
    ts = row["ts"]
    assert (await usage_store.summary(usage_settings, TENANT, since_ms=ts, until_ms=ts + 1))["totals"][
        "calls"
    ] == 1
    assert (await usage_store.summary(usage_settings, TENANT, since_ms=ts + 1, until_ms=ts + DAY_MS))[
        "totals"
    ]["calls"] == 0
    assert (await usage_store.summary(usage_settings, TENANT, since_ms=ts - DAY_MS, until_ms=ts))["totals"][
        "calls"
    ] == 0
