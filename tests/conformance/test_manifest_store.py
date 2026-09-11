"""One contract for the manifest store, run against both backends.

Which manifest version is *active* is what every request resolves through, and the canary
pointer beside it decides what fraction of traffic gets a different agent. Until now the
Postgres half of both ran only under `test_migrations.py`, which creates the schema and never
queries it — so the version pointer that production reads on every turn was asserted only
against a dict.

The pointer semantics are where the two are least alike: the twin mutates a dict entry in place
while Postgres does a `db.get` and an upsert with `on_conflict_do_nothing`, so "the first write
wins" and "activation clears the canary" are properties that could hold on one and not the
other.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.manifests import store as manifests
from felix.manifests.loader import parse_manifest

BACKENDS = ["memory", "postgres"]
parametrized = pytest.mark.parametrize("store_settings", BACKENDS, indirect=True)

TENANT = "conformance"
NAME = "agent"


def _manifest(prompt: str = "one") -> Any:
    return parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": NAME},
            "spec": {"pattern": "react", "system_prompt": {"inline": prompt}},
        }
    )


async def _put(settings: Any, prompt: str = "one", *, tenant: str = TENANT) -> dict[str, Any]:
    return await manifests.put_version(settings, tenant, NAME, _manifest(prompt))


# --- versions -------------------------------------------------------------------------------


@parametrized
@pytest.mark.asyncio
async def test_a_version_round_trips(store_settings: Any) -> None:
    created = await _put(store_settings, "hello")

    fetched = await manifests.get_version(store_settings, TENANT, NAME, created["version"])
    assert fetched is not None
    assert fetched["name"] == NAME
    assert fetched["version"] == 1
    # The whole document, not one leaf: this also catches a JSONB round trip that coerced a
    # type — a tuple flattened to a list, an int widened — which a string survives unchanged.
    assert fetched["manifest"] == _manifest("hello").model_dump(mode="json")


@parametrized
@pytest.mark.asyncio
async def test_versions_increment_rather_than_overwrite(store_settings: Any) -> None:
    """Every write is a new version, because a pin names one and must keep resolving."""
    first = await _put(store_settings, "one")
    second = await _put(store_settings, "two")

    assert (first["version"], second["version"]) == (1, 2)
    kept = await manifests.get_version(store_settings, TENANT, NAME, 1)
    assert kept is not None
    assert kept["manifest"]["spec"]["system_prompt"]["inline"] == "one"


@parametrized
@pytest.mark.asyncio
async def test_an_unknown_version_is_none(store_settings: Any) -> None:
    await _put(store_settings)

    assert await manifests.get_version(store_settings, TENANT, NAME, 99) is None
    assert await manifests.get_version(store_settings, TENANT, "no-such-agent", 1) is None


# --- the active pointer ---------------------------------------------------------------------


@parametrized
@pytest.mark.asyncio
async def test_the_first_write_becomes_active_and_later_ones_do_not(store_settings: Any) -> None:
    """Publishing a version must not silently promote it.

    Postgres upserts the pointer with `on_conflict_do_nothing`; the twin writes only when the
    key is absent. Both mean the same thing and neither is obvious from the other, which is why
    it is pinned: a store that promoted on every write would roll a tenant onto an unreviewed
    manifest the moment it was uploaded.
    """
    await _put(store_settings, "one")
    assert [row["version"] for row in await manifests.list_active(store_settings, TENANT)] == [1]

    await _put(store_settings, "two")
    assert [row["version"] for row in await manifests.list_active(store_settings, TENANT)] == [1]


@parametrized
@pytest.mark.asyncio
async def test_activating_a_version_moves_the_pointer(store_settings: Any) -> None:
    await _put(store_settings, "one")
    await _put(store_settings, "two")

    activated = await manifests.activate_version(store_settings, TENANT, NAME, version=2)
    assert activated is not None and activated["version"] == 2

    assert [row["version"] for row in await manifests.list_active(store_settings, TENANT)] == [2]


@parametrized
@pytest.mark.asyncio
async def test_activating_a_version_that_does_not_exist_is_refused(store_settings: Any) -> None:
    """Otherwise the pointer names a version no request can resolve, and every turn 404s."""
    await _put(store_settings, "one")

    assert await manifests.activate_version(store_settings, TENANT, NAME, version=99) is None
    assert [row["version"] for row in await manifests.list_active(store_settings, TENANT)] == [1]


@parametrized
@pytest.mark.asyncio
async def test_activating_an_unknown_manifest_is_none(store_settings: Any) -> None:
    """Seeded first, so this proves the lookup is name-scoped rather than the store empty."""
    await _put(store_settings)

    assert await manifests.activate_version(store_settings, TENANT, "no-such", version=1) is None
    assert await manifests.activate_version(store_settings, TENANT, NAME, version=1) is not None


# --- the canary -----------------------------------------------------------------------------


@parametrized
@pytest.mark.asyncio
async def test_a_canary_is_recorded_with_its_weight(store_settings: Any) -> None:
    """The weight is the fraction of traffic diverted, so both halves have to persist."""
    await _put(store_settings, "one")
    await _put(store_settings, "two")

    updated = await manifests.set_canary(store_settings, TENANT, NAME, canary_version=2, canary_weight=25)
    assert updated is not None
    assert (updated["canary_version"], updated["canary_weight"]) == (2, 25)

    rows = await manifests.list_active(store_settings, TENANT)
    assert len(rows) == 1, rows
    active = rows[0]
    assert (active["canary_version"], active["canary_weight"]) == (2, 25)
    assert active["version"] == 1, "a canary must not move the active pointer"


@parametrized
@pytest.mark.asyncio
async def test_a_canary_on_an_unknown_version_is_refused(store_settings: Any) -> None:
    """A canary pointing at nothing would divert traffic to a manifest that cannot resolve."""
    await _put(store_settings, "one")

    with pytest.raises(LookupError):
        await manifests.set_canary(store_settings, TENANT, NAME, canary_version=99, canary_weight=10)


@parametrized
@pytest.mark.asyncio
async def test_activating_clears_the_canary(store_settings: Any) -> None:
    """Promoting the canary's own version must not leave it diverting traffic to itself.

    Both arms reset the pair on activation. Left set, the rollout would keep splitting traffic
    after the operator believed it had finished.
    """
    await _put(store_settings, "one")
    await _put(store_settings, "two")
    await manifests.set_canary(store_settings, TENANT, NAME, canary_version=2, canary_weight=25)

    await manifests.activate_version(store_settings, TENANT, NAME, version=2)

    rows = await manifests.list_active(store_settings, TENANT)
    assert len(rows) == 1, rows
    active = rows[0]
    assert active["version"] == 2
    assert active["canary_version"] is None, active
    assert active["canary_weight"] == 0, active


@parametrized
@pytest.mark.asyncio
async def test_a_canary_can_be_cleared_explicitly(store_settings: Any) -> None:
    await _put(store_settings, "one")
    await _put(store_settings, "two")
    await manifests.set_canary(store_settings, TENANT, NAME, canary_version=2, canary_weight=25)

    cleared = await manifests.set_canary(store_settings, TENANT, NAME, canary_version=None, canary_weight=0)
    assert cleared is not None
    assert cleared["canary_version"] is None
    assert cleared["canary_weight"] == 0


@parametrized
@pytest.mark.asyncio
async def test_a_canary_on_an_unknown_manifest_is_none(store_settings: Any) -> None:
    assert (
        await manifests.set_canary(store_settings, TENANT, "no-such", canary_version=None, canary_weight=0)
        is None
    )


# --- tenancy --------------------------------------------------------------------------------


@parametrized
@pytest.mark.asyncio
async def test_manifests_do_not_cross_the_tenant_boundary(store_settings: Any) -> None:
    """One tenant's agent name must not resolve to another tenant's manifest."""
    await _put(store_settings, "mine", tenant=TENANT)
    await _put(store_settings, "theirs", tenant="other")

    mine = await manifests.get_version(store_settings, TENANT, NAME, 1)
    theirs = await manifests.get_version(store_settings, "other", NAME, 1)
    assert mine is not None and theirs is not None
    assert mine["manifest"]["spec"]["system_prompt"]["inline"] == "mine"
    assert theirs["manifest"]["spec"]["system_prompt"]["inline"] == "theirs"

    assert len(await manifests.list_active(store_settings, TENANT)) == 1
    assert len(await manifests.list_active(store_settings, "other")) == 1


@parametrized
@pytest.mark.asyncio
async def test_every_tenant_with_a_manifest_is_listed(store_settings: Any) -> None:
    """`run_continuous_eval` iterates this; a tenant missing from it is never benchmarked.

    What it pins is narrower than the function's name suggests, and saying so is the point:
    listing tenants by *version* row rather than by active *pointer* passes this test, because
    the first write to a name creates both and nothing here can separate them. Neither backend
    exposes a way to drop a pointer while keeping its versions, so the distinction is not
    reachable from a contract written against the public surface.
    """
    await _put(store_settings, "mine", tenant=TENANT)
    await _put(store_settings, "theirs", tenant="other")

    assert await manifests.list_tenants_with_active(store_settings) == ["conformance", "other"]


# --- divergences the contract was written to catch ------------------------------------------


@parametrized
@pytest.mark.asyncio
@pytest.mark.parametrize("weight", [-1, 101, 150])
async def test_a_canary_weight_outside_the_allowed_range_is_refused(store_settings: Any, weight: int) -> None:
    """`0001_baseline` carries CHECK (canary_weight BETWEEN 0 AND 100); the twin carried nothing.

    Postgres refused an out-of-range weight with an IntegrityError while the twin stored it, and
    the stored value is not inert — it feeds the canary hash router, so a weight of 150 diverted
    every request to the canary on `memory://` and was unreachable on the system of record. The
    REST route already bounds the field; a store is a public seam that plugins and worker jobs
    call directly, so it has to say the same thing.
    """
    await _put(store_settings, "one")
    await _put(store_settings, "two")

    with pytest.raises((ValueError, Exception)):
        await manifests.set_canary(store_settings, TENANT, NAME, canary_version=2, canary_weight=weight)

    rows = await manifests.list_active(store_settings, TENANT)
    assert len(rows) == 1, rows
    assert rows[0]["canary_weight"] == 0, rows


@parametrized
@pytest.mark.asyncio
async def test_the_returned_manifest_is_a_copy_not_the_stored_one(store_settings: Any) -> None:
    """Editing what a read handed back must not rewrite the store.

    Postgres deserialises fresh JSONB per read, so this holds there by construction. The twin
    returned the stored dict itself, and a caller editing it silently changed what every later
    reader of that version saw — a corruption with no write in sight, on the backend the whole
    test suite runs against.
    """
    created = await _put(store_settings, "original")

    first = await manifests.get_version(store_settings, TENANT, NAME, created["version"])
    assert first is not None
    first["manifest"]["spec"]["system_prompt"]["inline"] = "tampered"

    second = await manifests.get_version(store_settings, TENANT, NAME, created["version"])
    assert second is not None
    assert second["manifest"]["spec"]["system_prompt"]["inline"] == "original", second
