"""One contract for the audit store, run against both backends.

Audit is the compliance record: what an agent was asked, which tools it ran, what governance
refused. Its Postgres half ran only under `test_migrations.py`, which creates the schema and
never queries it — so everything asserted about audit was asserted about a list of dicts, and
`tests/unit/test_invariants.py` only requires that such a twin exists.

What this covers is the part a dict scan and a `SELECT` are easy to get subtly different on,
and the part the API's `/audit` pagination depends on: the order rows come back in, whether a
filter composes with a cursor, and whether paging through a tenant's history yields each event
exactly once. An audit trail that silently drops or repeats a row is worse than one that is
missing, because it is still believed.

`ts` is a millisecond timestamp the caller may supply, so these tests supply it. Real traffic
produces ties — several events inside one millisecond is ordinary for a single turn — and a tie
is where "newest first" stops being a total order unless something breaks it.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.audit import store as audit

BACKENDS = ["memory", "postgres"]
parametrized = pytest.mark.parametrize("store_settings", BACKENDS, indirect=True)

TENANT = "conformance"
OTHER = "other-tenant"


async def _record(settings: Any, **fields: Any) -> None:
    """Record one event and flush it, so both arms are read back from their real store."""
    audit.record_event(
        settings,
        fields.pop("tenant_id", TENANT),
        fields.pop("event_type", "tool_call"),
        **fields,
    )
    await audit.flush_pending(settings)


async def _record_many(settings: Any, events: list[dict[str, Any]]) -> None:
    """Buffer a batch and flush once — the shape the worker's `flush_audit` produces."""
    for event in events:
        audit.record_event(
            settings,
            event.pop("tenant_id", TENANT),
            event.pop("event_type", "tool_call"),
            **event,
        )
    await audit.flush_pending(settings)


# --- the record itself ----------------------------------------------------------------------


@parametrized
@pytest.mark.asyncio
async def test_an_event_round_trips_with_every_field(store_settings: Any) -> None:
    """Field by field, because a column the twin keeps and the write drops reads as empty.

    `payload_json` matters most: it is the only free-form field, it is what an investigation
    actually reads, and it is the one a JSONB round trip can change the shape of.
    """
    await _record(
        store_settings,
        ts=1_000,
        event_type="tool_call",
        manifest_id="governed",
        principal_subj="alice",
        status="allowed",
        payload_json={"tool": "calculator", "args": {"expression": "2+2"}, "nested": [1, 2]},
    )

    events, cursor = await audit.query(store_settings, TENANT)

    assert cursor is None
    assert len(events) == 1
    row = events[0]
    assert row["tenant_id"] == TENANT
    assert row["ts"] == 1_000
    assert row["event_type"] == "tool_call"
    assert row["manifest_id"] == "governed"
    assert row["principal_subj"] == "alice"
    assert row["status"] == "allowed"
    assert row["payload_json"] == {"tool": "calculator", "args": {"expression": "2+2"}, "nested": [1, 2]}
    assert row["id"]


@parametrized
@pytest.mark.asyncio
async def test_flush_reports_what_it_wrote_and_leaves_nothing_behind(store_settings: Any) -> None:
    """The worker's cron reports this number, and a buffer that keeps its batch double-writes."""
    await _record_many(
        store_settings,
        [{"ts": 10, "status": "allowed"}, {"ts": 20, "status": "denied"}],
    )

    assert await audit.flush_pending(store_settings) == 0
    events, _ = await audit.query(store_settings, TENANT)
    assert len(events) == 2


@parametrized
@pytest.mark.asyncio
async def test_one_tenants_events_are_invisible_to_another(store_settings: Any) -> None:
    await _record_many(
        store_settings,
        [
            {"ts": 10, "tenant_id": TENANT, "principal_subj": "alice"},
            {"ts": 20, "tenant_id": OTHER, "principal_subj": "mallory"},
        ],
    )

    mine, _ = await audit.query(store_settings, TENANT)
    theirs, _ = await audit.query(store_settings, OTHER)

    assert [e["principal_subj"] for e in mine] == ["alice"]
    assert [e["principal_subj"] for e in theirs] == ["mallory"]


