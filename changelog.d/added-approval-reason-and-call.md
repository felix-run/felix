**An approval now says why it fired and what it is blocking, on both channels.** `GET
/approvals` rows carry `reason` and `tool_call_id` (migration
`0015_approval_reason_and_call`), and the `approval_required` stream frame carries
`expires_at`. Until now the two channels were each missing what the other had, and both
sides of the wire had written that down: `manifests/builder.py` at the emit noted that the
rule's `description` "reached no client by any route: the `/approvals` row does not carry it
either", and `@felix/client`'s `PendingApproval` documents `reason` as **frame-only** ("an
approval the poll found has none to show") and `expiresAt` as poll-only ("the frame carries
no deadline").

That asymmetry falls hardest on the path with no choice. A **durable** run's agent is in the
worker while its stream is served by the API, so no side event can cross and the poll is the
whole channel — and the poll was the half that could not say *why* a gate fired. An operator
who found a waiting approval was shown a tool name and a rule id, while `description` — the
one field in `ApprovalRule` written to be read by a person — sat unread. `tool_call_id` is
the same shape of gap: it is what attaches the prompt to the tool card it is blocking, so
without it a polled approval floats free of anything on screen.

`expires_at` on the frame is read off the row rather than recomputed, so the two channels
cannot disagree about when an offer lapses. Null there means the rule set no `ttl_seconds`,
which is a real state a client renders from its own default rather than a missing value.

`list_approvals` also takes an optional `thread_id`. It **under-reports by construction**:
`create_pending` reuses a pending row keyed on (tenant, manifest, tool, call signature), so
the row names whichever thread asked first and a second thread blocked on the same reused row
is not listed under its own id. That is the safe direction — a caller asking about one thread
never learns about another's — and it is why `thread_id` is attribution rather than ownership.

Expand-only, empty on every historical row, and a widening for clients: `''` is the harness
saying it has no answer (a command-screening gate has no rule description; a gated tool
called outside a tool loop has no call id) rather than a missing value, the same choice
`rule_id` and `thread_id` already made. Clients mirroring the wire should add all three as
**optional**, so they keep working against a harness that predates them.
