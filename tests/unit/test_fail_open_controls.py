"""Security controls must not disable themselves silently.

Four controls degraded on a transient failure with `logger.debug` as the only signal —
invisible at the INFO default — and no metric. A control that cannot run has not cleared
anything, so "unavailable" must be distinguishable from "clean".
"""

from __future__ import annotations

import logging

import pytest
from felix.config import Settings
from felix.governance.inbound import (
    INJECTION_THRESHOLD,
    SCREEN_CHARS,
    ScreenResult,
    screen_for_injection,
)


def _settings() -> Settings:
    return Settings(
        database_url="memory://failopen",
        object_store="memory",
        allow_insecure=True,
        auth_mode="none",
    )


# --- screening is tri-state -----------------------------------------------------


def test_unavailable_is_not_clean() -> None:
    """`None` used to mean both, and both call sites read it as clean."""
    r = ScreenResult(available=False, reason="x")
    assert r.unavailable is True
    assert r.flagged is False


def test_clean_is_not_flagged() -> None:
    r = ScreenResult(score=0.1)
    assert r.flagged is False
    assert r.unavailable is False


def test_high_score_flags() -> None:
    assert ScreenResult(score=INJECTION_THRESHOLD).flagged is True
    assert ScreenResult(score=0.99).flagged is True


@pytest.mark.asyncio
async def test_screener_outage_reports_unavailable(caplog: pytest.LogCaptureFixture) -> None:
    """A missing key, an expired credential, or a 429 must not read as clean."""
    with caplog.at_level(logging.ERROR, logger="felix.governance.inbound"):
        result = await screen_for_injection(_settings(), "hello", "no-such-model-id")
    assert result.unavailable is True
    assert result.flagged is False
    assert "unavailable" in caplog.text


