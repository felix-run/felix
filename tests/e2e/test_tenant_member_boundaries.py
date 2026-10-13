"""Two members of one tenant, over the real stack: what one may do with the other's identity.

Threads are shared within a tenant by design, and everything here keeps that. What these pin is
the narrower set of things that are a *person's* inside a shared tenant, each of which a second
member could previously reach by holding an ordinary scope:

- a scheduled job runs as its writer, checked against the manifest's inbound auth like any
  caller -- `jobs:write` used to reach any manifest in the tenant, running it as `cron` -- and
  only its writer (or an operator) may run it by hand;
- an approval is answered once, while it is waiting, and -- under
  `FELIX_APPROVALS_REQUIRE_SECOND_PERSON` -- not by the person whose run asked;
- loosening a thread's permission mode is deciding its approvals, so it needs `approvals:write`;
- a repository checkout in a shared thread is its opener's to read, list, open over, restore
  into and remove.

All under `auth_mode=api_key` with a key per person, because every one of these checks is
skipped under `auth_mode=none`.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from felix.manifests.loader import parse_manifest
from felix_ai.providers.scripted import ScriptedTurn
from felix_ai.types import ToolCall

ADMIN = "sk-admin-not-a-secret"
ALICE = "sk-alice-not-a-secret"
BOB = "sk-bob-not-a-secret"
CAROL = "sk-carol-not-a-secret"
PINNED = "sk-pinned-not-a-secret"

_ENTRIES: dict[str, dict[str, Any]] = {
    ADMIN: {"tenant_id": "default", "sub": "admin", "scopes": ["admin"]},
    ALICE: {
        "tenant_id": "default",
        "sub": "github:42",
        "scopes": ["approvals:write", "jobs:write", "ops:run"],
    },
    BOB: {"tenant_id": "default", "sub": "github:43", "scopes": ["approvals:write", "jobs:write"]},
    CAROL: {"tenant_id": "default", "sub": "carol", "scopes": []},
    PINNED: {"tenant_id": "default", "sub": "pinned", "scopes": ["jobs:write"], "felix_manifests": ["quick"]},
}


def _keys(entries: dict[str, dict[str, Any]] | None = None, **extra: str) -> dict[str, str]:
    return {
        "FELIX_AUTH_MODE": "api_key",
        "FELIX_AUTH_API_KEYS": json.dumps(entries if entries is not None else _ENTRIES),
        **extra,
    }


def _as(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _manifest(name: str, inbound: dict[str, Any], **spec: Any) -> Any:
    return parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": name},
            "spec": {"pattern": "react", "auth": {"inbound": inbound}, **spec},
        }
    )


MANIFESTS = {
    "ops-only": _manifest("ops-only", {"required_scopes": ["ops:run"]}),
    "open": _manifest("open", {"allow_anonymous": True}),
    "gated": _manifest(
        "gated",
        {"allow_anonymous": True},
        tools=["calculator"],
        approvals=[{"id": "calc-gate", "tools": ["calculator"], "ttl_seconds": 2}],
    ),
}


# --- jobs ------------------------------------------------------------------------------------


async def _put_job(app: Any, token: str, name: str, manifest: str) -> Any:
    return await app.client.put(
        f"/jobs/{name}",
        json={"manifest_id": manifest, "payload": {"prompt": "go"}},
        headers=_as(token),
    )


async def _run_job(app: Any, name: str, token: str = ADMIN) -> dict[str, Any]:
    # By the admin unless said otherwise: a manual run is the job's writer's, never the asker's,
    # so an admin asking must not lend the admin's scopes to it.
    ran = await app.client.post(f"/jobs/{name}/run", headers=_as(token))
    assert ran.status_code == 200, ran.text
    return ran.json()


async def test_a_job_runs_as_its_writer_and_its_manifest_s_inbound_auth_applies(boot: Any) -> None:
    async with boot([ScriptedTurn(content="ops ran")], env=_keys(), manifests=MANIFESTS) as app:
        put = await _put_job(app, BOB, "ops", "ops-only")
        refused = await _run_job(app, "ops")
        assert app.spy.calls == [], "bob's job reached the model on a manifest bob may not run"

        assert (await _put_job(app, ALICE, "ops", "ops-only")).status_code == 200
        allowed = await _run_job(app, "ops")
        shown = (await app.client.get("/jobs/ops", headers=_as(ADMIN))).json()
        (listed,) = (await app.client.get("/jobs", headers=_as(ADMIN))).json()["items"]

    assert refused["status"] == "error", refused
    assert refused["error"] == "missing scopes: ops:run", refused
    assert allowed["status"] == "ok", allowed
    assert allowed["result"]["answer"] == "ops ran"
    # Who it runs for, and since when, is readable; the authority it carries is not.
    for view in (put.json(), shown, listed):
        assert "created_auth" not in view, view
        assert isinstance(view["recorded_at"], int) and view["recorded_at"] > 0, view
    assert (put.json()["created_by"], shown["created_by"]) == ("github:43", "github:42")


async def test_only_a_job_s_writer_or_an_operator_runs_it_by_hand(boot: Any) -> None:
    """Alice holds every scope bob's job needs, and `jobs:write`; asking still does not make
    bob's run happen -- it carries bob's authority, at a time bob did not choose."""
    async with boot([ScriptedTurn(content="ran")], env=_keys(), manifests=MANIFESTS) as app:
        await _put_job(app, BOB, "bobs", "open")
        by_alice = await app.client.post("/jobs/bobs/run", headers=_as(ALICE))
        assert app.spy.calls == []
        by_bob = await app.client.post("/jobs/bobs/run", headers=_as(BOB))

    assert by_alice.status_code == 403 and by_alice.json()["detail"] == "not_job_writer", by_alice.text
    assert by_bob.status_code == 200 and by_bob.json()["status"] == "ok", by_bob.text


async def test_a_job_cannot_reach_past_its_writer_s_manifest_pin(boot: Any) -> None:
    async with boot([], env=_keys(), manifests=MANIFESTS) as app:
        assert (await _put_job(app, PINNED, "pinned", "open")).status_code == 200
        run = await _run_job(app, "pinned")
        assert app.spy.calls == []

    assert run["status"] == "error", run
    assert run["error"] == "this credential may not invoke manifest 'open'", run


async def test_an_api_key_writer_s_job_carries_only_what_the_key_holds_now(boot: Any) -> None:
    """A job outlives the request that wrote it; the key it was written with may not."""
    from felix.jobs import store as jobs_store
    from felix.jobs.scheduler import fire_job, now_ms

    async with boot([], env=_keys(), manifests=MANIFESTS) as app:
        assert (await _put_job(app, ALICE, "ops", "ops-only")).status_code == 200
        job = await jobs_store.get_job(app.settings, "default", "ops")

        cut = {**_ENTRIES, ALICE: {**_ENTRIES[ALICE], "scopes": ["jobs:write"]}}
        narrowed = await fire_job(
            app.settings.model_copy(update={"auth_api_keys": json.dumps(cut)}),
            "default",
            job,
            started_at=now_ms(),
        )
        gone = {k: v for k, v in _ENTRIES.items() if k != ALICE}
        removed = await fire_job(
            app.settings.model_copy(update={"auth_api_keys": json.dumps(gone)}),
            "default",
            job,
            started_at=now_ms(),
        )
        assert app.spy.calls == []

    assert (narrowed["status"], narrowed["error"]) == ("error", "missing scopes: ops:run"), narrowed
    assert removed["status"] == "error", removed
    assert removed["error"].startswith("the API key this job's writer used is no longer configured"), removed


async def test_a_job_written_before_writers_were_recorded_runs_with_no_scopes(boot: Any) -> None:
    """Fail closed where the manifest asks for anything; run as it always did where it does not."""
    from felix.jobs import store as jobs_store

    async with boot([ScriptedTurn(content="open ran")], env=_keys(), manifests=MANIFESTS) as app:
        for name, manifest in (("old-ops", "ops-only"), ("old-open", "open")):
            await jobs_store.put_job(
                app.settings, "default", name, manifest_id=manifest, payload={"prompt": "go"}
            )
        refused = await _run_job(app, "old-ops")
        allowed = await _run_job(app, "old-open")
        # No writer to match, so by hand it is an operator's.
        by_writer = await app.client.post("/jobs/old-open/run", headers=_as(ALICE))

    assert (refused["status"], refused["error"]) == ("error", "missing scopes: ops:run"), refused
    assert allowed["status"] == "ok" and allowed["result"]["answer"] == "open ran", allowed
    assert by_writer.status_code == 403, by_writer.text


async def test_a_gated_tool_in_a_job_asks_on_behalf_of_the_job_s_writer(boot: Any) -> None:
    """The pending row names the writer, not `cron`: that is who `may_decide` and `bind_principal`
    are about."""
    from felix.approvals import store as approvals_store

    script = [
        ScriptedTurn(tool_calls=[ToolCall(id="c1", name="calculator", args={"expression": "1+1"})]),
        ScriptedTurn(content="done"),
    ]
    async with boot(script, env=_keys(), manifests=MANIFESTS) as app:
        assert (await _put_job(app, ALICE, "calc", "gated")).status_code == 200
        run = asyncio.create_task(app.client.post("/jobs/calc/run", headers=_as(ALICE)))
        pending: list[dict[str, Any]] = []
        for _ in range(200):
            pending = await approvals_store.list_approvals(app.settings, "default", status="pending")
            if pending:
                break
            await asyncio.sleep(0.01)
        await run

    (row,) = pending
    assert row["principal_subj"] == "github:42", row


# --- approvals -------------------------------------------------------------------------------


async def _seed(app: Any, *, principal: str, sig: str, **kw: Any) -> str:
    from felix.approvals import store as approvals_store

    row = await approvals_store.create_pending(
        app.settings,
        "default",
        tool_name=kw.pop("tool_name", "calculator"),
        call_signature=sig,
        manifest_id="quick",
        principal_subj=principal,
        **kw,
    )
    return str(row["id"])


async def _decide(app: Any, approval_id: str, token: str, decision: str = "approved") -> Any:
    return await app.client.post(
        f"/approvals/{approval_id}/decide", json={"decision": decision}, headers=_as(token)
    )


async def test_by_default_a_person_answers_their_own_run_s_approval(boot: Any) -> None:
    """The ordinary single-user loop: the setting is off unless an operator turns it on."""
    async with boot([], env=_keys()) as app:
        mine = await _seed(app, principal="github:42", sig="a")
        own = await _decide(app, mine, ALICE)

    assert own.status_code == 200 and own.json()["decided_by"] == "github:42", own.text


async def test_with_a_second_person_required_an_approval_is_not_answered_by_its_asker(boot: Any) -> None:
    async with boot([], env=_keys(FELIX_APPROVALS_REQUIRE_SECOND_PERSON="true")) as app:
        mine = await _seed(app, principal="github:42", sig="a")
        own = await _decide(app, mine, ALICE)
        other = await _decide(app, mine, BOB)

        admins = await _seed(app, principal="admin", sig="b")
        admin_own = await _decide(app, admins, ADMIN)

        # Leaving plan mode is the asker's own decision, and one `/chat/mode` already lets them make.
        plan = await _seed(
            app, principal="github:42", sig="c", tool_name="exit_plan_mode", rule_id="plan-approval"
        )
        plan_own = await _decide(app, plan, ALICE)

    assert own.status_code == 403 and own.json()["detail"] == "self_approval_forbidden", own.text
    assert other.status_code == 200 and other.json()["decided_by"] == "github:43", other.text
    assert admin_own.status_code == 200, admin_own.text
    assert plan_own.status_code == 200, plan_own.text


async def test_an_approval_is_answered_once_and_only_while_it_waits(boot: Any) -> None:
    from felix.approvals import store as approvals_store

    async with boot([], env=_keys()) as app:
        answered = await _seed(app, principal="github:42", sig="a")
        first = await _decide(app, answered, BOB, "denied")
        again = await _decide(app, answered, ADMIN, "approved")
        row = await approvals_store.get_approval(app.settings, "default", answered)

        lapsed = await _seed(app, principal="github:42", sig="b", ttl_seconds=-1)
        late = await _decide(app, lapsed, BOB)

    assert first.status_code == 200, first.text
    assert again.status_code == 409 and again.json()["detail"] == "approval_already_decided", again.text
    assert (row["status"], row["decided_by"]) == ("denied", "github:43"), "the first answer stands"
    assert late.status_code == 409 and late.json()["detail"] == "approval_expired", late.text


# --- permission modes ------------------------------------------------------------------------


async def _mode(app: Any, mode: str, token: str) -> Any:
    return await app.client.post("/chat/mode", json={"thread_id": "shared", "mode": mode}, headers=_as(token))


async def test_loosening_a_thread_s_mode_needs_approvals_write_and_tightening_does_not(boot: Any) -> None:
    async with boot([], env=_keys()) as app:
        tighten = await _mode(app, "plan", CAROL)
        out_of_plan = await _mode(app, "default", CAROL)
        out_by_writer = await _mode(app, "default", BOB)
        widen = await _mode(app, "accept_edits", CAROL)
        widen_by_writer = await _mode(app, "accept_edits", BOB)
        back = await _mode(app, "default", CAROL)

    assert tighten.status_code == 200, tighten.text
    assert out_of_plan.status_code == 403, out_of_plan.text
    assert out_of_plan.json()["detail"] == "missing_scope:approvals:write"
    assert out_by_writer.status_code == 200, out_by_writer.text
    assert widen.status_code == 403, widen.text
    assert widen_by_writer.status_code == 200, widen_by_writer.text
    assert back.status_code == 200, back.text


# --- repository checkouts --------------------------------------------------------------------


@pytest.fixture
def checkout_env(tmp_path: Path) -> dict[str, str]:
    (tmp_path / "ws").mkdir()
    return _keys(FELIX_DATA_DIR=str(tmp_path / "data"), FELIX_WORKSPACE_ROOT=str(tmp_path / "ws"))


_ALICES_STATE = {
    "state": "ready",
    "repo": "acme/private",
    "base": "main",
    "private": True,
    "github_user_id": 42,
    "opened_by": "github:42",
}


def _alices_checkout(settings: Any) -> Path:
    from felix.repos import checkouts

    directory = checkouts.thread_dir(settings, "default", "default:t1")
    (directory / "repo").mkdir(parents=True)
    checkouts._write_state(directory, dict(_ALICES_STATE))
    return directory


async def test_a_checkout_in_a_shared_thread_is_its_opener_s(boot: Any, checkout_env: dict[str, str]) -> None:
    from felix.repos import checkouts

    async with boot([], env=checkout_env) as app:
        directory = _alices_checkout(app.settings)
        base = "/chat/sessions/t1/workspace/repo"
        pane = ("/chat/workspace/tree", {"thread_id": "t1"})
        rewind = {"thread_id": "t1", "event_id": "nope", "restore_files": True}

        bob = {
            "get": await app.client.get(base, headers=_as(BOB)),
            "files": await app.client.get(f"{base}/files", headers=_as(BOB)),
            "open": await app.client.post(base, json={"full_name": "acme/private"}, headers=_as(BOB)),
            "delete": await app.client.delete(base, headers=_as(BOB)),
            "pane": await app.client.get(pane[0], params=pane[1], headers=_as(BOB)),
            "rewind": await app.client.post("/chat/rewind", json=rewind, headers=_as(BOB)),
        }
        state_after = checkouts.read_checkout(app.settings, "default", "default:t1")
        still_there = directory.is_dir()
        alice_get = await app.client.get(base, headers=_as(ALICE))
        alice_pane = await app.client.get(pane[0], params=pane[1], headers=_as(ALICE))
        admin_get = await app.client.get(base, headers=_as(ADMIN))
        alice_delete = await app.client.delete(base, headers=_as(ALICE))

    for what, resp in bob.items():
        assert resp.status_code == 403, (what, resp.text)
        body = resp.json()
        assert body.get("error", body.get("detail")) == "not_checkout_opener", (what, body)
    assert still_there and state_after == _ALICES_STATE, "bob's refused calls changed alice's checkout"
    assert alice_get.status_code == 200 and alice_get.json()["repo"] == "acme/private", alice_get.text
    # The empty, non-git `repo/` lists as an empty tree: what matters is that she is let in.
    assert alice_pane.status_code == 200, alice_pane.text
    assert admin_get.status_code == 200, admin_get.text
    assert alice_delete.status_code == 204, alice_delete.text
