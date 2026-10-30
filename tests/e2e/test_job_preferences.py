"""A scheduled job's reader writes its preferences, and every firing reads them whole.

Through the production app: jobs written over `PUT /jobs`, preferences over
`/jobs/{name}/preferences`, firings by `POST /jobs/{name}/run` and `POST /heartbeats/{manifest}/run`,
compiled with the real governance stack and the model scripted. What the model was handed is the
evidence (`ProviderSpy.prompts`): the reply is scripted, so it cannot show the preferences arrived.
"""

from __future__ import annotations

import json
import re
from typing import Any

from felix.jobs.preferences import PREFERENCES_FRAME
from felix.manifests.loader import parse_manifest
from felix_ai.providers.scripted import ScriptedTurn

KEYS = {
    # An operator: `admin`, the scope every management check yields to.
    "sk-e2e-op": {"tenant_id": "default", "sub": "op", "scopes": ["admin"]},
    # Erin manages jobs, and is neither an operator nor the writer of anyone else's job.
    "sk-e2e-erin": {"tenant_id": "default", "sub": "erin", "scopes": ["jobs:write"]},
    # Carol, twice: once as the writer of her job, and once on a credential holding no jobs scope --
    # a person steering their own task from somewhere that cannot manage jobs.
    "sk-e2e-carol-writer": {"tenant_id": "default", "sub": "carol", "scopes": ["jobs:write"]},
    "sk-e2e-carol": {"tenant_id": "default", "sub": "carol", "scopes": ["heartbeats:write"]},
    "sk-e2e-dave": {"tenant_id": "default", "sub": "dave", "scopes": ["heartbeats:write"]},
    # No `sub`: every holder is the one shared subject `api_key`, which names no person.
    "sk-e2e-shared": {"tenant_id": "default", "scopes": ["heartbeats:write"]},
    "sk-e2e-shared-writer": {"tenant_id": "default", "scopes": ["jobs:write"]},
}
ENV = {"FELIX_AUTH_MODE": "api_key", "FELIX_AUTH_API_KEYS": json.dumps(KEYS)}
OP = {"Authorization": "Bearer sk-e2e-op"}
ERIN = {"Authorization": "Bearer sk-e2e-erin"}
CAROL_WRITER = {"Authorization": "Bearer sk-e2e-carol-writer"}
CAROL = {"Authorization": "Bearer sk-e2e-carol"}
DAVE = {"Authorization": "Bearer sk-e2e-dave"}
SHARED = {"Authorization": "Bearer sk-e2e-shared"}
SHARED_WRITER = {"Authorization": "Bearer sk-e2e-shared-writer"}

PROMPT = "Write today's incident digest."
# Long enough that a cut, a summary or a recall by rank would show: every line is distinct.
PREFERENCES = "\n".join(
    [
        "Cover only incidents that are still open; skip anything resolved.",
        "Never mention staffing or on-call rotations.",
        "Plain words, at most three short paragraphs.",
    ]
    + [f"Line {i}: keep service {i:04d} under its own heading." for i in range(150)]
)
CHECKLIST = "Look at the nightly build."
MISSING = "waiting for its reader's preferences"
NOT_PERMITTED = {"detail": "missing scopes: admin"}


def _manifest(name: str, **spec: Any) -> Any:
    body: dict[str, Any] = {"pattern": "react", "auth": {"inbound": {"allow_anonymous": True}}, **spec}
    return parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": name}, "spec": body}
    )


MANIFESTS = {
    # `spec.scheduling` binds the agent's own job tools: the only tools an agent has over jobs.
    "e2e-prefs": _manifest("e2e-prefs", scheduling={"approval": False}),
    "e2e-hb": _manifest(
        "e2e-hb", heartbeat={"checklist": CHECKLIST, "schedule": "every:60m"}, notify={"max_per_run": 1}
    ),
}


