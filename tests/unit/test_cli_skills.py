"""`felix skills browse|add` against the real `/skill-library` routes.

`FelixClient` opens its own `httpx.AsyncClient` per call, so the app's ASGI transport is bound in
underneath it, as `tests/e2e/test_docs_sync.py` does; GitHub is `tests/skill_import_fake.py` at
the production path's client factory. Synchronous: the command runs its own event loop.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from felix.config import Settings
from felix_cli.main import app as cli
from typer.testing import CliRunner

from tests.skill_import_fake import FakeRepos, skill_md

SOURCE = "github:acme/skills/skills/invoice-triage"


@pytest.fixture
def served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeRepos:
    from felix_api.app import create_app

    settings = Settings(
        auth_mode="none",
        allow_insecure=True,
        environment="development",
        object_store="memory",
        database_url="memory://cli-skills",
        data_dir=str(tmp_path),
        skill_import_sources="github:acme/*",
    )
    transport = httpx.ASGITransport(app=create_app(settings=settings, plugins=[]))
    real = httpx.AsyncClient

    class _Bound(real):  # type: ignore[misc,valid-type]
        def __init__(self, *a: Any, **k: Any) -> None:
            k["transport"] = transport
            super().__init__(*a, **k)

    monkeypatch.setattr(httpx, "AsyncClient", _Bound)
    fake = FakeRepos()
    fake.push(
        "acme/skills",
        {
            "skills/invoice-triage/SKILL.md": skill_md("invoice-triage"),
            "skills/invoice-triage/examples/one.md": b"example\n",
        },
    )
    fake.serve(monkeypatch)
    return fake


def _run(*args: str) -> Any:
    return CliRunner().invoke(cli, ["skills", *args, "--url", "http://felix.test"])


def test_browse_lists_what_add_takes(served: FakeRepos) -> None:
    result = _run("browse", "github:acme/skills")
    assert result.exit_code == 0, result.output
    assert f"{SOURCE}\tinvoice-triage\tRoute invoices to the right queue." in result.output


def test_add_saves_a_draft_and_says_what_it_dropped(served: FakeRepos) -> None:
    result = _run("add", SOURCE)
    assert result.exit_code == 0, result.output
    assert (
        "invoice-triage@0.1.0 saved as a draft from github:acme/skills/skills/invoice-triage @ "
        in result.output
    )
    assert "dropped examples/one.md" in result.output
    assert "then publish with POST /skill-library/invoice-triage/versions/0.1.0/publish" in result.output

    again = _run("add", SOURCE)
    assert again.exit_code == 0, again.output
    assert "nothing saved" in again.output


def test_add_has_no_way_to_publish(served: FakeRepos) -> None:
    result = _run("add", SOURCE, "--publish")
    assert result.exit_code == 2, "an import is published after review, never by the command that fetched it"


def test_a_repositorys_text_reaches_the_terminal_without_its_control_characters(served: FakeRepos) -> None:
    # YAML's own escapes, so the parsed description carries ESC, BEL and the 8-bit CSI.
    escaped = r'"Routes invoices.\e]0;owned\a\e[2J\x9b31m done"'
    served.push(
        "acme/skills",
        {
            "skills/invoice-triage/SKILL.md": f"---\nname: invoice-triage\ndescription: {escaped}\n---\n\n# Body\n".encode()
        },
    )
    result = _run("browse", "github:acme/skills")
    assert result.exit_code == 0, result.output
    assert not any(c in result.output for c in "\x1b\x07\x9b")
    assert "Routes invoices.]0;owned[2J31m done" in result.output


def test_a_refusal_prints_its_code_and_exits_1(served: FakeRepos) -> None:
    result = _run("add", "github:elsewhere/skills/x")
    assert result.exit_code == 1
    assert "source_not_allowed: github:elsewhere/skills/x is not a source" in result.output
    assert served.requests == []


def test_clean_strips_bidi_and_zero_width_characters() -> None:
    from felix_cli.skills import clean

    # RLO makes `github:evil/x` display as something else; ZWSP and BOM hide in plain sight.
    shown = clean("github:\u202eevil/x\u202c \u2066a\u2069\u200bb\ufeffc\u200fd")
    assert shown == "github:evil/x abcd"
