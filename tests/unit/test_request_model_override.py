"""A request's `model` override is held to the manifest's provider pin and cost cap.

The override replaces `spec.model.id` for one turn, and `_allowlisted_model` accepted any
configured route. So a caller with only `chat:write` could send a manifest pinned to Anthropic
to OpenAI, or run a capped manifest on a route Felix cannot price, with one request field.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from felix.config import Settings
from felix.manifests.governance import request_model_refusal, request_model_tree_refusal
from felix.manifests.loader import parse_manifest

from tests.support.factories import app_client, make_settings

ROUTES = {
    "known": {"provider": "anthropic", "model": "claude-sonnet-5"},
    "other": {"provider": "anthropic", "model": "claude-haiku-5-5"},
    "elsewhere": {"provider": "openai", "model": "gpt-4.1"},
    "mystery": {"provider": "anthropic", "model": "mystery-model"},
}


def _settings() -> Settings:
    return make_settings(model_routes=json.dumps(ROUTES), auth_mode="none", redis_url="")


def _manifest(name: str = "pinned", **spec: Any) -> Any:
    return parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": name},
            "spec": {"model": {"id": "known"}, **spec},
        }
    )


PIN = {"auth": {"outbound": {"providers": ["anthropic"]}}}
CAP = {"limits": {"max_cost_usd": 5.0}}


@pytest.mark.parametrize(
    ("spec", "model_id", "refusal"),
    [
        pytest.param({}, "elsewhere", None, id="no-pin-no-cap"),
        pytest.param(PIN, "elsewhere", "model_provider_not_allowed", id="off-pin"),
        pytest.param(PIN, "other", None, id="on-pin"),
        pytest.param(CAP, "mystery", "model_unpriced", id="cap-unpriced"),
        pytest.param(CAP, "other", None, id="cap-priced"),
        # The override keeps `spec.model.price`, set for the routes the manifest names.
        pytest.param(
            {**CAP, "model": {"id": "known", "price": {"input": 1.0, "output": 2.0}}},
            "other",
            "model_not_priced_by_manifest",
            id="price-override-other-route",
        ),
        pytest.param(
            {**CAP, "model": {"id": "known", "fallbacks": ["other"], "price": {"input": 1.0, "output": 2.0}}},
            "other",
            None,
            id="price-override-named-route",
        ),
        pytest.param(PIN, "not-a-route", None, id="unknown-left-to-model-layer"),
    ],
)
def test_the_refusal_follows_the_manifests_own_rules(spec: dict, model_id: str, refusal: str | None) -> None:
    assert request_model_refusal(_manifest(**spec), model_id, _settings()) == refusal


async def test_the_chat_route_refuses_an_override_off_the_pin() -> None:
    """Through the real route: the refusal is a 400 naming the reason, before any model call."""
    from felix.manifests import store as manifest_store

    settings = _settings()
    open_and_pinned = {
        "auth": {"inbound": {"allow_anonymous": True}, "outbound": {"providers": ["anthropic"]}}
    }
    await manifest_store.put_version(settings, "default", "pinned", _manifest(**open_and_pinned))
    turn = {"manifest": "pinned", "messages": [{"role": "user", "content": "hi"}]}

    async with app_client(settings) as client:
        refused = await client.post("/chat", json={**turn, "model": "elsewhere"})
        allowed = await client.post("/chat", json={**turn, "model": "other"})

    assert refused.status_code == 400, refused.text
    assert refused.json()["detail"] == "model_provider_not_allowed:elsewhere"
    # An on-pin override gets past the check; what the turn does after that is not under test.
    assert "model_provider_not_allowed" not in allowed.text and "model_not_allowlisted" not in allowed.text


OPEN = {"auth": {"inbound": {"allow_anonymous": True}}}


async def test_an_override_handed_to_a_pinned_sub_agent_is_held_to_its_pin() -> None:
    """`_child_input` passes the override to each child turn, and each child compiles from its
    own manifest: checking the unpinned parent alone let a caller's route into a pinned child."""
    from felix.manifests import store as manifest_store

    settings = _settings()
    await manifest_store.put_version(settings, "default", "leaf", _manifest("leaf", **PIN))
    await manifest_store.put_version(settings, "default", "middle", _manifest("middle", sub_agents=["leaf"]))
    router = _manifest("router", pattern="router", sub_agents=["middle"], **OPEN)
    await manifest_store.put_version(settings, "default", "router", router)

    assert request_model_refusal(router, "elsewhere", settings) is None, "the parent alone allows it"
    refusal = await request_model_tree_refusal(router, "elsewhere", settings, tenant_id="default")
    assert refusal == "model_provider_not_allowed"
    assert await request_model_tree_refusal(router, "other", settings, tenant_id="default") is None

    turn = {"manifest": "router", "messages": [{"role": "user", "content": "hi"}], "model": "elsewhere"}
    async with app_client(settings) as client:
        refused = await client.post("/chat", json=turn)
    assert refused.status_code == 400, refused.text
    assert refused.json()["detail"] == "model_provider_not_allowed:elsewhere"


async def test_a_durable_run_rechecks_its_override_on_resume() -> None:
    """Checked when the run is enqueued; a run can outlive the rules or route table it was
    checked against, so the step checks again before the turn runs."""
    from felix.durability import fibers
    from felix.durability.fibers import create_fiber, get_fiber, resume_due_fibers
    from felix.manifests import store as manifest_store

    fibers.reset_memory_fibers()
    settings = _settings()
    await manifest_store.put_version(settings, "default", "pinned", _manifest(**PIN))
    step = {"op": "invoke", "manifest_id": "pinned", "prompt": "hi", "model_id": "elsewhere"}
    created = await create_fiber(
        settings, "default", status="pending", state={"steps": [step], "cursor": 0, "stash": {}}
    )

    await resume_due_fibers(settings)

    row = await get_fiber(settings, "default", str(created["id"]))
    assert row is not None and row["status"] == "failed", row
    assert row["state_json"]["stash"]["last"]["error"] == "model_provider_not_allowed:elsewhere"
