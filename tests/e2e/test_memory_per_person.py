"""Long-term memory per person, through the real app: `spec.memory.scope` reaching the runtime.

Two people in one tenant, each with an API key of their own, so each is a person key
`api_key|<sub>` (`felix/auth/persons.py`). What one remembers -- through the `remember` tool, or
captured from their turn, live, in a durable run, or in a job they wrote -- reaches their next
session's prompt and never the other person's. A third key is the first person signing in another
way, linked to them by `FELIX_PRINCIPAL_ALIASES`. And `scope: agent` keeps the old behaviour: one
pool for the agent, whoever is talking. The compile resolves the pool once and hands it to the
prelude, the tools and capture, so each of the three is asserted here rather than only the
function behind it.
"""

from __future__ import annotations

import json
from typing import Any

from felix.manifests.schema import Manifest
from felix_ai.providers.scripted import ScriptedTurn
from felix_ai.types import ToolCall

from tests.support.e2e import DEFAULT_ROUTE

ALICE_KEY = "api_key|github:1"
BOB_KEY = "api_key|github:2"
KEYS = {
    "sk-alice-not-a-secret": {"tenant_id": "default", "sub": "github:1", "scopes": ["admin"]},
    "sk-bob-not-a-secret": {"tenant_id": "default", "sub": "github:2", "scopes": ["admin"]},
    # Alice again, under another subject.
    "sk-alice-google-not-a-secret": {"tenant_id": "default", "sub": "google:9", "scopes": ["admin"]},
}
ENV = {
    "FELIX_AUTH_MODE": "api_key",
    "FELIX_AUTH_API_KEYS": json.dumps(KEYS),
    "FELIX_PRINCIPAL_ALIASES": json.dumps({"api_key|google:9": ALICE_KEY}),
}
ALICE = {"Authorization": "Bearer sk-alice-not-a-secret"}
BOB = {"Authorization": "Bearer sk-bob-not-a-secret"}
ALICE_GOOGLE = {"Authorization": "Bearer sk-alice-google-not-a-secret"}

FACT = "The user's timezone is Europe/Lisbon."
REMEMBER = ToolCall(id="call-1", name="remember", args={"content": FACT, "topic_key": "user.timezone"})
CAPTURED = "The user's favourite colour is green."
EXTRACTION = json.dumps([{"content": CAPTURED, "kind": "fact", "topic_key": "user.colour"}])


def _manifest(name: str, *, scope: str, capture: bool = False, durable: bool = False) -> Manifest:
    memory: dict[str, Any] = {"store": "memory", "scope": scope, "recall": {"tools": True}}
    if capture:
        # The scripted route, and no minimum: every turn runs one extraction call on the script.
        memory["capture"] = {"enabled": True, "model": DEFAULT_ROUTE, "min_chars": 0}
    spec: dict[str, Any] = {"pattern": "react", "memory": memory}
    if durable:
        spec["execution"] = {"mode": "durable"}
    return Manifest.model_validate(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": name}, "spec": spec}
    )


def _chat(manifest: str, thread: str, text: str) -> dict[str, Any]:
    return {"manifest": manifest, "thread_id": thread, "messages": [{"role": "user", "content": text}]}


def _shown(prompt: list[Any], text: str) -> bool:
    return any(text in str(getattr(m, "content", "") or "") for m in prompt)


