"""`felix chat` picks its bearer: `--token` wins, else the login saved for `--base`, else none.

Driven through the `felix` Typer app, so the argument parsing and the header it builds are the
ones a person gets. The REPL sends one line, then input ends with an `EOFError`.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from felix_client.login import LoginToken, save_token

_BASE = "https://felix.test"
# What the mocked server answers the next turn with; a test may replace it.
_REPLY = {"next": httpx.Response(200, json={"final": {"role": "assistant", "content": "hi"}})}


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[httpx.Headers]:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    save_token(
        LoginToken(
            access_token="saved-token",
            tenant="acme",
            scopes=(),
            expires_at=time.time() + 3600,
            base_url=_BASE,
        )
    )
    headers: list[httpx.Headers] = []
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        headers.append(request.headers)
        return _REPLY["next"]

    def client(**kwargs: Any) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    # One line, so one turn reaches the server, then the end of input.
    lines = iter(["hello"])

    def one_line(_: str = "") -> str:
        try:
            return next(lines)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setattr("builtins.input", one_line)
    return headers


def _run(monkeypatch: pytest.MonkeyPatch, *args: str) -> str:
    from felix_cli.main import app
    from typer.testing import CliRunner

    result = CliRunner().invoke(app, ["chat", *args])
    assert result.exit_code == 0, result.output
    return result.output


def test_the_saved_login_for_this_server_is_sent(
    sent: list[httpx.Headers], monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(monkeypatch, "--base", _BASE)
    assert sent[0]["authorization"] == "Bearer saved-token"


def test_an_explicit_token_wins(sent: list[httpx.Headers], monkeypatch: pytest.MonkeyPatch) -> None:
    _run(monkeypatch, "--base", _BASE, "--token", "explicit")
    assert sent[0]["authorization"] == "Bearer explicit"


def test_another_servers_saved_login_is_not_sent(
    sent: list[httpx.Headers], monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(monkeypatch, "--base", "https://elsewhere.test")
    assert "authorization" not in sent[0]


def test_felix_api_key_is_read_like_the_sibling_commands(
    sent: list[httpx.Headers], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FELIX_API_KEY", "from-env")
    _run(monkeypatch, "--url", _BASE)
    assert sent[0]["authorization"] == "Bearer from-env"


def test_a_durable_run_that_did_not_complete_says_why(
    sent: list[httpx.Headers], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dead fiber answered `agent> ` and nothing else: its final content is empty."""
    dead = {"status": "dead", "final": {"role": "assistant", "content": ""}, "error": "step failed 3 times"}
    monkeypatch.setitem(_REPLY, "next", httpx.Response(200, json=dead))
    assert "agent> [run dead] step failed 3 times" in _run(monkeypatch, "--url", _BASE)


def test_an_error_answer_is_printed_and_the_session_goes_on(
    sent: list[httpx.Headers], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(_REPLY, "next", httpx.Response(503, json={"detail": "busy"}))
    output = _run(monkeypatch, "--url", _BASE)
    assert f"error: 503 from {_BASE}" in output and output.rstrip().endswith("bye")
