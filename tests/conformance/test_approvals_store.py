"""One contract for the approvals store, run against both backends.

The approvals store is the human gate on a governed tool, and until now its Postgres half ran
only under `test_migrations.py` — which creates the schema and never queries it. Everything
asserted about approvals was asserted about the in-memory twin, and `tests/unit/test_invariants.py`
only requires that a twin *exists*.

The semantics here are the ones that are easy to get subtly different between a dict scan and a
`SELECT`: which grant is returned when several match, whether an expired one is filtered, whether
a grant bound to one principal authorises another, and whether a one-shot grant can be spent
twice. Each of those is a security property rather than a storage detail — `bind_principal` and
`one_shot` are manifest fields operators set expecting them to hold.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.approvals import store as approvals

BACKENDS = ["memory", "postgres"]
parametrized = pytest.mark.parametrize("store_settings", BACKENDS, indirect=True)

TENANT = "conformance"
MANIFEST = "governed"
TOOL = "calculator"
SIG = "calculator:2+2"


async def _pending(settings: Any, **kw: Any) -> dict[str, Any]:
    return await approvals.create_pending(
        settings,
        kw.pop("tenant_id", TENANT),
        tool_name=kw.pop("tool_name", TOOL),
        call_signature=kw.pop("call_signature", SIG),
        manifest_id=kw.pop("manifest_id", MANIFEST),
        **kw,
    )


async def _approve(settings: Any, approval_id: str, *, by: str = "operator") -> dict[str, Any] | None:
    return await approvals.decide(settings, TENANT, approval_id, decision="approved", decided_by=by)


# --- creating and deciding ------------------------------------------------------------------


@parametrized
@pytest.mark.asyncio
async def test_a_pending_approval_round_trips(store_settings: Any) -> None:
    created = await _pending(store_settings, args={"expression": "2+2"}, principal_subj="alice")

    fetched = await approvals.get_approval(store_settings, TENANT, created["id"])
    assert fetched is not None
    assert fetched["tool_name"] == TOOL
    assert fetched["call_signature"] == SIG
    assert fetched["manifest_id"] == MANIFEST
    assert fetched["status"] == "pending"
    assert fetched["principal_subj"] == "alice"
    assert fetched["args"] == {"expression": "2+2"}
    assert fetched["consumed_at"] is None


@parametrized
@pytest.mark.asyncio
async def test_creating_the_same_pending_twice_reuses_the_first(store_settings: Any) -> None:
    """Two identical calls arriving together must not queue two approvals for one decision."""
    first = await _pending(store_settings)
    second = await _pending(store_settings)

    assert second["id"] == first["id"]
    listed = await approvals.list_approvals(store_settings, TENANT, status="pending")
    assert [row["id"] for row in listed] == [first["id"]]


@parametrized
@pytest.mark.asyncio
async def test_a_decision_is_recorded_with_its_decider(store_settings: Any) -> None:
    created = await _pending(store_settings)

    decided = await approvals.decide(
        store_settings, TENANT, created["id"], decision="denied", decided_by="carol", note="no"
    )
    assert decided is not None
    assert decided["status"] == "denied"
    assert decided["decided_by"] == "carol"
    assert decided["decision_note"] == "no"
    assert decided["decided_at"]

    assert (await approvals.get_approval(store_settings, TENANT, created["id"]))["status"] == "denied"


@parametrized
@pytest.mark.asyncio
async def test_deciding_an_unknown_approval_returns_none(store_settings: Any) -> None:
    """Not an exception and not a fabricated row: the caller has to see that it was absent."""
    assert (
        await approvals.decide(store_settings, TENANT, "no-such-id", decision="approved", decided_by="x")
        is None
    )
    assert await approvals.get_approval(store_settings, TENANT, "no-such-id") is None


# --- listing --------------------------------------------------------------------------------


@parametrized
@pytest.mark.asyncio
async def test_listing_filters_by_status(store_settings: Any) -> None:
    pending = await _pending(store_settings, call_signature="a")
    approved = await _pending(store_settings, call_signature="b")
    await _approve(store_settings, approved["id"])

    assert [r["id"] for r in await approvals.list_approvals(store_settings, TENANT, status="pending")] == [
        pending["id"]
    ]
    assert [r["id"] for r in await approvals.list_approvals(store_settings, TENANT, status="approved")] == [
        approved["id"]
    ]


@parametrized
@pytest.mark.asyncio
async def test_approvals_do_not_cross_the_tenant_boundary(store_settings: Any) -> None:
    mine = await _pending(store_settings)
    theirs = await _pending(store_settings, tenant_id="other", call_signature="theirs")

    assert await approvals.get_approval(store_settings, "other", mine["id"]) is None
    assert [r["id"] for r in await approvals.list_approvals(store_settings, TENANT, status=None)] == [
        mine["id"]
    ]
    assert [r["id"] for r in await approvals.list_approvals(store_settings, "other", status=None)] == [
        theirs["id"]
    ]


# --- finding a grant: the security-bearing half ---------------------------------------------


@parametrized
@pytest.mark.asyncio
async def test_an_approved_grant_authorises_the_matching_call(store_settings: Any) -> None:
    created = await _pending(store_settings)
    await _approve(store_settings, created["id"])

    found = await approvals.find_approved(
        store_settings, TENANT, manifest_id=MANIFEST, tool_name=TOOL, call_signature=SIG
    )
    assert found is not None and found["id"] == created["id"]


@parametrized
@pytest.mark.asyncio
async def test_a_pending_or_denied_approval_authorises_nothing(store_settings: Any) -> None:
    """The gate is `status == approved`; anything else must not open it."""
    pending = await _pending(store_settings)
    assert (
        await approvals.find_approved(
            store_settings, TENANT, manifest_id=MANIFEST, tool_name=TOOL, call_signature=SIG
        )
        is None
    )

    await approvals.decide(store_settings, TENANT, pending["id"], decision="denied", decided_by="x")
    assert (
        await approvals.find_approved(
            store_settings, TENANT, manifest_id=MANIFEST, tool_name=TOOL, call_signature=SIG
        )
        is None
    )


@parametrized
@pytest.mark.asyncio
async def test_a_different_signature_or_tool_is_not_authorised(store_settings: Any) -> None:
    """The signature is what makes a grant specific to one call rather than to a tool."""
    created = await _pending(store_settings)
    await _approve(store_settings, created["id"])

    assert (
        await approvals.find_approved(
            store_settings, TENANT, manifest_id=MANIFEST, tool_name=TOOL, call_signature="other"
        )
        is None
    )
    assert (
        await approvals.find_approved(
            store_settings, TENANT, manifest_id=MANIFEST, tool_name="other", call_signature=SIG
        )
        is None
    )
    assert (
        await approvals.find_approved(
            store_settings, TENANT, manifest_id="other", tool_name=TOOL, call_signature=SIG
        )
        is None
    )


@parametrized
@pytest.mark.asyncio
async def test_an_expired_grant_authorises_nothing(store_settings: Any) -> None:
    """`ttl_seconds` is the operator's statement that consent goes stale."""
    created = await _pending(store_settings, ttl_seconds=-1)
    await _approve(store_settings, created["id"])

    assert (
        await approvals.find_approved(
            store_settings, TENANT, manifest_id=MANIFEST, tool_name=TOOL, call_signature=SIG
        )
        is None
    )


