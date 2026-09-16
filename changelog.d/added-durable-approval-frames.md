**A durable run now says what it is waiting for you to approve.** `POST /chat/stream` on a
manifest with `spec.execution.mode: durable` emits `approval_required` frames for the gates
the run is blocked on, alongside the `run_status` and `session_event` frames it already
carried. Previously it emitted neither: the agent runs in the worker while the stream is
served by the API, so the in-process side event could not cross, and `GET /approvals` was the
only channel — on precisely the path where a human has time to answer, because the mode exists
for work nobody is watching.

**Rebuilt from the approvals row, not forwarded across a bus**, which is the whole design. A
pub/sub bridge would be at-most-once, and here the message *is* the prompt: drop it and the run
blocks its entire `ttl_seconds` before denying, with nobody ever asked. Reading the durable row
instead keeps the property the session-log tail relies on — a missed poll costs latency, never
a decision — and it means a client that attaches *after* a gate fired still sees it, which a
bus cannot offer. This is what the preceding change (`reason` and `tool_call_id` on the row)
was for: every field the frame carries is now on the row, so it can be re-derived.

The frame is the same eight keys the transient path emits, so a client folds it with the
handler it already has.

**The frames require the `approvals:read` scope**, the same one `GET /approvals` requires; a
caller without it gets the transcript, the status and the answer exactly as before, and nothing
about gates. That is not caution for its own sake — `thread_id` comes from the request body, so
the thread a durable run names is a question the caller *chose* rather than one they own, and
nothing in Felix binds a thread to a principal. Ungated, chat access alone would have read the
tool names, arguments and gate reasons of any thread in the tenant. `admin` bypasses and
`approvals:write` implies `approvals:read`, as on the route, because both ask one function.

Two more behaviours worth knowing. **Each approval is announced once per stream** — `GET
/approvals` answers "what is pending now", so the row returns on every poll until it is decided,
and re-showing a prompt someone has already answered is worse than showing it late; a decided
**or expired** gate is never announced, since both are history rather than a question. And the
frames are **thread-scoped**: a run only ever announces gates attributed to its own thread.

**`GET /approvals` remains the channel of record.** A stream that was never opened, or that
dropped before a gate fired, sees nothing — the frames are a convenience for a watching client,
not a replacement for the poll. An operator console should still poll.
