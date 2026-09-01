"""`GET /manifests/{name}` reports the variant that actually serves a thread.

The route has always returned a `variant` field, but resolved without a thread
id — and `pick_variant` short-circuits to "stable" when the thread is empty. So
for any rollout between 1% and 99% the field was a constant, which is worse than
absent: a client reading it would confidently show the wrong side.
"""

from __future__ import annotations

import pytest
from felix.config import Settings
from felix.manifests.resolver import pick_variant
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def settings() -> Settings:
    return Settings(
        allow_insecure=True,
        auth_mode="none",
        environment="development",
        object_store="memory",
        database_url="memory://variant",
    )


def _threads_on_each_side(tenant: str, name: str, weight: int) -> tuple[str, str]:
    """Find one thread suffix bucketed to stable and one to canary."""
    stable: str | None = None
    canary: str | None = None
    for i in range(500):
        suffix = f"t{i}"
        side = pick_variant(
            tenant_id=tenant,
            thread_id=f"{tenant}:{suffix}",
            manifest_name=name,
            stable_version=1,
            canary_version=2,
            canary_weight=weight,
        )
        if side == "stable" and stable is None:
            stable = suffix
        elif side == "canary" and canary is None:
            canary = suffix
        if stable and canary:
            return stable, canary
    raise AssertionError("no thread pair spanning both sides")


@pytest.mark.asyncio
async def test_variant_follows_the_thread(settings: Settings) -> None:
    from felix_api.app import create_app

    app = create_app(settings=settings, plugins=[])
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        quick = (await client.get("/manifests/quick")).json()["manifest"]

        assert (await client.put("/manifests/quick", json={"manifest": quick})).status_code == 200
        assert (await client.put("/manifests/quick", json={"manifest": quick})).status_code == 200
        canary = await client.post("/manifests/quick/canary", json={"canary_version": 2, "canary_weight": 50})
        assert canary.status_code == 200

        stable_thread, canary_thread = _threads_on_each_side("default", "quick", 50)

        # Without a thread the route cannot hash, and says so consistently.
        assert (await client.get("/manifests/quick")).json()["variant"] == "stable"

        on_stable = await client.get("/manifests/quick", params={"thread_id": stable_thread})
        on_canary = await client.get("/manifests/quick", params={"thread_id": canary_thread})
        assert on_stable.json()["variant"] == "stable"
        assert on_canary.json()["variant"] == "canary"

        # Same thread, same answer — a rollout must not flap mid-conversation.
        again = await client.get("/manifests/quick", params={"thread_id": canary_thread})
        assert again.json()["variant"] == "canary"


@pytest.mark.asyncio
async def test_thread_suffix_cannot_escape_the_tenant(settings: Settings) -> None:
    from felix_api.app import create_app

    app = create_app(settings=settings, plugins=[])
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # params= so the delimiters are percent-encoded and actually reach the
        # server; a bare "#" in the URL would be stripped as a fragment.
        for bad in ("other:t1", "t1#frag"):
            res = await client.get("/manifests/quick", params={"thread_id": bad})
            assert res.status_code == 400, bad
            assert res.json()["detail"] == "invalid_thread_id"


def test_bundled_loader_contains_the_name_within_its_directory(tmp_path) -> None:
    """`load_bundled` builds a path from a URL path segment.

    `assert_valid_manifest_name` bars separators, so nothing reachable escapes
    today. The containment check makes that a property of the loader rather than
    of a regex two modules away, and keeps the guarantee if either loosens.
    """
    from felix.manifests.loader import load_bundled

    (tmp_path / "real.yaml").write_text(
        "apiVersion: felix/v1\nkind: Agent\nmetadata:\n  name: real\n", encoding="utf-8"
    )
    outside = tmp_path.parent / "outside.yaml"
    outside.write_text("apiVersion: felix/v1\nkind: Agent\nmetadata:\n  name: outside\n", encoding="utf-8")

    assert load_bundled("real", bundled_dir=tmp_path).metadata.name == "real"

    # Separators never get as far as the loader.
    for escape in ("../outside", "/etc/passwd", "a/../../b"):
        with pytest.raises(ValueError):
            load_bundled(escape, bundled_dir=tmp_path)

    # Dot-only names stay inside: an extension is always appended.
    with pytest.raises(FileNotFoundError):
        load_bundled("..", bundled_dir=tmp_path)


@pytest.mark.asyncio
async def test_bundled_posture_never_reads_the_store(tmp_path) -> None:
    """`manifest_source=bundled` removes the layers rather than finding them empty.

    The point of the posture is that Postgres and the object store are not consulted at
    all, so a deployment that sets it has no runtime-authoring surface to guard. A store
    that raises if touched is the only way to assert absence rather than emptiness.
    """
    from felix.config import Settings
    from felix.manifests.resolver import resolve_manifest

    (tmp_path / "solo.yaml").write_text(
        "apiVersion: felix/v1\nkind: Agent\nmetadata:\n  name: solo\nspec:\n  pattern: react\n"
    )

    class _Explodes:
        async def get_active(self, *a, **k):
            raise AssertionError("the manifest store was read under manifest_source=bundled")

        async def get_version(self, *a, **k):
            raise AssertionError("the manifest store was read under manifest_source=bundled")

        async def get(self, *a, **k):
            raise AssertionError("the object store was read under manifest_source=bundled")

    settings = Settings(database_url="memory://t", manifest_source="bundled")
    resolved = await resolve_manifest(
        settings,
        "default",
        "solo",
        manifest_store=_Explodes(),
        object_store=_Explodes(),
        bundled_dir=tmp_path,
    )
    assert resolved.source == "bundled"
    assert resolved.manifest.metadata.name == "solo"


@pytest.mark.asyncio
async def test_bundled_posture_rejects_a_pinned_version(tmp_path) -> None:
    """A pin names a stored version, and under this posture there are none."""
    from felix.config import Settings
    from felix.manifests.resolver import resolve_manifest

    (tmp_path / "solo.yaml").write_text(
        "apiVersion: felix/v1\nkind: Agent\nmetadata:\n  name: solo\nspec:\n  pattern: react\n"
    )
    settings = Settings(database_url="memory://t", manifest_source="bundled")
    with pytest.raises(LookupError):
        await resolve_manifest(settings, "default", "solo", pin_version=3, bundled_dir=tmp_path)
