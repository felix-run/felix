**A caller-supplied `response_format` reached the model unscreened.** Every string leaf of a
JSON Schema sent to `POST /v1/chat/completions` — `title`, `description`, a property name — is
serialised verbatim into the provider request, and because per-request options are resolved once
and reused, it sat in front of the model on *every* turn of the loop rather than on one.
`apply_inbound_screening` iterates messages; this arrived on `model_options`, the one place it
does not look, so content screening and input guardrails never saw it. A schema is now screened
on the same path as the turn it rides with, and refused rather than redacted — rewriting a
description would silently change the contract the caller is holding.

The size bounds were also not the ones the code claimed: node count is orthogonal to bytes, and
900 KB of schema fits in six nodes, so one accepted request could have that re-serialised into
the provider body on every turn against the operator's own credential — and on Anthropic, where
the schema is a tool definition inside the cached prefix, destroy the conversation's prompt cache
as well. There is now a byte bound. `$id` and `$dynamicRef` are held to the same local-reference
rule as `$ref`, since `$id` is what decides where a `#` pointer resolves; a property *named*
`$ref` is no longer mistaken for one.