@parametrized
@pytest.mark.asyncio
async def test_a_grant_bound_to_a_principal_does_not_authorise_another(store_settings: Any) -> None:
    """`ApprovalRule.bind_principal`. Without it, one caller's consent covers everyone's."""
    created = await _pending(store_settings, principal_subj="alice")
    await _approve(store_settings, created["id"])

    for subject, expected in (("alice", created["id"]), ("mallory", None)):
        found = await approvals.find_approved(
            store_settings,
            TENANT,
            manifest_id=MANIFEST,
            tool_name=TOOL,
            call_signature=SIG,
            principal_subj=subject,
        )
        assert (found or {}).get("id") == expected, (subject, found)


@parametrized
@pytest.mark.asyncio
async def test_a_one_shot_grant_stops_authorising_once_consumed(store_settings: Any) -> None:
    """`ApprovalRule.one_shot`. Without it a single grant replays until it expires."""
    created = await _pending(store_settings)
    await _approve(store_settings, created["id"])

    assert (
        await approvals.find_approved(
            store_settings,
            TENANT,
            manifest_id=MANIFEST,
            tool_name=TOOL,
            call_signature=SIG,
            unconsumed_only=True,
        )
        is not None
    )

    assert await approvals.consume_approval(store_settings, TENANT, created["id"]) is True

    assert (
        await approvals.find_approved(
            store_settings,
            TENANT,
            manifest_id=MANIFEST,
            tool_name=TOOL,
            call_signature=SIG,
            unconsumed_only=True,
        )
        is None
    )


@parametrized
@pytest.mark.asyncio
async def test_a_grant_can_only_be_consumed_once(store_settings: Any) -> None:
    """The check-and-set that stops two concurrent identical calls both spending one grant."""
    created = await _pending(store_settings)
    await _approve(store_settings, created["id"])

    assert await approvals.consume_approval(store_settings, TENANT, created["id"]) is True
    assert await approvals.consume_approval(store_settings, TENANT, created["id"]) is False


@parametrized
@pytest.mark.asyncio
async def test_consuming_an_unknown_grant_is_false_not_an_error(store_settings: Any) -> None:
    assert await approvals.consume_approval(store_settings, TENANT, "no-such-id") is False


