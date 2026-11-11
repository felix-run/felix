"""A claim that lapsed unheld is over: its holder runs nothing and releases nothing.

The cron sweep claimed a batch of fifty and started `fiber_concurrency` of them; the rest held a
claim nobody renewed. Past the lease, the fiber loop in the same process claimed one again and
started its turn -- and then the sweep's slot freed, and it ran the row it had been holding. The
release, the renewal and the attempt record were scoped to the *process* (`lease_owner`), which
both claims share, so the stale holder's release cleared the live claim: a `running` row with no
lease, which the next claim took, for a second agent loop on one thread. And on a pattern that
cannot resume from its log the stale holder never asked whether the row was still its own, so it
ran the whole turn a second time.

Both claims here are taken by one process, the way a sweep and a loop share a worker.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.manifests.loader import parse_manifest
from felix_ai.providers.scripted import ScriptedTurn

REQUEST = "what is 2+2?"


def _manifest(name: str, **spec: Any) -> Any:
    base = {
        "pattern": "react",
        "tools": [],
        "auth": {"inbound": {"allow_anonymous": True}},
        # Outlives the lease this test lets lapse; the default run TTL is the lease's length.
        "execution": {"mode": "durable", "resume_token_ttl_seconds": 3600},
        **spec,
    }
    return parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": name}, "spec": base}
    )


REACT = {"e2e-stale": _manifest("e2e-stale")}
# A composite reads the request to route, so it never resumes from the log -- the branch of
# `_invoke_resume_point` that returned before anything checked the claim.
ROUTER = {
    "e2e-a": _manifest("e2e-a", execution={}),
    "e2e-stale": _manifest("e2e-stale", pattern="router", sub_agents=["e2e-a"]),
}
# No log to resume from: the other branch that returned unchecked.
NO_LOG = {"e2e-stale": _manifest("e2e-stale", memory={"checkpointer": "none"})}


@pytest.mark.parametrize(
    ("manifests", "script", "one_run"),
    [
        (REACT, [ScriptedTurn(content="It is 4."), ScriptedTurn(content="again")], 1),
        (
            ROUTER,
            [
                ScriptedTurn(content="e2e-a"),
                ScriptedTurn(content="It is 4."),
                ScriptedTurn(content="e2e-a"),
                ScriptedTurn(content="again"),
            ],
            2,
        ),
        (NO_LOG, [ScriptedTurn(content="It is 4."), ScriptedTurn(content="again")], 1),
    ],
    ids=["react", "router", "no-checkpointer"],
)
async def test_a_stale_holder_runs_nothing_and_leaves_the_live_claim_alone(
    boot: Any,
    monkeypatch: pytest.MonkeyPatch,
    manifests: dict[str, Any],
    script: list[ScriptedTurn],
    one_run: int,
) -> None:
    from felix.durability import fibers as F

    async with boot(script, manifests=manifests) as app:
        resp = await app.client.post(
            "/chat", json={"manifest": "e2e-stale", "messages": [{"role": "user", "content": REQUEST}]}
        )
        assert resp.status_code == 202, resp.text
        token = resp.json()["resume_token"]
        clock = {"now": F.now_ms()}
        monkeypatch.setattr(F, "now_ms", lambda: clock["now"])

        # Claimed, and never started: nothing renewed it.
        (stale,) = await F._claim_due(app.settings, 1)
        clock["now"] += F.FIBER_LEASE_MS + 1
        (live,) = await F._claim_due(app.settings, 1)
        assert live["lease_owner"] == stale["lease_owner"], "one process took both claims"

        await F._advance_claimed(app.settings, stale)

        assert len(app.spy.prompts) == 0, "the stale holder ran the turn beside the live claim"
        held = await F.get_fiber(app.settings, "default", token)
        assert held is not None
        assert (held["status"], held["lease_owner"], held["lease_until"]) == (
            "running",
            live["lease_owner"],
            live["lease_until"],
        ), "the stale holder released a claim that was not its own"
        assert await F._claim_due(app.settings, 1) == [], "the fiber was free for a third claim"

        await F._advance_claimed(app.settings, live)
        run = (await app.client.get(f"/chat/runs/{token}")).json()
        calls = len(app.spy.prompts)

    assert run["status"] == "completed" and run["final"]["content"] == "It is 4."
    assert calls == one_run, "the turn ran exactly once"
