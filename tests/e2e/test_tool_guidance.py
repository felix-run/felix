"""The system prompt carries guidance only for the tools the agent actually has.

Advice about a tool written into `system_prompt` outlives the tool: remove `fetch` and the prompt
still says to use it. `spec.tool_guidance` keys the advice by tool, and the compile appends it
only for tools that are bound — read here off the prompt the model was sent.
"""

from __future__ import annotations

from typing import Any

from felix.manifests.loader import parse_manifest
from felix_ai.providers.scripted import ScriptedTurn


def _manifest(**spec: Any) -> Any:
    base = {
        "pattern": "react",
        "tools": ["calculator"],
        "auth": {"inbound": {"allow_anonymous": True}},
        **spec,
    }
    return parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": "e2e-guided"}, "spec": base}
    )


GUIDANCE = {
    "calculator": "Use `calculator` for any arithmetic rather than doing it in your head.",
    "fetch": "Use `fetch` to read a page before relying on it.",
}


async def _system_prompt(boot: Any, manifest: Any) -> str:
    async with boot([ScriptedTurn(content="ok")], manifests={"e2e-guided": manifest}) as app:
        resp = await app.client.post(
            "/chat", json={"manifest": "e2e-guided", "messages": [{"role": "user", "content": "hi"}]}
        )
        assert resp.status_code == 200, resp.text
        [prompt] = app.spy.prompts
    return str(next(m.content for m in prompt if m.role == "system"))


async def test_guidance_reaches_the_prompt_for_bound_tools_only(boot: Any) -> None:
    system = await _system_prompt(boot, _manifest(tool_guidance=GUIDANCE))
    assert "Tool guidance:" in system
    assert GUIDANCE["calculator"] in system
    assert "fetch" not in system, "no guidance for a tool that is not bound"


async def test_the_section_can_be_turned_off(boot: Any) -> None:
    manifest = _manifest(
        tool_guidance=GUIDANCE,
        system_prompt={"inline": "Be brief.", "include_tool_guidance": False},
    )
    system = await _system_prompt(boot, manifest)
    assert "Tool guidance:" not in system and GUIDANCE["calculator"] not in system
