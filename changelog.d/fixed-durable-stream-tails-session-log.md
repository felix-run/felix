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
