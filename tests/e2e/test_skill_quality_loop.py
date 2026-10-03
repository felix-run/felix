"""Feedback becomes a reviewed draft, an evaluation measures it, and the policy gates on that.

The chain over real HTTP, with the model scripted: an operator files feedback and accepts it →
the worker's sweep (`run_skill_jobs`, what `felix_worker.tasks.skill_jobs` runs) rewrites the
skill into a draft in the review queue → a tenant policy with `require_eval` refuses to publish
that draft → an evaluation is queued and the sweep runs it → the evaluation succeeded with an
uplift above the tenant's floor, on the bundle's own scenarios (the only kind that counts for an
agent's version) → the same publish now goes through.
"""

from __future__ import annotations

import json
from typing import Any

from felix.skills.format import serialize_skill_md
from felix.skills.jobs import run_skill_jobs
from felix_ai.providers.scripted import ScriptedTurn

NAME = "invoice-triage"
SCENARIOS = json.dumps([{"name": "large-invoice", "prompt": "An invoice for 900 arrived. Where does it go?"}])


def _skill_md(steps: str) -> str:
    body = f"\n# Invoice triage\n\nUse this when an invoice arrives.\n\n## Steps\n\n{steps}"
    return serialize_skill_md({"name": NAME, "description": "Route incoming invoices."}, body)


ORIGINAL = _skill_md("1. Read the vendor and the amount.\n2. Route large amounts to finance.\n")
IMPROVED = _skill_md("1. Read the vendor and the amount.\n2. Route amounts over 500 to the finance queue.\n")


def _judge(score: float) -> ScriptedTurn:
    return ScriptedTurn(content=json.dumps({"score": score, "reason": "scripted"}))


async def test_feedback_to_a_reviewed_draft_to_an_eval_gated_publish(boot: Any) -> None:
    async with boot([]) as app:
        created = await app.client.post(
            "/skill-library",
            json={"files": {"SKILL.md": ORIGINAL, "evals/scenarios.json": SCENARIOS}, "publish": True},
        )
        assert created.status_code == 201 and created.json()["published"] is True, created.text

        # An operator files feedback and accepts it; nothing runs until the sweep does.
        filed = await app.client.post(
            f"/skill-library/{NAME}/feedback", json={"body": "Say the threshold is 500, and name the queue."}
        )
        assert filed.status_code == 201, filed.text
        feedback_id = filed.json()["id"]
        accepted = await app.client.post(f"/skill-library/-/feedback/{feedback_id}/accept")
        assert (accepted.status_code, accepted.json()["improve"]) == (200, True), accepted.text
        assert app.spy.calls == []

        app.push(ScriptedTurn(content=IMPROVED))
        assert (await run_skill_jobs(app.settings))["improvements"] == 1

        applied = (await app.client.get("/skill-library/-/feedback?status=applied")).json()["items"]
        assert [(f["id"], f["result_version"]) for f in applied] == [(feedback_id, "0.1.1")]
        (draft,) = (await app.client.get("/skill-library/-/review")).json()["items"]
        assert (draft["version"], draft["source"], draft["author"], draft["live_version"]) == (
            "0.1.1",
            "agent",
            "skill-improver",
            "0.1.0",
        )
        improved = await app.client.get(f"/skill-library/{NAME}/versions/0.1.1/files/SKILL.md")
        assert "over 500 to the finance queue" in improved.json()["content"]

        # The tenant requires an evaluation: the draft cannot go live yet.
        # The tenant requires an evaluation with a positive uplift: a draft that made the answers
        # worse would stay blocked even after its evaluation succeeded.
        policy = await app.client.patch(
            "/skill-library/-/policy", json={"require_eval": True, "min_eval_uplift": 1}
        )
        assert (policy.json()["source"], policy.json()["require_eval"], policy.json()["min_eval_uplift"]) == (
            "tenant",
            True,
            1,
        ), policy.text
        blocked = await app.client.post(f"/skill-library/{NAME}/versions/0.1.1/publish")
        assert blocked.status_code == 422 and blocked.json()["error"] == "publish_blocked", blocked.text
        assert any("succeeded evaluation" in r for r in blocked.json()["reasons"])

        queued = await app.client.post(f"/skill-library/{NAME}/versions/0.1.1/eval")
        assert (queued.status_code, queued.json()["status"]) == (202, "queued"), queued.text
        # One scenario from the bundle the draft kept: two answers, then two scores.
        app.push(
            ScriptedTurn(content="Send it to whoever handles money."),
            ScriptedTurn(content="Over 500, so the finance queue."),
            _judge(0.3),
            _judge(0.9),
        )
        assert (await run_skill_jobs(app.settings))["evals"] == 1

        result = (await app.client.get(f"/skill-library/{NAME}/evals/{queued.json()['id']}")).json()
        assert (result["status"], result["scenario_source"]) == ("succeeded", "bundle"), result["error"]
        assert (result["baseline_score"], result["with_skill_score"], result["uplift"]) == (30, 90, 60)
        # An agent's version (the improver's draft) on the bundle's own scenarios: it counts.
        assert (result["counts_for_gate"], result["attempts"]) == (True, 1), result["gate_note"]

        published = await app.client.post(f"/skill-library/{NAME}/versions/0.1.1/publish")
        assert (published.status_code, published.json()["status"]) == (200, "published"), published.text