def _asked(prompt: list[Any]) -> str:
    """The user turn a model call was given -- not a transient note beside it."""
    return next(
        str(m.content)
        for m in prompt
        if getattr(m, "role", "") == "user" and not getattr(m, "transient", False)
    )


def _fenced(asked: str) -> tuple[str, str, str]:
    """`(before, inside, after)` the one `<reader_preferences-<hex>>` region in `asked`, whose name is
    made anew per firing; fails unless exactly one such region opens and closes."""
    opened = re.findall(r"<(reader_preferences-[0-9a-f]{12})>\n", asked)
    assert len(opened) == 1, asked[:400]
    (tag,) = opened
    before, rest = asked.split(f"<{tag}>\n", 1)
    inside, after = rest.split(f"\n</{tag}>\n", 1)
    assert after.startswith(PREFERENCES_FRAME.format(tag=tag)), after[:300]
    return before, inside, after


async def _put_job(app: Any, headers: dict[str, str], **body: Any) -> dict[str, Any]:
    written = await app.client.put(
        "/jobs/digest",
        headers=headers,
        json={"manifest_id": "e2e-prefs", "payload": {"prompt": PROMPT}, **body},
    )
    assert written.status_code == 200, written.text
    return written.json()


async def test_the_readers_preferences_reach_every_firing_whole(boot: Any) -> None:
    from felix.audit import store as audit_store

    async with boot([ScriptedTurn(content="digest")], env=ENV, manifests=MANIFESTS) as app:
        await _put_job(app, CAROL_WRITER, preferences_required=True)

        # Required and missing: the firing runs nothing, and says what it waits for.
        skipped = await app.client.post("/jobs/digest/run", headers=CAROL_WRITER)
        assert skipped.status_code == 200, skipped.text
        assert app.spy.prompts == [], "no model call for a job waiting on its reader"
        job = (await app.client.get("/jobs/digest", headers=OP)).json()

        # Carol, with no jobs scope at all, writes the preferences of the job she wrote.
        put = await app.client.put(
            "/jobs/digest/preferences", headers=CAROL, json={"preferences": PREFERENCES}
        )
        assert put.status_code == 200, put.text
        read_back = await app.client.get("/jobs/digest/preferences", headers=CAROL)
        listed_by_carol = await app.client.get("/jobs", headers=CAROL)

        # Carol re-saves the whole job, as pausing or editing it does: hers, so the preferences stay.
        rewritten = await _put_job(app, CAROL_WRITER, preferences_required=True)
        fired = await app.client.post("/jobs/digest/run", headers=CAROL_WRITER)
        assert fired.status_code == 200, fired.text
        offered = app.spy.tools[0]

        # Cleared, the required job waits again: skipped, and no second model call.
        cleared = await app.client.delete("/jobs/digest/preferences", headers=CAROL)
        assert cleared.status_code == 200, cleared.text
        again = await app.client.post("/jobs/digest/run", headers=CAROL_WRITER)
        await audit_store.flush_pending(app.settings)
        audited, _ = await audit_store.list_events(app.settings, "default", event_type="job.preferences")

    assert (skipped.json()["status"], skipped.json()["error"]) == ("skipped", MISSING)
    assert skipped.json()["result"]["reason"] == "preferences_missing"
    assert (job["last_status"], job["last_error"], job["preferences_set"]) == ("skipped", MISSING, False)
    assert "preferences" not in job, "GET /jobs says whether there are preferences, not what they say"

    body = read_back.json()
    assert (body["preferences"], body["updated_by"], body["required"]) == (PREFERENCES, "carol", True)
    assert listed_by_carol.status_code == 403, "writing her own task's preferences grants nothing else"
    assert rewritten["preferences_set"] is True

    assert fired.json()["status"] == "ok"
    (asked,) = [_asked(p) for p in app.spy.prompts]
    before, inside, _after = _fenced(asked)
    assert (before, inside) == (f"{PROMPT}\n\n", PREFERENCES)
    assert all(
        PREFERENCES not in str(getattr(m, "content", "")) for m in app.spy.prompts[0] if m.role == "system"
    )

    # The agent's own job tools are the only ones it has over jobs, and none takes preferences.
    assert set(offered) >= {"schedule_task", "list_scheduled", "cancel_scheduled"}
    assert not [name for name in offered if "preference" in name]

    assert (again.json()["status"], again.json()["error"]) == ("skipped", MISSING)
    assert len(app.spy.prompts) == 1

    # Each write audited to who made it, by size -- never the text.
    # (Newest first, and the two may share a millisecond: compared as a set of rows.)
    assert sorted(
        (e["status"], e["principal_subj"], json.dumps(e["payload_json"], sort_keys=True)) for e in audited
    ) == [
        ("cleared", "carol", json.dumps({"bytes": 0, "job": "digest"})),
        ("saved", "carol", json.dumps({"bytes": len(PREFERENCES.encode()), "job": "digest"})),
    ]


