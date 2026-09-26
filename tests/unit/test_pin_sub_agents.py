"""The sub-agent digest a pin records: what moves it, what does not, and old pins upgrading.

The e2e test drives a pinned thread through an edit. These pin the digest's own properties —
nested children, a child that resolves nowhere, cycles — and the one transition that could
otherwise be an outage: a thread pinned before this existed, which must not drift on its
next turn just because its pin has no digest to compare.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.manifests.loader import parse_manifest
from felix.manifests.pin import ManifestDriftError, assert_pin_matches, ensure_thread_pin, sub_agents_hash


def _agent(name: str, **spec: Any) -> Any:
    return parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": name}, "spec": spec}
    )


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The tenant's manifests, resolved by name the way `resolve_tenant_manifest` would."""
    from felix import runtime

    manifests: dict[str, Any] = {}

    async def resolve(settings: Any, tenant_id: str, name: str, **_: Any) -> Any:
        if name not in manifests:
            raise LookupError(f"Unknown manifest: {name}")

        class _Resolved:
            manifest = manifests[name]

        return _Resolved()

    monkeypatch.setattr(runtime, "resolve_tenant_manifest", resolve)
    return manifests


@pytest.mark.asyncio
async def test_a_manifest_without_sub_agents_has_no_digest(store: dict[str, Any]) -> None:
    assert await sub_agents_hash(None, "t", _agent("solo")) is None


@pytest.mark.asyncio
async def test_the_digest_moves_with_a_child_or_a_grandchild_and_only_then(store: dict[str, Any]) -> None:
    parent = _agent("parent", pattern="router", sub_agents=["child"])
    store["child"] = _agent("child", pattern="router", sub_agents=["grandchild"])
    store["grandchild"] = _agent("grandchild", system_prompt={"inline": "v1"})
    before = await sub_agents_hash(None, "t", parent)
    assert before == await sub_agents_hash(None, "t", parent), "stable when nothing changed"

    store["grandchild"] = _agent("grandchild", system_prompt={"inline": "v2"})
    assert await sub_agents_hash(None, "t", parent) != before, "a grandchild edit reaches the parent's pin"


@pytest.mark.asyncio
async def test_a_missing_child_or_a_cycle_is_recorded_not_raised(store: dict[str, Any]) -> None:
    store["a"] = _agent("a", pattern="router", sub_agents=["b"])
    store["b"] = _agent("b", pattern="router", sub_agents=["a", "nowhere"])
    assert await sub_agents_hash(None, "t", store["a"]) is not None


def test_a_pin_without_a_digest_does_not_drift_and_one_with_it_does() -> None:
    m = _agent("parent", pattern="router", sub_agents=["child"], governance={"pin_compile": True})
    from felix.manifests.pin import manifest_content_hash

    base = {"manifest_hash": manifest_content_hash(m), "pin_compile": True}
    assert_pin_matches(base, m, sub_agents="new")  # an old pin: nothing to compare
    with pytest.raises(ManifestDriftError, match="sub-agent"):
        assert_pin_matches({**base, "sub_agents_hash": "old"}, m, sub_agents="new")


@pytest.mark.asyncio
async def test_an_old_pin_is_upgraded_on_its_next_turn_then_enforced(store: dict[str, Any]) -> None:
    """A thread pinned before sub-agents were covered carries on, gains a digest, and is held
    to it from then on."""
    from felix.config import Settings
    from felix.manifests.pin import manifest_content_hash
    from felix.session.thread_state import get_thread_meta, update_thread_meta

    settings = Settings(database_url="memory://pin-upgrade", object_store="memory")
    parent = _agent("parent", pattern="router", sub_agents=["child"], governance={"pin_compile": True})
    store["child"] = _agent("child", system_prompt={"inline": "v1"})
    await update_thread_meta(
        settings=settings,
        tenant_id="t",
        thread_id="th",
        manifest_name="parent",
        manifest_hash=manifest_content_hash(parent),
        pin_compile=True,
    )

    await ensure_thread_pin(settings=settings, tenant_id="t", thread_id="th", manifest=parent)
    meta = await get_thread_meta(settings=settings, tenant_id="t", thread_id="th")
    assert meta.get("sub_agents_hash"), "the old pin gained a digest"

    store["child"] = _agent("child", system_prompt={"inline": "v2"})
    with pytest.raises(ManifestDriftError, match="sub-agent"):
        await ensure_thread_pin(settings=settings, tenant_id="t", thread_id="th", manifest=parent)


@pytest.mark.asyncio
async def test_a_resuming_durable_run_is_held_to_its_childrens_digest(store: dict[str, Any]) -> None:
    """What `durability/fibers.py` calls on resume: a pin taken at enqueue with a digest is
    enforced against the children as they resolve now; one without a digest is not."""
    from felix.manifests.pin import assert_resume_pin, pin_fields_for

    parent = _agent("parent", pattern="router", sub_agents=["child"])
    store["child"] = _agent("child", system_prompt={"inline": "v1"})
    pinned = {**(await pin_fields_for(None, "t", parent)), "pin_compile": True}
    await assert_resume_pin(None, "t", pinned, parent)

    store["child"] = _agent("child", system_prompt={"inline": "v2"})
    with pytest.raises(ManifestDriftError, match="sub-agent"):
        await assert_resume_pin(None, "t", pinned, parent)
    await assert_resume_pin(None, "t", {**pinned, "sub_agents_hash": None}, parent)


@pytest.mark.asyncio
async def test_a_shared_child_is_resolved_once_however_many_parents_name_it(
    store: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A diamond — every child naming the same grandchildren — cost fan-out to the power of
    depth without the memo, on a process other tenants share."""
    from felix import runtime

    fan = [f"c{i}" for i in range(6)]
    store.update({name: _agent(name, pattern="router", sub_agents=["g0", "g1"]) for name in fan})
    store.update({g: _agent(g) for g in ("g0", "g1")})
    real = runtime.resolve_tenant_manifest
    calls: list[str] = []

    async def counting(settings: Any, tenant_id: str, name: str, **kw: Any) -> Any:
        calls.append(name)
        return await real(settings, tenant_id, name, **kw)

    monkeypatch.setattr(runtime, "resolve_tenant_manifest", counting)
    await sub_agents_hash(None, "t", _agent("root", pattern="router", sub_agents=fan))
    assert sorted(calls) == sorted([*fan, "g0", "g1"]), "each name resolved exactly once"
