"""One version number, and the list of where it lives cannot go stale.

`v0.2.1` shipped with the chart's `image.tag` pointing at the previous image because the
release edited every version-bearing file from memory. `scripts/bump-version.py` is now
the list; this proves the list matches the tree and the release workflow refuses a tag
that disagrees.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import ModuleType

import pytest

from tests._scripts import load_script

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def bump() -> ModuleType:
    return load_script("bump-version")


@pytest.fixture
def scratch_tree(bump: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A copy of every version-bearing file, with the script pointed at it."""
    for rel, _field, _pattern in bump.LOCATIONS:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((ROOT / rel).read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(bump, "ROOT", tmp_path)
    monkeypatch.setattr(bump.subprocess, "run", lambda *a, **k: None)  # no `uv lock` on a scratch tree
    return tmp_path


def test_every_location_agrees(bump: ModuleType) -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", bump.check(None))


def test_the_script_lists_every_file_the_tree_carries_a_version_in(bump: ModuleType) -> None:
    """A new workspace member or chart field must be added to the script, not forgotten."""
    listed = {(rel, field) for rel, field, _ in bump.LOCATIONS}
    pyprojects = {
        (str(p.relative_to(ROOT)), "version")
        for p in [
            ROOT / "pyproject.toml",
            *ROOT.glob("packages/*/pyproject.toml"),
            *ROOT.glob("apps/*/pyproject.toml"),
        ]
        if re.search(r'^version = "', p.read_text(encoding="utf-8"), re.MULTILINE)
    }
    inits = {
        (str(p.relative_to(ROOT)), "__version__")
        for p in [*ROOT.glob("packages/*/src/*/__init__.py"), *ROOT.glob("apps/*/src/*/__init__.py")]
        if "__version__" in p.read_text(encoding="utf-8")
    }
    helm = {
        ("deploy/helm/felix/Chart.yaml", "version"),
        ("deploy/helm/felix/Chart.yaml", "appVersion"),
        ("deploy/helm/felix/values.yaml", "image.tag"),
    }
    expected = pyprojects | inits | helm
    assert listed == expected, {
        "missing from the script": sorted(expected - listed),
        "listed but not in the tree": sorted(listed - expected),
    }


def test_a_disagreeing_tree_is_refused_and_names_the_field(bump: ModuleType, scratch_tree: Path) -> None:
    values = scratch_tree / "deploy/helm/felix/values.yaml"
    values.write_text(
        values.read_text(encoding="utf-8").replace('  tag: "', '  tag: "9.9.9-', 1), encoding="utf-8"
    )
    with pytest.raises(SystemExit, match=r"(?s)disagrees.*values\.yaml image\.tag") as exc:
        bump.check(None)
    assert "9.9.9-" in str(exc.value)
    values.write_text((ROOT / "deploy/helm/felix/values.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(SystemExit, match=r"expected 1\.0\.0"):
        bump.check("1.0.0")
    with pytest.raises(SystemExit, match="not a semantic version"):
        bump.check("1.0.0$(id)")  # a tag name may carry this; the shell never sees it


def test_bump_rewrites_every_location_without_touching_anything_else(
    bump: ModuleType, scratch_tree: Path
) -> None:
    before = bump.check(None)
    bump.bump("1.2.3")
    assert bump.check("1.2.3") == "1.2.3"
    for rel, _field, _pattern in bump.LOCATIONS:
        original = (ROOT / rel).read_text(encoding="utf-8")
        assert (scratch_tree / rel).read_text(encoding="utf-8") == original.replace(before, "1.2.3")
