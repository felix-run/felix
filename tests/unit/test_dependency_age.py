"""The dependency hold reads the lock as written and judges age, nothing else."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest

from tests._scripts import load_script

LOCK = """
version = 1

[[package]]
name = "felix-harness"
version = "0.2.2"
source = { editable = "packages/harness" }

[[package]]
name = "httpx"
version = "0.28.1"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "vendored-thing"
version = "1.0.0"
source = { git = "https://github.com/example/thing?rev=abc#abc" }
"""


@pytest.fixture
def age() -> ModuleType:
    return load_script("check-dependency-age")


def test_only_pypi_packages_are_asked_about(age: ModuleType) -> None:
    assert age.locked_pypi_packages(LOCK) == [("httpx", "0.28.1")]


def test_a_version_inside_the_hold_is_named_and_an_allowlisted_one_is_not(age: ModuleType) -> None:
    now = datetime.now(UTC)
    stamps = {
        ("fresh", "1.0.0"): now - timedelta(hours=3),
        ("old", "1.0.0"): now - timedelta(days=30),
        ("unknown", "9.9.9"): None,
    }
    young = age.too_young(stamps, hours=48, allow=set())
    assert [(n, v) for n, v, _ in young] == [("fresh", "1.0.0")]
    assert age.too_young(stamps, hours=48, allow={"fresh"}) == []
    assert age.too_young(stamps, hours=1, allow=set()) == [], "a 1h hold does not reach a 3h-old release"


def test_a_pypi_lookup_failure_is_not_a_hold_violation(
    age: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An outage of the metadata API must not block every PR; it reports nothing, and the
    lock check beside it still runs."""
    monkeypatch.setattr(age.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    assert age.uploaded_at("httpx", "0.28.1") is None


def test_every_lookup_failing_is_an_outage_not_a_pass(
    age: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A PyPI outage over 300 packages must not read as a clean bill of health."""
    lock = tmp_path / "uv.lock"
    lock.write_text(LOCK, encoding="utf-8")
    monkeypatch.setattr(age, "uploaded_at", lambda name, version: None)
    assert age.main(["--lock", str(lock)]) == 1
    assert "unreachable" in capsys.readouterr().err


def test_a_lookalike_registry_is_not_treated_as_pypi(age: ModuleType) -> None:
    """A prefix test on the URL string reads `https://pypi.org.evil.test/simple` as PyPI,
    and the hold would then be cleared by real PyPI's metadata for a package that came
    from somewhere else. The host decides, not the prefix."""
    for registry in (
        "https://pypi.org.evil.test/simple",
        "https://pypi.org@evil.test/simple",
        "https://evil.test/https://pypi.org/simple",
        "https://notpypi.org/simple",
        "file:///tmp/pypi.org/simple",
        # The right host over a scheme PyPI does not serve is not PyPI either.
        "ftp://pypi.org/simple",
    ):
        lock = f'[[package]]\nname = "httpx"\nversion = "0.28.1"\nsource = {{ registry = "{registry}" }}\n'
        assert age.locked_pypi_packages(lock) == [], registry

    for registry in ("https://pypi.org/simple", "https://PyPI.org/simple", "https://pypi.org:443/simple"):
        lock = f'[[package]]\nname = "httpx"\nversion = "0.28.1"\nsource = {{ registry = "{registry}" }}\n'
        assert age.locked_pypi_packages(lock) == [("httpx", "0.28.1")], registry
