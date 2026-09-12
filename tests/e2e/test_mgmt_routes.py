"""The management routers, over the wire, with real scopes.

`routes/jobs.py` and `routes/eval.py` received **zero** requests anywhere in the suite;
`routes/audit.py` had one. Between them they are the operator's whole view of what the harness
scheduled, evaluated and refused, and they are the surface an operator reaches for after
something has gone wrong — the worst moment to discover a 500.

These run under `auth_mode=api_key` rather than the suite's usual `none`, because
`require_mgmt_scopes` is skipped entirely when auth is off. A test of a scoped route under
`auth_mode=none` proves the handler works and says nothing about who may reach it, which is
the half that matters for a management API.
"""

from __future__ import annotations

import json
from typing import Any

from felix_ai.providers.scripted import ScriptedTurn

ADMIN = "sk-admin-not-a-secret"
READER = "sk-reader-not-a-secret"
WRITER = "sk-writer-not-a-secret"


def _keys(*, reader: list[str], writer: list[str] | None = None) -> dict[str, str]:
    """Three keys: `admin` bypasses everything, `reader` and `writer` hold one scope each.

    The precise scopes matter. `admin` satisfies every gate by design, so a positive case
    driven with the admin key cannot tell "this scope grants access" from "admin bypasses the
    check" — and a route whose `require_mgmt_scopes` call was deleted would still pass it. The
    writer key holds only the write scope under test, so its success is evidence about that
    scope and nothing else.
    """
    return {
        "FELIX_AUTH_MODE": "api_key",
        "FELIX_AUTH_API_KEYS": json.dumps(
            {
                ADMIN: {"tenant_id": "default", "sub": "admin", "scopes": ["admin"]},
                READER: {"tenant_id": "default", "sub": "reader", "scopes": reader},
                WRITER: {"tenant_id": "default", "sub": "writer", "scopes": writer or []},
            }
        ),
    }


