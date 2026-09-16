**Structured output: `spec.output_schema`.** A JSON Schema the agent's answer must match,
enforced by the model provider rather than asked for in the prompt. `message.content` is then a
JSON document on every provider — `response_format` on the OpenAI wire (strict where the schema
closes every object and requires every property, which is the only setting under which the shape
is guaranteed), and on Anthropic, which has no equivalent, a tool the model is required to call,
folded back into the reply so a caller sees the same document either way. Tools still work: it is
the turn that answers in text that is constrained, not the turns that call a tool on the way.

`POST /v1/chat/completions` accepts OpenAI's `response_format` for the same thing per request, so
an OpenAI SDK works unchanged. A manifest that declares `spec.output_schema` overrides it — an
agent published with an answer contract keeps answering to it rather than to whichever shape the
last caller preferred.

Supported on `pattern: react` and `pattern: deep`. The composite patterns compose their answer in
a turn that takes no per-request options yet, so a manifest declaring `output_schema` on one of
those is refused at compile rather than quietly answering in prose; a plugin's pattern opts in
with `register_pattern(..., honours_output_schema=True)`.
