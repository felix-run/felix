"""A caller's own skills reach the model their turn compiles, and no one else's.

Two API-key callers on one tenant and one manifest, through the production app. What decides the
catalog is the system prompt the model is handed, so that is what each test reads: a personal
skill is offered to its owner, shadows a tenant skill of its name for them alone, stays out of
every other caller's turn and out of a manifest that did not ask for personal skills, and is still
there when a durable run resumes on the worker -- the path that compiles with no request behind it.
"""

from __future__ import annotations

import json
from typing import Any

from felix.manifests.loader import parse_manifest
from felix_ai.providers.scripted import ScriptedTurn

ALICE_KEY, BOB_KEY = "sk-alice-e2e-not-a-secret", "sk-bob-e2e-not-a-secret"
ENV = {
    "FELIX_AUTH_MODE": "api_key",
    "FELIX_AUTH_API_KEYS": json.dumps(
        {
            ALICE_KEY: {"tenant_id": "default", "sub": "alice", "scopes": ["admin"]},
            BOB_KEY: {"tenant_id": "default", "sub": "bob", "scopes": ["admin"]},
        }
    ),
}
ALICE = "api_key|alice"  # `personal_owner` of an API key's principal
BOB = "api_key|bob"


def _manifest(name: str, **spec: Any) -> Any:
    return parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": name},
            "spec": {"pattern": "react", "skills_declared_only": False, **spec},
        }
    )


MANIFESTS = {
    "e2e-personal": _manifest("e2e-personal", personal_skills="read"),
    "e2e-org-only": _manifest("e2e-org-only", skills=[{"name": "notes"}]),
    "e2e-personal-durable": _manifest(
        "e2e-personal-durable", personal_skills="read", execution={"mode": "durable"}
    ),
}


async def _publish(settings: Any, owner: str, name: str, description: str) -> None:
    """A live skill in ``owner``'s library, written the way a save writes one: bytes at the
    store's own key, a row carrying their digest, then a publish."""
    from felix.skills.copy_rule import digest
    from felix.skills.library_store import get_skill_library_store
    from felix.storage import get_object_store

    lib = get_skill_library_store(settings, owner=owner)
    body = f"---\nname: {name}\ndescription: {description}\n---\n\nDo the thing.\n".encode()
    await get_object_store(settings).put(lib.object_key("default", name, "0.1.0", "SKILL.md"), body)
    row = {
        "name": name,
        "version": "0.1.0",
        "status": "draft",
        "source": "operator",
        "security_status": "pass",
        "description": description,
        "created_at": 1,
    }
    files = [{"path": "SKILL.md", "sha256": digest(body), "size": len(body)}]
    await lib.insert_version("default", row, files, created_by=owner or "ops", at=1)
    await lib.publish("default", name, "0.1.0", from_statuses={"draft"}, by=owner or "ops", at=2)


async def _seed(settings: Any) -> None:
    from felix.skills.library_keys import ORG_OWNER

    await _publish(settings, ORG_OWNER, "notes", "The tenant's shared notes")
    await _publish(settings, ALICE, "notes", "Alice's own notes")
    await _publish(settings, ALICE, "alice-drafts", "Alice's drafting habits")
    await _publish(settings, BOB, "bob-ledger", "Bob's ledger rules")


async def _system_prompt(app: Any, key: str, manifest: str) -> str:
    resp = await app.client.post(
        "/chat",
        json={"manifest": manifest, "messages": [{"role": "user", "content": "hi"}]},
        headers={"authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200, resp.text
    return _system(app.spy.prompts[-1])


def _system(prompt: list[Any]) -> str:
    return "\n".join(str(m.content) for m in prompt if m.role == "system")


async def test_each_caller_is_offered_their_own_skills_and_no_one_elses(boot: Any) -> None:
    async with boot([ScriptedTurn(content="ok")] * 2, env=ENV, manifests=MANIFESTS) as app:
        await _seed(app.settings)
        alice = await _system_prompt(app, ALICE_KEY, "e2e-personal")
        bob = await _system_prompt(app, BOB_KEY, "e2e-personal")

    assert "Alice's drafting habits" in alice and "Alice's own notes" in alice
    assert "The tenant's shared notes" not in alice, "her notes shadow the tenant's, for her"
    assert "Bob's ledger rules" not in alice
    assert "Bob's ledger rules" in bob and "The tenant's shared notes" in bob
    assert "Alice" not in bob, "nothing of Alice's reaches Bob's turn"


async def test_a_manifest_that_did_not_ask_offers_no_ones_own_skills(boot: Any) -> None:
    async with boot([ScriptedTurn(content="ok")], env=ENV, manifests=MANIFESTS) as app:
        await _seed(app.settings)
        alice = await _system_prompt(app, ALICE_KEY, "e2e-org-only")

    assert "The tenant's shared notes" in alice
    assert "Alice" not in alice


async def test_a_durable_run_resumes_with_its_starters_skills(boot: Any) -> None:
    """The worker compiles with no request behind it; the owner recorded at enqueue is the only
    thing that can say whose skills the run had."""
    from felix.durability.fibers import resume_due_fibers

    async with boot([ScriptedTurn(content="ok")], env=ENV, manifests=MANIFESTS) as app:
        await _seed(app.settings)
        resp = await app.client.post(
            "/chat",
            json={"manifest": "e2e-personal-durable", "messages": [{"role": "user", "content": "hi"}]},
            headers={"authorization": f"Bearer {ALICE_KEY}"},
        )
        assert resp.status_code == 202, resp.text
        assert app.spy.prompts == [], "nothing ran before the worker claimed it"
        await resume_due_fibers(app.settings)
        resumed = _system(app.spy.prompts[-1])

    assert "Alice's drafting habits" in resumed and "Alice's own notes" in resumed
    assert "The tenant's shared notes" not in resumed
