"""`POST /chat/ask`: one question about a thread, answered from it, and the thread untouched (#404).

Over the wire against a thread a real turn created, so what the question is answered from is the
session log the product writes. The assertions are on state: the snapshot after an ask is
byte-identical to the one before, a thread leased to someone else can still be asked about, and
the answer was drawn from the thread — the model was handed its history, not just the question.
"""

from __future__ import annotations

from typing import Any

from felix.flush import flush_all
from felix_ai.providers.scripted import ScriptedTurn

from tests.e2e.conftest import Booted

THREAD = "e2e-ask"


async def _seed(app: Booted) -> None:
    resp = await app.client.post(
        "/chat",
        json={
            "manifest": "quick",
            "thread_id": THREAD,
            "messages": [{"role": "user", "content": "remember the zucchini"}],
        },
    )
    assert resp.status_code == 200, resp.text


async def _ask(app: Booted, question: str = "what should I remember?") -> Any:
    return await app.client.post(
        "/chat/ask", json={"thread_id": THREAD, "manifest": "quick", "question": question}
    )


async def test_an_ask_answers_from_the_thread_and_leaves_it_byte_identical(boot: Any) -> None:
    async with boot([ScriptedTurn(content="noted"), ScriptedTurn(content="The zucchini.")]) as app:
        await _seed(app)
        before = await app.client.get(f"/chat/sessions/{THREAD}")

        resp = await _ask(app)
        after = await app.client.get(f"/chat/sessions/{THREAD}")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "answered" and body["answer"] == "The zucchini.", body
    assert body["thread_id"].endswith(THREAD)
    assert after.content == before.content, "an ask adds nothing to the thread"
    asked = [str(m.content) for m in app.spy.prompts[-1]]
    assert any("remember the zucchini" in text for text in asked), "answered from the thread's history"
    assert app.spy.tools[-1] == [], "no tools are bound to a side question"


async def test_a_thread_leased_to_another_holder_can_still_be_asked_about(boot: Any) -> None:
    async with boot([ScriptedTurn(content="noted"), ScriptedTurn(content="The zucchini.")]) as app:
        await _seed(app)
        lease = await app.client.post(
            "/chat/sessions/lease",
            json={"thread_id": THREAD, "holder_id": "someone-else", "mode": "exclusive"},
        )
        assert lease.status_code == 200, lease.text

        resp = await _ask(app)

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "answered"


async def test_a_question_the_thread_cannot_answer_reads_not_in_context(boot: Any) -> None:
    async with boot([ScriptedTurn(content="noted"), ScriptedTurn(content="NOT_IN_CONTEXT")]) as app:
        await _seed(app)
        resp = await _ask(app, "what is the user's birthday?")

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "not_in_context"
    assert resp.json()["answer"] == ""


async def test_an_ask_is_metered_under_the_threads_manifest(boot: Any) -> None:
    async with boot([ScriptedTurn(content="noted"), ScriptedTurn(content="The zucchini.")]) as app:
        await _seed(app)
        await flush_all(app.settings)
        before = (await app.client.get("/usage", params={"manifest_id": "quick"})).json()["items"]

        resp = await _ask(app)
        await flush_all(app.settings)
        after = (await app.client.get("/usage", params={"manifest_id": "quick"})).json()["items"]

    assert resp.status_code == 200, resp.text
    assert len(after) == len(before) + 1, (before, after)
    assert resp.json()["usage"], "the response carries the call's usage block"


async def test_an_unknown_manifest_and_a_bad_thread_are_refused(boot: Any) -> None:
    async with boot([]) as app:
        unknown = await app.client.post(
            "/chat/ask", json={"thread_id": THREAD, "manifest": "no-such-manifest", "question": "q"}
        )
        bad = await app.client.post(
            "/chat/ask", json={"thread_id": "a:b:c", "manifest": "quick", "question": "q"}
        )

    assert unknown.status_code == 404 and unknown.json()["detail"] == "unknown_manifest:no-such-manifest"
    assert bad.status_code == 400 and bad.json()["detail"] == "invalid_thread_id"
