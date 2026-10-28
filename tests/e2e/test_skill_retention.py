"""An activated skill stays in front of the model after the window drops the turn that activated it.

Through the real app: the builder composes `SkillRetentionStrategy` once the catalog is loaded, the
pattern logs the activation, and a later turn's render -- windowed to two events -- puts the logged
result back ahead of the history, exactly once, as a non-transient message.
"""

from __future__ import annotations

from typing import Any

from felix.manifests.loader import parse_manifest
from felix_ai.providers.scripted import ScriptedTurn
from felix_ai.types import ToolCall

SKILL = "calculator-help"


def _manifest() -> Any:
    return parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "e2e-retain"},
            "spec": {
                "pattern": "react",
                "skills": [{"name": SKILL}],
                "session": {"strategy": "windowed:2"},
                "auth": {"inbound": {"allow_anonymous": True}},
            },
        }
    )


async def _chat(app: Any, text: str) -> None:
    response = await app.client.post(
        "/chat",
        json={
            "manifest": "e2e-retain",
            "thread_id": "e2e-retain-1",
            "messages": [{"role": "user", "content": text}],
        },
    )
    assert response.status_code == 200, response.text


def _retained(prompt: list[Any]) -> list[int]:
    return [
        i for i, m in enumerate(prompt) if str(getattr(m, "content", "") or "").startswith("<retained_skill")
    ]


async def test_an_activation_the_window_dropped_is_put_back_once(boot: Any) -> None:
    script = [
        ScriptedTurn(tool_calls=[ToolCall(id="c1", name="activate_skill", args={"name": SKILL})]),
        ScriptedTurn(content="activated"),
        ScriptedTurn(content="two"),
        ScriptedTurn(content="three"),
        ScriptedTurn(content="four"),
    ]
    async with boot(script, manifests={"e2e-retain": _manifest()}) as app:
        await _chat(app, "Use the calculator skill.")
        assert _retained(app.spy.prompts[1]) == [], "the activation is still in view"
        for text in ("And then?", "More?", "Last one."):
            await _chat(app, text)

        last = app.spy.prompts[-1]
        [at] = _retained(last)
        assert f'name="{SKILL}"' in str(last[at].content)
        assert not getattr(last[at], "transient", False)
        assert all(getattr(m, "role", "") == "system" for m in last[:at]), "ahead of the history"
