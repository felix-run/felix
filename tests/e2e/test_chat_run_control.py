"""The controls a client uses to steer a run that is already going.

Eight endpoints — steer, abort, continue, fork, rewind, compact, thinking, ui — and until now
none of them had a test. Several are exercised only by `.github/workflows/smoke.yml` against
production every six hours, and that workflow asserts status codes.

These are the endpoints where "returned 200" is least like "did the thing": abort that does not
stop the run, a fork that shares the parent's log instead of copying it, a rewind that reports
success and moves nothing. So each test reads the state back — from the snapshot, from the
forked thread's own log — rather than trusting the acknowledgement.
"""

from __future__ import annotations

from typing import Any

from felix_ai.providers.scripted import ScriptedTurn

from tests.e2e.conftest import Booted


def _answer(text: str = "noted") -> ScriptedTurn:
    return ScriptedTurn(content=text)


async def _seed(app: Booted, thread: str, text: str = "hello") -> dict[str, Any]:
    resp = await app.client.post(
        "/chat",
        json={"manifest": "quick", "thread_id": thread, "messages": [{"role": "user", "content": text}]},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _snapshot(app: Booted, thread: str) -> dict[str, Any]:
    resp = await app.client.get(f"/chat/sessions/{thread}")
    assert resp.status_code == 200, resp.text
    return resp.json()


# --- thinking ------------------------------------------------------------------------------


async def test_the_thinking_level_reaches_the_next_turns_model_spec(boot: Any) -> None:
    """The level is written twice and read from two different places, and only one matters.

    The snapshot resolves it from a `thinking_level_change` event — that is the rendering. The
    next turn resolves it from thread metadata and turns it into a thinking budget on the spec
    handed to the provider. Dropping the metadata write leaves a thread that *displays* "high"
    and *runs* with thinking off, so the snapshot alone is not enough: the budget on the spec
    is the thing a user is paying for.
    """
    thread = "e2e-thinking"
    async with boot([_answer(), _answer("after")]) as app:
        await _seed(app, thread)
        assert (await _snapshot(app, thread))["thinkingLevel"] == "off"
        assert not getattr(app.spy.specs[-1], "thinking_budget", None)

        set_level = await app.client.post(
            "/chat/thinking", json={"thread_id": thread, "thinking_level": "high"}
        )
        assert set_level.status_code == 200, set_level.text
        assert (await _snapshot(app, thread))["thinkingLevel"] == "high"

        await _seed(app, thread, "and now?")
        spec = app.spy.specs[-1]
        assert getattr(spec, "thinking_level", None) == "high", spec
        assert getattr(spec, "thinking_budget", 0) > 0, spec


async def test_an_unknown_thinking_level_is_refused(boot: Any) -> None:
    """The field is a Literal, so this is the schema refusing before the handler runs."""
    thread = "e2e-thinking-bad"
    async with boot([_answer()]) as app:
        await _seed(app, thread)
        resp = await app.client.post(
            "/chat/thinking", json={"thread_id": thread, "thinking_level": "enormous"}
        )
        assert resp.status_code == 422, resp.text


# --- steering ------------------------------------------------------------------------------


async def test_a_steer_is_queued_and_visible_on_the_snapshot(boot: Any) -> None:
    """A steer the run never sees is the failure mode; the queue is where it waits."""
    thread = "e2e-steer"
    async with boot([_answer()]) as app:
        await _seed(app, thread)
        assert (await _snapshot(app, thread))["queuedSteerCount"] == 0

        steered = await app.client.post(
            "/chat/steer", json={"thread_id": thread, "text": "actually, use metric", "kind": "steer"}
        )
        assert steered.status_code == 200, steered.text

        snap = await _snapshot(app, thread)
        assert snap["queuedSteerCount"] == 1, snap
        # The snapshot reports that something is queued without echoing it: the text is a
        # pending instruction to the model, not thread content, and only the run consumes it.
        assert snap["queuedSteer"] == [{"placeholder": True}], snap["queuedSteer"]


async def test_a_follow_up_is_delivered_to_the_model(boot: Any) -> None:
    """`kind: follow_up` is the idle path, and delivery is the whole point of queuing.

    Asserted on the messages the model was handed rather than on the reply: the reply is
    scripted, so a run that never saw the follow-up answers identically. The count going to
    zero proves nothing either — see the test below, where it goes to zero on the way to
    nowhere.
    """
    thread = "e2e-follow-up"
    async with boot([_answer(), _answer("understood"), _answer("done")]) as app:
        await _seed(app, thread)
        queued = await app.client.post(
            "/chat/steer",
            json={"thread_id": thread, "text": "prefer metric units", "kind": "follow_up"},
        )
        assert queued.status_code == 200, queued.text

        await _seed(app, thread, "carry on")

        assert any("prefer metric units" in text for text in app.spy.texts_seen()), app.spy.texts_seen()


async def test_a_steer_queued_while_idle_is_dropped_without_reaching_anyone(boot: Any) -> None:
    """Pins a wart rather than a guarantee, so that changing it is a deliberate act.

    `POST /chat/steer` with the default `kind: steer` on an idle thread answers 200 with
    `{"queued": "steer"}`, and the snapshot then reports one queued item. The next turn clears
    the count — and the text reaches neither the model nor the transcript. From the client's
    side that is indistinguishable from delivery: it was accepted, it was counted, and then it
    was gone.

    A steer is meant to interrupt tools in a run that is already going, so having nothing to
    interrupt is arguably the caller's mistake — but nothing tells them, and `follow_up` is the
    kind that would have worked. If this is ever made to error, redirect, or hold the message
    until a run starts, this test should fail and be rewritten.
    """
    thread = "e2e-steer-dropped"
    async with boot([_answer(), _answer("understood")]) as app:
        await _seed(app, thread)
        queued = await app.client.post(
            "/chat/steer", json={"thread_id": thread, "text": "prefer metric units", "kind": "steer"}
        )
        assert queued.status_code == 200
        assert queued.json()["queued"] == "steer"
        assert (await _snapshot(app, thread))["queuedSteerCount"] == 1

        await _seed(app, thread, "carry on")

        assert (await _snapshot(app, thread))["queuedSteerCount"] == 0
        # Prove the instrument is live before asserting an absence through it: a
        # `texts_seen()` that silently stopped recording would satisfy the next line.
        assert any("carry on" in t for t in app.spy.texts_seen()), app.spy.texts_seen()
        assert not any("prefer metric units" in t for t in app.spy.texts_seen()), app.spy.texts_seen()
        transcript = (await _snapshot(app, thread))["transcript"]
        assert not any("prefer metric units" in str(e.get("content") or "") for e in transcript)


# --- abort and continue --------------------------------------------------------------------


async def test_abort_marks_the_thread_aborted(boot: Any) -> None:
    """`phase` is what a reattaching client reads to know the run stopped."""
    thread = "e2e-abort"
    async with boot([_answer()]) as app:
        await _seed(app, thread)

        aborted = await app.client.post("/chat/abort", json={"thread_id": thread})
        assert aborted.status_code == 200, aborted.text
        assert aborted.json()["snapshot"]["phase"] == "aborted", aborted.json()

        assert (await _snapshot(app, thread))["phase"] == "aborted"

        # `phase` is what a client renders; the flag is what the running loop checks each step.
        # A `request_abort` that returned the right shape and set nothing would stop the UI and
        # not the run, so both are asserted.
        from felix.steer import is_aborted

        assert await is_aborted("default", f"default:{thread}") is True


async def test_continuing_a_thread_that_has_nothing_to_continue_is_refused(boot: Any) -> None:
    """A fresh thread has no interrupted turn, and resuming one would invent a reply."""
    async with boot([]) as app:
        resp = await app.client.post(
            "/chat/continue", json={"thread_id": "e2e-continue-fresh", "manifest": "quick"}
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"] == "nothing_to_continue"


async def test_continuing_a_completed_turn_is_refused(boot: Any) -> None:
    """The other guard: the last turn is a finished assistant message, so there is no gap."""
    thread = "e2e-continue-done"
    async with boot([_answer()]) as app:
        await _seed(app, thread)

        resp = await app.client.post("/chat/continue", json={"thread_id": thread, "manifest": "quick"})
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"] == "already_complete"


# --- forking and rewinding -----------------------------------------------------------------


async def test_a_fork_copies_the_log_and_records_its_parent(boot: Any) -> None:
    """A fork that shared the source's log would let a branch rewrite its own history.

    Asserted by writing to the fork afterwards and checking the source did not move.
    """
    source, forked = "e2e-fork-src", "e2e-fork-dst"
    async with boot([_answer("first"), _answer("second")]) as app:
        await _seed(app, source, "original question")

        fork = await app.client.post("/chat/fork", json={"thread_id": source, "new_thread_id": forked})
        assert fork.status_code == 200, fork.text

        forked_snap = await _snapshot(app, forked)
        contents = [e.get("content") for e in forked_snap["transcript"]]
        assert "original question" in contents, contents
        assert forked_snap["parentSessionId"] == f"default:{source}", forked_snap

        # Writing to the fork must not reach the source.
        await _seed(app, forked, "only on the branch")
        source_contents = [e.get("content") for e in (await _snapshot(app, source))["transcript"]]
        assert "only on the branch" not in source_contents, source_contents


async def test_a_rewind_moves_the_leaf_back_to_the_named_event(boot: Any) -> None:
    """Rewind is how a client undoes a turn, so the leaf must actually move."""
    thread = "e2e-rewind"
    async with boot([_answer("first"), _answer("second")]) as app:
        await _seed(app, thread, "one")
        first_snap = await _snapshot(app, thread)
        target = first_snap["transcript"][0]["id"]
        leaf_before = first_snap["leafId"]

        await _seed(app, thread, "two")
        assert (await _snapshot(app, thread))["leafId"] != leaf_before

        # `summarize` is on by default and appends a summary of the abandoned branch, which
        # moves the leaf again. Turned off here so this test is about the rewind alone.
        rewound = await app.client.post(
            "/chat/rewind", json={"thread_id": thread, "event_id": target, "summarize": False}
        )
        assert rewound.status_code == 200, rewound.text
        assert rewound.json()["leaf_id"] == target, rewound.json()

        assert (await _snapshot(app, thread))["leafId"] == target


async def test_rewinding_to_an_event_that_does_not_exist_is_a_404(boot: Any) -> None:
    """An unknown event id must not silently truncate the thread to nothing."""
    thread = "e2e-rewind-missing"
    async with boot([_answer()]) as app:
        await _seed(app, thread)
        snap_before = await _snapshot(app, thread)
        before, leaf_before = len(snap_before["transcript"]), snap_before["leafId"]

        resp = await app.client.post("/chat/rewind", json={"thread_id": thread, "event_id": "no-such-event"})
        assert resp.status_code == 404, resp.text

        after = await _snapshot(app, thread)
        assert len(after["transcript"]) == before
        # A rewind that moved the leaf and *then* validated would leave the thread pointing at
        # nothing while still answering 404.
        assert after["leafId"] == leaf_before


# --- the ui prompt bridge ------------------------------------------------------------------


async def test_answering_a_pending_ui_prompt_resolves_the_waiter(boot: Any) -> None:
    """The behaviour the endpoint exists for: a prompt is waiting, and the answer reaches it.

    Driven through the real prompt helper rather than a mock — `request_confirm` emits the
    request on the side channel and blocks on a waiter, and `POST /chat/ui` is the only thing
    that can release it. The request id is read off the emitted event, because that is how a
    client learns it too.
    """
    import asyncio

    from felix.side_events import drain
    from felix.ui import request_confirm

    thread = "default:e2e-ui"
    async with boot([]) as app:
        pending = asyncio.create_task(request_confirm(thread, "proceed?", timeout=10))
        request_id = None
        for _ in range(50):
            await asyncio.sleep(0.01)
            for event in await drain(thread):
                if event.get("event") == "ui_request":
                    request_id = event["data"]["request_id"]
            if request_id:
                break
        assert request_id, "the prompt was never announced on the side channel"

        resolved = await app.client.post("/chat/ui", json={"request_id": request_id, "value": "yes"})
        assert resolved.status_code == 200, resolved.text

        answer = await asyncio.wait_for(pending, timeout=5)
        assert answer.cancelled is False, answer
        assert answer.value == "yes", answer


async def test_answering_a_ui_prompt_nobody_asked_reports_ok_anyway(boot: Any) -> None:
    """The client can race a prompt that already resolved, so an unknown id must not 500.

    Worth stating what it does rather than what it might: the route signals a waiter that may
    not exist and reports `ok` either way, so the response says the message was delivered, not
    that anything was listening. A caller cannot use it to tell a live prompt from a stale one.
    """
    async with boot([]) as app:
        resp = await app.client.post("/chat/ui", json={"request_id": "no-such-prompt", "value": "yes"})
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "request_id": "no-such-prompt"}, resp.json()


# --- compaction ----------------------------------------------------------------------------


def _tiny_window_manifest() -> Any:
    """A manifest that keeps nothing recent, so two turns are already compactable.

    `keep_recent_tokens: 0` is a legal value the schema allows and, until the route stopped
    reading it through `or`, one that silently became 20000 — which is why this manifest is
    what proves that fix as well as what makes the summarising branch reachable at all.
    """
    from felix.manifests.loader import parse_manifest

    return parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "e2e-tiny-window"},
            "spec": {
                "pattern": "react",
                "tools": ["calculator"],
                "auth": {"inbound": {"allow_anonymous": True}},
                "session": {
                    "strategy": "compacting",
                    "context_window_tokens": 1024,
                    "reserve_tokens": 1000,
                    "keep_recent_tokens": 0,
                    "compaction_enabled": True,
                },
            },
        }
    )


