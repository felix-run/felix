"""`replica_id` has to name one process, because lease predicates compare against it.

`durability/fibers.py` decides whether a fiber claim is its own with
`lease_owner == replica_id` — `_renew_lease` today, and `_release_fiber` /
`_record_attempt` alongside it. The setting defaulted to the constant `"local"` and nothing
set it: not the Helm chart, not a Compose overlay, and `validate_runtime()` did not ask for
it under `scale_out`. So every worker in a scaled deployment claimed under one name and
those predicates matched each other's claims — a guard that reads as protection and decides
nothing, which is this repo's recurring defect shape.

Found by the security review on felix#262.
"""

from __future__ import annotations

import os

import pytest
from felix.config import Settings
from felix.durability import fibers


def _settings(**over: object) -> Settings:
    return Settings(
        database_url="memory://replica",
        object_store="memory",
        allow_insecure=True,
        auth_mode="none",
        **over,  # type: ignore[arg-type]
    )


@pytest.fixture(autouse=True)
def _clean() -> None:
    fibers.reset_memory_fibers()


def test_the_default_names_this_process_and_not_a_constant() -> None:
    """Stable within a process, because it is this process's identity; and carrying the pid,
    because that is what makes two of them on one host different."""
    first, second = _settings(), _settings()

    assert first.replica_id == second.replica_id, "the identity changed inside one process"
    assert first.replica_id != "local", "the shared constant is back"
    assert first.replica_id.endswith(f":{os.getpid()}"), first.replica_id


def test_an_empty_replica_id_is_refused() -> None:
    """Worse than the constant it replaced: `lease_owner` is `""` on every *unclaimed* row,
    so an empty id would match every released claim as this worker's own."""
    for blank in ("", "   "):
        with pytest.raises(Exception, match="FELIX_REPLICA_ID"):
            _settings(replica_id=blank)


def test_two_processes_get_different_identities() -> None:
    """The property that actually changed, and the only way to see it.

    Two `Settings()` inside one interpreter *should* agree — it is one process — so the
    distinctness this default exists for is invisible without a second one. Two workers are
    two processes, so this asks two interpreters, the way production does.
    """
    import subprocess
    import sys

    read_it = "from felix.config import Settings; print(Settings().replica_id)"
    seen = [
        subprocess.run(
            [sys.executable, "-c", read_it], capture_output=True, text=True, check=True, timeout=120
        ).stdout.strip()
        for _ in range(2)
    ]
    assert all(seen), seen
    assert seen[0] != seen[1], f"two processes claim the same identity: {seen[0]!r}"


@pytest.mark.asyncio
async def test_one_worker_cannot_renew_another_workers_claim() -> None:
    """The consumer, pinned — this is what the unique default is *for*.

    It passes on the old code too, because it hands both workers an explicit id: the
    predicate was always correct, and what was missing was two names to tell apart. It earns
    its place by failing if the predicate is ever dropped, which is the event that would make
    the default above pointless without anything else noticing.

    `_renew_lease`'s own comment — "a lease we already lost must not be stolen back
    mid-step" — was describing a guard that, between two real replicas, did not hold.
    """
    worker_a = _settings(replica_id="worker-a")
    worker_b = _settings(replica_id="worker-b")

    created = await fibers.create_fiber(worker_a, "acme", status="pending", state={"steps": [], "cursor": 0})
    claimed = await fibers._claim_due_memory(worker_a, fibers.now_ms())
    assert len(claimed) == 1, claimed
    row = claimed[0]
    assert row["lease_owner"] == "worker-a", row["lease_owner"]

    stored = fibers._memory_fibers[("acme", str(created["id"]))]
    held_until = stored["lease_until"]

    # B tries to push out a claim it does not hold.
    await fibers._renew_lease(worker_b, row)
    assert stored["lease_until"] == held_until, "worker-b renewed worker-a's claim"

    # …and A, which does hold it, still can.
    await fibers._renew_lease(worker_a, row)
    assert stored["lease_until"] >= held_until, "the owner could not renew its own claim"


def test_every_felix_deployment_in_the_chart_is_given_an_identity() -> None:
    """The wiring, not the setting.

    A process-unique default is only half of it: the chart could still pin one name across
    pods, and the default would never be reached. This asserts the env var is set from the
    pod name in the helper every Felix deployment includes — so a new deployment template
    inherits it rather than having to remember.
    """
    import pathlib
    import re

    chart = pathlib.Path(__file__).resolve().parents[2] / "deploy" / "helm" / "felix" / "templates"
    helpers = (chart / "_helpers.tpl").read_text()

    datastore = helpers.split('define "felix.datastoreEnv"')[1].split("{{- end -}}")[0]
    assert "FELIX_REPLICA_ID" in datastore, "the shared env helper does not name the replica"
    assert "metadata.name" in datastore, "FELIX_REPLICA_ID is not taken from the pod name"

    deployments = sorted(p.name for p in chart.glob("deployment-*.yaml"))
    assert deployments, "no deployment templates found"
    for name in deployments:
        body = (chart / name).read_text()
        assert re.search(r'include\s+"felix\.datastoreEnv"', body), (
            f"{name} does not include the helper that names the replica"
        )
