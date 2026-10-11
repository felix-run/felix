"""The preview backfill's parts with no store contract in them: the log read and the command.

What it fills and leaves, per backend and per tenant, is `tests/conformance/test_preview_backfill.py`.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.config import Settings, get_settings
from felix.session.preview_backfill import PreviewBackfillReport, first_user_text
from felix.session.types import AppendableEvent, GetEventsOpts
from felix_cli.main import app
from typer.testing import CliRunner


class _CountingSession:
    """A session log that records each page asked of it."""

    id = "t:x"

    def __init__(self, events: list[AppendableEvent]) -> None:
        from felix.session.store import InMemorySessionStore

        self._inner = InMemorySessionStore(tenant_id="t").open("t:x")
        self._events = events
        self.pages: list[GetEventsOpts] = []

    async def load(self) -> None:
        await self._inner.append_batch(self._events)

    async def get_events(self, opts: GetEventsOpts | None = None) -> Any:
        assert opts is not None
        self.pages.append(opts)
        return await self._inner.get_events(opts)


async def test_the_first_user_message_costs_one_page_however_long_the_log() -> None:
    events = [AppendableEvent(kind="message", role="user", content="hi")]
    events += [AppendableEvent(kind="message", role="assistant", content=str(n)) for n in range(500)]
    session = _CountingSession(events)
    await session.load()

    assert await first_user_text(session) == "hi"  # type: ignore[arg-type]
    assert len(session.pages) == 1
    assert session.pages[0].kinds == ["message"] and session.pages[0].limit is not None


async def test_the_read_pages_on_past_a_run_of_turns_with_no_user_text() -> None:
    events = [AppendableEvent(kind="message", role="assistant", content=str(n)) for n in range(45)]
    events += [AppendableEvent(kind="tool_result", role="tool", content="not a message")] * 5
    events.append(AppendableEvent(kind="message", role="user", content="  late  "))
    session = _CountingSession(events)
    await session.load()

    assert await first_user_text(session) == "  late  "  # type: ignore[arg-type]
    assert [p.from_seq for p in session.pages] == [0, 20, 40]


async def test_a_log_with_no_user_text_ends_on_a_short_page() -> None:
    session = _CountingSession([AppendableEvent(kind="message", role="user", content=" ")] * 3)
    await session.load()

    assert await first_user_text(session) is None  # type: ignore[arg-type]
    assert len(session.pages) == 1


@pytest.fixture
def _cli_env(monkeypatch: pytest.MonkeyPatch) -> Any:
    import rich

    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setattr(rich.get_console(), "_width", 200)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_the_command_refuses_the_in_memory_url(_cli_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FELIX_DATABASE_URL", "memory://cli")
    get_settings.cache_clear()

    result = CliRunner().invoke(app, ["sessions", "backfill-previews"])

    assert result.exit_code == 2, result.output
    assert "memory://" in result.output


def _substitute(monkeypatch: pytest.MonkeyPatch, reports: list[PreviewBackfillReport]) -> dict[str, Any]:
    """Point the command at a real-looking URL and capture what it asks the backfill for.

    Port 1: if the substitution ever slips, the test fails to connect rather than reaching a
    developer's running Postgres.
    """
    import felix.session.preview_backfill as backfill_mod

    monkeypatch.setenv("FELIX_DATABASE_URL", "postgresql+psycopg://felix:felix@127.0.0.1:1/felix")
    get_settings.cache_clear()
    seen: dict[str, Any] = {}

    async def fake(settings: Settings, **kwargs: Any) -> list[PreviewBackfillReport]:
        seen.update(kwargs, settings=settings)
        return reports

    monkeypatch.setattr(backfill_mod, "backfill_previews", fake)
    return seen


def test_the_command_passes_its_options_and_reports_per_tenant(
    _cli_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _substitute(monkeypatch, [PreviewBackfillReport(tenant_id="acme", scanned=4, filled=3, no_text=1)])

    result = CliRunner().invoke(
        app, ["sessions", "backfill-previews", "--tenant", "acme", "--batch-size", "50", "--dry-run"]
    )

    assert result.exit_code == 0, result.output
    assert (seen["tenant_id"], seen["batch_size"], seen["dry_run"]) == ("acme", 50, True)
    assert "acme: would fill 3/4 missing" in result.output
    assert "1 no user text" in result.output


def test_the_command_fails_when_a_thread_failed(_cli_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _substitute(monkeypatch, [PreviewBackfillReport(tenant_id="default", scanned=1, failed=["default:x"])])

    result = CliRunner().invoke(app, ["sessions", "backfill-previews"])

    assert result.exit_code == 1, result.output
    assert "default:x" in result.output


def test_the_command_rejects_a_batch_of_nothing(_cli_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _substitute(monkeypatch, [])

    result = CliRunner().invoke(app, ["sessions", "backfill-previews", "--batch-size", "0"])

    assert result.exit_code == 2, result.output
