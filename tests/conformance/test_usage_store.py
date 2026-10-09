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


def _record(
    settings: Any, *, manifest: str, model: str, tokens: int, tenant: str = TENANT, thread: str = ""
) -> None:
    usage_store.record_tokens(
        settings,
        tenant_id=tenant,
        manifest_id=manifest,
        model_id=model,
        wire_model_id=WIRE,
        tokens_input=tokens,
        cost_usd=_cost(tokens),
        thread_id=thread,
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


@parametrized
@pytest.mark.asyncio
async def test_a_retried_flush_does_not_duplicate_or_block(usage_settings: Any) -> None:
    """The usage flush commits per tenant, so a retry after a partial commit re-inserts rows
    already written. That must be a no-op — not a primary-key violation retried forever, and
    not a double charge."""
    usage_store.record_tokens(
        usage_settings, tenant_id=TENANT, manifest_id="m", model_id="m", tokens_input=5, tokens_output=1
    )
    [event] = usage_store.pending_buffer().snapshot()
    assert await usage_store.flush_pending(usage_settings) == 1

    usage_store.pending_buffer().append(dict(event))
    assert await usage_store.flush_pending(usage_settings) == 1
    assert len(usage_store.pending_buffer()) == 0 and usage_store.pending_buffer().quarantined == 0
    rows, _ = await usage_store.query(usage_settings, TENANT, limit=10)
    assert [r["tokens_input"] for r in rows] == [5], "billed once"


# --- by thread ---------------------------------------------------------------------------


def _t(suffix: str) -> str:
    return f"{TENANT}:{suffix}"


@parametrized
@pytest.mark.asyncio
async def test_the_thread_survives_the_round_trip_and_filters_the_listing(usage_settings: Any) -> None:
    _record(usage_settings, manifest="support", model="fast", tokens=10, thread=_t("a"))
    _record(usage_settings, manifest="support", model="fast", tokens=20, thread=_t("b"))
    _record(usage_settings, manifest="support", model="fast", tokens=30)
    _record(usage_settings, manifest="support", model="fast", tokens=40, tenant="other", thread="other:a")
    assert await usage_store.flush_pending(usage_settings) == 4

    every, _ = await usage_store.query(usage_settings, TENANT, limit=10)
    assert sorted(r["thread_id"] for r in every) == ["", _t("a"), _t("b")]
    only_a, _ = await usage_store.query(usage_settings, TENANT, thread_id=_t("a"))
    assert [r["tokens_input"] for r in only_a] == [10]
    off_thread, _ = await usage_store.query(usage_settings, TENANT, thread_id="")
    assert [r["tokens_input"] for r in off_thread] == [30], "'' is the calls outside a thread, not no filter"
    assert (await usage_store.query(usage_settings, TENANT, thread_id="other:a"))[0] == []


@parametrized
@pytest.mark.asyncio
async def test_an_event_buffered_before_the_column_flushes_as_no_thread(usage_settings: Any) -> None:
    """A process that predates the field buffered events with no `thread_id` key; a durable
    buffer can hand them to one that has it."""
    _record(usage_settings, manifest="m", model="fast", tokens=5)
    [event] = usage_store.pending_buffer().snapshot()
    usage_store.pending_buffer().reset_for_tests()
    event.pop("thread_id")
    usage_store.pending_buffer().append(event)
    assert await usage_store.flush_pending(usage_settings) == 1
    (row,) = (await usage_store.query(usage_settings, TENANT))[0]
    assert row["thread_id"] == ""


@parametrized
@pytest.mark.asyncio
async def test_threads_group_order_and_total_over_the_window(
    usage_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = {"now": 1_000_000}
    monkeypatch.setattr(usage_store, "now_ms", lambda: clock["now"])

    def at(ts: int, tokens: int, thread: str = "", tenant: str = TENANT) -> None:
        clock["now"] = ts
        _record(usage_settings, manifest="m", model="fast", tokens=tokens, thread=thread, tenant=tenant)

    at(1_000, 100, _t("old"))
    at(5_000, 200, _t("old"))
    at(2_000, 300)  # outside any thread
    at(6_000, 400, _t("Zed"))
    # Mixed case at one `last_ts`: the tie breaks on `thread_id` by code point on both arms.
    at(6_000, 500, _t("alpha"))
    at(4_000, 600, _t("alpha"))
    at(9_000, 9_000, "other:x", tenant="other")
    at(20_000, 700, _t("later"))  # outside the window below
    await usage_store.flush_pending(usage_settings)

    out = await usage_store.threads(usage_settings, TENANT, since_ms=1_000, until_ms=20_000)
    assert (out["since_ms"], out["until_ms"]) == (1_000, 20_000)
    assert out["truncated"] is False
    assert [i["thread_id"] for i in out["items"]] == [_t("Zed"), _t("alpha"), _t("old"), ""]
    by = {i["thread_id"]: i for i in out["items"]}
    assert by[_t("alpha")] == {
        "thread_id": _t("alpha"),
        "calls": 2,
        "tokens_input": 1_100,
        "tokens_output": 0,
        "cache_creation": 0,
        "cache_read": 0,
        "cost_usd": pytest.approx(_cost(500) + _cost(600)),
        "first_ts": 4_000,
        "last_ts": 6_000,
    }
    assert (by[_t("old")]["first_ts"], by[_t("old")]["last_ts"]) == (1_000, 5_000), (
        "the window is inclusive below"
    )
    assert by[""]["calls"] == 1 and by[""]["tokens_input"] == 300
    assert out["totals"]["calls"] == 6
    assert out["totals"]["tokens_input"] == 2_100, "not the later row, not the other tenant's"

    # Half-open: a row at exactly `until_ms` is out, and so is everything before `since_ms`.
    narrow = await usage_store.threads(usage_settings, TENANT, since_ms=5_000, until_ms=6_000)
    assert [(i["thread_id"], i["calls"]) for i in narrow["items"]] == [(_t("old"), 1)]


@parametrized
@pytest.mark.asyncio
async def test_threads_truncate_the_page_but_total_every_thread(
    usage_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = {"now": 0}
    monkeypatch.setattr(usage_store, "now_ms", lambda: clock["now"])
    for i in range(5):
        clock["now"] = 1_000 + i
        _record(usage_settings, manifest="m", model="fast", tokens=10 * (i + 1), thread=_t(f"t{i}"))
    await usage_store.flush_pending(usage_settings)

    page = await usage_store.threads(usage_settings, TENANT, since_ms=0, until_ms=10_000, limit=2)
    assert [i["thread_id"] for i in page["items"]] == [_t("t4"), _t("t3")]
    assert page["truncated"] is True
    assert page["totals"]["calls"] == 5
    assert page["totals"]["tokens_input"] == 150, "the totals are the window's, not the page's"

    exact = await usage_store.threads(usage_settings, TENANT, since_ms=0, until_ms=10_000, limit=5)
    assert exact["truncated"] is False and len(exact["items"]) == 5


@parametrized
@pytest.mark.asyncio
async def test_threads_default_to_the_summary_window(usage_settings: Any) -> None:
    _record(usage_settings, manifest="m", model="fast", tokens=10, thread=_t("now"))
    await usage_store.flush_pending(usage_settings)
    out = await usage_store.threads(usage_settings, TENANT)
    assert out["until_ms"] - out["since_ms"] == usage_store.SUMMARY_DEFAULT_WINDOW_MS
    assert [i["thread_id"] for i in out["items"]] == [_t("now")]
    empty = await usage_store.threads(usage_settings, "nobody")
    assert empty["items"] == [] and empty["truncated"] is False and empty["totals"]["calls"] == 0