@pytest.mark.asyncio
async def test_unparseable_reply_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reply we cannot parse is not evidence the text is clean."""
    from felix.governance import inbound
    from felix.patterns.types import ChatMessage

    class _Model:
        model_id = "m"

        async def chat(self, messages, tools):
            class _R:
                message = ChatMessage(role="assistant", content="I cannot rate this.")

            return _R()

    monkeypatch.setattr(inbound, "build_model", lambda *a, **k: _Model(), raising=False)
    import felix.patterns.model as model_mod

    monkeypatch.setattr(model_mod, "build_model", lambda *a, **k: _Model())
    result = await screen_for_injection(_settings(), "hi", "m")
    assert result.unavailable is True


def test_screen_window_is_named_not_magic() -> None:
    """A payload past the truncation point is never seen by the model screener."""
    assert SCREEN_CHARS == 4000


# --- the PII latch --------------------------------------------------------------


def test_transient_init_failure_is_not_latched(monkeypatch: pytest.MonkeyPatch) -> None:
    """One failed init used to pin the process to three regexes for its lifetime."""
    from felix.governance import pii

    monkeypatch.setattr(pii, "_presidio_checked", False)
    monkeypatch.setattr(pii, "_analyzer", None)
    monkeypatch.setattr(pii, "_spacy_model_ready", lambda: True)

    calls = {"n": 0}

    class _Boom:
        def __init__(self, *a, **k):
            calls["n"] += 1
            raise RuntimeError("transient")

    import sys
    import types

    fake_an = types.ModuleType("presidio_analyzer")
    fake_an.AnalyzerEngine = _Boom  # type: ignore[attr-defined]
    fake_anon = types.ModuleType("presidio_anonymizer")
    fake_anon.AnonymizerEngine = _Boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "presidio_analyzer", fake_an)
    monkeypatch.setitem(sys.modules, "presidio_anonymizer", fake_anon)

    assert pii._try_load_presidio() is False
    assert pii._presidio_checked is False, "a transient failure must not latch"
    assert pii._try_load_presidio() is False
    assert calls["n"] == 2, "second call should have retried"


def test_missing_package_is_latched(monkeypatch: pytest.MonkeyPatch) -> None:
    """Absent packages will not appear at runtime, so that result is safe to cache."""
    import sys

    from felix.governance import pii

    monkeypatch.setattr(pii, "_presidio_checked", False)
    monkeypatch.setattr(pii, "_analyzer", None)
    monkeypatch.setitem(sys.modules, "presidio_analyzer", None)
    assert pii._try_load_presidio() is False
    assert pii._presidio_checked is True


def test_degradation_is_visible_at_warning(caplog: pytest.LogCaptureFixture) -> None:
    from felix.governance import pii

    with caplog.at_level(logging.WARNING, logger="felix.governance.pii"):
        pii._warn_degraded("no spaCy model")
    assert "degraded to the regex fallback" in caplog.text


# --- guardrails.providers is validated ------------------------------------------


def test_provider_typo_is_rejected() -> None:
    """A typo meant no wrapper was applied at all, while guardrails_enabled() stayed
    True so compile validation passed and nothing warned."""
    import pydantic
    from felix.manifests.schema import Guardrails

    for typo in ("PII", "pii-redaction", "Pii"):
        with pytest.raises(pydantic.ValidationError):
            Guardrails(providers=[typo])  # type: ignore[list-item]


def test_valid_provider_accepted() -> None:
    from felix.manifests.schema import Guardrails

    assert Guardrails(providers=["pii"]).providers == ["pii"]


def test_outbound_providers_stay_open() -> None:
    """That registry is extensible, so it is deliberately not a Literal."""
    from felix.manifests.schema import OutboundAuth

    assert OutboundAuth(providers=["anthropic", "some-plugin"]).providers == [
        "anthropic",
        "some-plugin",
    ]


# --- command screening sees the payload -----------------------------------------


def test_sandbox_code_is_screened() -> None:
    """The built-in sandbox runs ["python", "-c", code]; the screener read only
    command/cmd, so it inspected nothing for the one tool that executes code."""
    from felix.manifests.builder import _screenable_command_text

    args = {"code": "import os; os.system('rm -rf /')", "stdin": ""}
    assert "rm -rf /" in _screenable_command_text(args, "sandbox")


def test_alternate_arg_names_are_screened() -> None:
    from felix.manifests.builder import _screenable_command_text

    for key in ("script", "shell_command", "argv", "stdin"):
        assert "sudo" in _screenable_command_text({key: "sudo reboot"}, "mcp")


def test_list_args_are_flattened() -> None:
    from felix.manifests.builder import _screenable_command_text

    assert "reboot" in _screenable_command_text({"argv": ["sudo", "reboot"]}, "mcp")


def test_ordinary_args_are_not_screened_for_local_tools() -> None:
    """Screening every string on a normal tool would be noise, not safety."""
    from felix.manifests.builder import _screenable_command_text

    assert _screenable_command_text({"query": "sudo reboot"}, "local") == ""
    assert _screenable_command_text({"command": "rm -rf /"}, "local") == "rm -rf /"


def test_every_string_is_screened_for_execution_transports() -> None:
    from felix.manifests.builder import _screenable_command_text

    out = _screenable_command_text({"anything": "sudo reboot"}, "container")
    assert "sudo reboot" in out


# --- the reflect verifier is a quality gate, so it fails closed too --------------


def _reflect_agent():
    from felix.patterns import delegating

    return delegating._DelegatingAgent(tools=[], pattern="reflect", manifest_id="r", manifest_version="1")


class _DeadVerifier:
    model_id = "dead"

    async def chat(self, messages, tools):
        raise RuntimeError("verifier unreachable")


class _ChattyVerifier:
    """Replies with prose around the number, which `float()` alone cannot read."""

    model_id = "chatty"

    def __init__(self, text: str) -> None:
        self.text = text

    async def chat(self, messages, tools):
        from felix.patterns.model import ModelChatResult
        from felix.patterns.types import ChatMessage

        return ModelChatResult(message=ChatMessage(role="assistant", content=self.text))


@pytest.mark.asyncio
async def test_unreachable_verifier_does_not_pass_the_gate(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """It returned 0.8 — above the 0.7 default threshold — so an outage *passed* every answer."""
    from felix.patterns import delegating

    monkeypatch.setattr(delegating, "build_model", lambda *a, **k: _DeadVerifier())
    answer = "a long answer that would have cleared the old length heuristic outright"

    with caplog.at_level(logging.WARNING):
        score = await _reflect_agent()._score(answer, "assert_present:zebra", "")

    assert score == 0.0, "an unmeasured answer must not clear the gate"
    assert any("verifier" in r.message for r in caplog.records), "the outage must be visible"


@pytest.mark.asyncio
async def test_verifier_outage_falls_back_to_a_real_measurement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fallback is the shared heuristic, not a constant — so criteria still decide."""
    from felix.patterns import delegating

    monkeypatch.setattr(delegating, "build_model", lambda *a, **k: _DeadVerifier())
    agent = _reflect_agent()

    assert await agent._score("hello there", "assert_present:hello", "") == 1.0
    assert await agent._score("hello there", "assert_absent:hello", "") == 0.0


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("0.9", 0.9),
        ("Score: 0.9", 0.9),
        ("0.9/1.0", 0.9),
        ("\n  0.25 \n", 0.25),
        ("7", 1.0),
        ("-2", 0.0),
    ],
)
@pytest.mark.asyncio
async def test_verifier_replies_are_parsed_not_guessed(
    monkeypatch: pytest.MonkeyPatch, reply: str, expected: float
) -> None:
    """`.split()[0]` + `float()` rejected every one of these but the first."""
    from felix.patterns import delegating

    monkeypatch.setattr(delegating, "build_model", lambda *a, **k: _ChattyVerifier(reply))
    assert await _reflect_agent()._score("some answer", "nonempty", "") == expected


@pytest.mark.asyncio
async def test_unparseable_verifier_reply_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    from felix.patterns import delegating

    monkeypatch.setattr(delegating, "build_model", lambda *a, **k: _ChattyVerifier("I cannot score this"))
    assert await _reflect_agent()._score("x", "assert_present:zebra", "") == 0.0
