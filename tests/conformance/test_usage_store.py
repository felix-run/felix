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


@pytest.fixture
def one_millisecond(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop the clock, so every row recorded under it shares a timestamp.

    `record_tokens` stamps `now_ms()` itself — there is no timestamp argument, which is
    right — so this is how the tie the paging tests need is produced. It is not contrived:
    several model calls inside one turn are metered microseconds apart, and `ts` is
    milliseconds.
    """
    monkeypatch.setattr(usage_store, "now_ms", lambda: 100)


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


@parametrized
@pytest.mark.asyncio
async def test_paging_usage_returns_every_row_once(usage_settings: Any, one_millisecond: None) -> None:
    """Nothing called `query` with a cursor — on either arm, anywhere in the repo.

    So the usage listing's pagination shipped unexercised, and the `(ts, id)` fix that stopped
    the audit listing losing rows to a tied millisecond ran zero assertions here even though
    the store is the same shape. Usage rows tie for the same reason audit rows do: several
    model calls inside one turn, metered microseconds apart.
    """
    for i in range(5):
        _record(usage_settings, manifest=f"m-{i}", model="fast", tokens=1_000)
    assert await usage_store.flush_pending(usage_settings) == 5

    stored, _ = await usage_store.query(usage_settings, TENANT, limit=100)
    assert len(stored) == 5, f"the flush stored {len(stored)} of 5 rows"
    # The positive control for the fixture: if `record_tokens` ever stops going through
    # `now_ms`, the patch goes inert, the tie disappears and this test quietly becomes the
    # distinct-timestamp case the old cursor already handled.
    assert len({row["ts"] for row in stored}) == 1, "the clock was not frozen; this is not a tie"

    seen: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(10):
        page, cursor = await usage_store.query(usage_settings, TENANT, limit=2, cursor=cursor)
        seen.extend(page)
        if cursor is None:
            break
    else:  # pragma: no cover - only on a cursor that does not terminate
        pytest.fail("the cursor never reported the end of the history")

    manifests = sorted(e["manifest_id"] for e in seen)
    assert manifests == [f"m-{i}" for i in range(5)], manifests


@parametrized
@pytest.mark.asyncio
async def test_the_manifest_filter_survives_a_tied_page_boundary(
    usage_settings: Any, one_millisecond: None
) -> None:
    """The filter and the tie together, which is what a per-manifest cost view pages through."""
    for i in range(6):
        _record(
            usage_settings,
            manifest="watched" if i % 2 else "other",
            model=f"model-{i}",
            tokens=1_000,
        )
    assert await usage_store.flush_pending(usage_settings) == 6

    stored, _ = await usage_store.query(usage_settings, TENANT, limit=100)
    assert len({row["ts"] for row in stored}) == 1, "the clock was not frozen; this is not a tie"

    seen: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(10):
        page, cursor = await usage_store.query(
            usage_settings, TENANT, manifest_id="watched", limit=1, cursor=cursor
        )
        seen.extend(page)
        if cursor is None:
            break
    else:  # pragma: no cover - only on a cursor that does not terminate
        pytest.fail("the cursor never reported the end of the filtered history")

    assert [e["manifest_id"] for e in seen] == ["watched"] * len(seen), seen
    assert len(seen) == 3, seen


@parametrized
@pytest.mark.asyncio
async def test_the_summary_rows_come_back_in_one_order(usage_settings: Any, one_millisecond: None) -> None:
    """The two arms sorted the same two text keys in opposite directions.

    The twin reversed the whole `(day, manifest_id, model_id)` tuple while the SQL ordered
    `day DESC, manifest_id ASC, model_id ASC` — so every row sharing a day came back in the
    opposite order, and neither arm was wrong about its own rows. The existing tests funnel
    the result into a dict before asserting, so order was never compared and this was green.

    Mixed case on purpose: `manifest_id` is tenant-supplied, `ORDER BY` on text uses the
    database collation, and the twin sorts by code point.

    The frozen clock is not decoration: with the real one, four rows written near UTC midnight
    can straddle a day boundary, and the outer `day DESC` sort would then split them and fail
    this for a reason that has nothing to do with collation.
    """
    for manifest in ("Zulu", "alpha", "_edge", "beta"):
        _record(usage_settings, manifest=manifest, model="fast", tokens=1_000)
    await usage_store.flush_pending(usage_settings)

    rows = (await usage_store.summary(usage_settings, TENANT))["items"]

    assert [r["manifest_id"] for r in rows] == sorted(("Zulu", "alpha", "_edge", "beta")), rows
