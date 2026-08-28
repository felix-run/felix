"""The CLI resolves plugin patterns and rejects unknown ones.

`packages/cli` never called `load_optional_plugins()`, so `validate-manifest`,
`eval` and `doctor` saw only built-ins: a manifest naming a plugin-registered
pattern validated as broken here while working against the API. The pattern check
itself is new, so it is also the only place a bad pattern name is caught before
build time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from felix_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()

MANIFEST = """apiVersion: felix/v1
kind: Agent
metadata:
  name: cli-probe
spec:
  pattern: {pattern}
"""


@pytest.fixture
def _restore_patterns() -> Any:
    from felix.patterns.registry import _patterns

    saved = dict(_patterns)
    yield
    _patterns.clear()
    _patterns.update(saved)


def _write(tmp_path: Path, pattern: str) -> Path:
    path = tmp_path / "m.yaml"
    path.write_text(MANIFEST.format(pattern=pattern), encoding="utf-8")
    return path


def test_a_builtin_pattern_validates(tmp_path: Path) -> None:
    result = runner.invoke(app, ["validate-manifest", str(_write(tmp_path, "react"))])
    assert result.exit_code == 0, result.output
    assert "ok" in result.output


def test_an_unknown_pattern_is_rejected(tmp_path: Path) -> None:
    result = runner.invoke(app, ["validate-manifest", str(_write(tmp_path, "no-such-pattern"))])
    assert result.exit_code == 1
    assert "unknown pattern" in result.output
    # The message must name what *is* registered, or it is not actionable.
    assert "react" in result.output


def test_a_plugin_registered_pattern_validates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _restore_patterns: Any
) -> None:
    """The regression: this failed before the CLI loaded plugins."""

    async def _build(ctx: dict[str, Any]) -> None:
        """A registered pattern builder. Never invoked — validation does not build."""
        return None

    def _load_with_plugin() -> list[str]:
        """Stands in for a plugin registering a pattern from its entry point."""
        from felix.patterns.registry import register_pattern

        register_pattern("acme-loop", _build)
        return ["acme"]

    monkeypatch.setattr("felix_cli.main._load_plugins", _load_with_plugin)

    result = runner.invoke(app, ["validate-manifest", str(_write(tmp_path, "acme-loop"))])
    assert result.exit_code == 0, result.output


def test_doctor_reports_plugins_and_patterns() -> None:
    result = runner.invoke(app, ["doctor"])
    # doctor exits non-zero when any check fails (e.g. no database locally); the
    # report itself is what matters here.
    assert "plugins" in result.output
    assert "patterns" in result.output
    assert "react" in result.output