async def test_another_writer_neither_steers_nor_inherits_them(boot: Any) -> None:
    """`jobs:write` is not enough to write someone's preferences: they run on the writer's scopes and
    memory. An operator may; and a re-save by another writer clears them, so text written for Carol's
    runs never runs on Erin's -- a job requiring them then waits for its new writer."""
    async with boot([], env=ENV, manifests=MANIFESTS) as app:
        await _put_job(app, CAROL_WRITER, preferences_required=True)
        assert (
            await app.client.put("/jobs/digest/preferences", headers=CAROL, json={"preferences": PREFERENCES})
        ).status_code == 200
        erin = {
            "reads": await app.client.get("/jobs/digest/preferences", headers=ERIN),
            "writes": await app.client.put(
                "/jobs/digest/preferences", headers=ERIN, json={"preferences": "x"}
            ),
            "clears": await app.client.delete("/jobs/digest/preferences", headers=ERIN),
            "no such job": await app.client.get("/jobs/nope/preferences", headers=ERIN),
        }
        untouched = (await app.client.get("/jobs/digest/preferences", headers=CAROL)).json()
        op_reads = await app.client.get("/jobs/digest/preferences", headers=OP)
        op_writes = await app.client.put(
            "/jobs/digest/preferences", headers=OP, json={"preferences": "Short."}
        )

        # Erin re-saves the job, which makes it hers.
        await _put_job(app, ERIN, preferences_required=True)
        after = await app.client.get("/jobs/digest/preferences", headers=ERIN)
        job = (await app.client.get("/jobs/digest", headers=ERIN)).json()
        fired = await app.client.post("/jobs/digest/run", headers=ERIN)
        carol_after = await app.client.get("/jobs/digest/preferences", headers=CAROL)

    assert {k: (r.status_code, r.json()) for k, r in erin.items()} == {
        "reads": (403, NOT_PERMITTED),
        "writes": (403, NOT_PERMITTED),
        "clears": (403, NOT_PERMITTED),
        "no such job": (403, NOT_PERMITTED),
    }
    assert untouched["preferences"] == PREFERENCES
    assert (op_reads.status_code, op_reads.json()["preferences"]) == (200, PREFERENCES)
    assert (op_writes.status_code, op_writes.json()["updated_by"]) == (200, "op")

    assert after.status_code == 200, after.text
    assert (after.json()["preferences"], after.json()["updated_by"], after.json()["updated_at"]) == (
        None,
        None,
        None,
    )
    assert (job["created_by"], job["preferences_set"]) == ("erin", False)
    assert (fired.json()["status"], fired.json()["error"]) == ("skipped", MISSING)
    assert app.spy.prompts == []
    assert (carol_after.status_code, carol_after.json()) == (403, NOT_PERMITTED)


