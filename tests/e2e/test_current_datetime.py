"""The model is told today's date and the current time.

Without them it answers date arithmetic against the year its training data ends in: asked a
person's age from a stored birthdate, it said 44 "as of 2024" in 2026. Read here off the prompts
the model was sent: the date at the end of the system prompt on every call of a run, and the
time — whose offset carries `FELIX_TIMEZONE` — at the tail of the first call only, so a value
that changes every run never sits in the cached prefix. Neither is persisted, and neither is a
user message on any call after the first, where tool selection would read it as the request.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from felix.clock import DATE_TAG, TIME_NOTE_PREFIX
from felix.manifests.loader import parse_manifest
from felix_ai.providers.scripted import ScriptedTurn
from felix_ai.types import ToolCall

from tests.support.e2e import sent_turns, without_date

# UTC+14 all year, so the offset proves the setting was read rather than the host's zone.
ZONE = "Pacific/Kiritimati"
CALC = ToolCall(id="call-1", name="calculator", args={"expression": "2+2"})


def _manifest(**system_prompt: Any) -> Any:
    spec = {
        "pattern": "react",
        "tools": ["calculator"],
        "auth": {"inbound": {"allow_anonymous": True}},
        "system_prompt": {"inline": "Be brief.", **system_prompt},
    }
    return parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": "e2e-clock"}, "spec": spec}
    )


async def _prompts(boot: Any, manifest: Any) -> list[list[Any]]:
    script = [ScriptedTurn(content="", tool_calls=[CALC], stop_reason="tool_use"), ScriptedTurn(content="4")]
    async with boot(script, env={"FELIX_TIMEZONE": ZONE}, manifests={"e2e-clock": manifest}) as app:
        resp = await app.client.post(
            "/chat",
            json={"manifest": "e2e-clock", "messages": [{"role": "user", "content": "how old am I?"}]},
        )
        assert resp.status_code == 200, resp.text
        return app.spy.prompts


def _today() -> set[str]:
    return {datetime.now(ZoneInfo(ZONE)).strftime("%Y-%m-%d")}


async def test_every_call_carries_the_date_and_the_first_the_time(boot: Any) -> None:
    before = _today()
    first, second = await _prompts(boot, _manifest())
    days = before | _today()  # a run that straddles midnight may name either day

    for prompt in (first, second):
        system = str(prompt[0].content)
        assert prompt[0].role == "system"
        assert without_date(system) == "Be brief.", "the date ends the system prompt"
        assert ZONE in system and any(day in system for day in days), system
        assert not any(DATE_TAG in str(m.content) for m in prompt[1:]), "nowhere but `system`"

    [note] = [m for m in first if str(m.content).startswith(TIME_NOTE_PREFIX)]
    assert first[-1] is note and note.transient, "the time is the tail of the first call"
    assert "+14:00" in str(note.content) and any(day in str(note.content) for day in days)
    assert not any(TIME_NOTE_PREFIX in str(m.content) for m in second), "first call only"


async def test_a_manifest_can_opt_out(boot: Any) -> None:
    first, second = await _prompts(boot, _manifest(include_datetime=False))
    for prompt in (first, second):
        assert str(prompt[0].content) == "Be brief."
        assert not any(TIME_NOTE_PREFIX in str(m.content) for m in prompt)


async def test_neither_reaches_the_session_log(boot: Any) -> None:
    """The next turn renders the first from the log: it carries its own time, and no earlier one."""
    body = {"manifest": "e2e-clock", "thread_id": "clock-thread"}
    script = [ScriptedTurn(content="first"), ScriptedTurn(content="second")]
    async with boot(script, env={"FELIX_TIMEZONE": ZONE}, manifests={"e2e-clock": _manifest()}) as app:
        for text in ("one", "two"):
            resp = await app.client.post(
                "/chat", json={**body, "messages": [{"role": "user", "content": text}]}
            )
            assert resp.status_code == 200, resp.text
        _, second = app.spy.prompts
    assert [str(m.content) for m in sent_turns(second, ("user", "assistant"))] == ["one", "first", "two"]
    assert sum(TIME_NOTE_PREFIX in str(m.content) for m in second) == 1
