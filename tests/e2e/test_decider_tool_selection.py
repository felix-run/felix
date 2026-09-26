"""`tools_retrieval.decider` as a request reaches it: the decider picks what the model is offered.

The chain under test is the one no unit test holds together: `FELIX_DECISION_ROUTES` → the
compile binding `spec.decider` → the react agent's per-step tool selection → the tools on the
model call → the decision metered on the run. The reply is scripted, so the only honest
assertion is about what the model was *offered*.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from felix.flush import flush_all
from felix.manifests.loader import parse_manifest
from felix.usage import store as usage_store
from felix_ai.decide import ChoiceAnswer
from felix_ai.providers.scripted import ScriptedTurn

TOOLS = ["calculator", "list_dir", "read_file", "write_file", "edit_file", "search_files"]


def _manifest(name: str, **spec: Any) -> Any:
    base: dict[str, Any] = {
        "pattern": "react",
        "tools": TOOLS,
        "auth": {"inbound": {"allow_anonymous": True}},
        "decider": {"id": "e2e-decider", "min_confidence": 0.5},
        "tools_retrieval": {"enabled": True, "top_k": 2, "model": "", "decider": True},
    }
    base.update(spec)
    return parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": name}, "spec": base}
    )


@pytest.fixture
def decider_script() -> Iterator[dict[str, Any]]:
    """Register the scripted decider for one test; the returned dict is its live answer."""
    from felix.decisions import register_builtin_deciders
    from felix_ai.decide import reset_decision_provider_registry
    from felix_ai.decide.scripted import register_scripted_decider

    state: dict[str, Any] = {"calls": 0}

    def answer(_state: Any, questions: Any) -> dict[str, Any]:
        state["calls"] += 1
        if state.get("raise"):
            raise RuntimeError("decider down")
        return {key: state["answer"] for key in questions}

    register_scripted_decider("scripted", answer)
    try:
        yield state
    finally:
        reset_decision_provider_registry()
        register_builtin_deciders()


def _keyword_ranking(manifest: Any, request: str) -> list[str]:
    """What `tools_retrieval` offers without a decider, for the same tools and request."""
    from felix.manifests.schema import ToolsRetrievalSpec
    from felix.patterns.types import ChatMessage
    from felix.tools.builtins import default_tool_provider
    from felix.tools.retrieval import select_tools

    tools = default_tool_provider().resolve(TOOLS)
    spec = ToolsRetrievalSpec(**{**manifest.spec.tools_retrieval.model_dump(), "decider": False})
    return [t.name for t in select_tools(tools, [ChatMessage(role="user", content=request)], spec)]


ENV = {"FELIX_DECISION_ROUTES": json.dumps({"e2e-decider": {"provider": "scripted", "model": "jev-latest"}})}


async def _chat(app: Any, name: str) -> Any:
    return await app.client.post(
        "/v1/chat/completions",
        json={"model": name, "messages": [{"role": "user", "content": "Save the note to notes.txt"}]},
    )


async def test_the_decider_chooses_the_tools_the_model_is_offered(boot: Any, decider_script: Any) -> None:
    decider_script["answer"] = ChoiceAnswer(
        "write_file",
        {"write_file": 0.7, "edit_file": 0.2, "calculator": 0.05, "(no tool)": 0.05},
        confidence=0.8,
    )
    m = _manifest("e2e-decided")
    async with boot([ScriptedTurn(content="saved")], env=ENV, manifests={"e2e-decided": m}) as app:
        resp = await _chat(app, "e2e-decided")
        assert resp.status_code == 200, resp.text
        assert app.spy.tools == [["write_file", "edit_file"]]

        await flush_all(app.settings)
        rows, _ = await usage_store.query(app.settings, "default", limit=50)
    assert decider_script["calls"] == 1
    decisions = [r for r in rows if r["model_id"] == "e2e-decider"]
    assert len(decisions) == 1, rows
    assert decisions[0]["tokens_input"] == 100, "the scripted decider's usage, metered on the run"
    assert decisions[0]["cost_usd"] > 0.0, "priced at the jev rate by its wire model"


async def test_a_decider_outage_falls_back_to_the_ranking_it_replaced(boot: Any, decider_script: Any) -> None:
    """An outage narrows by keywords as before — it does not fail the turn, and it does not
    hand the model the whole catalogue."""
    decider_script["raise"] = True
    m = _manifest("e2e-decided")
    async with boot([ScriptedTurn(content="saved")], env=ENV, manifests={"e2e-decided": m}) as app:
        resp = await _chat(app, "e2e-decided")
        assert resp.status_code == 200, resp.text
        [offered] = app.spy.tools
    assert offered == _keyword_ranking(m, "Save the note to notes.txt"), "the ranking it replaced, exactly"
    assert decider_script["calls"] == 1


async def test_an_unrouted_decider_fails_the_compile_rather_than_going_quiet(
    boot: Any, decider_script: Any
) -> None:
    """The same failure an unknown `spec.pattern` gives, in the same place. A decider that
    could never be reached, compiled into an agent that silently ranks by keywords, would
    report `tools_retrieval.decider: true` for a manifest that has never asked it anything."""
    m = _manifest("e2e-unrouted", decider={"id": "not-a-route"})
    async with boot(env=ENV, manifests={"e2e-unrouted": m}) as app:
        with pytest.raises(ValueError, match="not-a-route"):
            await _chat(app, "e2e-unrouted")
        assert app.spy.tools == []
    assert decider_script["calls"] == 0