@parametrized
@pytest.mark.asyncio
async def test_the_most_recent_decision_is_the_one_that_authorises(store_settings: Any) -> None:
    """Two live grants for one call, and both backends must return the same one.

    Postgres orders `decided_at DESC LIMIT 1`. The in-memory twin scanned a dict and returned
    the first row it met — the *oldest* match — so the two disagreed about which grant applies,
    and with it about `principal_subj` and `edited_args`. An operator who substituted arguments
    on the newer approval would have had them honoured on one backend and silently ignored on
    the other.

    The sleep is load-bearing. `decided_at` is milliseconds, so two decisions inside one tie —
    and while both arms now break ties identically on `(decided_at, created_at, id)`, that
    resolution is deterministic rather than meaningful: `id` is a uuid, so the winner of a tie
    is arbitrary with respect to which decision came second. What this test asserts is the
    property that has meaning, so it creates a real gap rather than asserting into a coin flip.
    """
    import asyncio

    older = await _pending(store_settings, principal_subj="alice")
    await _approve(store_settings, older["id"], by="first-decider")

    await asyncio.sleep(0.01)

    newer = await _pending(store_settings, principal_subj="bob")
    # Distinct because `older` is no longer pending, not because the principal differs:
    # `create_pending` reuses on (tenant, manifest, tool, signature, status=pending) and does
    # not consider `principal_subj` at all.
    assert newer["id"] != older["id"], "an approved grant must not be reused as a pending row"
    await _approve(store_settings, newer["id"], by="second-decider")

    found = await approvals.find_approved(
        store_settings, TENANT, manifest_id=MANIFEST, tool_name=TOOL, call_signature=SIG
    )
    assert found is not None
    assert found["id"] == newer["id"], found
    assert found["decided_by"] == "second-decider", found


@parametrized
@pytest.mark.asyncio
async def test_an_expired_grant_does_not_hide_a_live_one(store_settings: Any) -> None:
    """The divergence a conformance suite exists to find, and it was live in production.

    Postgres took the newest approved row with `LIMIT 1` and only *then* checked expiry, so one
    expired grant hid a still-valid older one and the call was denied. The twin scanned every
    row and skipped expired ones, so it authorised the same call. `create_pending` reuses only
    *pending* rows, so approved grants accumulate per signature — an operator re-approving
    after a short TTL lapsed produced exactly this pair, and got a working tool on `memory://`
    and a refusal on the system of record.
    """
    import asyncio

    live = await _pending(store_settings)
    await _approve(store_settings, live["id"], by="still-valid")

    # A real gap, so "the expired one is newer" is true by construction rather than by tie.
    await asyncio.sleep(0.01)

    expired = await _pending(store_settings, ttl_seconds=-1)
    assert expired["id"] != live["id"]
    await _approve(store_settings, expired["id"], by="lapsed")

    found = await approvals.find_approved(
        store_settings, TENANT, manifest_id=MANIFEST, tool_name=TOOL, call_signature=SIG
    )
    assert found is not None, "a live grant exists; an expired one must not hide it"
    assert found["id"] == live["id"], found


@parametrized
@pytest.mark.asyncio
async def test_the_thread_round_trips_and_survives_a_decision(store_settings: Any) -> None:
    """`thread_id` is what makes `GET /approvals` able to name the blocked conversation.

    It matters most on the arm that is hardest to check: a durable run reaches an operator
    only through this row, because side events are an in-process queue keyed by thread and
    the run's agent is in the worker while its stream is served by the API.
    """
    created = await _pending(store_settings, thread_id="default:t-42")
    assert created["thread_id"] == "default:t-42"

    listed = await approvals.list_approvals(store_settings, TENANT, status="pending")
    assert [row["thread_id"] for row in listed] == ["default:t-42"]

    decided = await _approve(store_settings, created["id"])
    assert decided is not None and decided["thread_id"] == "default:t-42"

    found = await approvals.find_approved(
        store_settings, TENANT, manifest_id=MANIFEST, tool_name=TOOL, call_signature=SIG
    )
    assert found is not None and found["thread_id"] == "default:t-42"


@parametrized
@pytest.mark.asyncio
async def test_a_row_with_no_thread_reads_as_empty_not_null(store_settings: Any) -> None:
    """A gated tool called outside a chat context is a real state, not a missing value —
    and a client that has to branch on `None` versus `""` per backend has two contracts."""
    created = await _pending(store_settings)
    assert created["thread_id"] == ""
    fetched = await approvals.get_approval(store_settings, TENANT, created["id"])
    assert fetched is not None and fetched["thread_id"] == ""


@parametrized
@pytest.mark.asyncio
async def test_a_reused_pending_row_keeps_the_thread_that_opened_it(store_settings: Any) -> None:
    """Attribution, not ownership, and both backends must agree on which.

    `create_pending` reuses on (tenant, manifest, tool, call_signature, status=pending), so two
    threads issuing a byte-identical gated call share one row and one decision. The thread on
    it therefore names the *originator*; a second thread's must not overwrite it, because the
    row an operator is looking at would then rename itself under them.
    """
    first = await _pending(store_settings, thread_id="default:first")
    second = await _pending(store_settings, thread_id="default:second")

    assert second["id"] == first["id"], "the reuse key changed; this contract no longer applies"
    assert second["thread_id"] == "default:first", "a later thread overwrote the originator"
