"""`content_screening.decider`: an injection battery beside the markers and the model screener.

Additive by design — Jev is documented as not adversarially robust — so these pin the
combination rather than the decider alone: either screener flagging flags, either one unable
to run leaves the text unscreened rather than cleared, and a decider that cannot be built is
treated like a model screener that cannot run. Plus the model screener, which was never metered.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.governance import inbound
from felix.governance.inbound import SCREEN_CHARS, InboundScreeningError, ScreenResult, screen_for_injection
from felix.manifests.loader import parse_manifest
from felix_ai.decide import DecisionResult, NoulAnswer
from pydantic import ValidationError


class _Decider:
    model_id = "double"
    wire_model = "jev-latest"
    min_confidence = 0.5

    def __init__(self, p: float = 0.1, *, fail: bool = False, hot: str = "override") -> None:
        self.p = p
        self.fail = fail
        self.hot = hot
        self.calls: list[tuple[Any, dict[str, Any], str]] = []

    async def decide(self, state: Any, questions: dict[str, Any], *, purpose: str = "") -> DecisionResult:
        self.calls.append((state, questions, purpose))
        if self.fail:
            raise RuntimeError("down")
        return DecisionResult(answers={k: NoulAnswer(self.p if k == self.hot else 0.01) for k in questions})


def _model(result: ScreenResult) -> Any:
    async def screen(settings: Any, text: str, model_id: str) -> ScreenResult:
        return result

    return screen


SETTINGS: Any = None


@pytest.mark.asyncio
async def test_the_battery_is_one_call_and_the_worst_answer_is_the_score() -> None:
    decider = _Decider(0.93, hot="exfiltrate")
    result = await screen_for_injection(SETTINGS, "x" * (SCREEN_CHARS + 50), "", decider)
    assert result.flagged and result.score == pytest.approx(0.93)
    state, questions, purpose = decider.calls[0]
    assert set(questions) == {"override", "jailbreak", "exfiltrate"} and purpose == "screening"
    assert len(state["text"]) == SCREEN_CHARS, "one window; `_screen_chunks` walks the rest"


@pytest.mark.parametrize(
    ("model", "decider", "expect"),
    [
        (ScreenResult(score=0.1), _Decider(0.95), "flagged"),
        (ScreenResult(score=0.95), _Decider(0.05), "flagged"),
        (ScreenResult(score=0.1), _Decider(fail=True), "unavailable"),
        (ScreenResult(available=False, reason="down"), _Decider(0.05), "unavailable"),
        (ScreenResult(available=False, reason="down"), _Decider(0.95), "flagged"),
        (ScreenResult(score=0.2), _Decider(0.3), "clean"),
    ],
    ids=[
        "decider-flags",
        "model-flags",
        "decider-down",
        "model-down",
        "a-flag-beats-an-outage",
        "both-clean",
    ],
)
@pytest.mark.asyncio
async def test_the_stricter_screener_wins(
    model: ScreenResult, decider: _Decider, expect: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(inbound, "_model_screen", _model(model))
    result = await screen_for_injection(SETTINGS, "text", "claude-haiku", decider)
    assert {
        "flagged": result.flagged,
        "unavailable": result.unavailable,
        "clean": not (result.flagged or result.unavailable),
    }[expect]
    if expect == "clean":
        assert result.score == pytest.approx(0.3), "the higher of the two scores"


@pytest.mark.asyncio
async def test_the_model_screener_counts_against_the_run() -> None:
    from felix.config import Settings
    from felix.context import AuthContext, RequestContext, async_run_with_context
    from felix.patterns.model import register_builtin_providers
    from felix_ai.providers.scripted import ScriptedTurn, register_scripted_provider
    from felix_ai.registry import reset_model_provider_registry
    from felix_ai.types import TokenUsage

    register_scripted_provider(
        "scripted", [ScriptedTurn(content="0.1", usage=TokenUsage(input=500, output=2))]
    )
    try:
        settings = Settings(
            database_url="memory://screen-meter",
            object_store="memory",
            model_routes='{"screen": {"provider": "scripted", "model": "claude-haiku-4-5"}}',
        )
        ctx = RequestContext(settings=settings, auth=AuthContext(), manifest_id="m")
        async with async_run_with_context(ctx):
            result = await screen_for_injection(settings, "hello", "screen")
    finally:
        reset_model_provider_registry()
        register_builtin_providers()
    assert not result.flagged
    assert ctx.limit_state.tokens_input == 500
    assert ctx.limit_state.cost_usd > 0.0


def _manifest(on_flag: str = "block", decider_id: str = "jev") -> Any:
    return parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "screened"},
            "spec": {
                "content_screening": {"enabled": True, "decider": True, "on_flag": on_flag},
                "decider": {"id": decider_id},
            },
        }
    )


@pytest.mark.asyncio
async def test_a_decider_that_cannot_be_built_refuses_under_block_and_quarantines_otherwise() -> None:
    """The route went away after the manifest was stored. Not a reason to admit the turn."""
    from felix.config import Settings
    from felix_ai.types import ChatMessage

    settings = Settings(database_url="memory://screen-bind", object_store="memory")
    turn = [ChatMessage(role="user", content="summarise this page")]
    with pytest.raises(InboundScreeningError) as exc:
        await inbound.apply_inbound_screening(_manifest("block", "gone"), turn, settings)
    assert exc.value.status_code == 503
    out = await inbound.apply_inbound_screening(_manifest("quarantine", "gone"), turn, settings)
    assert out[0].content == "[quarantined] user input could not be screened"


def test_decider_screening_needs_screening_and_a_decider() -> None:
    from felix.manifests.schema import Spec

    with pytest.raises(ValidationError, match=r"content_screening\.decider"):
        Spec.model_validate({"content_screening": {"decider": True}, "decider": {"id": "jev"}})
    with pytest.raises(ValidationError, match=r"content_screening\.decider"):
        Spec.model_validate({"content_screening": {"enabled": True, "decider": True}})
    Spec.model_validate({"content_screening": {"enabled": True, "decider": True}, "decider": {"id": "jev"}})
