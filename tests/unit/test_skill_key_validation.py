"""Manifest-supplied skill names must not shape the object key.

`SkillRef.name` and `version` are unvalidated strings interpolated straight into
`skills/{tenant}/{name}/SKILL.md`. Non-exploitable on any shipped backend — the fs
store rejects `..` segments and S3/GCS/memory treat keys literally — but
`artifacts.py` closed the identical hole with its own check rather than relying on
backend behaviour, and this loader did not get the same treatment.
"""

from __future__ import annotations

import pytest
from felix.skills.loader import load_skill_from_store

SKILL = "---\nname: real\ndescription: A real skill.\n---\n\nBody.\n"


class _Store:
    def __init__(self, objects: dict[str, str] | None = None) -> None:
        self._objects = objects or {}
        self.seen: list[str] = []

    async def get(self, key: str) -> bytes | None:
        self.seen.append(key)
        raw = self._objects.get(key)
        return raw.encode() if raw is not None else None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    ["../victim/secrets", "a/b", "..", ".", "", "UPPER", "has space", "x" * 65],
)
async def test_a_bad_skill_name_never_reaches_a_key(name: str) -> None:
    store = _Store()

    assert await load_skill_from_store(store, tenant_id="attacker", name=name) is None
    assert store.seen == [], f"attempted keys for {name!r}: {store.seen}"


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["../..", "a/b", "..", ""])
async def test_a_bad_version_never_reaches_a_key(version: str) -> None:
    store = _Store()

    await load_skill_from_store(store, tenant_id="t", name="real", version=version)

    assert not any(seg in {".", ".."} for k in store.seen for seg in k.split("/"))
    assert not any(version and version in k for k in store.seen)


@pytest.mark.asyncio
async def test_a_valid_skill_still_loads() -> None:
    store = _Store({"skills/t/real/SKILL.md": SKILL})

    skill = await load_skill_from_store(store, tenant_id="t", name="real")

    assert skill is not None and skill.name == "real"


@pytest.mark.asyncio
async def test_a_versioned_skill_still_loads() -> None:
    store = _Store({"skills/t/real/1.2.0/SKILL.md": SKILL})

    skill = await load_skill_from_store(store, tenant_id="t", name="real", version="1.2.0")

    assert skill is not None


@pytest.mark.asyncio
async def test_a_tenants_own_skill_wins_over_the_shared_one() -> None:
    """Tenant keys were interleaved with shared ones: a shared *versioned* skill was
    tried before the tenant's own unversioned one, the same shape as the AGENTS.md
    shadowing fixed alongside the context-file scoping."""
    store = _Store(
        {
            "skills/t/real/SKILL.md": SKILL.replace("A real skill.", "TENANT OWN"),
            "skills/real/1.0.0/SKILL.md": SKILL.replace("A real skill.", "SHARED"),
        }
    )

    skill = await load_skill_from_store(store, tenant_id="t", name="real", version="1.0.0")

    assert skill is not None
    assert "TENANT OWN" in skill.description


@pytest.mark.asyncio
async def test_the_shared_operator_skill_is_still_reachable() -> None:
    """No route lets a tenant write a bare `skills/` key, so it is an operator layer."""
    store = _Store({"skills/real/SKILL.md": SKILL})

    assert await load_skill_from_store(store, tenant_id="t", name="real") is not None


@pytest.mark.asyncio
async def test_a_bad_name_is_rejected_through_load_manifest_skills() -> None:
    """The validator only helps if the manifest entry point routes through it.

    `spec.skills` reaches `load_skill_from_store` via `load_manifest_skills`, which
    is what `build_agent` calls.
    """
    from felix.skills.loader import load_manifest_skills

    store = _Store({"skills/victim/secrets/SKILL.md": SKILL})

    catalog = await load_manifest_skills(
        [{"name": "../victim/secrets"}], tenant_id="attacker", object_store=store
    )

    assert store.seen == [], f"attempted keys: {store.seen}"
    # The ref still surfaces as a placeholder rather than vanishing silently.
    skill = catalog.get("../victim/secrets")
    assert skill is None or "not found" in skill.description.lower()
