"""`--strict` turns a manifest warning into a CI failure; without it nothing changes.

Warnings are printed and exit 0 by default because `PUT /manifests` stores those manifests too.
`--strict` is the operator choosing to hold their own manifests tighter: exit 1 — the code every
other `validate-manifest` failure uses — after every path and every warning has been reported,
so one CI run names all of them rather than the first.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from felix.config import get_settings
from felix.manifests.loader import clear_bundled_cache
from felix.manifests.resolver import clear_resolver_cache
from felix_cli.main import app
from typer.testing import CliRunner


def _manifest(name: str, *, gate_delete: bool) -> dict[str, Any]:
    gated = ["write_file", "delete_file"] if gate_delete else ["write_file"]
    return {
        "apiVersion": "felix/v1",
        "kind": "Agent",
        "metadata": {"name": name},
        "spec": {
            "tools": ["write_file", "delete_file"],
            "approvals": [{"id": f"{name}-write", "tools": gated, "ttl_seconds": 60}],
        },
    }


def _write(tmp_path: Path, name: str, *, gate_delete: bool) -> Path:
    path = tmp_path / f"{name}.yaml"
    path.write_text(yaml.safe_dump(_manifest(name, gate_delete=gate_delete)), encoding="utf-8")
    return path


def _run(*args: str) -> tuple[int, str]:
    result = CliRunner().invoke(app, ["validate-manifest", *args, "--no-resolve-egress"])
    # Rich wraps to the console width, mid-path included, so compare with no whitespace at all.
    return result.exit_code, "".join(result.output.split())


def test_a_warning_passes_by_default(tmp_path: Path) -> None:
    code, out = _run(str(_write(tmp_path, "gap", gate_delete=False)))
    assert code == 0, out
    assert "warning" in out and "delete_file" in out and "ok" in out, out


def test_a_warning_fails_under_strict(tmp_path: Path) -> None:
    code, out = _run(str(_write(tmp_path, "gap", gate_delete=False)), "--strict")
    assert code == 1, out
    # Still reported in full, still `ok` as far as the store is concerned.
    assert "warning" in out and "delete_file" in out and "`gap-write`" in out, out
    assert "gap.yaml(gap)" in out and "--strict" in out, out


def test_strict_passes_a_manifest_with_no_warning(tmp_path: Path) -> None:
    code, out = _run(str(_write(tmp_path, "clean", gate_delete=True)), "--strict")
    assert code == 0, out
    assert "warning" not in out and "ok" in out, out


def test_every_path_is_reported_before_the_exit(tmp_path: Path) -> None:
    first = _write(tmp_path, "first", gate_delete=False)
    clean = _write(tmp_path, "clean", gate_delete=True)
    second = _write(tmp_path, "second", gate_delete=False)

    for strict, expected in ((False, 0), (True, 1)):
        args = [str(first), str(clean), str(second)] + (["--strict"] if strict else [])
        code, out = _run(*args)
        assert code == expected, out
        assert "`first-write`" in out and "`second-write`" in out, out
        assert out.count("adddelete_filetorule") == 2, out
        for name in ("first", "clean", "second"):
            assert f"{name}.yaml({name})" in out, out  # its `ok` line


def test_an_invalid_path_does_not_hide_the_others(tmp_path: Path) -> None:
    broken = tmp_path / "broken.yaml"
    broken.write_text("apiVersion: felix/v1\nkind: Agent\n", encoding="utf-8")
    gap = _write(tmp_path, "gap", gate_delete=False)

    code, out = _run(str(broken), str(gap))
    assert code == 1, out
    assert "invalid" in out and "broken.yaml:" in out, out
    assert "`gap-write`" in out and "gap.yaml(gap)" in out, out


def test_no_path_is_a_usage_error() -> None:
    result = CliRunner().invoke(app, ["validate-manifest"])
    assert result.exit_code == 2


@pytest.fixture
def manifests_dir(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    def set_to(path: Path) -> None:
        monkeypatch.setenv("FELIX_MANIFESTS_DIR", str(path))
        get_settings.cache_clear()
        clear_bundled_cache()
        clear_resolver_cache()

    yield set_to
    monkeypatch.delenv("FELIX_MANIFESTS_DIR", raising=False)
    get_settings.cache_clear()
    clear_bundled_cache()
    clear_resolver_cache()


def test_the_bundled_manifests_pass_strict() -> None:
    """What CI runs: a bundled manifest gaining a warning fails the build here first."""
    result = CliRunner().invoke(app, ["bundle-manifests", "--strict"])
    assert result.exit_code == 0, result.output
    assert "warning" not in result.output


def test_bundle_manifests_strict_reports_every_warning(tmp_path: Path, manifests_dir: Any) -> None:
    _write(tmp_path, "extra-gap", gate_delete=False)
    _write(tmp_path, "other-gap", gate_delete=False)
    manifests_dir(tmp_path)

    loose = CliRunner().invoke(app, ["bundle-manifests"])
    assert loose.exit_code == 0, loose.output
    assert "warning extra-gap:" in loose.stderr and "warning other-gap:" in loose.stderr

    strict = CliRunner().invoke(app, ["bundle-manifests", "--strict"])
    assert strict.exit_code == 1, strict.output
    assert "warning extra-gap:" in strict.stderr and "warning other-gap:" in strict.stderr
    # stdout is JSON for a parser; the warnings never land there.
    assert "warning" not in strict.stdout
