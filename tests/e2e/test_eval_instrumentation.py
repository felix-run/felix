"""An eval run reports what each item cost and whether its judge actually ran.

`duration_ms`, token counts and cost are measured on the candidate's own turn — metered onto
the item's request context the way a chat turn is — and summed into the run's `stats`. A judge
that cannot run used to fall back to the heuristic in silence; the item now says so, and the
run counts it, because a weaker eval that still reports a result is worse than a failed one.
"""

from __future__ import annotations

from typing import Any

from felix_ai.providers.scripted import ScriptedTurn
from felix_ai.types import TokenUsage


async def _dataset(app: Any, rubric: dict[str, Any]) -> None:
    resp = await app.client.put(
        "/eval/datasets/instrumented",
        json={"items": [{"item_id": "one", "user_input": "what is 2+2?", "rubric": rubric}]},
    )
    assert resp.status_code == 200, resp.text


async def test_each_item_reports_its_time_tokens_and_cost_and_the_run_sums_them(boot: Any) -> None:
    turn = ScriptedTurn(content="It is 4.", usage=TokenUsage(input=11, output=7))
    async with boot([turn]) as app:
        await _dataset(app, {"contains": "4"})
        resp = await app.client.post(
            "/eval/datasets/instrumented/run",
            json={"candidate_manifest": "quick", "deterministic_judge": True},
        )
        assert resp.status_code == 200, resp.text
        run = resp.json()
        fetched = (await app.client.get(f"/eval/runs/{run['id']}")).json()

    [score] = run["scores"]
    assert score["pass"] is True
    assert (score["tokens_input"], score["tokens_output"]) == (11, 7), "the candidate's own turn"
    assert score["cost_usd"] > 0 and score["duration_ms"] >= 0
    stats = fetched["stats"]
    assert (stats["tokens_input"], stats["tokens_output"]) == (11, 7)
    assert stats["cost_usd"] == score["cost_usd"] and stats["judge_fallbacks"] == 0
    assert stats["wall_ms"] is not None and stats["wall_ms"] >= 0


async def test_a_judge_that_cannot_run_is_reported_not_hidden(boot: Any) -> None:
    async with boot([ScriptedTurn(content="It is 4.")]) as app:
        await _dataset(app, {"contains": "4", "llm_judge": True, "judge_model": "e2e-no-such-route"})
        resp = await app.client.post(
            "/eval/datasets/instrumented/run",
            json={"candidate_manifest": "quick", "deterministic_judge": False, "use_llm_judge": True},
        )
        assert resp.status_code == 200, resp.text
        run = resp.json()

    [score] = run["scores"]
    assert score["judge_fallback"] is True and score["judge_error"], score
    assert score["rule"] == "contains", "scored by the heuristic, and it says which"
    assert run["stats"]["judge_fallbacks"] == 1