async def _turn(app: Any, headers: dict[str, str], manifest: str, thread: str, *, calls: int) -> list[Any]:
    """One chat turn; `calls` is how many model calls it makes. Returns the reply call's prompt."""
    before = len(app.spy.prompts)
    resp = await app.client.post(
        "/chat", json=_chat(manifest, thread, "What do you know about me?"), headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert len(app.spy.prompts) == before + calls, "the turn made a different number of model calls"
    return app.spy.prompts[before]


async def test_a_remembered_fact_reaches_its_person_and_no_one_else(boot: Any) -> None:
    script = [
        ScriptedTurn(content="", tool_calls=[REMEMBER], stop_reason="tool_use"),
        ScriptedTurn(content="Noted."),
        ScriptedTurn(content="Bob's answer."),
        ScriptedTurn(content="Alice's answer."),
    ]
    manifests = {"e2e-person": _manifest("e2e-person", scope="person")}
    async with boot(script, env=ENV, manifests=manifests) as app:
        first = await app.client.post(
            "/chat", json=_chat("e2e-person", "a-1", "I live in Lisbon."), headers=ALICE
        )
        assert first.status_code == 200, first.text
        assert len(app.spy.prompts) == 2

        assert not _shown(await _turn(app, BOB, "e2e-person", "b-1", calls=1), FACT), (
            "Alice's fact reached Bob"
        )
        assert _shown(await _turn(app, ALICE_GOOGLE, "e2e-person", "a-2", calls=1), FACT), (
            "Alice, signed in through her alias, lost her own fact"
        )

        # The operator's view: the row is in Alice's pool, keyed by issuer and subject.
        listed = await app.client.get("/memory", params={"principal": ALICE_KEY}, headers=ALICE)
        assert [i["content"] for i in listed.json()["items"]] == [FACT], listed.text


async def test_a_captured_fact_lands_in_the_person_pool(boot: Any) -> None:
    script = [
        ScriptedTurn(content="Green, noted."),
        ScriptedTurn(content=EXTRACTION),
        ScriptedTurn(content="Bob's answer."),
        ScriptedTurn(content="[]"),
        ScriptedTurn(content="Alice's answer."),
        ScriptedTurn(content="[]"),
    ]
    manifests = {"e2e-capture": _manifest("e2e-capture", scope="person", capture=True)}
    async with boot(script, env=ENV, manifests=manifests) as app:
        await _turn(app, ALICE, "e2e-capture", "a-1", calls=2)
        assert not _shown(await _turn(app, BOB, "e2e-capture", "b-1", calls=2), CAPTURED), (
            "a fact captured from Alice reached Bob"
        )
        assert _shown(await _turn(app, ALICE, "e2e-capture", "a-2", calls=2), CAPTURED), (
            "the fact captured from Alice's turn did not reach her"
        )


async def test_a_durable_run_writes_the_starters_pool(boot: Any) -> None:
    """The fiber resumes as a machine actor on Alice's behalf: the person key it was recorded with
    is the one capture writes, so her next session -- on another of her agents -- has the fact."""
    from felix.durability.fibers import resume_due_fibers

    script = [
        ScriptedTurn(content="Green, noted."),
        ScriptedTurn(content=EXTRACTION),
        ScriptedTurn(content="Bob's answer."),
        ScriptedTurn(content="[]"),
        ScriptedTurn(content="Alice's answer."),
        ScriptedTurn(content="[]"),
    ]
    manifests = {
        "e2e-durable": _manifest("e2e-durable", scope="person", capture=True, durable=True),
        "e2e-capture": _manifest("e2e-capture", scope="person", capture=True),
    }
    async with boot(script, env=ENV, manifests=manifests) as app:
        queued = await app.client.post(
            "/chat", json=_chat("e2e-durable", "a-d", "I love green."), headers=ALICE
        )
        assert queued.status_code == 202, queued.text
        await resume_due_fibers(app.settings)
        assert len(app.spy.prompts) == 2, "the durable run did not reply and capture"

        assert not _shown(await _turn(app, BOB, "e2e-capture", "b-1", calls=2), CAPTURED)
        assert _shown(await _turn(app, ALICE, "e2e-capture", "a-1", calls=2), CAPTURED)


async def test_a_job_writes_its_writers_pool(boot: Any) -> None:
    script = [
        ScriptedTurn(content="Green, noted."),
        ScriptedTurn(content=EXTRACTION),
        ScriptedTurn(content="Bob's answer."),
        ScriptedTurn(content="[]"),
        ScriptedTurn(content="Alice's answer."),
        ScriptedTurn(content="[]"),
    ]
    manifests = {"e2e-capture": _manifest("e2e-capture", scope="person", capture=True)}
    async with boot(script, env=ENV, manifests=manifests) as app:
        put = await app.client.put(
            "/jobs/colour",
            json={"manifest_id": "e2e-capture", "payload": {"prompt": "I love green."}},
            headers=ALICE,
        )
        assert put.status_code == 200, put.text
        # Run by Bob: a manual run is still its writer's, never the asker's.
        ran = await app.client.post("/jobs/colour/run", headers=BOB)
        assert ran.status_code == 200 and ran.json()["status"] == "ok", ran.text
        assert len(app.spy.prompts) == 2, "the job did not reply and capture"

        assert not _shown(await _turn(app, BOB, "e2e-capture", "b-1", calls=2), CAPTURED)
        assert _shown(await _turn(app, ALICE, "e2e-capture", "a-1", calls=2), CAPTURED)


async def test_scope_agent_keeps_one_pool_for_everyone(boot: Any) -> None:
    script = [
        ScriptedTurn(content="", tool_calls=[REMEMBER], stop_reason="tool_use"),
        ScriptedTurn(content="Noted."),
        ScriptedTurn(content="Bob's answer."),
    ]
    manifests = {"e2e-agent": _manifest("e2e-agent", scope="agent")}
    async with boot(script, env=ENV, manifests=manifests) as app:
        first = await app.client.post(
            "/chat", json=_chat("e2e-agent", "a-1", "I live in Lisbon."), headers=ALICE
        )
        assert first.status_code == 200, first.text
        assert _shown(await _turn(app, BOB, "e2e-agent", "b-1", calls=1), FACT), (
            "scope: agent stopped sharing"
        )
