"""`felix skills browse|add` against the real `/skill-library` routes.

`FelixClient` opens its own `httpx.AsyncClient` per call, so the app's ASGI transport is bound in
underneath it, as `tests/e2e/test_docs_sync.py` does; GitHub is `tests/support/skill_import_fake.py` at
the production path's client factory. Synchronous: the command runs its own event loop.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
import typer
from felix.config import Settings
from felix.skills.library_keys import ORG_OWNER
from felix_cli.main import app as cli
from typer.testing import CliRunner

from tests.support.skill_import_fake import FakeRepos, skill_md

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


def _moved(served: FakeRepos, queues: bytes) -> str:
    return served.push(
        "acme/skills",
        {
            "skills/invoice-triage/SKILL.md": skill_md("invoice-triage"),
            "skills/invoice-triage/references/q.md": queues,
        },
    )


def test_outdated_diff_and_update_follow_an_imported_skill(served: FakeRepos) -> None:
    assert _run("add", SOURCE).exit_code == 0
    up_to_date = _run("outdated")
    assert up_to_date.exit_code == 0, up_to_date.output
    assert "invoice-triage\t0.1.0\tgithub:acme/skills/skills/invoice-triage @ main" in up_to_date.output
    assert up_to_date.output.rstrip().endswith("up to date")

    commit = _moved(served, b"# Queues\n\nfinance\nlegal\n")
    outdated = _run("outdated", "--cached")
    assert "up to date" in outdated.output, "the cached listing asks GitHub nothing, so has not seen it"
    assert f"-> {commit[:12]}\tupdate available" in _run("outdated").output

    diff = _run("diff", "invoice-triage")
    assert diff.exit_code == 0, diff.output
    assert "added\treferences/q.md\t- -> 24 bytes" in diff.output
    assert "compared with 0.1.0" in diff.output
    assert (
        "--- /dev/null\n+++ b/references/q.md\n  @@ -0,0 +1,4 @@\n  +# Queues\n  +\n  +finance\n  +legal\n"
        in diff.output
    )

    updated = _run("update", "invoice-triage")
    assert updated.exit_code == 0, updated.output
    assert f"invoice-triage@0.1.1 saved as a draft from {SOURCE} @ {commit}." in updated.output
    assert "then publish with POST /skill-library/invoice-triage/versions/0.1.1/publish" in updated.output
    assert "nothing saved" in _run("update", "invoice-triage").output


def test_update_has_no_way_to_publish(served: FakeRepos) -> None:
    # The declared options, not the rendered error: Rich colours that in CI, splitting the text.
    update = typer.main.get_command(cli).commands["skills"].commands["update"]
    assert not any("publish" in opt for p in update.params for opt in p.opts), update.params
    assert _run("update", "invoice-triage", "--publish").exit_code == 2


def test_a_diff_reaches_the_terminal_without_its_control_characters_but_keeps_its_lines(
    served: FakeRepos,
) -> None:
    assert _run("add", SOURCE).exit_code == 0
    _moved(served, "one\x1b]0;owned\x07\ntwo\u009b31m\n".encode())

    diff = _run("diff", "invoice-triage")

    assert diff.exit_code == 0, diff.output
    assert not any(c in diff.output for c in "\x1b\x07\x9b")
    assert "  +one]0;owned\n  +two31m\n" in diff.output


def test_a_files_own_text_cannot_pass_for_a_file_header(served: FakeRepos) -> None:
    """A line `++ b/SKILL.md` in the file is the diff line `+++ b/SKILL.md`: printed indented, it
    never reads as the header of a SKILL.md change that is not there."""
    assert _run("add", SOURCE).exit_code == 0
    _moved(served, b"++ b/SKILL.md\n-- a/SKILL.md\n")

    diff = _run("diff", "invoice-triage")

    assert diff.exit_code == 0, diff.output
    lines = diff.output.splitlines()
    assert [line for line in lines if line.startswith(("+++ ", "--- "))] == [
        "--- /dev/null",
        "+++ b/references/q.md",
    ]
    assert "  +++ b/SKILL.md" in lines and "  +-- a/SKILL.md" in lines


def test_a_diff_of_a_skill_the_library_does_not_hold_is_not_found(served: FakeRepos) -> None:
    result = _run("diff", "never-imported")
    assert result.exit_code == 1 and "not_found: never-imported is not in the library" in result.output


def test_a_diff_or_update_of_a_skill_that_was_not_imported_says_so(served: FakeRepos) -> None:
    import asyncio

    from felix.skills import library

    asyncio.run(
        library.save_draft(
            Settings(database_url="memory://cli-skills"),
            "default",
            files={"SKILL.md": skill_md("house-rules").decode()},
            provenance=library.DraftProvenance(source="operator", author="ops"),
            owner=ORG_OWNER,
        )
    )
    for command in ("diff", "update"):
        result = _run(command, "house-rules")
        assert result.exit_code == 1, result.output
        assert "not_imported: house-rules was not imported, so it has no origin to check" in result.output
    assert served.requests == [], "refused before GitHub is asked"


def test_adopt_saves_an_operator_draft_and_never_publishes(served: FakeRepos) -> None:
    assert _run("add", SOURCE).exit_code == 0
    no_reason = _run("adopt", "invoice-triage", "0.1.0")
    assert no_reason.exit_code == 2, "the reason is required"
    assert _run("adopt", "invoice-triage", "0.1.0", "--reason", "ok", "--publish").exit_code == 2

    result = _run("adopt", "invoice-triage", "0.1.0", "--reason", "vetted it")
    assert result.exit_code == 0, result.output
    assert "invoice-triage@0.1.1 saved as an operator draft adopted from 0.1.0." in result.output
    assert "then publish with POST /skill-library/invoice-triage/versions/0.1.1/publish" in result.output

    again = _run("adopt", "invoice-triage", "0.1.1", "--reason", "again")
    assert again.exit_code == 1
    assert "not_imported: invoice-triage@0.1.1 carries no imported text" in again.output

    # The skill is the operator's now: it no longer follows its origin.
    update = _run("update", "invoice-triage")
    assert update.exit_code == 1 and "not_imported:" in update.output


def test_clean_strips_bidi_and_zero_width_characters() -> None:
    from felix_cli.skills import clean

    # RLO makes `github:evil/x` display as something else; ZWSP and BOM hide in plain sight.
    shown = clean("github:\u202eevil/x\u202c \u2066a\u2069\u200bb\ufeffc\u200fd")
    assert shown == "github:evil/x abcd"
    # The Arabic letter mark, word joiner and invisible operators, a soft hyphen, and the line and
    # paragraph separators.
    assert clean("a\u061cb\u2060c\u2064d\xade\u2028f\u2029g") == "abcdefg"
    # Blank-rendering fillers and separators, and the tag characters that hide an ASCII copy.
    hidden = "".join(chr(0xE0000 + ord(c)) for c in "evil") + "\U000e007f"
    assert clean(f"a\u180eb\u115fc\u1160d\u3164e{hidden}f\U000e0001g") == "abcdefg"
