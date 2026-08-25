"""The bundled skills catalog is read once, not once per chat request.

`build_agent` runs per request, and it called `load_skills_from_dir` every time:
`rglob("SKILL.md")` plus `read_text` on every hit, synchronously, on the event loop —
so it stalled every other request on the worker, not just the one that asked. On a
network or container-overlay filesystem with more skills it is far worse than the
numbers below.

Measured on this checkout (one SKILL.md), per call:

    candidate-dir probing        20.0 µs   three is_dir() on mostly-absent paths
    rglob walk                   25.5 µs
    + read + parse               56.6 µs   total
    cached                        1.4 µs

The walk dominates, which is why the cache key is one `stat` of the root (1.0 µs) and
not a signature that needs a walk to compute — `rglob` + `stat` each is 26.7 µs, barely
better than doing the work.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from felix.skills import loader as skills_loader


@pytest.fixture(autouse=True)
def _clear_caches() -> None:
    """Tolerant of the caches not existing, so these fail against a version without
    them rather than erroring. An error says "the test could not run"; the point is to
    say "the behaviour is wrong"."""
    cache = getattr(skills_loader, "_bundled_cache", None)
    if cache is not None:
        cache.clear()
    resolver = getattr(skills_loader, "_default_bundled_dir", None)
    if resolver is not None and hasattr(resolver, "cache_clear"):
        resolver.cache_clear()


def _write_skill(root: Path, name: str, description: str = "d") -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nbody of {name}\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_the_directory_is_walked_once_not_once_per_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_skill(tmp_path, "alpha")
    walks = []
    real = skills_loader.load_skills_from_dir
    monkeypatch.setattr(
        skills_loader,
        "load_skills_from_dir",
        lambda root: (walks.append(root), real(root))[1],
    )

    for _ in range(10):
        catalog = await skills_loader.load_manifest_skills([], bundled_dir=tmp_path)
        assert "alpha" in catalog.skills

    assert len(walks) == 1, f"walked the skills directory {len(walks)} times for 10 requests"


@pytest.mark.asyncio
async def test_adding_a_skill_is_picked_up_without_waiting(tmp_path: Path) -> None:
    """The root's mtime moves when an entry is added, so this needs no TTL wait."""
    _write_skill(tmp_path, "alpha")
    first = await skills_loader.load_manifest_skills([], bundled_dir=tmp_path)
    assert set(first.skills) == {"alpha"}

    _write_skill(tmp_path, "beta")
    second = await skills_loader.load_manifest_skills([], bundled_dir=tmp_path)
    assert set(second.skills) == {"alpha", "beta"}, "a new skill directory was not noticed"


@pytest.mark.asyncio
async def test_an_edit_to_an_existing_skill_lands_once_the_ttl_expires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Editing a nested file does not move the root directory's mtime, and nothing
    cheap detects that — which is the whole reason there is a TTL as well as a stamp."""
    _write_skill(tmp_path, "alpha", description="before")
    first = await skills_loader.load_manifest_skills([], bundled_dir=tmp_path)
    assert first.skills["alpha"].description == "before"

    _write_skill(tmp_path, "alpha", description="after")
    monkeypatch.setattr(skills_loader, "_CATALOG_TTL_SECONDS", 0.0)
    skills_loader._bundled_cache.clear()  # re-stamp with the zero TTL in force

    again = await skills_loader.load_manifest_skills([], bundled_dir=tmp_path)
    assert again.skills["alpha"].description == "after"


@pytest.mark.asyncio
async def test_the_walk_happens_off_the_event_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Blocking filesystem work on the loop stalls every other request on the worker.

    Asserted by observing that the load goes through `asyncio.to_thread`, because the
    alternative — measuring loop latency — is a timing test that a fast machine passes
    for the wrong reason.
    """
    _write_skill(tmp_path, "alpha")
    threaded: list[object] = []
    real_to_thread = skills_loader.asyncio.to_thread

    async def _spy(fn, /, *args, **kwargs):
        threaded.append(fn)
        return await real_to_thread(fn, *args, **kwargs)

    monkeypatch.setattr(skills_loader.asyncio, "to_thread", _spy)
    await skills_loader.load_manifest_skills([], bundled_dir=tmp_path)
    assert skills_loader.load_skills_from_dir in threaded, "the walk ran on the event loop"


@pytest.mark.asyncio
async def test_a_caller_cannot_corrupt_the_cached_catalog(tmp_path: Path) -> None:
    """The returned catalog is per-request; the cached one is shared by every request
    that follows. Placeholder entries for unresolved refs are written into the former."""
    _write_skill(tmp_path, "alpha")
    first = await skills_loader.load_manifest_skills([{"name": "not-a-real-skill"}], bundled_dir=tmp_path)
    assert "not-a-real-skill" in first.skills, "the placeholder should reach the caller"

    second = await skills_loader.load_manifest_skills([], bundled_dir=tmp_path)
    assert set(second.skills) == {"alpha"}, "a placeholder leaked into the shared catalog"


@pytest.mark.asyncio
async def test_the_bundled_directory_is_probed_once_per_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three `is_dir()` calls per chat request, against paths derived from `__file__`
    that cannot change while the process runs."""
    probes: list[Path] = []
    real_is_dir = Path.is_dir

    def _counting_is_dir(self: Path) -> bool:
        probes.append(self)
        return real_is_dir(self)

    monkeypatch.setattr(Path, "is_dir", _counting_is_dir)
    resolver = getattr(skills_loader, "_default_bundled_dir", None)
    if resolver is None:
        # No resolver at all means the probing is still inline in load_manifest_skills.
        for _ in range(5):
            await skills_loader.load_manifest_skills([])
    else:
        for _ in range(5):
            resolver()
    assert len(probes) <= 3, f"probed the candidate directories {len(probes)} times"
