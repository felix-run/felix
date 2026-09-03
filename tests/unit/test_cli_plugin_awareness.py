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


def test_validate_manifest_resolves_outbound_hosts(tmp_path, monkeypatch) -> None:
    """Authoring feedback moved here when the schema validators stopped resolving.

    The validators had to stop: a blocking `getaddrinfo` inside a pydantic validator ran on
    the API event loop for every ref on every read and write. But an author still needs to
    learn that a URL is blocked, and a CLI is the right place to spend a DNS lookup — no
    request is waiting on it.
    """
    from felix.manifests.loader import load_manifest_file
    from felix.security import ssrf
    from felix_cli.main import _assert_outbound_hosts_resolve

    path = tmp_path / "m.yaml"
    path.write_text(
        "apiVersion: felix/v1\n"
        "kind: Agent\n"
        "metadata:\n  name: m\n"
        "spec:\n"
        "  pattern: react\n"
        "  mcp_servers:\n"
        "    - name: evil\n      url: https://rebinds.example.com/mcp\n"
    )
    manifest = load_manifest_file(path)

    class _Settings:
        environment = "production"
        allow_insecure = False

    monkeypatch.setattr(ssrf, "resolve_host", lambda host: ["10.0.0.5"])
    with pytest.raises(ValueError, match="blocked address"):
        _assert_outbound_hosts_resolve(manifest, _Settings())

    monkeypatch.setattr(ssrf, "resolve_host", lambda host: ["93.184.216.34"])
    _assert_outbound_hosts_resolve(manifest, _Settings())