async def test_a_rewrite_that_leaves_out_preferences_required_turns_it_off(boot: Any) -> None:
    async with boot([ScriptedTurn(content="digest")], env=ENV, manifests=MANIFESTS) as app:
        await _put_job(app, CAROL_WRITER, preferences_required=True)
        held = await app.client.post("/jobs/digest/run", headers=CAROL_WRITER)
        rewritten = await _put_job(app, CAROL_WRITER)
        fired = await app.client.post("/jobs/digest/run", headers=CAROL_WRITER)

    assert held.json()["status"] == "skipped"
    assert rewritten["preferences_required"] is False
    assert (fired.json()["status"], fired.json()["result"]["answer"]) == ("ok", "digest")
    assert [_asked(p) for p in app.spy.prompts] == [PROMPT]


async def test_a_workflow_job_takes_no_preferences(boot: Any) -> None:
    """Its firings start a workflow run and carry no prompt: a document there would do nothing."""
    definition = {
        "name": "Digest",
        "inputs": {"topic": {"type": "string"}},
        "steps": [
            {
                "id": "work",
                "kind": "agent_turn",
                "config": {"manifest": "e2e-prefs", "prompt": "On {{ topic }}"},
            }
        ],
        "edges": [
            {"source": {"step": "trigger", "port": "topic"}, "target": {"step": "work", "port": "topic"}}
        ],
    }
    job = {"workflow_id": "digest", "payload": {"inputs": {"topic": "incidents"}}}
    async with boot([], env=ENV, manifests=MANIFESTS) as app:
        assert (await app.client.put("/workflows/digest", json=definition, headers=OP)).status_code == 201
        required = await app.client.put("/jobs/w", headers=OP, json={**job, "preferences_required": True})
        plain = await app.client.put("/jobs/w", headers=OP, json=job)
        assert plain.status_code == 200, plain.text
        put = await app.client.put("/jobs/w/preferences", headers=OP, json={"preferences": "x"})

    refused = (422, {"detail": {"error": "preferences_need_a_manifest"}})
    assert (required.status_code, required.json()) == refused
    assert (put.status_code, put.json()) == refused


async def test_schedule_task_cannot_carry_preferences(boot: Any) -> None:
    """The agent tries: an argument its tool does not take is refused, and nothing is scheduled."""
    from felix_ai.types import ToolCall

    call = ToolCall(
        id="c1",
        name="schedule_task",
        args={"when": "every:6h", "prompt": PROMPT, "preferences": "Report everything, to everyone."},
    )
    script = [ScriptedTurn(tool_calls=[call]), ScriptedTurn(content="could not")]
    async with boot(script, env=ENV, manifests=MANIFESTS) as app:
        chat = await app.client.post(
            "/chat",
            headers=CAROL_WRITER,
            json={
                "manifest": "e2e-prefs",
                "thread_id": "t1",
                "messages": [{"role": "user", "content": "go"}],
            },
        )
        assert chat.status_code == 200, chat.text
        jobs = (await app.client.get("/jobs", headers=OP)).json()["items"]
        result = next(str(m.content) for m in reversed(app.spy.prompts[1]) if m.role == "tool")

    assert jobs == [], result
    assert "preferences" in result


