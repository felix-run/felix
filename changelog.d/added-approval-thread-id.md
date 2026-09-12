**An approval row now names the conversation it is blocking.** `GET /approvals` (and
`GET /approvals/{id}`) carry `thread_id`, which the `approval_required` stream frame has
carried all along. The two channels do not cover the same runs: side events are an
in-process queue keyed by thread, so a **durable** run — agent in the worker, stream served
by the API — can only be seen through the poll, and that was the half with no thread on it.
An operator opening a tab cold could be told that something was waiting but not what.

It is the **originating** thread, not an owner. `create_pending` reuses a pending row keyed
on (tenant, manifest, tool, call signature), so two threads issuing a byte-identical gated
call still share one row and one decision; the field names whichever asked first and a later
thread does not overwrite it. Treat it as attribution — a hint good enough to link to, not a
claim that exactly one conversation is waiting. Widening the reuse key to make it exact would
change grant scope, which is a product decision rather than a serialization fix.

Empty where there is no thread — a gated tool called outside a chat context — the same way
`rule_id` is empty when no rule named it. Historical rows read `""`, since they genuinely
have no answer; `migrations/versions/0014_approval_thread_id.py` is expand-only with no
backfill. Clients mirroring the wire should add it as **optional**, so they keep working
against a harness that predates it.

One case never reads empty, and it is the motivating one: a **durable** run started without a
thread gets `{tenant}:fiber:{fiber_id}` synthesized for it, which is a real session thread and
openable like any other. So the poll can attribute a durable approval even when the caller
never opened a conversation.