def _as(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _answer(text: str = "noted") -> ScriptedTurn:
    return ScriptedTurn(content=text)


# --- jobs ----------------------------------------------------------------------------------


async def test_a_job_round_trips_through_the_api(boot: Any) -> None:
    """Create, read, list, and delete — the CRUD an operator drives the scheduler with.

    The delete is asserted by the 404 that follows it, not by its own status: a delete that
    answers `{"status": "deleted"}` and removes nothing is the failure worth catching.
    """
    async with boot([], env=_keys(reader=["jobs:read"])) as app:
        created = await app.client.put(
            "/jobs/nightly",
            json={
                "schedule": "0 3 * * *",
                "manifest_id": "quick",
                "enabled": True,
                "payload": {"note": "nightly sweep"},
            },
            headers=_as(ADMIN),
        )
        assert created.status_code == 200, created.text

        fetched = await app.client.get("/jobs/nightly", headers=_as(ADMIN))
        assert fetched.status_code == 200, fetched.text
        assert fetched.json()["schedule"] == "0 3 * * *"
        assert fetched.json()["manifest_id"] == "quick"
        # All four upsert fields, not the two that are obviously interesting: a `put_job` that
        # dropped `payload` or `enabled` would be green on schedule and manifest alone.
        assert fetched.json()["enabled"] is True
        assert fetched.json()["payload"] == {"note": "nightly sweep"}

        listing = await app.client.get("/jobs", headers=_as(ADMIN))
        assert listing.status_code == 200, listing.text
        assert [row["name"] for row in listing.json()["items"]] == ["nightly"]

        runs = await app.client.get("/jobs/nightly/runs", headers=_as(ADMIN))
        assert runs.status_code == 200, runs.text
        assert runs.json()["items"] == []

        deleted = await app.client.delete("/jobs/nightly", headers=_as(ADMIN))
        assert deleted.status_code == 200, deleted.text
        assert (await app.client.get("/jobs/nightly", headers=_as(ADMIN))).status_code == 404


async def test_an_unknown_job_is_a_404_on_both_read_and_delete(boot: Any) -> None:
    """A missing row must not read as an empty one, which a caller would treat as configured."""
    async with boot([], env=_keys(reader=["jobs:read"])) as app:
        assert (await app.client.get("/jobs/ghost", headers=_as(ADMIN))).status_code == 404
        assert (await app.client.delete("/jobs/ghost", headers=_as(ADMIN))).status_code == 404


async def test_writing_a_job_needs_a_write_scope(boot: Any) -> None:
    """`jobs:read` must not carry `jobs:write` — the whole point of splitting them.

    Read is asserted alongside so a blanket refusal cannot pass for scope enforcement.
    """
    async with boot([], env=_keys(reader=["jobs:read"], writer=["jobs:write"])) as app:
        # The write scope alone grants the write: evidence about `jobs:write`, not about admin.
        created = await app.client.put("/jobs/nightly", json={"schedule": "@daily"}, headers=_as(WRITER))
        assert created.status_code == 200, created.text

        assert (await app.client.get("/jobs", headers=_as(READER))).status_code == 200
        # `x:write` implies `x:read`, so the writer can also list.
        assert (await app.client.get("/jobs", headers=_as(WRITER))).status_code == 200

        refused = await app.client.put("/jobs/nightly", json={"schedule": "@hourly"}, headers=_as(READER))
        assert refused.status_code == 403, refused.text

        # And the refusal actually prevented the write.
        current = await app.client.get("/jobs/nightly", headers=_as(ADMIN))
        assert current.json()["schedule"] == "@daily", current.json()


async def test_jobs_are_partitioned_by_the_keys_tenant(boot: Any) -> None:
    """Each tenant sees its own jobs and only its own.

    The positive half is what makes this a test of isolation rather than of a read outage: a
    `get_job` that returned nothing for every tenant but `default` would satisfy the 404s on
    their own. So the other tenant writes and reads back its own row, and both listings are
    asserted exactly.
    """
    env = {
        "FELIX_AUTH_MODE": "api_key",
        "FELIX_AUTH_API_KEYS": json.dumps(
            {
                ADMIN: {"tenant_id": "default", "sub": "a", "scopes": ["admin"]},
                READER: {"tenant_id": "other", "sub": "b", "scopes": ["admin"]},
            }
        ),
    }
    async with boot([], env=env) as app:
        await app.client.put("/jobs/mine", json={"schedule": "@daily"}, headers=_as(ADMIN))
        theirs = await app.client.put("/jobs/theirs", json={"schedule": "@hourly"}, headers=_as(READER))
        assert theirs.status_code == 200, theirs.text

        # Each reads its own.
        assert (await app.client.get("/jobs/theirs", headers=_as(READER))).status_code == 200
        assert (await app.client.get("/jobs/mine", headers=_as(ADMIN))).status_code == 200

        # And neither reads the other's.
        assert (await app.client.get("/jobs/mine", headers=_as(READER))).status_code == 404
        assert (await app.client.get("/jobs/theirs", headers=_as(ADMIN))).status_code == 404

        assert [r["name"] for r in (await app.client.get("/jobs", headers=_as(ADMIN))).json()["items"]] == [
            "mine"
        ]
        assert [r["name"] for r in (await app.client.get("/jobs", headers=_as(READER))).json()["items"]] == [
            "theirs"
        ]


async def test_a_job_cannot_be_deleted_across_the_tenant_boundary(boot: Any) -> None:
    """Reads being partitioned does not imply writes are, and a delete is the costly one."""
    env = {
        "FELIX_AUTH_MODE": "api_key",
        "FELIX_AUTH_API_KEYS": json.dumps(
            {
                ADMIN: {"tenant_id": "default", "sub": "a", "scopes": ["admin"]},
                READER: {"tenant_id": "other", "sub": "b", "scopes": ["admin"]},
            }
        ),
    }
    async with boot([], env=env) as app:
        await app.client.put("/jobs/mine", json={"schedule": "@daily"}, headers=_as(ADMIN))

        assert (await app.client.delete("/jobs/mine", headers=_as(READER))).status_code == 404
        assert (await app.client.get("/jobs/mine", headers=_as(ADMIN))).status_code == 200


# --- eval ----------------------------------------------------------------------------------


async def test_an_eval_dataset_round_trips(boot: Any) -> None:
    """Datasets are how a run is defined, so what comes back must be what went in."""
    items = [{"user_input": "what is 2+2?", "rubric": {"expect": "4"}}]
    async with boot([], env=_keys(reader=["eval:read"])) as app:
        created = await app.client.put(
            "/eval/datasets/smoke",
            json={"description": "arithmetic", "items": items},
            headers=_as(ADMIN),
        )
        assert created.status_code == 200, created.text

        fetched = await app.client.get("/eval/datasets/smoke", headers=_as(ADMIN))
        assert fetched.status_code == 200, fetched.text
        body = fetched.json()
        assert body["description"] == "arithmetic"
        assert [i["user_input"] for i in body["items"]] == ["what is 2+2?"], body
        assert [i["rubric"] for i in body["items"]] == [{"expect": "4"}], body

        listing = await app.client.get("/eval/datasets", headers=_as(ADMIN))
        # Exact, now that the stores are reset per test: this also catches a listing that
        # leaked another tenant's dataset or duplicated the row on re-put.
        assert [row["name"] for row in listing.json()["items"]] == ["smoke"], listing.json()


async def test_an_eval_item_with_unrecognised_keys_is_stored_empty(boot: Any) -> None:
    """Pins a sharp edge rather than a guarantee, so changing it is a deliberate act.

    `items` is `list[dict[str, Any]]`, and `put_dataset` reads only `user_input` and `rubric`
    off each entry. An item written with any other spelling is accepted with 200, listed as
    present, and stored with an empty prompt and an empty rubric. The dataset then looks
    configured and scores nothing — the shape of defect this audit keeps finding.

    Not changed here: `items` is deliberately schema-free, so rejecting unknown keys is an API
    decision rather than a bug fix. If validation is ever added, this test should fail.
    """
    async with boot([], env=_keys(reader=["eval:read"])) as app:
        created = await app.client.put(
            "/eval/datasets/mistyped",
            json={"items": [{"input": "what is 2+2?", "expect": "4"}]},
            headers=_as(ADMIN),
        )
        assert created.status_code == 200, created.text

        stored = (await app.client.get("/eval/datasets/mistyped", headers=_as(ADMIN))).json()
        assert len(stored["items"]) == 1, stored
        assert stored["items"][0]["user_input"] == "", stored
        assert stored["items"][0]["rubric"] == {}, stored


async def test_writing_an_eval_dataset_needs_a_write_scope(boot: Any) -> None:
    """An eval a reader can rewrite is an eval nobody can trust."""
    async with boot([], env=_keys(reader=["eval:read"], writer=["eval:write"])) as app:
        assert (await app.client.get("/eval/datasets", headers=_as(READER))).status_code == 200

        granted = await app.client.put("/eval/datasets/smoke", json={"items": []}, headers=_as(WRITER))
        assert granted.status_code == 200, granted.text

        refused = await app.client.put("/eval/datasets/smoke", json={"items": []}, headers=_as(READER))
        assert refused.status_code == 403, refused.text


async def test_an_unknown_eval_dataset_and_run_are_404(boot: Any) -> None:
    """Both id-addressed reads, because an empty body would read as a finished run."""
    async with boot([], env=_keys(reader=["eval:read"])) as app:
        assert (await app.client.get("/eval/datasets/ghost", headers=_as(ADMIN))).status_code == 404
        assert (await app.client.get("/eval/runs/ghost", headers=_as(ADMIN))).status_code == 404


# --- audit ---------------------------------------------------------------------------------


async def test_the_audit_log_returns_what_a_turn_wrote(boot: Any) -> None:
    """The operator's record of what the agent did, read back over the wire.

    The turn is driven with the same key, so this also pins that an audited event carries the
    principal rather than an anonymous blank.
    """
    from felix.flush import flush_all

    async with boot([_answer()], env=_keys(reader=["audit:read"])) as app:
        turn = await app.client.post(
            "/chat",
            json={"manifest": "quick", "messages": [{"role": "user", "content": "hello"}]},
            headers=_as(ADMIN),
        )
        assert turn.status_code == 200, turn.text
        await flush_all(app.settings)

        listed = await app.client.get("/audit", headers=_as(ADMIN))
        assert listed.status_code == 200, listed.text
        events = listed.json()["items"]
        types = {e["event_type"] for e in events}
        assert {"user_input", "final_response"} <= types, types
        # Narrowed to the event this route test is about: quantifying over every audit row
        # would redden here the day any unrelated event type is written without a principal.
        assert [e["principal_subj"] for e in events if e["event_type"] == "user_input"] == ["admin"]


async def test_reading_the_audit_log_needs_a_read_scope(boot: Any) -> None:
    """The audit log carries prompts and tool arguments; it is not ambiently readable."""
    async with boot([], env=_keys(reader=["jobs:read"], writer=["audit:read"])) as app:
        assert (await app.client.get("/audit", headers=_as(READER))).status_code == 403
        # `audit:read` alone grants it, so this is evidence about that scope rather than
        # about admin, which satisfies every gate by design.
        assert (await app.client.get("/audit", headers=_as(WRITER))).status_code == 200


async def test_paging_the_audit_log_returns_every_event_once(boot: Any) -> None:
    """Over the wire, because the cursor is a query parameter and a client round-trips it.

    A turn writes several events inside the same millisecond, which is what broke this: the
    cursor carried only a timestamp, so `ts < last_seen` stepped over every sibling event and
    no page ever returned them. The route reported 200 each time and the operator saw a
    shorter history than the one that happened.
    """
    from felix.audit import store as audit_store
    from felix.flush import flush_all

    async with boot([_answer()], env=_keys(reader=["audit:read"])) as app:
        turn = await app.client.post(
            "/chat",
            json={"manifest": "quick", "messages": [{"role": "user", "content": "hello"}]},
            headers=_as(ADMIN),
        )
        assert turn.status_code == 200, turn.text
        # Two events pinned to one millisecond. The turn's own events are stamped with the
        # real clock, so whether any of them tie is timing — and on a run where none did, this
        # walk would pass against the cursor it exists to rule out.
        for subject in ("tied-a", "tied-b"):
            audit_store.record_event(app.settings, "default", "tool_call", ts=1_000, principal_subj=subject)
        await flush_all(app.settings)

        whole = await app.client.get("/audit", params={"limit": 500}, headers=_as(ADMIN))
        assert whole.status_code == 200, whole.text
        listed = whole.json()["items"]
        expected = {e["id"] for e in listed}
        # The positive control. `flush_all` swallows a failed audit flush by design, and a
        # single turn writes two audit events of its own — so without this the pinned pair
        # could fail to land, `expected` and `seen` would both be computed from what *is*
        # there, and the test would quietly become the real-clock case it exists to rule out.
        assert {"tied-a", "tied-b"} <= {e["principal_subj"] for e in listed}, listed

        seen: set[str] = set()
        cursor: str | None = None
        for _ in range(20):  # bounded, so a cursor that never advances fails rather than hangs
            params: dict[str, Any] = {"limit": 1}
            if cursor is not None:
                params["cursor"] = cursor
            page = await app.client.get("/audit", params=params, headers=_as(ADMIN))
            assert page.status_code == 200, page.text
            body = page.json()
            for event in body["items"]:
                assert event["id"] not in seen, f"{event['id']} was returned on two pages"
                seen.add(event["id"])
            cursor = body["next_cursor"]
            if cursor is None:
                break
        else:  # pragma: no cover - only on a cursor that does not terminate
            raise AssertionError("the audit cursor never reported the end of the history")

        assert seen == expected, expected - seen


async def test_a_malformed_audit_cursor_is_a_bad_request(boot: Any) -> None:
    """A cursor arrives from the client, so it can be anything.

    Unhandled it reached the caller as a 500 — a server error for someone else's typo, and a
    page for whoever watches the error rate.
    """
    async with boot([], env=_keys(reader=["audit:read"])) as app:
        bad = await app.client.get("/audit", params={"cursor": "not-a-cursor"}, headers=_as(ADMIN))
        assert bad.status_code == 400, bad.text
        assert "cursor" in bad.json()["detail"], bad.text

        usage = await app.client.get("/usage", params={"cursor": "nope"}, headers=_as(ADMIN))
        assert usage.status_code == 400, usage.text


# --- approvals -----------------------------------------------------------------------------


async def test_listing_approvals_is_empty_before_anything_pauses(boot: Any) -> None:
    """The baseline the pending test below is measured against."""
    async with boot([], env=_keys(reader=["approvals:read"])) as app:
        listed = await app.client.get("/approvals", headers=_as(ADMIN))
        assert listed.status_code == 200, listed.text
        assert listed.json()["items"] == []


async def test_an_unknown_approval_is_a_404_on_read_and_on_decide(boot: Any) -> None:
    """Deciding an approval that does not exist must not create one."""
    async with boot([], env=_keys(reader=["approvals:read"])) as app:
        assert (await app.client.get("/approvals/ghost", headers=_as(ADMIN))).status_code == 404
        decided = await app.client.post(
            "/approvals/ghost/decide", json={"decision": "approved"}, headers=_as(ADMIN)
        )
        assert decided.status_code == 404, decided.text

        # And the refusal did not bring one into existence. Asserted by re-reading the id,
        # not by listing: `params={"status": None}` serialises to `?status=`, which filters on
        # the empty string and matches nothing on any store — an assertion that cannot fail.
        assert (await app.client.get("/approvals/ghost", headers=_as(ADMIN))).status_code == 404


async def test_deciding_an_approval_needs_a_write_scope(boot: Any) -> None:
    """Approval is the human gate on a governed tool, so reading it is not deciding it."""
    async with boot([], env=_keys(reader=["approvals:read"], writer=["approvals:write"])) as app:
        assert (await app.client.get("/approvals", headers=_as(READER))).status_code == 200

        refused = await app.client.post(
            "/approvals/any/decide", json={"decision": "approved"}, headers=_as(READER)
        )
        assert refused.status_code == 403, refused.text

        # The write scope gets past the gate and into the handler, which then 404s on the id.
        # A 404 here rather than a 403 is what distinguishes "the scope granted" from "every
        # decide is refused", which the line above alone cannot show.
        allowed = await app.client.post(
            "/approvals/any/decide", json={"decision": "approved"}, headers=_as(WRITER)
        )
        assert allowed.status_code == 404, allowed.text


async def test_a_pending_approval_is_listed_and_can_be_decided(boot: Any) -> None:
    """The success path, which every other approvals test here steps around.

    Listing empties and 404s only prove the route does not crash. A decide that returned the
    row without flipping `status`, or stamped an anonymous `decided_by`, is green against all
    of them — and this is the human gate on a governed tool, so who decided is the record that
    matters. The approval is seeded through the store rather than by pausing a real run: the
    governed-tool path is covered in `tests/unit/test_approval_binding.py`, and what is
    untested is the store-to-wire seam this exercises.
    """
    from felix.approvals import store as approvals_store

    async with boot([], env=_keys(reader=["approvals:read"], writer=["approvals:write"])) as app:
        pending = await approvals_store.create_pending(
            app.settings,
            "default",
            tool_name="calculator",
            call_signature="calculator:2+2",
            args={"expression": "2+2"},
            manifest_id="quick",
            rule_id="calc-approval",
        )
        approval_id = pending["id"]

        listed = await app.client.get("/approvals", headers=_as(READER))
        assert listed.status_code == 200, listed.text
        rows = listed.json()["items"]
        assert [r["id"] for r in rows] == [approval_id], rows
        assert rows[0]["tool_name"] == "calculator"
        assert rows[0]["status"] == "pending"

        decided = await app.client.post(
            f"/approvals/{approval_id}/decide",
            json={"decision": "approved", "note": "looks fine"},
            headers=_as(WRITER),
        )
        assert decided.status_code == 200, decided.text
        assert decided.json()["status"] == "approved", decided.json()
        # The principal is taken from the credential, not from the body: an approval that
        # recorded an anonymous decider would be an audit trail with the answer missing.
        assert decided.json()["decided_by"] == "writer", decided.json()

        reread = await app.client.get(f"/approvals/{approval_id}", headers=_as(READER))
        assert reread.json()["status"] == "approved", reread.json()
        assert (await app.client.get("/approvals", headers=_as(READER))).json()["items"] == []


async def test_an_eval_run_scores_its_dataset_and_is_listed(boot: Any) -> None:
    """The wiring, not the scorer: `start_run` is unit-tested, reaching it over HTTP was not.

    What this pins is that the route starts a run, that the run scores its dataset, and that
    the result is listable and fetchable by id afterwards.

    What it does *not* pin, stated because the obvious reading is that it does: the route
    passes `request.app.state.tools` into the run, and this dataset item never calls a tool, so
    replacing that argument with `None` leaves the test green. Covering it needs an item whose
    scored turn makes a tool call. The `use_llm_judge` inversion is likewise uncovered — a run
    that quietly consulted a judge still returns a score.
    """
    items = [{"user_input": "say ok", "rubric": {"expect": "ok"}}]
    async with boot([_answer("ok")], env=_keys(reader=["eval:read"], writer=["eval:write"])) as app:
        await app.client.put("/eval/datasets/scored", json={"items": items}, headers=_as(ADMIN))

        started = await app.client.post(
            "/eval/runs",
            json={
                "dataset_name": "scored",
                "candidate_manifest": "quick",
                "deterministic_judge": True,
            },
            headers=_as(WRITER),
        )
        assert started.status_code == 200, started.text
        run = started.json()
        assert run["dataset_name"] == "scored", run
        # The run scored the one item rather than merely being recorded: a run that started
        # and evaluated nothing would still be listed below.
        assert run["pass_count"] + run["fail_count"] == 1, run

        listed = await app.client.get("/eval/runs", headers=_as(READER))
        assert listed.status_code == 200, listed.text
        assert run["id"] in [r["id"] for r in listed.json()["items"]], listed.json()

        fetched = await app.client.get(f"/eval/runs/{run['id']}", headers=_as(READER))
        assert fetched.status_code == 200, fetched.text
        assert fetched.json()["id"] == run["id"]


async def test_starting_an_eval_run_without_a_dataset_is_refused(boot: Any) -> None:
    """`dataset_name` is what the run scores; without it there is nothing to score."""
    async with boot([], env=_keys(reader=["eval:read"], writer=["eval:write"])) as app:
        resp = await app.client.post("/eval/runs", json={"candidate_manifest": "quick"}, headers=_as(WRITER))
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"] == "dataset_name_required"


async def test_audit_metrics_rolls_up_tool_calls_by_name(boot: Any) -> None:
    """The inspector's metrics panel, which reads a payload key nothing else asserts.

    `payload.tool` is how each call is attributed; a rename gives every row `unknown` and the
    panel still renders, so the attribution is the assertion that matters.
    """
    from felix.flush import flush_all
    from felix_ai.types import ToolCall

    calc = ToolCall(id="c1", name="calculator", args={"expression": "2+2"})
    script = [
        ScriptedTurn(content="", tool_calls=[calc], stop_reason="tool_use"),
        _answer("4"),
    ]
    async with boot(script, env=_keys(reader=["audit:read"])) as app:
        turn = await app.client.post(
            "/chat",
            json={"manifest": "quick", "messages": [{"role": "user", "content": "2+2?"}]},
            headers=_as(ADMIN),
        )
        assert turn.status_code == 200, turn.text
        await flush_all(app.settings)

        metrics = await app.client.get("/audit/metrics", headers=_as(ADMIN))
        assert metrics.status_code == 200, metrics.text
        rows = {r["tool"]: r for r in metrics.json()["tools"]}
        assert "calculator" in rows, metrics.json()
        assert rows["calculator"]["calls"] == 1, rows
        assert rows["calculator"]["errors"] == 0, rows


async def test_audit_metrics_needs_a_read_scope(boot: Any) -> None:
    """It is a projection of the audit log, so it inherits the audit log's gate."""
    async with boot([], env=_keys(reader=["jobs:read"], writer=["audit:read"])) as app:
        assert (await app.client.get("/audit/metrics", headers=_as(READER))).status_code == 403
        assert (await app.client.get("/audit/metrics", headers=_as(WRITER))).status_code == 200
