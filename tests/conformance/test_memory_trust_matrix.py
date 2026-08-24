"""The memory trust rules, stated once as data, asserted against every backend.

Eight review rounds on this subsystem found the same class of bug eight times: a
mutation route that one arm guards and the other does not, or a field one guard
preserves and another forgets. The worst of them was live in production and invisible
to CI — the Postgres upsert preserved `status` on a refused write but took the
incoming `metadata`, erasing the `forgotten_by` stamp, so the *second* identical write
resurrected a row the operator had deleted. The `memory://` arm never had it. Every
per-case test written to that point passed on both arms.

Per-case tests cannot catch the *next* divergence, because the next one is in the case
nobody thought of. So the rules are a table here, and both arms are asserted against
the table rather than against each other. A backend that disagrees fails, a rule that
changes has one place to change, and a combination nobody considered is present
because the table is a product rather than a list.

`EXPECTED` is the specification. If a row here is wrong, the fix is to argue about the
row — not to adjust an arm until it passes.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.memory import store as memory_store
from felix.memory.store import ACTIVE, FORGOTTEN

BACKENDS = ["memory", "postgres"]
parametrized = pytest.mark.parametrize("memory_settings", BACKENDS, indirect=True)

TENANT = "trust-matrix"
MANIFEST = "m"

OPERATOR = "management_api"
AGENT = "assistant"
TOOL = "remember_tool"

# (writer of the existing row, who forgot it or None, writer of the incoming write)
#   -> the status the row must have afterwards.
#
# The rule in one sentence: a write reactivates a forgotten row only if its writer
# ranks at or above whoever forgot it; everything else about a refused write is
# preserved, including the stamp naming the forgetter.
EXPECTED: dict[tuple[str, str | None, str], str] = {
    # Nobody forgot it — an ordinary write is just a write.
    (AGENT, None, AGENT): ACTIVE,
    (AGENT, None, OPERATOR): ACTIVE,
    (OPERATOR, None, AGENT): ACTIVE,
    (OPERATOR, None, OPERATOR): ACTIVE,
    # The operator forgot it. Only the operator brings it back.
    (AGENT, OPERATOR, AGENT): FORGOTTEN,
    (AGENT, OPERATOR, TOOL): FORGOTTEN,
    (AGENT, OPERATOR, OPERATOR): ACTIVE,
    (OPERATOR, OPERATOR, AGENT): FORGOTTEN,
    (OPERATOR, OPERATOR, OPERATOR): ACTIVE,
    # The agent forgot its own row. It can undo that, and so can the operator.
    (AGENT, TOOL, AGENT): ACTIVE,
    (AGENT, TOOL, TOOL): ACTIVE,
    (AGENT, TOOL, OPERATOR): ACTIVE,
}

CONTENT = "The deploy runbook lives in the ops repository."


async def _seed(settings: Any, writer: str, forgetter: str | None) -> str:
    row = await memory_store.put_memory(
        settings,
        TENANT,
        content=CONTENT,
        kind="instruction",
        manifest_id=MANIFEST,
        metadata={"source": writer},
    )
    if forgetter is not None:
        assert await memory_store.forget(settings, TENANT, row["id"], source=forgetter) is True, (
            f"{forgetter} could not forget a {writer}-written row"
        )
    return str(row["id"])


@parametrized
@pytest.mark.parametrize(("writer", "forgetter", "incoming"), sorted(EXPECTED, key=str))
@pytest.mark.asyncio
async def test_write_against_an_existing_row(
    memory_settings: Any, writer: str, forgetter: str | None, incoming: str
) -> None:
    mem_id = await _seed(memory_settings, writer, forgetter)
    await memory_store.put_memory(
        memory_settings,
        TENANT,
        content=CONTENT,
        kind="fact",
        manifest_id=MANIFEST,
        metadata={"source": incoming},
    )
    rows = await memory_store.get_many(memory_settings, TENANT, [mem_id])
    want = EXPECTED[(writer, forgetter, incoming)]
    assert rows[mem_id]["status"] == want, f"({writer}, {forgetter}, {incoming}) -> want {want}"


@parametrized
@pytest.mark.parametrize(("writer", "forgetter", "incoming"), sorted(EXPECTED, key=str))
@pytest.mark.asyncio
async def test_the_rule_is_stable_under_repetition(
    memory_settings: Any, writer: str, forgetter: str | None, incoming: str
) -> None:
    """The same write, three times.

    This is the shape that caught the production-only bug: the first refused write
    erased the stamp the second one consulted, so one write looked correct and the
    next did not. Any rule that depends on state a refused write is allowed to
    change will fail here and pass the single-write version.
    """
    mem_id = await _seed(memory_settings, writer, forgetter)
    want = EXPECTED[(writer, forgetter, incoming)]
    for attempt in range(3):
        await memory_store.put_memory(
            memory_settings,
            TENANT,
            content=CONTENT,
            kind="fact",
            manifest_id=MANIFEST,
            metadata={"source": incoming},
        )
        rows = await memory_store.get_many(memory_settings, TENANT, [mem_id])
        assert rows[mem_id]["status"] == want, (
            f"({writer}, {forgetter}, {incoming}) drifted to {rows[mem_id]['status']} "
            f"on write {attempt + 1}; want {want}"
        )


@parametrized
@pytest.mark.parametrize(("writer", "forgetter"), sorted({(w, f) for w, f, _ in EXPECTED}, key=str))
@pytest.mark.asyncio
async def test_a_refused_write_preserves_the_whole_row(
    memory_settings: Any, writer: str, forgetter: str | None
) -> None:
    """Not just `status`. Each round of review found another field a guard forgot —
    `kind`, then `topic_key` and `importance`, then `metadata` itself. The set of
    fields a refused write may not touch is the point, so it is asserted as a set."""
    if forgetter is None:
        pytest.skip("nothing is refused when the row was never forgotten")
    mem_id = await _seed(memory_settings, writer, forgetter)
    before = (await memory_store.get_many(memory_settings, TENANT, [mem_id]))[mem_id]

    await memory_store.put_memory(
        memory_settings,
        TENANT,
        content=CONTENT,
        kind="fact",
        manifest_id=MANIFEST,
        topic_key="attacker.key",
        importance=0.99,
        metadata={"source": AGENT},
    )
    after = (await memory_store.get_many(memory_settings, TENANT, [mem_id]))[mem_id]

    if EXPECTED[(writer, forgetter, AGENT)] != FORGOTTEN:
        pytest.skip("this combination permits the write")
    for field in ("status", "kind", "topic_key", "importance"):
        assert after.get(field) == before.get(field), f"refused write changed {field}"
    assert (after.get("metadata") or {}).get("forgotten_by") == forgetter, (
        "refused write erased the stamp naming who forgot the row"
    )


@parametrized
@pytest.mark.asyncio
async def test_supersede_is_guarded_like_the_other_routes(memory_settings: Any) -> None:
    """The route nobody was calling, and therefore nobody guarded.

    `supersede` is exported from `felix.memory` and had no trust predicate at all —
    a way in that a grep for callers would not have surfaced, which is why the
    invariant test enumerates routes from the source rather than from usage.
    """
    row = await memory_store.put_memory(
        memory_settings,
        TENANT,
        content="Never send credentials off-network.",
        manifest_id=MANIFEST,
        metadata={"source": OPERATOR},
    )
    await memory_store.supersede(memory_settings, TENANT, row["id"], 1, source=AGENT)
    active = await memory_store.list_active(memory_settings, TENANT, manifest_id=MANIFEST)
    assert [r["content"] for r in active] == ["Never send credentials off-network."]

    await memory_store.supersede(memory_settings, TENANT, row["id"], 1, source=OPERATOR)
    assert await memory_store.list_active(memory_settings, TENANT, manifest_id=MANIFEST) == []


@parametrized
@pytest.mark.asyncio
async def test_a_refused_forget_reports_refusal_on_both_arms(memory_settings: Any) -> None:
    """The twins answered differently for the same call.

    Postgres put the monotonicity test in the WHERE, so the statement matched nothing
    and rowcount was 0; the in-memory arm tested inside the function and still
    returned True. No state difference — but `_forget_tool` turns the return into two
    different strings back to the model, and nothing asserted it.
    """
    row = await memory_store.put_memory(
        memory_settings, TENANT, content=CONTENT, manifest_id=MANIFEST, metadata={"source": AGENT}
    )
    assert await memory_store.forget(memory_settings, TENANT, row["id"], source=OPERATOR) is True
    assert await memory_store.forget(memory_settings, TENANT, row["id"], source=TOOL) is False
