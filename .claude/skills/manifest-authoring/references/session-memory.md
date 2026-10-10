# Session and memory internals

`session/` holds one thread's history; `memory/` holds facts per `(tenant, manifest)` pool, across threads.

## The session log: what is appended, what is derived

A thread is an append-only list of `SessionEvent`s (`session/types.py`) in `session_events`, written
through `session/store.py`. Nothing is edited in place. An append masks configured secrets in content,
tool-call arguments and metadata, then wakes readers through `session/notify.py:notify_appended` (a
hint only; readers still re-query the log).

| Appended to the log | Derived from it, or kept beside it |
|---|---|
| messages, tool calls and results, thinking | the model context, rendered by the strategy every turn |
| `compaction` events holding the summary and kept tail (`session/compaction.py`) | the active branch, walked from the leaf over `event_id`/`parent_id` (`session/tree.py:active_branch_events`) |
| `branch_summary` on rewind (`session/branch.py:summarize_abandoned_branch`) | leaf, labels, name, thinking level, preview: the `thread_state` row (`session/thread_state.py`) |
| `thinking_level_change`, `label`, `custom` and other bookkeeping kinds | search hits, snapshots, JSONL export (`session/search.py`, `session/snapshot.py`, `session/export.py`) |

`session/types.py:include_in_llm_context` decides which kinds reach the model: bookkeeping kinds never
do, `custom` only with `metadata.in_context`. Rewind deletes nothing — `session/branch.py:rewind_and_persist`
moves the leaf under the thread's leaf lock and bumps `leaf_epoch`; the abandoned events stay.
`session/branch.py:fork_and_persist` copies the active branch to a new thread id and refuses one that
exists. On Postgres each turn re-reads the stored leaf (`session/tree.py:sync_leaf`), since the
in-process leaf is per replica.

| Module | Job |
|---|---|
| `session/lease.py` | one exclusive holder plus shared observers per thread; Redis across replicas, in-process otherwise |
| `session/side_question.py` | `POST /chat/ask`: renders the thread with its manifest's strategy over a read-only session; writes nothing |
| `session/handoff.py` | a context note when the next model routes to a different provider (`patterns/react.py`) |
| `session/thinking.py` / `session/preview_backfill.py` | level-to-budget map for `POST /chat/thinking`; `felix sessions backfill-previews` |

## Which manifest field drives which module

`runtime.py:session_plumbing` turns a manifest into a store and a strategy: `validate_checkpointer_config`,
then `build_checkpointer` (`spec.memory.checkpointer`), then `get_session_strategy` (`spec.session`).
The strategies are listed in [spec-fields.md](spec-fields.md); `compacting` and
`summarizing` both build `session/compaction.py:CompactingSessionStrategy`. `patterns/react.py` reads
`steering_mode`, `follow_up_mode` and `compact_after_turn`. `session.branch_summary` is read only by
`/chat/rewind`, when the request names a manifest and leaves `summarize` unset; fork never summarises.

| Field | Code |
|---|---|
| `memory.store` / `memory.capture` | `memory/capture.py:active_facts_prompt` at compile time — on when the store is not `none` and capture is enabled **or** the store is `pgvector`/`memory`, so the default gets it; `memory/capture.py:capture_from_turn` → `memory/extraction.py` after the reply controls settle |
| `memory.recall` | `memory/tools.py:make_memory_tools`, bound before the wrapper stack; ranking in `memory/recall.py:recall` (full-text, topic-key and vector channels fused by RRF) |
| `memory.consolidate` | `memory/consolidation.py:consolidate_all_pools`, from the worker's `consolidate_memory` cron |
| `procedural_memory` | `memory/procedural.py:make_remember_procedure_tool`; `retrieve_procedures` per turn in `patterns/react.py`, as transient guidance |

Memory rows (`memory_vectors`) are content-addressed (`memory/store.py:memory_id`) and never deleted:
superseded or forgotten, with `superseded_seq` letting `memory/store.py:as_of` rebuild what was known
at a turn. Only an operator write (`/memory`) retires a fact by `topic_key`; an agent write lands beside
it. The vector channel needs an embedder (`FELIX_MEMORY_EMBEDDER`, default `auto`, local only;
`memory/embedder.py:register_embedder_backend`); without one recall skips it. `plans/store.py` backs the
`deep` pattern's plan tools (`patterns/plan_tools.py`) and `/plans`; `prompts/templates.py` expands
`spec.prompts`.

## memory:// twins, and where the arms must agree

`session/store.py:get_session_store` keeps one `InMemorySessionStore` per tenant for the process
lifetime; search, leases, notify and `thread_state` carry their own in-process arms. The session-side
checks also treat a URL containing `:memory:` or `sqlite` as in-memory, while
`felix/db/session.py:_use_memory` (memory, plans) matches `memory://` only. Search differs most:
Postgres ranks over `content_tsv` and falls back to `ILIKE`; the twin is a substring scan.

`tests/conformance/test_session_store.py` is the contract both arms run (ordering, dense `seq`,
windows, reset, wake, secret masking, concurrent appends); a backend added to its `BACKENDS` inherits
every assertion. Its siblings in `tests/conformance/` cover leases, `thread_state`, the turn leaf,
search, the memory store, recall, consolidation and plans; the Postgres arm skips without
`FELIX_CONFORMANCE_DATABASE_URL`. Unit coverage: `tests/unit/test_compaction_*.py`,
`tests/unit/test_memory_*.py`, `tests/unit/test_side_question_read_only.py`.