# --- ordering and paging --------------------------------------------------------------------


@parametrized
@pytest.mark.asyncio
async def test_events_come_back_newest_first(store_settings: Any) -> None:
    await _record_many(
        store_settings,
        [{"ts": 10, "status": "first"}, {"ts": 30, "status": "third"}, {"ts": 20, "status": "second"}],
    )

    events, _ = await audit.query(store_settings, TENANT)

    assert [e["ts"] for e in events] == [30, 20, 10]


@parametrized
@pytest.mark.asyncio
async def test_paging_yields_every_event_exactly_once(store_settings: Any) -> None:
    """The property the `/audit` route's cursor actually promises.

    Asserting page contents one page at a time misses the failure that matters: a cursor
    boundary that repeats a row or steps over one. Walking the whole history and comparing the
    multiset to what was written catches both, and says which happened.
    """
    written = [{"ts": 100 + i, "principal_subj": f"user-{i}"} for i in range(7)]
    await _record_many(store_settings, [dict(e) for e in written])

    seen: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(10):  # bounded, so a cursor that never advances fails instead of hanging
        page, cursor = await audit.query(store_settings, TENANT, limit=3, cursor=cursor)
        seen.extend(page)
        if cursor is None:
            break
    else:  # pragma: no cover - only on a cursor that does not terminate
        pytest.fail("the cursor never reported the end of the history")

    subjects = [e["principal_subj"] for e in seen]
    assert sorted(subjects) == sorted(e["principal_subj"] for e in written), subjects
    assert len(subjects) == len(set(subjects)), f"an event was returned twice: {subjects}"


@parametrized
@pytest.mark.asyncio
async def test_the_last_page_reports_no_cursor(store_settings: Any) -> None:
    """A cursor on the final page makes a caller ask for a page that is always empty."""
    await _record_many(store_settings, [{"ts": 10 + i} for i in range(3)])

    page, cursor = await audit.query(store_settings, TENANT, limit=3)

    assert len(page) == 3
    assert cursor is None


# --- filters --------------------------------------------------------------------------------


@parametrized
@pytest.mark.asyncio
async def test_filters_select_and_compose(store_settings: Any) -> None:
    """`event_type` and `status` are the two the route exposes, and they must intersect.

    A filter applied to one arm and not the other is the classic divergence here: the twin
    scans a list and the store builds a `WHERE`, and dropping one clause reads as "no matching
    events" rather than as an error.
    """
    await _record_many(
        store_settings,
        [
            {"ts": 10, "event_type": "tool_call", "status": "allowed"},
            {"ts": 20, "event_type": "tool_call", "status": "denied"},
            {"ts": 30, "event_type": "final_response", "status": "allowed"},
        ],
    )

    by_type, _ = await audit.query(store_settings, TENANT, event_type="tool_call")
    by_status, _ = await audit.query(store_settings, TENANT, status="denied")
    both, _ = await audit.query(store_settings, TENANT, event_type="tool_call", status="allowed")
    neither, _ = await audit.query(store_settings, TENANT, event_type="nothing-uses-this")

    assert [e["ts"] for e in by_type] == [20, 10]
    assert [e["ts"] for e in by_status] == [20]
    assert [e["ts"] for e in both] == [10]
    assert neither == []


@parametrized
@pytest.mark.asyncio
async def test_a_filter_still_applies_on_the_second_page(store_settings: Any) -> None:
    """The cursor and the `WHERE` have to survive together.

    A cursor built from the unfiltered history pages past filtered rows; a filter dropped on
    the follow-up query returns events the caller excluded. Both look like a working endpoint.
    """
    await _record_many(
        store_settings,
        [{"ts": 10 + i, "status": "denied" if i % 2 else "allowed"} for i in range(6)],
    )

    first, cursor = await audit.query(store_settings, TENANT, status="denied", limit=2)
    assert cursor is not None, "a filtered history longer than the page reported no cursor"
    second, _ = await audit.query(store_settings, TENANT, status="denied", limit=2, cursor=cursor)

    statuses = [e["status"] for e in first + second]
    assert statuses == ["denied"] * len(statuses), statuses
    timestamps = [e["ts"] for e in first + second]
    assert timestamps == sorted(timestamps, reverse=True), timestamps
    assert len(timestamps) == len(set(timestamps)), f"an event was returned twice: {timestamps}"


