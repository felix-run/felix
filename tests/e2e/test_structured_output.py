"""Structured output as a caller reaches it: a manifest field and an OpenAI `response_format`.

`spec.output_schema` and the `/v1` `response_format` are both requests for the *provider* to
enforce an answer shape, so the only honest assertion is about what the model was asked, not
about what it answered — a scripted reply is whatever the test wrote, and a JSON-shaped reply
would pass with the whole feature disconnected. The spy records the `ModelChatOptions` of every
call for exactly that reason.

The two wires' own halves — `response_format` on one, a forced tool folded back into text on
the other — are in `tests/conformance/test_model_provider.py` and
`tests/unit/test_output_schema.py`. What is here is the chain between a request and them.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.manifests.loader import parse_manifest
from felix_ai.providers.scripted import ScriptedTurn

ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"answer": {"type": "string"}, "confidence": {"type": "number"}},
    "required": ["answer", "confidence"],
    "additionalProperties": False,
}

# Distinguishable from the manifest's, so "the manifest wins" cannot pass by both being equal.
CLIENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}

STRUCTURED = '{"answer": "4", "confidence": 1.0}'


def _manifest(name: str, **spec: Any) -> Any:
    base: dict[str, Any] = {
        "pattern": "react",
        "tools": ["calculator"],
        "auth": {"inbound": {"allow_anonymous": True}},
    }
    base.update(spec)
    return parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": name}, "spec": base}
    )


def _response_format(schema: dict[str, Any]) -> dict[str, Any]:
    return {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema}}


async def _completion(app: Any, **body: Any) -> Any:
    return await app.client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "What is 2+2?"}], **body},
    )


async def test_a_manifest_schema_reaches_the_model(boot: Any) -> None:
    """The whole chain for the operator-declared half: `spec.output_schema` → the compile →
    `PatternBuildContext` → the react agent → the options on the model call.

    `_chat_options` returned `None` outright when the caller sent no sampling of its own, which
    is every request that does not name a temperature — so a declared schema reaching the model
    at all is the assertion, not a detail of it.
    """
    shaped = _manifest("e2e-shaped", output_schema=ANSWER_SCHEMA)
    async with boot([ScriptedTurn(content=STRUCTURED)], manifests={"e2e-shaped": shaped}) as app:
        resp = await _completion(app, model="e2e-shaped")
        assert resp.status_code == 200, resp.text
        assert resp.json()["choices"][0]["message"]["content"] == STRUCTURED
        assert [o and o.output_schema for o in app.spy.options] == [ANSWER_SCHEMA]


async def test_a_client_may_ask_for_a_shape_the_manifest_does_not_declare(boot: Any) -> None:
    """`response_format` on `/v1`, which is how an OpenAI SDK asks for this and therefore how
    most callers will. A manifest with no `output_schema` leaves the choice to the request."""
    plain = _manifest("e2e-plain")
    async with boot([ScriptedTurn(content='{"summary": "four"}')], manifests={"e2e-plain": plain}) as app:
        resp = await _completion(app, model="e2e-plain", response_format=_response_format(CLIENT_SCHEMA))
        assert resp.status_code == 200, resp.text
        assert [o and o.output_schema for o in app.spy.options] == [CLIENT_SCHEMA]


async def test_a_manifest_schema_overrides_the_clients(boot: Any) -> None:
    """An agent published with an answer contract keeps answering to it. Otherwise the shape a
    deployment guarantees is whichever one the last caller preferred, which is not a
    guarantee — and `response_format` is unauthenticated client input on this surface."""
    shaped = _manifest("e2e-shaped", output_schema=ANSWER_SCHEMA)
    async with boot([ScriptedTurn(content=STRUCTURED)], manifests={"e2e-shaped": shaped}) as app:
        resp = await _completion(app, model="e2e-shaped", response_format=_response_format(CLIENT_SCHEMA))
        assert resp.status_code == 200, resp.text
        assert [o and o.output_schema for o in app.spy.options] == [ANSWER_SCHEMA]


async def test_an_unshaped_request_asks_for_nothing(boot: Any) -> None:
    """The counterpart: without a schema on either side the model call carries no constraint,
    so the tests above cannot pass by shaping every request. `None` for the whole options
    object is the normal case — a request that names no sampling has nothing to send."""
    plain = _manifest("e2e-plain")
    async with boot([ScriptedTurn(content="4")], manifests={"e2e-plain": plain}) as app:
        resp = await _completion(app, model="e2e-plain")
        assert resp.status_code == 200, resp.text
        assert [o and o.output_schema for o in app.spy.options] == [None]


@pytest.mark.parametrize(
    "response_format",
    [
        # OpenAI's schema-less JSON mode. Silently dropping it would leave a caller believing
        # a shape was being enforced — the failure mode `.claude/rules/felix-invariants.md`
        # opens with.
        {"type": "json_object"},
        # A type this harness has never heard of.
        {"type": "sonnet"},
        # The envelope without the schema, and the envelope with the wrong kind of value.
        {"type": "json_schema", "json_schema": {"name": "answer"}},
        {"type": "json_schema", "json_schema": "answer"},
        # A well-formed envelope around a schema no provider would accept.
        {"type": "json_schema", "json_schema": {"name": "a", "schema": {"type": "array"}}},
    ],
)
async def test_a_response_format_this_harness_cannot_honour_is_refused(
    boot: Any, response_format: dict[str, Any]
) -> None:
    """Refused at the edge, so the caller gets one message naming the problem rather than a
    provider `invalid_request_error` relayed back from two hops away — or, worse, a request
    that succeeds with an answer nothing constrained.

    400 rather than 422 throughout: the point of this surface is that an OpenAI SDK works
    unchanged, and those SDKs map 400 to `BadRequestError` and 422 to a generic
    `APIStatusError`.
    """
    plain = _manifest("e2e-plain")
    async with boot(manifests={"e2e-plain": plain}) as app:
        resp = await _completion(app, model="e2e-plain", response_format=response_format)
        assert resp.status_code == 400, resp.text
        assert resp.json()["error"]["type"] == "invalid_request_error"
        assert app.spy.calls == [], "a refused request must not reach the model"


async def test_a_caller_supplied_schema_is_screened_like_the_turn_it_rides_with(boot: Any) -> None:
    """A schema's text reaches the model, and inbound screening never saw it.

    Every string leaf of `response_format` — `title`, `description`, a property name — is
    serialised verbatim into the provider request, and because options are resolved once and
    reused, it is in front of the model on *every* turn of the loop rather than on one.
    `apply_inbound_screening` iterates messages; this rides on `model_options`, which is the
    one place it does not look. A control the operator switched on, bypassed at the field
    level.

    Refused rather than redacted: rewriting a description would silently change the contract
    the caller is holding, and there is no model to warn the way a quarantined turn warns one.
    """
    governed = _manifest("e2e-screened", guardrails={"providers": ["pii"], "targets": ["input"]})
    async with boot(manifests={"e2e-screened": governed}) as app:
        schema = {
            "type": "object",
            "properties": {"answer": {"type": "string", "description": "mail alice@example.com"}},
            "required": ["answer"],
            "additionalProperties": False,
        }
        resp = await _completion(app, model="e2e-screened", response_format=_response_format(schema))
        assert resp.status_code == 422, resp.text
        assert app.spy.calls == [], "a refused schema must not reach the model"


async def test_a_clean_schema_still_passes_the_same_screening(boot: Any) -> None:
    """The counterpart, so the test above cannot pass by refusing every governed request."""
    governed = _manifest("e2e-screened", guardrails={"providers": ["pii"], "targets": ["input"]})
    async with boot([ScriptedTurn(content=STRUCTURED)], manifests={"e2e-screened": governed}) as app:
        resp = await _completion(app, model="e2e-screened", response_format=_response_format(ANSWER_SCHEMA))
        assert resp.status_code == 200, resp.text
        assert [o and o.output_schema for o in app.spy.options] == [ANSWER_SCHEMA]
