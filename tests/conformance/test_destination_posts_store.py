"""One contract for the destination post ledger, run against both backends.

What `post` relies on, and what a dict and an `INSERT ... ON CONFLICT DO NOTHING` could quietly
disagree on: that of two claims on one edition exactly one wins, even when they race; that the
losers read the row the winner holds; that only a `posting` row moves, so a settled post is never
released or overwritten; and that the same edition under two tenants, two destinations or two
manifests is two rows.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from felix.destinations import ledger

BACKENDS = ["memory", "postgres"]
parametrized = pytest.mark.parametrize("store_settings", BACKENDS, indirect=True)

EDITION = "brief:2026-10-29"


async def _claim(
    settings: Any,
    tenant: str = "acme",
    destination: str = "team",
    manifest: str = "brief",
    edition: str = EDITION,
    thread: str = "acme:t",
) -> Any:
    return await ledger.claim(settings, tenant, destination, manifest, edition, thread_id=thread)


@parametrized
async def test_the_first_claim_wins_and_the_next_reads_its_row(store_settings: Any) -> None:
    first = await _claim(store_settings)
    second = await _claim(store_settings, thread="acme:other")

    assert first.won and not second.won
    assert (second.row["status"], second.row["manifest_id"], second.row["thread_id"]) == (
        "posting",
        "brief",
        "acme:t",
    )


@parametrized
async def test_racing_claims_have_exactly_one_winner_and_the_losers_read_its_row(store_settings: Any) -> None:
    claims = await asyncio.gather(*(_claim(store_settings, thread=f"acme:t{i}") for i in range(5)))

    (winner,) = [c for c in claims if c.won]
    losers = [c for c in claims if not c.won]
    assert len(losers) == 4
    assert {(c.row["status"], c.row["thread_id"]) for c in losers} == {("posting", winner.row["thread_id"])}


@parametrized
async def test_a_settled_post_is_read_back_and_never_released_or_resettled(store_settings: Any) -> None:
    await _claim(store_settings)
    await ledger.settle(store_settings, "acme", "team", "brief", EDITION, status=ledger.POSTED, ref="msg-1")
    await ledger.settle(store_settings, "acme", "team", "brief", EDITION, status=ledger.MAYBE_POSTED)

    assert not await ledger.release(store_settings, "acme", "team", "brief", EDITION)
    again = await _claim(store_settings)
    assert not again.won
    assert (again.row["status"], again.row["ref"]) == ("posted", "msg-1")


@parametrized
async def test_a_released_claim_frees_the_edition_and_maybe_posted_holds_it(store_settings: Any) -> None:
    await _claim(store_settings)
    assert await ledger.release(store_settings, "acme", "team", "brief", EDITION)
    assert await ledger.get(store_settings, "acme", "team", "brief", EDITION) is None
    assert (await _claim(store_settings)).won

    await ledger.settle(store_settings, "acme", "team", "brief", EDITION, status=ledger.MAYBE_POSTED)
    assert not await ledger.release(store_settings, "acme", "team", "brief", EDITION)
    row = await ledger.get(store_settings, "acme", "team", "brief", EDITION)
    assert row is not None and (row["status"], row["ref"]) == ("maybe_posted", None)


@parametrized
async def test_an_edition_is_its_own_per_tenant_and_per_destination(store_settings: Any) -> None:
    assert (await _claim(store_settings)).won
    assert (await _claim(store_settings, tenant="globex")).won
    assert (await _claim(store_settings, destination="other")).won
    assert await ledger.get(store_settings, "globex", "other", "brief", EDITION) is None


@parametrized
async def test_two_manifests_posting_one_edition_to_one_destination_both_post(store_settings: Any) -> None:
    assert (await _claim(store_settings, manifest="brief")).won
    assert (await _claim(store_settings, manifest="digest")).won
    await ledger.settle(store_settings, "acme", "team", "brief", EDITION, status=ledger.POSTED, ref="m1")

    digest = await ledger.get(store_settings, "acme", "team", "digest", EDITION)
    assert digest is not None and (digest["status"], digest["ref"]) == ("posting", None)
    assert await ledger.release(store_settings, "acme", "team", "digest", EDITION)
    brief = await ledger.get(store_settings, "acme", "team", "brief", EDITION)
    assert brief is not None and brief["status"] == "posted"
