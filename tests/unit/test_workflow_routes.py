"""`/workflows`: saves conditional on HTTP's own headers, and the ports and problems beside each one.

Driven through `create_app` over ASGI, so the middleware, the scope checks and the response models
are the production ones.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from felix.audit import store as audit_store
from felix.workflows import store as workflow_store

from tests.support.factories import app_client, make_settings


def _definition(**overrides: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "name": "Triage issue",
        "inputs": {"issue": {"type": "string"}},
        "steps": [
            {
                "id": "classify",
                "kind": "agent_turn",
                "config": {
                    "manifest": "quick",
                    "prompt": "Label this issue: {{ issue }}",
                    "output_schema": {
                        "type": "object",
                        "properties": {"label": {"type": "string", "enum": ["bug", "feature"]}},
                        "required": ["label"],
                    },
                },
            },
            {"id": "route", "kind": "branch", "config": {"cases": ["bug", "feature"]}},
        ],
        "edges": [
            {"source": {"step": "trigger", "port": "issue"}, "target": {"step": "classify", "port": "issue"}},
            {"source": {"step": "classify", "port": "label"}, "target": {"step": "route", "port": "value"}},
        ],
    }
    spec.update(overrides)
    return spec


async def test_a_save_returns_the_definition_its_ports_its_problems_and_an_etag() -> None:
    async with app_client() as client:
        created = await client.put("/workflows/triage", json=_definition())
        assert created.status_code == 201
        body = created.json()
        assert created.headers["etag"] == f'"{body["revision"]}"'
        assert body["problems"] == []
        assert body["definition"]["name"] == "Triage issue"
        assert set(body["ports"]) == {"trigger", "classify", "route"}
        label = next(p for p in body["ports"]["classify"]["outputs"] if p["name"] == "label")
        assert label == {
            "name": "label",
            "type": "enum<bug|feature>",
            "schema": {"type": "string", "enum": ["bug", "feature"]},
            "control": False,
            "required": False,
        }
        when = body["ports"]["route"]["inputs"][-1]
        assert (when["name"], when["type"], when["control"]) == ("when", "control", True)

        replaced = await client.put("/workflows/triage", json=_definition(name="Triage v2"))
        assert replaced.status_code == 200
        assert replaced.json()["version"] == 2

        read = await client.get("/workflows/triage")
        assert read.status_code == 200
        assert read.headers["etag"] == replaced.headers["etag"]
        assert read.json() == replaced.json()


async def test_a_half_wired_draft_saves_and_says_what_is_missing() -> None:
    draft = _definition(edges=[])
    async with app_client() as client:
        saved = await client.put("/workflows/draft", json=draft)
        assert saved.status_code == 201
        assert [(p["code"], p["step"], p["port"]) for p in saved.json()["problems"]] == [
            ("unconnected_input", "classify", "issue"),
            ("unconnected_input", "route", "value"),
        ]


async def test_if_match_saves_only_over_the_revision_it_names() -> None:
    async with app_client() as client:
        first = await client.put("/workflows/triage", json=_definition())
        theirs = await client.put(
            "/workflows/triage", json=_definition(name="Theirs"), headers={"If-Match": first.headers["etag"]}
        )
        assert theirs.status_code == 200

        stale = await client.put(
            "/workflows/triage", json=_definition(name="Mine"), headers={"If-Match": first.headers["etag"]}
        )
        assert stale.status_code == 412
        detail = stale.json()["detail"]
        assert detail["error"] == "workflow_changed"
        assert detail["current"]["revision"] == theirs.json()["revision"]
        assert detail["current"]["definition"]["name"] == "Theirs"
        assert "ports" in detail["current"], "the editor redraws *theirs* from the same shape"

        # A weak tag compares strongly, so it matches nothing.
        weak = await client.put(
            "/workflows/triage", json=_definition(), headers={"If-Match": "W/" + theirs.headers["etag"]}
        )
        assert weak.status_code == 412


async def test_if_none_match_star_creates_only() -> None:
    async with app_client() as client:
        assert (
            await client.put("/workflows/new", json=_definition(), headers={"If-None-Match": "*"})
        ).status_code == 201
        again = await client.put("/workflows/new", json=_definition(), headers={"If-None-Match": "*"})
        assert again.status_code == 412
        assert again.json()["detail"]["current"]["version"] == 1


@pytest.mark.parametrize(
    ("headers", "detail"),
    [
        ({"If-Match": '"a.1"', "If-None-Match": "*"}, "conflicting_preconditions"),
        ({"If-None-Match": '"a.1"'}, "if_none_match_must_be_star"),
        ({"If-Match": "*"}, "if_match_must_name_one_revision"),
        ({"If-Match": '"a.1", "b.2"'}, "if_match_must_name_one_revision"),
    ],
)
async def test_preconditions_an_editor_would_not_send_are_refused(
    headers: dict[str, str], detail: str
) -> None:
    async with app_client() as client:
        refused = await client.put("/workflows/triage", json=_definition(), headers=headers)
        assert refused.status_code == 400
        assert refused.json()["detail"] == detail


async def test_delete_honours_if_match_and_is_audited() -> None:
    settings = make_settings()
    async with app_client(settings) as client:
        saved = await client.put("/workflows/triage", json=_definition())
        assert (
            await client.delete("/workflows/triage", headers={"If-Match": '"stale.1"'})
        ).status_code == 412
        assert (
            await client.delete("/workflows/triage", headers={"If-Match": saved.headers["etag"]})
        ).status_code == 200
        assert (await client.get("/workflows/triage")).status_code == 404
        assert (await client.delete("/workflows/triage")).status_code == 404

        await audit_store.flush_pending(settings)
        for event_type in ("workflow_saved", "workflow_deleted"):
            audit = (await client.get("/audit", params={"event_type": event_type})).json()["items"]
            assert len(audit) == 1, event_type
            assert audit[0]["payload_json"]["workflow_id"] == "triage"


async def test_the_listing_summarises_without_definitions() -> None:
    async with app_client() as client:
        await client.put("/workflows/alpha", json=_definition(name="Alpha"))
        await client.put("/workflows/bravo", json=_definition(name="Bravo", steps=[], edges=[]))
        listed = await client.get("/workflows")
        assert listed.status_code == 200
        body = listed.json()
        assert body["limit"] == workflow_store.MAX_PER_TENANT
        assert [(w["id"], w["name"], w["step_count"]) for w in body["items"]] == [
            ("bravo", "Bravo", 0),
            ("alpha", "Alpha", 2),
        ]
        assert "definition" not in body["items"][0]


async def test_check_answers_without_saving() -> None:
    async with app_client() as client:
        checked = await client.post("/workflows/-/check", json=_definition(edges=[]))
        assert checked.status_code == 200
        assert [p["code"] for p in checked.json()["problems"]] == ["unconnected_input", "unconnected_input"]
        assert (await client.get("/workflows")).json()["items"] == []


async def test_kinds_lists_the_registry_with_each_config_schema() -> None:
    async with app_client() as client:
        kinds = (await client.get("/workflows/-/kinds")).json()["items"]
    by_name = {k["name"]: k for k in kinds}
    assert {"agent_turn", "tool_call", "approval", "branch"} <= set(by_name)
    assert by_name["approval"]["label"] == "Approval gate"
    assert "ttl_seconds" in by_name["approval"]["config_schema"]["properties"]


async def test_a_stored_definition_that_no_longer_parses_is_reported_not_failed() -> None:
    settings = make_settings()
    async with app_client(settings) as client:
        await client.put("/workflows/old", json=_definition())
        # What a later schema change does to a stored row: a key the model no longer accepts.
        workflow_store._memory_workflows[("default", "old")]["definition"]["retired_field"] = 1
        read = await client.get("/workflows/old")
        assert read.status_code == 200
        body = read.json()
        assert body["ports"] == {}
        assert [p["code"] for p in body["problems"]] == ["invalid_definition"]
        assert "retired_field" in body["problems"][0]["message"]


@pytest.mark.parametrize("bad", ["Triage", "-x", "a" * 65, "tri age"])
async def test_an_id_outside_the_alphabet_is_a_422(bad: str) -> None:
    async with app_client() as client:
        assert (await client.put(f"/workflows/{bad}", json=_definition())).status_code == 422


async def test_the_tenant_cap_is_a_409(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workflow_store, "MAX_PER_TENANT", 1)
    async with app_client() as client:
        assert (await client.put("/workflows/one", json=_definition())).status_code == 201
        refused = await client.put("/workflows/two", json=_definition())
        assert refused.status_code == 409
        assert refused.json()["detail"] == {"error": "workflow_limit", "limit": 1}
        # Replacing one at the cap is not creating one.
        assert (await client.put("/workflows/one", json=_definition(name="v2"))).status_code == 200


async def test_reads_need_workflows_read_and_writes_workflows_write() -> None:
    keys = {
        "reader-token": {"tenant_id": "default", "sub": "ops", "scopes": ["workflows:read"]},
        "writer-token": {"tenant_id": "default", "sub": "ops", "scopes": ["workflows:write"]},
        "other-token": {"tenant_id": "default", "sub": "ops", "scopes": ["plans:write"]},
    }
    settings = make_settings(auth_mode="api_key", auth_api_keys=json.dumps(keys))
    reader, writer, other = ({"Authorization": f"Bearer {k}"} for k in keys)
    async with app_client(settings) as client:
        assert (await client.get("/workflows", headers=other)).status_code == 403
        assert (await client.put("/workflows/w", json=_definition(), headers=reader)).status_code == 403
        assert (await client.put("/workflows/w", json=_definition(), headers=writer)).status_code == 201
        assert (await client.get("/workflows/w", headers=reader)).status_code == 200
        assert (await client.get("/workflows/w", headers=writer)).status_code == 200, "write implies read"
        assert (await client.post("/workflows/-/check", json=_definition(), headers=other)).status_code == 403
        assert (await client.delete("/workflows/w", headers=reader)).status_code == 403


async def test_a_412_carries_the_current_revision_as_its_etag() -> None:
    async with app_client() as client:
        first = await client.put("/workflows/triage", json=_definition())
        second = await client.put("/workflows/triage", json=_definition(name="Theirs"))
        stale = await client.put(
            "/workflows/triage", json=_definition(), headers={"If-Match": first.headers["etag"]}
        )
        assert stale.status_code == 412
        assert stale.headers["etag"] == second.headers["etag"]


async def test_an_unconditional_save_that_loses_a_create_race_is_a_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # What the store raises when the other create's insert won the primary key.
    async def lost_race(*_a: Any, **kw: Any) -> Any:
        assert kw["precondition"] is None
        raise workflow_store.WorkflowConflict("triage", None)

    async with app_client() as client:
        monkeypatch.setattr(workflow_store, "put_workflow", lost_race)
        lost = await client.put("/workflows/triage", json=_definition())
    assert lost.status_code == 409
    assert lost.json()["detail"] == {"error": "workflow_changed", "current": None}


async def test_a_long_name_is_quoted_short_and_problems_are_capped() -> None:
    from felix.workflows.check import MAX_PROBLEMS

    long_name = "x" * 500
    # Two problems a step: `body` is fed an object missing `long_name`, `title` is not fed at all.
    steps = [
        {
            "id": f"post{i}",
            "kind": "tool_call",
            "config": {
                "manifest": "quick",
                "tool": "t",
                "input_schema": {
                    "properties": {
                        "body": {"type": "object", "properties": {}, "required": [long_name]},
                        "title": {"type": "string"},
                    },
                    "required": ["body", "title"],
                },
            },
        }
        for i in range(150)
    ]
    edges = [
        {"source": {"step": "trigger", "port": "issue"}, "target": {"step": s["id"], "port": "body"}}
        for s in steps
    ]
    definition = _definition(inputs={"issue": {"type": "object", "properties": {}}}, steps=steps, edges=edges)
    async with app_client() as client:
        checked = await client.post("/workflows/-/check", json=definition)
    assert checked.status_code == 200, checked.text[:500]
    problems = checked.json()["problems"]
    assert len(problems) == MAX_PROBLEMS + 1
    assert problems[-1] == {
        "code": "too_many_problems",
        "message": f"{300 - MAX_PROBLEMS} more problems not listed",
        "step": None,
        "port": None,
    }
    assert all(len(p["message"]) < 200 for p in problems)


async def test_an_edge_listed_twice_is_a_422() -> None:
    definition = _definition()
    definition["edges"].append(definition["edges"][0])
    async with app_client() as client:
        assert (await client.post("/workflows/-/check", json=definition)).status_code == 422


async def test_a_kind_with_no_json_schema_is_listed_without_a_form(monkeypatch: pytest.MonkeyPatch) -> None:
    from felix.workflows import kinds
    from pydantic import BaseModel

    class Opaque(BaseModel):
        @classmethod
        def model_json_schema(cls, *_a: Any, **_kw: Any) -> dict[str, Any]:  # type: ignore[override]
            raise TypeError("cannot describe")

    monkeypatch.setitem(
        kinds._kinds, "opaque", kinds.StepKind(config=Opaque, ports=lambda _c: ([], []), label="Opaque")
    )
    async with app_client() as client:
        listed = await client.get("/workflows/-/kinds")
    assert listed.status_code == 200
    by_name = {k["name"]: k for k in listed.json()["items"]}
    assert by_name["opaque"]["config_schema"] == {}
    assert by_name["approval"]["config_schema"]


def test_a_listed_tool_whose_schema_cannot_become_ports_says_so() -> None:
    from felix.tools.executor import local_executor
    from felix.tools.types import Tool
    from felix.workflows.present import step_tool_view as _step_tool

    async def run(_args: Any, _ctx: Any = None) -> str:
        return ""

    def tool(schema: dict[str, Any]) -> Tool:
        return Tool(
            name="t", description="d", args_schema=None, executor=local_executor(run), raw_input_schema=schema
        )

    fine = _step_tool("quick", tool({"type": "object", "properties": {"pullNumber": {"type": "integer"}}}))
    assert (fine["problem"], [p["name"] for p in fine["inputs"]]) == (None, ["pullNumber"])
    for schema in (
        {"type": "object", "properties": {"when": {}}},
        {"type": "object", "properties": {"q": {}}, "required": "q"},
    ):
        listed = _step_tool("quick", tool(schema))
        assert listed["inputs"] is None
        assert listed["problem"] == "this tool's input schema cannot become step ports"
        assert listed["input_schema"] == schema, "the schema is still shown, so the author can see why"


async def test_a_save_records_its_creator_and_a_replace_keeps_them() -> None:
    # A workflow made through the routes is a person's, never an agent's: `authored_via` stays
    # empty, so `spec.workflow_authoring` may not change it, whoever its creator is.
    keys = {
        "ada-token": {"tenant_id": "default", "sub": "ada", "scopes": ["workflows:write"]},
        "bob-token": {"tenant_id": "default", "sub": "bob", "scopes": ["workflows:write"]},
    }
    settings = make_settings(auth_mode="api_key", auth_api_keys=json.dumps(keys))
    ada, bob = ({"Authorization": f"Bearer {k}"} for k in keys)
    async with app_client(settings) as client:
        created = (await client.put("/workflows/w", json=_definition(), headers=ada)).json()
        assert (created["created_by"], created["authored_via"], created["updated_by"]) == ("ada", "", "ada")
        replaced = (await client.put("/workflows/w", json=_definition(), headers=bob)).json()
        assert (replaced["created_by"], replaced["authored_via"], replaced["updated_by"]) == (
            "ada",
            "",
            "bob",
        )
        listed = (await client.get("/workflows", headers=bob)).json()["items"]
        assert [(i["created_by"], i["authored_via"]) for i in listed] == [("ada", "")]


async def test_an_agent_authored_workflow_reads_back_as_one() -> None:
    # Every route-made workflow has an empty `authored_via`, so only a row an agent wrote proves
    # the routes return the stored value rather than a constant.
    from felix.workflows.schema import WorkflowSpec

    settings = make_settings()
    await workflow_store.put_workflow(
        settings,
        "default",
        "agent-made",
        WorkflowSpec.model_validate(_definition()),
        updated_by="ada",
        created_by="ada",
        authored_via="agent:quick",
    )
    async with app_client(settings) as client:
        read = (await client.get("/workflows/agent-made")).json()
        assert (read["created_by"], read["authored_via"]) == ("ada", "agent:quick")
        listed = (await client.get("/workflows")).json()["items"]
        assert [(i["id"], i["authored_via"]) for i in listed] == [("agent-made", "agent:quick")]
