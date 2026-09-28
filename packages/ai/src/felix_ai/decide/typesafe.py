"""Jev, reached directly (`api.typesafe.ai`) or through Cloudflare Workers AI.

Both endpoints take the same `{state, questions}` and answer with the same `{answers,
usage}`; they differ in where the model name goes and in Cloudflare's response envelope.
One class with two constructors keeps that difference to the two places it lives.

A plain POST rather than `typesafe_sdk`: the request is one JSON body, and the SDK would be
a dependency on the default install for the sake of a retry loop `post_with_retry` already
provides.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from felix_ai.decide.types import DecisionResult, Question, answers_from_wire, question_to_wire
from felix_ai.types import TokenUsage
from felix_ai.wire.transport import DEFAULT_CONNECT_TIMEOUT_S, ModelGatewayError, post_with_retry

TYPESAFE_BASE_URL = "https://api.typesafe.ai/v1"
WORKERS_AI_BASE_URL = "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai"


class JevDecider:
    """A `DecisionProvider` over the TypeSafe wire format."""

    def __init__(
        self,
        *,
        model_id: str,
        wire_model: str,
        url: str,
        api_key: str,
        timeout_s: float,
        label: str,
        nest_input: bool,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        self.model_id = model_id
        self.wire_model = wire_model
        self._url = url
        self._api_key = api_key
        self._timeout_s = timeout_s
        self._label = label
        # Workers AI wants `{model, input: {...}}`; TypeSafe wants the fields at top level.
        self._nest_input = nest_input
        self._headers = dict(extra_headers or {})

    @classmethod
    def typesafe(
        cls, *, model_id: str, wire_model: str, api_key: str, timeout_s: float, base_url: str = ""
    ) -> JevDecider:
        base = (base_url or TYPESAFE_BASE_URL).rstrip("/")
        return cls(
            model_id=model_id,
            wire_model=wire_model,
            url=f"{base}/systemone",
            api_key=api_key,
            timeout_s=timeout_s,
            label="typesafe",
            nest_input=False,
        )

    @classmethod
    def workers_ai(
        cls,
        *,
        model_id: str,
        wire_model: str,
        api_key: str,
        account_id: str,
        timeout_s: float,
        gateway_id: str = "",
        base_url: str = "",
    ) -> JevDecider:
        template = (base_url or WORKERS_AI_BASE_URL).rstrip("/")
        if "{account_id}" in template and not account_id:
            raise ValueError(
                "decision provider 'workers_ai' needs account_id — set it in "
                'FELIX_MODEL_PROVIDER_OPTIONS, e.g. {"workers_ai": {"account_id": "..."}}'
            )
        return cls(
            model_id=model_id,
            wire_model=wire_model,
            url=f"{template.replace('{account_id}', account_id)}/run",
            api_key=api_key,
            timeout_s=timeout_s,
            label="workers_ai",
            nest_input=True,
            extra_headers={"cf-aig-gateway-id": gateway_id} if gateway_id else None,
        )

    def _body(self, state: Any, questions: Mapping[str, Question]) -> dict[str, Any]:
        payload = {"state": state, "questions": {k: question_to_wire(q) for k, q in questions.items()}}
        if self._nest_input:
            return {"model": self.wire_model, "input": payload}
        return {"model": self.wire_model, **payload}

    async def decide(
        self, state: str | Mapping[str, Any] | list[Any], questions: Mapping[str, Question]
    ) -> DecisionResult:
        headers = {"Content-Type": "application/json", **self._headers}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        timeout = httpx.Timeout(self._timeout_s, connect=DEFAULT_CONNECT_TIMEOUT_S)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await post_with_retry(
                client,
                self._url,
                label=self._label,
                json=self._body(state, questions),
                headers=headers,
            )
            if resp.status_code >= 400:
                raise ModelGatewayError(self._label, resp.status_code, resp.text)
            data = resp.json()
        return parse_response(data, questions)


def parse_response(data: Any, questions: Mapping[str, Question]) -> DecisionResult:
    """Answers from either endpoint's response.

    Cloudflare wraps every REST result as `{result, success, errors, messages}`; TypeSafe
    returns the body bare. Unwrapping only when `answers` is absent at the top keeps one
    parser for both without guessing which endpoint answered.
    """
    if not isinstance(data, dict):
        raise ValueError("decision provider returned a non-object body")
    if "answers" not in data and isinstance(data.get("result"), dict):
        if data.get("success") is False:
            raise ValueError(f"decision provider reported failure: {data.get('errors')!r}")
        data = data["result"]
    raw = data.get("answers")
    if not isinstance(raw, dict):
        raise ValueError("decision provider response carries no answers")
    usage_raw = data.get("usage") or {}
    return DecisionResult(
        answers=answers_from_wire(questions, raw),
        usage=TokenUsage(
            input=int(usage_raw.get("input_tokens") or 0),
            output=int(usage_raw.get("output_tokens") or 0),
        ),
        model=str(data.get("model") or ""),
    )


__all__ = ["TYPESAFE_BASE_URL", "WORKERS_AI_BASE_URL", "JevDecider", "parse_response"]
