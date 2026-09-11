"""The session surfaces, over the wire, against a thread a real turn created.

Nine of these endpoints ship today and are exercised only by `.github/workflows/smoke.yml`,
which runs against production every six hours. A regression in any of them reaches an operator
before it reaches CI — and the smoke workflow asserts status codes and one search hit, not that
what was written can be read back.

So these tests assert on state rather than on acknowledgement: a rename must come back from a
different endpoint, a labelled event must carry the label in the snapshot, an appended entry
must appear in the export, and a held lease must actually refuse the second holder. Every one
runs on a thread that a scripted turn genuinely created, so the session log is the one the
product writes rather than one the test hand-assembled.
"""

from __future__ import annotations

import json
from typing import Any

from felix_ai.providers.scripted import ScriptedTurn

from tests.e2e.conftest import Booted


def _answer(text: str = "noted") -> ScriptedTurn:
    return ScriptedTurn(content=text)


async def _seed(app: Booted, thread: str, text: str = "remember the zucchini") -> dict[str, Any]:
    """Create the thread the way the product does: one real turn through `POST /chat`."""
    resp = await app.client.post(
        "/chat",
        json={"manifest": "quick", "thread_id": thread, "messages": [{"role": "user", "content": text}]},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# --- reading a thread back -----------------------------------------------------------------


async def test_a_turn_shows_up_in_the_session_list_and_snapshot(boot: Any) -> None:
    """`GET /chat/sessions` and `GET /chat/sessions/{id}` see what `POST /chat` wrote.

    The list is keyed on thread metadata and the snapshot on the event log — two different
    stores — so a thread present in one and absent from the other is the interesting failure.
    """
    thread = "e2e-list"
    async with boot([_answer()]) as app:
        body = await _seed(app, thread)
        namespaced = body["thread_id"]

        listing = await app.client.get("/chat/sessions")
        assert listing.status_code == 200, listing.text
        threads = [s.get("id") for s in listing.json()["sessions"]]
        assert namespaced in threads, threads

        snap = await app.client.get(f"/chat/sessions/{thread}")
        assert snap.status_code == 200, snap.text
        body_snap = snap.json()
        assert body_snap["id"] == namespaced
        # The transcript is the point: a snapshot that resolves the thread but returns an
        # empty log would satisfy an id check and tell a reattaching client nothing.
        roles = [(e.get("role"), e.get("content")) for e in body_snap["transcript"]]
        assert ("user", "remember the zucchini") in roles, roles
        assert ("assistant", "noted") in roles, roles


async def test_search_finds_a_thread_by_its_content(boot: Any) -> None:
    """The search index is fed by the turn, not by a separate write the test performs."""
    async with boot([_answer()]) as app:
        await _seed(app, "e2e-search", "the zucchini marker is unmistakable")

        hits = await app.client.get("/chat/sessions/search", params={"q": "zucchini", "limit": 5})
        assert hits.status_code == 200, hits.text
        body = hits.json()
        assert body["query"] == "zucchini"
        assert any("zucchini" in (hit.get("content") or "") for hit in body["hits"]), body


async def test_search_for_something_absent_returns_no_hits(boot: Any) -> None:
    """The other half of the contract: a search that matches nothing says so.

    Without this, a search wired to return every event would satisfy the test above.
    """
    async with boot([_answer()]) as app:
        await _seed(app, "e2e-search-miss", "the zucchini marker is unmistakable")

        hits = await app.client.get("/chat/sessions/search", params={"q": "aubergine", "limit": 5})
        assert hits.status_code == 200, hits.text
        assert hits.json()["hits"] == []


# --- writing to a thread -------------------------------------------------------------------


async def test_a_renamed_session_reads_back_under_its_new_name(boot: Any) -> None:
    """Naming writes thread metadata and appends an event; both must hold the name."""
    thread = "e2e-name"
    async with boot([_answer()]) as app:
        await _seed(app, thread)

        named = await app.client.post("/chat/sessions/name", json={"thread_id": thread, "name": "Zucchini"})
        assert named.status_code == 200, named.text
        assert named.json()["name"] == "Zucchini"

        listing = await app.client.get("/chat/sessions")
        names = {s.get("id"): s.get("sessionName") for s in listing.json()["sessions"]}
        assert names.get(named.json()["thread_id"]) == "Zucchini", names

        # And on the snapshot, which reads thread metadata by a different path.
        snap = await app.client.get(f"/chat/sessions/{thread}")
        assert snap.json()["name"] == "Zucchini", snap.json()


async def test_a_custom_entry_is_stored_with_the_in_context_flag_it_was_given(boot: Any) -> None:
    """`in_context` decides whether the model ever sees the entry, so it must be *stored*.

    The response body echoes the request, so asserting on it proves nothing about what was
    written — flipping the value the route persists left this test green until it read the
    flag back off the event instead.
    """
    thread = "e2e-custom"
    async with boot([_answer()]) as app:
        await _seed(app, thread)

        added = await app.client.post(
            "/chat/sessions/custom",
            json={
                "thread_id": thread,
                "role": "system",
                "content": "operator note: handle with care",
                "in_context": True,
            },
        )
        assert added.status_code == 200, added.text
        event_id = added.json()["event_id"]
        assert event_id

        snap = await app.client.get(f"/chat/sessions/{thread}")
        stored = next(e for e in snap.json()["transcript"] if e["id"] == event_id)
        assert stored["content"] == "operator note: handle with care"
        assert stored["kind"] == "custom"
        assert stored["metadata"]["in_context"] is True, stored

        export = await app.client.get(f"/chat/sessions/{thread}/export")
        assert export.status_code == 200, export.text
        assert "operator note: handle with care" in export.text


async def test_an_entry_marked_out_of_context_is_stored_that_way_too(boot: Any) -> None:
    """The other half: a flag hardcoded to True would satisfy the test above."""
    thread = "e2e-custom-off"
    async with boot([_answer()]) as app:
        await _seed(app, thread)
        added = await app.client.post(
            "/chat/sessions/custom",
            json={"thread_id": thread, "content": "sidebar", "in_context": False},
        )
        assert added.status_code == 200, added.text

        snap = await app.client.get(f"/chat/sessions/{thread}")
        stored = next(e for e in snap.json()["transcript"] if e["id"] == added.json()["event_id"])
        assert stored["metadata"]["in_context"] is False, stored


async def test_a_label_lands_on_the_event_it_names(boot: Any) -> None:
    """Labelling takes an event id, so the wrong id is a plausible and silent failure."""
    thread = "e2e-label"
    async with boot([_answer()]) as app:
        await _seed(app, thread)
        added = await app.client.post(
            "/chat/sessions/custom",
            json={"thread_id": thread, "content": "checkpoint", "in_context": False},
        )
        event_id = added.json()["event_id"]

        labelled = await app.client.post(
            "/chat/sessions/label",
            json={"thread_id": thread, "event_id": event_id, "label": "milestone"},
        )
        assert labelled.status_code == 200, labelled.text

        snap = await app.client.get(f"/chat/sessions/{thread}")
        assert snap.status_code == 200, snap.text
        labels = snap.json().get("labels") or {}
        assert labels.get(event_id) == "milestone", snap.json()


# --- exporting -----------------------------------------------------------------------------


async def test_the_export_is_jsonl_of_the_active_branch(boot: Any) -> None:
    """Every line parses, and the user's own words are in it.

    The export feeds eval artifacts and sharing, so a body that is almost-JSONL — a trailing
    blank line, a dict per file rather than per event — breaks a consumer, not this endpoint.
    """
    thread = "e2e-export"
    async with boot([_answer("acknowledged")]) as app:
        await _seed(app, thread, "export me")

        export = await app.client.get(f"/chat/sessions/{thread}/export")
        assert export.status_code == 200, export.text
        assert export.headers["content-type"].startswith("application/x-ndjson")
        assert "attachment" in export.headers.get("content-disposition", "")

        lines = [json.loads(line) for line in export.text.splitlines() if line.strip()]
        assert lines, export.text
        contents = [str(entry.get("content") or "") for entry in lines]
        assert any("export me" in c for c in contents), contents
        assert any("acknowledged" in c for c in contents), contents


# --- leases --------------------------------------------------------------------------------


async def test_an_exclusive_lease_locks_out_a_second_holder_until_released(boot: Any) -> None:
    """The lease is what stops two clients driving one thread at once.

    A lease endpoint that returns 200 and stores nothing looks identical to a working one
    until two clients collide, so the refusal is the assertion that matters.
    """
    thread = "e2e-lease"
    async with boot([_answer()]) as app:
        await _seed(app, thread)

        first = await app.client.post(
            "/chat/sessions/lease",
            json={"thread_id": thread, "holder_id": "holder-a", "mode": "exclusive"},
        )
        assert first.status_code == 200, first.text
        assert first.json()["ok"] and first.json()["token"]

        contended = await app.client.post(
            "/chat/sessions/lease",
            json={"thread_id": thread, "holder_id": "holder-b", "mode": "exclusive"},
        )
        assert contended.status_code == 409, contended.text

        released = await app.client.post(
            "/chat/sessions/lease/release",
            json={"thread_id": thread, "holder_id": "holder-a"},
        )
        assert released.status_code == 200, released.text

        regained = await app.client.post(
            "/chat/sessions/lease",
            json={"thread_id": thread, "holder_id": "holder-b", "mode": "exclusive"},
        )
        assert regained.status_code == 200, regained.text


async def test_the_lock_a_lease_takes_is_visible_and_is_given_back(boot: Any) -> None:
    """A client reattaching reads the lock from the snapshot, not from its own memory.

    Asserted in both directions. `locked` that is simply always true would satisfy the
    acquire half, and a release that returns 200 without clearing the lock is precisely the
    failure the lease exists to prevent — the thread stays unusable to everyone.
    """
    thread = "e2e-lease-snapshot"
    async with boot([_answer()]) as app:
        await _seed(app, thread)

        before = await app.client.get(f"/chat/sessions/{thread}")
        assert before.json()["locked"] is False, before.json()

        held = await app.client.post(
            "/chat/sessions/lease",
            json={"thread_id": thread, "holder_id": "holder-a", "mode": "exclusive"},
        )
        assert held.json()["status"]["holder_id"] == "holder-a", held.json()

        during = await app.client.get(f"/chat/sessions/{thread}")
        assert during.status_code == 200, during.text
        assert during.json()["locked"] is True, during.json()

        await app.client.post(
            "/chat/sessions/lease/release",
            json={"thread_id": thread, "holder_id": "holder-a"},
        )
        after = await app.client.get(f"/chat/sessions/{thread}")
        assert after.json()["locked"] is False, after.json()


# --- tenant scoping ------------------------------------------------------------------------


async def test_a_thread_id_that_escapes_its_tenant_is_refused(boot: Any) -> None:
    """Thread ids are namespaced per tenant on the way in; a caller-supplied namespace
    separator is how that would be bypassed, and every one of these routes accepts one."""
    async with boot([_answer()]) as app:
        for path, payload in (
            ("/chat/sessions/name", {"thread_id": "other:thread", "name": "x"}),
            ("/chat/sessions/custom", {"thread_id": "other:thread", "content": "x"}),
            ("/chat/sessions/lease", {"thread_id": "other:thread", "holder_id": "h"}),
        ):
            resp = await app.client.post(path, json=payload)
            assert resp.status_code == 400, f"{path} accepted a namespaced thread id: {resp.text}"
            assert resp.json()["detail"] == "invalid_thread_id"


# --- the new index must not become a second copy of the secrets ---------------------------


async def test_a_secret_is_not_searchable_after_being_masked_on_the_way_in(boot: Any) -> None:
    """The search index is a second copy of event content, so it inherits the masking rule.

    Masking happens on the way into the store, and the index is fed from the same masked
    string — but "fed from the same variable" is a property of one line of code, and this is
    the assertion that keeps it true. Indexing `ev.content` instead of the redacted `content`
    would leave every secret in the tree findable by exact search while the stored event
    still looked clean.
    """
    import felix.secrets as secrets_mod

    secret = "super-secret-value-9f2b"
    async with boot([_answer()]) as app:
        original = secrets_mod.collected_secret_values
        secrets_mod.collected_secret_values = lambda *a, **k: [secret]  # type: ignore[assignment]
        try:
            await _seed(app, "e2e-secret", f"the key is {secret}")

            hits = await app.client.get("/chat/sessions/search", params={"q": secret})
            assert hits.status_code == 200, hits.text
            assert hits.json()["hits"] == [], hits.json()

            # And the masked form is what is there instead, so this is not passing because
            # the event was never indexed at all.
            masked = await app.client.get("/chat/sessions/search", params={"q": "REDACTED"})
            assert masked.json()["hits"], masked.json()
            assert all(secret not in (h.get("content") or "") for h in masked.json()["hits"])
        finally:
            secrets_mod.collected_secret_values = original  # type: ignore[assignment]


# --- deleting must delete from the index too ----------------------------------------------


async def test_deleting_a_thread_makes_its_content_unsearchable(boot: Any) -> None:
    """A delete that leaves the text findable is a delete that did not happen.

    The search index is a second copy of event content, so it has to go wherever the events
    go. On Postgres that is automatic — `content_tsv` is generated and dies with the row — so
    giving the in-memory index a writer without a matching delete path is how the twin would
    start answering searches with text the caller had just removed.
    """
    thread = "e2e-delete"
    async with boot([_answer()]) as app:
        await _seed(app, thread, "the zucchini marker is unmistakable")
        assert (await app.client.get("/chat/sessions/search", params={"q": "zucchini"})).json()["hits"]

        deleted = await app.client.delete(f"/chat/history/{thread}")
        assert deleted.status_code == 200, deleted.text

        after = await app.client.get("/chat/sessions/search", params={"q": "zucchini"})
        assert after.status_code == 200, after.text
        assert after.json()["hits"] == [], after.json()


async def test_a_reused_seq_after_delete_does_not_collide_in_the_index(boot: Any) -> None:
    """The twin restarts `seq` at zero after a reset; Postgres cannot, since the row is gone.

    Without the index delete, one thread would hold two entries at `seq` 0 with different
    content, and a client deep-linking from a hit would land on the wrong event.
    """
    thread = "e2e-delete-reuse"
    async with boot([_answer(), _answer()]) as app:
        await _seed(app, thread, "first life aubergine")
        await app.client.delete(f"/chat/history/{thread}")
        await _seed(app, thread, "second life aubergine")

        hits = (await app.client.get("/chat/sessions/search", params={"q": "aubergine"})).json()["hits"]
        contents = [h.get("content") for h in hits]
        assert all("first life" not in (c or "") for c in contents), contents
        assert any("second life" in (c or "") for c in contents), contents