async def test_a_manual_compaction_of_a_short_thread_does_nothing_and_says_so(boot: Any) -> None:
    """The common case, pinned because it is easy to mistake for the real thing.

    Nothing overflows a 128k window in a test, so a compaction against a bundled manifest
    answers `ok` having called no model at all. A test asserting only the status code would be
    green here and equally green if compaction never worked anywhere.
    """
    thread = "e2e-compact-noop"
    async with boot([_answer("one"), _answer("two")]) as app:
        await _seed(app, thread, "first message")
        await _seed(app, thread, "second message")
        calls_before = len(app.spy.prompts)

        compacted = await app.client.post("/chat/compact", json={"thread_id": thread, "manifest": "quick"})
        assert compacted.status_code == 200, compacted.text
        assert len(app.spy.prompts) == calls_before, "nothing to compact must call no model"


async def test_compaction_that_actually_runs_summarises_and_is_metered(boot: Any) -> None:
    """The branch that costs money: the summariser reaches a model and is billed for it.

    This is the test the silent default was hiding. With `keep_recent_tokens: 0` read as 20000,
    `_find_cut` found nothing older to summarise, so a manual compaction of any test-sized
    thread returned `ok` having called nothing — indistinguishable from compaction being broken.
    """
    from felix.flush import flush_all
    from felix.usage import store as usage_store

    thread = "e2e-compact-real"
    manifests = {"e2e-tiny-window": _tiny_window_manifest()}
    async with boot([_answer("one"), _answer("two"), _answer("a summary")], manifests=manifests) as app:
        for text in ("first message here", "second message here"):
            resp = await app.client.post(
                "/chat",
                json={
                    "manifest": "e2e-tiny-window",
                    "thread_id": thread,
                    "messages": [{"role": "user", "content": text}],
                },
            )
            assert resp.status_code == 200, resp.text
        calls_before = len(app.spy.prompts)
        rows_before, _ = await usage_store.query(app.settings, "default", limit=50)

        compacted = await app.client.post(
            "/chat/compact", json={"thread_id": thread, "manifest": "e2e-tiny-window"}
        )
        assert compacted.status_code == 200, compacted.text
        assert len(app.spy.prompts) > calls_before, "the summariser must reach the model"

        await flush_all(app.settings)
        rows, _ = await usage_store.query(app.settings, "default", limit=50)
        assert len(rows) > len(rows_before), "the summariser turn must be billed like any other"
        assert all(r["cost_usd"] > 0 for r in rows), rows


async def test_compacting_against_an_unknown_manifest_is_a_404(boot: Any) -> None:
    """The manifest decides the window and the summariser model, so it cannot be guessed."""
    thread = "e2e-compact-unknown"
    async with boot([_answer()]) as app:
        await _seed(app, thread)
        resp = await app.client.post(
            "/chat/compact", json={"thread_id": thread, "manifest": "no-such-manifest"}
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"] == "unknown_manifest:no-such-manifest"