@parametrized
@pytest.mark.asyncio
async def test_the_tenant_filter_is_not_defeated_by_another_tenants_cursor(store_settings: Any) -> None:
    """A cursor is a timestamp, so it is forgeable and shared across tenants by construction.

    Handing one tenant's cursor to another must page that other tenant's history, never leak
    the first one's rows — the cursor narrows, the tenant scopes.
    """
    await _record_many(
        store_settings,
        [
            {"ts": 100, "tenant_id": OTHER, "principal_subj": "mallory"},
            {"ts": 50, "tenant_id": TENANT, "principal_subj": "alice"},
        ],
    )

    page, _ = await audit.query(store_settings, TENANT, cursor="100")

    assert [e["principal_subj"] for e in page] == ["alice"]


@parametrized
@pytest.mark.asyncio
async def test_paging_survives_events_sharing_a_timestamp(store_settings: Any) -> None:
    """`ts` is milliseconds and the cursor is a `ts`, so ties are the paging boundary case.

    A single turn writes several events inside one millisecond routinely — a user turn, a
    tool call and a final response are microseconds apart. If the cursor is `ts < last_seen`,
    every other event sharing that timestamp is stepped over and never returned by any page:
    silent loss from a compliance record, visible only to someone who counts.
    """
    written = [{"ts": 100, "principal_subj": f"user-{i}"} for i in range(5)]
    await _record_many(store_settings, [dict(e) for e in written])

    # The precondition, stated separately: if the flush wrote fewer rows than were recorded,
    # the walk below reports a paging bug for something that went wrong two steps earlier.
    stored, _ = await audit.query(store_settings, TENANT, limit=100)
    assert len(stored) == len(written), f"the flush stored {len(stored)} of {len(written)} events"

    seen: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(10):
        page, cursor = await audit.query(store_settings, TENANT, limit=2, cursor=cursor)
        seen.extend(page)
        if cursor is None:
            break
    else:  # pragma: no cover - only on a cursor that does not terminate
        pytest.fail("the cursor never reported the end of the history")

    subjects = sorted(e["principal_subj"] for e in seen)
    assert subjects == sorted(e["principal_subj"] for e in written), subjects


@parametrized
@pytest.mark.asyncio
async def test_a_filter_and_a_tie_together(store_settings: Any) -> None:
    """The two paging hazards at once, which neither other test reaches.

    `test_a_filter_still_applies_on_the_second_page` gives every event a distinct timestamp, so
    the pair comparison degenerates to the timestamp comparison the old cursor already got
    right. `test_paging_survives_events_sharing_a_timestamp` has no filter. Real traffic has
    both: one turn's events share a millisecond, and an operator looking for refusals filters
    by status while paging through them.
    """
    written = [
        {"ts": 100, "principal_subj": f"user-{i}", "status": "denied" if i % 2 else "allowed"}
        for i in range(6)
    ]
    denied = sorted(e["principal_subj"] for e in written if e["status"] == "denied")
    await _record_many(store_settings, [dict(e) for e in written])

    seen: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(10):
        page, cursor = await audit.query(store_settings, TENANT, status="denied", limit=1, cursor=cursor)
        seen.extend(page)
        if cursor is None:
            break
    else:  # pragma: no cover - only on a cursor that does not terminate
        pytest.fail("the cursor never reported the end of the filtered history")

    assert [e["status"] for e in seen] == ["denied"] * len(seen), seen
    assert sorted(e["principal_subj"] for e in seen) == denied
