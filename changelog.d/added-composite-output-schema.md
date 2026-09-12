**`spec.output_schema` now works on `router`, `parallel`, `reflect` and `plan_execute`.**
It shipped supporting only the single-agent patterns, and `build_agent` refused the
combination outright for the composites rather than accept a manifest that declares an
answer contract and returns free text.

The reason it was refused is the reason it is interesting: a composite reaches a model
several times per run — routing, planning, critiquing, scoring, synthesizing — and exactly
one of those turns produces what the caller receives. So the contract is placed per
pattern, never applied wholesale:

- **`parallel`** shapes the synthesis. The specialists stay free-form; their answers are
  raw material for the aggregator's prompt, not the reply.
- **`plan_execute`** shapes the final synthesis. The planning turn and each executor step
  stay free-form — a plan shaped like the answer schema is not a plan, and a subtask answer
  shaped like it arrives as a JSON envelope in a notes list the synthesis reads as prose.
  That took stripping `output_schema` from the context the executor is built from, not just
  withholding it at the call site: `build_react_agent` reads the schema off the build
  context onto the agent itself, so an executor built from the shared context carried it
  regardless of what it was handed per turn.
- **`router`** shapes the child it routes to. The classifier turn does not: a router that
  replied with its classifier's JSON would satisfy the schema and answer nothing.
- **`reflect`** shapes every draft, because the loop exits as soon as one clears the
  threshold and "the last iteration" is not knowable in advance.

`groupchat` stays refused, and the refusal now carries its reason: its answer is the last
speaker's message *stamped with its name* (`[researcher] …`), so even a child returning
perfect JSON comes back with a prefix in front of it. Supporting it means dropping the stamp
— losing who spoke, which is the pattern's point — or adding a synthesis turn it does not
have.

`_child_input` also stopped dropping `model_options`, so a caller's `/v1` `response_format`
reaches a composite's answering turn — and a child — for the first time. Where a manifest and
a request both specify a schema the **manifest** wins, matching `react._chat_options`: an
agent published with an answer contract keeps answering to it rather than to whichever shape
the last request preferred.

A composite that declares no schema is unchanged, deliberately including the case where the
request carries other options. `_DelegatingAgent` has no `limits`, so unlike `react` it
cannot clamp `max_tokens` to `limits.max_output_tokens` — forwarding a request's options to
the synthesis turn would let `max_tokens: 200000` size the turn that composes the answer on a
manifest capping output at 2000.
