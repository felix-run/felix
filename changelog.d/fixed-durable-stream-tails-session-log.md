**A durable run now reports what it is doing, not only that it is running.** `POST
/chat/stream` against a manifest with `spec.execution.mode: durable` carried
`run_accepted` → `run_status` → `final` and nothing else, so the answer arrived and the
tool calls behind it did not — a client drawing tool cards showed a bare reply until the
thread was next hydrated. The stream now interleaves the thread's **session log** between
status frames, as `session_event` frames with the same `id:` cursor semantics `GET
/chat/stream/{thread_id}` has used all along, through one shared tail helper.

The diagnosis that looks obvious is wrong twice over, which is why this is not a transport
change. `felix.side_events` is an in-process queue drained inside the agent's own loop, so
for a durable run both ends are already in the worker; and the events do not exist to be
bridged anyway, because the fiber calls `agent.invoke` — `_run(..., emit_events=False)` —
which drops the deltas and the `on_tool_start`/`on_tool_end` pairs at the source. What the
fiber *does* produce is the session log: `_append_produced` writes each assistant turn and
its tool results as they land, outside every `emit_events` guard. So there was already a
durable, ordered, cross-replica record of the run's progress, and one endpoint that knows
how to tail it. No new bus, no second delivery path, no second source of truth.

**A `session_event` frame now carries `tool_calls`, `tool_call_id` and `id`,** which it never
has. Found while building the above, and it is the difference between a transcript and a
transcript you can read: a client folds these rows with the same function it folds a
`snapshot` with, reading `tool_calls` off an assistant message to open a card per call and
matching `tool_call_id` on the tool message to attach the result. Carrying neither, an
assistant turn that called a tool folded to an empty message and the result was dropped
outright — so a **warm reattach** to `GET /chat/stream/{thread_id}` (one that replays events
rather than opening on a snapshot) has always rendered a transcript with no tool calls in it.
The snapshot has carried all three since it was written; only the incremental frame was
thinner, which is why it read as a reattach quirk rather than a missing field. This is a
widening — a client that ignores the new fields is unaffected — and `id` is spelled the way
the snapshot spells it, so a turn keeps one identity whichever way the client received it.

**Only completed messages, never token deltas.** Chunks are never persisted, so a durable
run yields tool cards and whole assistant messages and nothing finer. That is the right
trade for the mode whose point is that nobody is watching, and it matches what clients
already render there.

Two details worth knowing. The cursor is captured **before** the run is enqueued, not when
`run_accepted` is emitted: a fiber can be claimed and start appending the moment its row
lands, and a later read would open the stream past the progress it exists to report. And a
run started without a `thread_id` is not a run without a thread — the fiber mints
`{tenant}:fiber:{id}` and writes its transcript there, so those runs report progress too;
`fiber_thread_id` is now named rather than interpolated at each end.

`POST /chat` (202 + `GET /chat/runs/{token}`) is unchanged and stays coarse: it is a status
read, not a stream, so it still reports only status and the final answer. A client that
wants the transcript there reattaches to `GET /chat/stream/{thread_id}`.
