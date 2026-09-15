"""A thread id the harness mints from a caller's value has to pass the same checks.

`effective_thread_id` guards every thread id a client asks for by name: no `:` or `#` in
the suffix, and `MAX_THREAD_ID` on the result. Three namespaces skipped all of it by
building their id with an f-string — `{tenant}:a2a:{task_id}`,
`{tenant}:eval:{run}:{item}`, `{tenant}:fiber:{id}` — and in two of the three the tail is
supplied by whoever calls the API.

The cap is not cosmetic. `thread_id` is the tail of the `session_events` primary key and
`task_id` is half of the `a2a_tasks` one, both plain btree indexes, so an incompressible id
past ~2700 bytes fails the insert rather than merely bloating it:

    ERROR: index row size 3864 exceeds btree version 4 maximum 2704 for index "probe_pkey"

`memory://` keys a dict and shows none of that, which is why these tests pin the *rule*
rather than waiting for a store to complain about it.
"""

from __future__ import annotations

import pytest
from felix.thread_ids import (
    MAX_THREAD_ID,
    a2a_thread_id,
    eval_thread_id,
    thread_belongs_to_tenant,
)


def test_a_usable_id_is_composed_the_way_it_always_was() -> None:
    """The names are unchanged, so no deployment's existing threads move."""
    assert a2a_thread_id("acme", "task-1") == "acme:a2a:task-1"
    assert eval_thread_id("acme", "run-7", "item-3") == "acme:eval:run-7:item-3"


@pytest.mark.parametrize(
    ("label", "task_id"),
    [
        ("`#` mints a thread `/internal` refuses", "has#hash"),
        ("an empty id would collapse every such task onto one thread", ""),
        ("an oversized id breaks the btree index row", "x" * MAX_THREAD_ID),
    ],
)
def test_an_unusable_a2a_task_id_yields_no_thread(label: str, task_id: str) -> None:
    assert a2a_thread_id("acme", task_id) is None, label


@pytest.mark.parametrize(
    ("label", "run_id", "item_id"),
    [
        ("`#` in the item", "run-7", "item#3"),
        ("an oversized item id", "run-7", "x" * MAX_THREAD_ID),
        ("an empty item id", "run-7", ""),
        # `run_id` is a `uuid4().hex` today, so these three are the composer holding a line
        # nothing currently crosses rather than a reachable failure.
        ("`#` in the run id", "run#7", "item-3"),
        ("a separator in the run id, which is not the permissive segment", "run:7", "item-3"),
        ("an empty run id", "", "item-3"),
    ],
)
def test_an_unusable_eval_id_yields_no_thread(label: str, run_id: str, item_id: str) -> None:
    assert eval_thread_id("acme", run_id, item_id) is None, label


def test_everything_they_return_is_addressable() -> None:
    """The property, not an example: what these compose, `/internal` accepts.

    This is the half that `#` broke. `thread_belongs_to_tenant` rejects `#` outright, so
    `{tenant}:a2a:{task_id}` with a `#` in the id was a thread the queue write-back route
    refused and no operator could address — minted by the server, on the server's own
    grammar, for a task that had already been written to the store.
    """
    composed = [
        ("acme", a2a_thread_id("acme", "task-1")),
        ("acme", a2a_thread_id("acme", "urn:uuid:9f8e")),
        ("acme", a2a_thread_id("acme", "x" * 400)),
        ("acme", eval_thread_id("acme", "run-7", "item-3")),
        ("acme", eval_thread_id("acme", "run-7", "urn:uuid:9f8e")),
        ("t", a2a_thread_id("t", "-")),
    ]
    assert all(thread is not None for _, thread in composed), composed
    for tenant, thread in composed:
        assert thread is not None  # narrowed above; keeps the type checker honest
        assert thread_belongs_to_tenant(tenant, thread), f"{thread!r} is not addressable"


def test_the_separator_is_legal_in_the_caller_supplied_segment() -> None:
    """Deliberate, and the one place this is looser than `effective_thread_id`.

    A2A clients in the wild send `urn:uuid:…` task ids, and rejecting them would be an
    interop break bought for nothing: the caller's segment is the whole remainder of the
    id, so the split stays unambiguous however many separators it contains. Every earlier
    segment is delimiter-free, which is what fixes the boundaries.
    """
    assert a2a_thread_id("acme", "urn:uuid:9f8e") == "acme:a2a:urn:uuid:9f8e"
    assert eval_thread_id("acme", "run-7", "urn:uuid:9f8e") == "acme:eval:run-7:urn:uuid:9f8e"
    # …and not in the segment before it, which is what keeps that boundary fixed.
    assert eval_thread_id("acme", "run:7", "item") is None


def test_no_namespace_can_name_another_namespaces_thread() -> None:
    """Injectivity, which is why only the caller's segment may carry `:`.

    A bare `":".join` over parts that may all contain the separator lets one namespace
    compose another's id — the shape that made a waiter key forgeable in felix#250. Here
    the cost would be two runs sharing a session log rather than a forged answer, but it is
    the same mistake and cheaper not to make twice.

    Each namespace has its own composer with its own arity, so *which* segment is
    permissive is fixed by a signature rather than by how many arguments a call site
    happens to pass. That was the other half of the same hazard: with one variadic helper,
    adding a trailing segment to the A2A id would quietly move `task_id` out of the
    permissive slot and start refusing the `urn:uuid:…` ids the test above pins.
    """
    minted = [
        a2a_thread_id("acme", "x:y"),
        eval_thread_id("acme", "x", "y"),
        eval_thread_id("acme", "run-7", "item-3"),
        eval_thread_id("acme", "run-7", "item-3:extra"),
    ]
    assert len(set(minted)) == len(minted), f"two namespaces composed one id: {minted}"


def test_the_tenant_prefix_is_still_the_boundary() -> None:
    """A tenant id carrying the delimiter is refused here as everywhere else."""
    assert a2a_thread_id("ac:me", "task-1") is None
    assert a2a_thread_id("", "task-1") is None
    assert eval_thread_id("ac:me", "run-7", "item-3") is None