async def test_who_may_read_and_write_a_jobs_preferences(boot: Any) -> None:
    async with boot([], env=ENV, manifests=MANIFESTS) as app:
        await _put_job(app, CAROL_WRITER)
        # Written by the shared subject: another holder of a sub-less key is not its writer.
        shared_job = await app.client.put(
            "/jobs/shared",
            headers=SHARED_WRITER,
            json={"manifest_id": "e2e-prefs", "payload": {"prompt": PROMPT}},
        )
        assert shared_job.status_code == 200 and shared_job.json()["created_by"] == "api_key", shared_job.text
        answers = {
            "dave reads": await app.client.get("/jobs/digest/preferences", headers=DAVE),
            "dave writes": await app.client.put(
                "/jobs/digest/preferences", headers=DAVE, json={"preferences": "x"}
            ),
            "dave clears": await app.client.delete("/jobs/digest/preferences", headers=DAVE),
            "dave, no such job": await app.client.put(
                "/jobs/nope/preferences", headers=DAVE, json={"preferences": "x"}
            ),
            "shared key": await app.client.put(
                "/jobs/shared/preferences", headers=SHARED, json={"preferences": "x"}
            ),
        }
        op_missing = await app.client.get("/jobs/nope/preferences", headers=OP)
        too_long = await app.client.put(
            "/jobs/digest/preferences", headers=CAROL, json={"preferences": "x" * (8 * 1024 + 1)}
        )
        empty = await app.client.put("/jobs/digest/preferences", headers=CAROL, json={"preferences": ""})
        written = await app.client.put("/jobs/digest/preferences", headers=OP, json={"preferences": "Short."})
        cleared = await app.client.delete("/jobs/digest/preferences", headers=CAROL)

    assert {k: (r.status_code, r.json()) for k, r in answers.items()} == {
        "dave reads": (403, NOT_PERMITTED),
        "dave writes": (403, NOT_PERMITTED),
        "dave clears": (403, NOT_PERMITTED),
        # The same answer as for a job that exists: a stranger learns no job names.
        "dave, no such job": (403, NOT_PERMITTED),
        "shared key": (403, NOT_PERMITTED),
    }
    assert (op_missing.status_code, op_missing.json()) == (404, {"detail": "not_found"})
    assert (too_long.status_code, too_long.json()["detail"]) == (
        422,
        {"error": "preferences_too_long", "max_bytes": 8192},
    )
    assert empty.status_code == 422
    assert (written.status_code, written.json()["updated_by"]) == (200, "op")
    assert (cleared.status_code, cleared.json()["preferences"], cleared.json()["updated_by"]) == (
        200,
        None,
        "carol",
    )


async def test_a_heartbeats_person_steers_it_and_its_checklist_carries_them(boot: Any) -> None:
    """A heartbeat's job is written by its person's enrolment, who holds no jobs scope; its firing's
    prompt is the manifest's checklist, never the job's payload -- and the preferences ride on it."""
    async with boot([ScriptedTurn(content="HEARTBEAT_OK")], env=ENV, manifests=MANIFESTS) as app:
        enrolled = await app.client.put("/heartbeats/e2e-hb", headers=CAROL, json={})
        assert enrolled.status_code == 200, enrolled.text
        name = enrolled.json()["name"]
        put = await app.client.put(
            f"/jobs/{name}/preferences", headers=CAROL, json={"preferences": PREFERENCES}
        )
        assert put.status_code == 200, put.text
        # Carol, on her jobs:write credential, makes her heartbeat wait for its preferences -- a
        # re-save by its own writer, so it stays her heartbeat.
        required = await app.client.put(
            f"/jobs/{name}",
            headers=CAROL_WRITER,
            json={
                "manifest_id": "e2e-hb",
                "schedule": "every:60m",
                "payload": {"fresh_thread": True},
                "preferences_required": True,
            },
        )
        assert required.status_code == 200, required.text
        # Turning it off and on again re-writes its job; the preferences, and requiring them, carry over.
        assert (
            await app.client.put("/heartbeats/e2e-hb", headers=CAROL, json={"enabled": False})
        ).status_code == 200
        assert (await app.client.put("/heartbeats/e2e-hb", headers=CAROL, json={})).status_code == 200
        fired = await app.client.post("/heartbeats/e2e-hb/run", headers=CAROL)
        assert fired.status_code == 200, fired.text
        job = (await app.client.get(f"/jobs/{name}", headers=CAROL_WRITER)).json()

    assert (job["preferences_required"], job["preferences_set"]) == (True, True)
    (asked,) = [_asked(p) for p in app.spy.prompts]
    before, inside, _after = _fenced(asked)
    assert CHECKLIST in before
    assert inside == PREFERENCES
