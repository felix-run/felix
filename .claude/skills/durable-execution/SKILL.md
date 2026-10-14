---
name: durable-execution
description: How Felix runs work that outlives a request — durable chat runs as fibers (spec.execution.mode durable, 202 + resume_token), the claim/lease/retry loop in the worker, resume and reattach streams and their cursor, signed completion webhooks, cross-process waiters for approvals and client tools, steer/abort, Idempotency-Key on POST /chat and /chat/stream, Web Push for a run waiting on a person, scheduled jobs, authored workflows (typed step graphs and their store), and the Taskiq worker and its cron table. Use when editing felix/durability/, felix/jobs/, felix/workflows/, felix/push/, felix/waiters.py, felix/idempotency.py, felix/steer.py or apps/worker, when a durable run never starts, stalls, repeats a tool call or never reports, when a webhook or push is not delivered, or when adding a periodic task.
allowed-tools: Read Grep Glob Bash(./scripts/test.sh:*)
metadata:
  covers: felix/durability/, felix/jobs/, felix/workflows/, felix/triggers.py, felix/push/, felix/idempotency.py, felix/waiters.py, felix/steer.py, felix_worker/
---

# Durable execution

A durable run is a row in `fibers` that a worker drives to a terminal status. Fibers are the only
backend: `FELIX_DURABILITY=temporal` fails at startup with what to do instead
(`tests/unit/test_durability_setting.py`), and rows an old Temporal deployment started are
claimed like any other (`tests/conformance/test_fiber_claim.py`).

## Lifecycle

1. **Accept.** `routes/chat.py:_chat_turn` runs `_refuse_if_run_in_flight`, resolves and screens,
   and on `spec.execution.mode: durable` calls `durability/runs.py:start_durable_chat`, answering
   `202` with `resume_token` (the fiber id), `fiber_id`, `expires_at`, `thread_id`. The durable arm
   of `routes/chat.py:chat_stream` enqueues the same way and answers with `durable_run_gen` instead.
2. **Enqueue.** One `invoke` step. `expires_at` is `resume_token_ttl_seconds` or
   `FELIX_HIBERNATE_AFTER_SECONDS` (300), capped at 86400 and at the caller's JWT `exp`. The
   caller's scopes, principal and skill owner go in `state_json.auth`, the manifest pin in `pin`.
   `durability/fibers.py:create_fiber` (`exclusive_on_thread=True`) takes a per-thread advisory
   lock and raises `RunInProgress` → `409 run_in_progress:<token>` if the thread has a run in
   flight. Webhook ids are checked here (`endpoints_for_run`), not after the run.
3. **Claim.** `durability/fibers.py:_claim_due` has two callers: `run_fiber_loop`, started by the
   worker's startup hook and polling every `FELIX_FIBER_POLL_SECONDS` (1; 0 disables), and the
   `fiber_scheduler` cron (`resume_due_fibers`) as backstop. `FOR UPDATE SKIP LOCKED` under
   `rls_bypass`; sets `running`, `lease_owner` (replica id), `lease_until` (+`FIBER_LEASE_MS`, 5 min).
   `FELIX_FIBER_CONCURRENCY` (8) fibers per worker; one parked on an approval holds its slot.
4. **Step.** `_advance_claimed` → `_step_with_lease` renews the lease every `FIBER_LEASE_RENEW_MS`
   and runs `_run_fiber_step` (`sleep`, `stash`, `invoke`, `workflow`, `complete`) until the fiber suspends, at
   most one `invoke` per claim. The invoke runs as principal `fiber`, `on_behalf_of` the starter,
   with the recorded scopes; resolves the manifest inside a `RequestContext` so RLS binds; and
   checks `assert_resume_pin`, forcing `pin_compile` when authority was recorded.
5. **Resume point.** `_invoke_resume_point` notes the log head as `invoke_began`. A retry at that
   cursor looks for this request's user turn since then: absent → run fresh; a final assistant
   reply → take it, call nothing; else continue from the log with no new user turn. Only `react` /
   `deep` with a non-`semantic` strategy and a checkpointer qualify; everything else re-sends.
6. **Save.** `_save_fiber` compare-and-sets `version`, clears the lease, and `_announce_fiber`
   wakes the thread (`session/notify.py:notify_appended`).
7. **Fail.** An exception inside the invoke ends the run `failed`. One outside it (save, store)
   goes to `_retry_or_dead`: `retry_delay_ms` 1m, 2m, 4m … 1h, `dead` after
   `FELIX_FIBER_MAX_ATTEMPTS` (5). A screener 503 parks the step 60 s, up to 10 times. Expiry is
   checked between steps only. Terminal = `FIBER_TERMINAL_STATUSES` (`completed`, `failed`,
   `expired`, `dead`); `retention_sweep` deletes those after `FELIX_FIBER_RETENTION_DAYS` (7).

| Data | Where |
|---|---|
| steps, `cursor`, `stash.last` (answer/final/error), `auth`, `pin`, `expires_at` | `fibers.state_json`, redacted on every write |
| claim, CAS, failure streak | `lease_owner`, `lease_until`, `version`, `attempts` columns |
| webhook delivery | `webhook_status`, `webhook_due_at`, `webhook_state`; only `durability/webhooks.py:_save_delivery` writes them |
| transcript | the thread's session log; with no `thread_id`, `fiber_thread_id` mints one |
| what the run is blocked on | the approvals table and client requests, re-read by `GateAnnouncer` |

## Watching a run

- `routes/chat.py:chat_run` (`GET /chat/runs/{resume_token}`) returns `durability/runs.py:run_view`,
  which is also the webhook payload's `run`, so the two cannot drift.
- `routes/_streaming.py:durable_run_gen`: `run_accepted`, `session_event`s, `run_status` per change,
  then `final` or an `error` frame typed `run_error`, then `[DONE]`. Its cursor is read before the
  enqueue. A disconnect cancels the poll, never the run; past `expires_at` it closes only once
  `_still_held` says no worker holds the step. No token deltas: the log holds completed messages.
- `routes/_streaming.py:resume_stream_gen` (`GET /chat/stream/{thread_id}`): a `snapshot` frame
  without `Last-Event-ID` (header or `last_event_id` query), a replay from it with one. Closes
  after `FELIX_STREAM_RESUME_IDLE_SECONDS` unless `active_durable_run` finds a run in flight.
- **Cursor:** every `id:` is the *next* seq to expect, handed back unchanged as `Last-Event-ID`;
  `drain_session_events` reads `from_seq=cursor` and advances to `seq + 1`.
- **Why the two loops share a module:** they are the same tail over the same log, through the
  same `drain_session_events` and `GateAnnouncer`, with `RUN_TERMINAL` built from
  `FIBER_TERMINAL_STATUSES`. Apart, they drift into meaning different things by one frame name.

## Webhooks, waiters, steer, push

- **Webhooks.** `spec.execution.webhooks` names ids (max 8, durable only) from
  `FELIX_WEBHOOK_ENDPOINTS` (JSON: `url`, `secret`, `tenants`, `private`), never URLs. The
  `webhook_delivery` cron (`deliver_due_webhooks`) claims due terminal rows `SKIP LOCKED`. Signed as
  Standard Webhooks: `webhook-id` (`<fiber>:<endpoint>`, stable across retries — receivers dedupe on
  it), `webhook-timestamp`, `webhook-signature: v1,` + base64 HMAC-SHA256 of `id.timestamp.body`
  (`sign`; a `whsec_` secret is base64-decoded). Verify with any Standard Webhooks library. Per
  endpoint: fiber backoff, `dead` after `FELIX_WEBHOOK_MAX_ATTEMPTS` (8) or at once if no longer
  registered for the tenant. Egress-guarded unless `private: true`.
- **Waiters.** `felix/waiters.py:wait` / `signal`: Redis `RPUSH`/`BLPOP` in 1 s slices plus an
  in-process future checked between them; names from `waiter_name` (injective escaping). Approvals
  (`approvals/interrupt.py:signal_decision`, from `POST /approvals/{id}/decide`; the wait also
  re-reads the row), client tools (`tools/client_bridge.py:complete_result`, `POST /chat/tool_result`),
  UI prompts (`ui/prompts.py:resolve_ui_response`, `POST /chat/ui`). A durable run waits in the
  worker and is answered by the API, so `validate_runtime` refuses an empty `FELIX_REDIS_URL`
  outside development.
- **Steer.** `felix/steer.py` keeps steer, follow-up, cancel and abort per `(tenant, thread)` in Redis
  with an in-process fallback; the react loop reads it, so `POST /chat/steer` and `/chat/abort`
  reach a run in the worker. It never touches the fiber row.
- **Push.** `push/notify.py:approval_pending` / `question_asked` run in the agent's process (the
  worker, for a durable run). Off until `FELIX_PUSH_VAPID_PRIVATE_KEY` and `FELIX_PUSH_VAPID_SUBJECT`
  are set; endpoints must match `FELIX_PUSH_ALLOWED_HOSTS`; 404/410 deletes the subscription.

## Idempotency-Key

`idempotency.py:once` on `POST /chat`, scoped by `principal_scope`, fingerprinted by body: same key
and body → stored response + `Idempotent-Replayed: true` (for durable, the same 202 and token);
still running → `409 idempotency_in_progress`; other body → `422 idempotency_key_reused`; a raise
frees the key. TTL `FELIX_IDEMPOTENCY_TTL_SECONDS` (86400); bodies over 256 KiB are not stored.
`POST /chat/stream` needs a `thread_id` (`_claim_stream_key`, `_HeldStreamKey`); a durable send is
stored as `mode: durable` and a resend reattaches via `durable_run_gen`. The key is judged before
`_refuse_if_run_in_flight`, so a resend reattaches rather than getting 409. Redis when
`redis_url_in_use`, else in-process; it degrades to in-process (one replica) while Redis fails.

## Worker, cron, jobs

`felix_worker/main.py:main` consumes; `felix_worker/main.py:scheduler_main` enqueues labelled tasks.
No scheduler, no cron; no worker, nothing runs, `run_fiber_loop` included. Each task's schedule
is its `@broker.task(schedule=...)` in `felix_worker/tasks.py`, and `EXPECTED_SCHEDULES` in
`tests/unit/test_worker_cron_tasks.py` pins the whole table — read it there rather than from a copy.

Plugin `cron_tasks` become `plugin_<name>` every minute via `_register_plugin_cron_tasks`, which only
the worker's startup hook calls. Each built-in task is wrapped in `_instrumented`; plugin runners are not. Jobs:
`jobs/scheduler.py:run_due_jobs_all_tenants` binds each tenant with `rls_tenant`,
`jobs/store.py:claim_run` compare-and-sets `next_run_at`, `fire_job` runs as `cron` on behalf of the
job's last writer (`created_by`, with the scopes recorded in `created_auth`) through
`runtime.prepare_tenant_invoke`, so the manifest's inbound auth applies; a job with no recorded
writer runs with no scopes. Fibers and jobs record and replay a caller through one module,
`auth/recorded.py` (`record_caller` / `replay_caller`); an API-key writer's job scopes are cut to the
key's current ones at each firing (`scheduler._current_api_key_scopes`). A job with a `workflow_id`
(`0043_job_workflow`) starts a workflow run instead (`scheduler._start_job_workflow`): the same
identity, `payload.inputs` as the inputs, `workflows:write` required of the recorded scopes, the
run's `trigger` `job:<name>` and its id in the job run's `result.workflow_run_id`. The job keeps
`workflow_revision` from its write and `start_run(revision=)` refuses any other
(`RevisionChanged`), so a later editor's steps never run on the writer's scopes. `start_run`'s
refusals share `engine.StartRefused` and its written `message`; it audits every start. A
`FELIX_TRIGGERS` entry with a `WorkflowTarget` starts a run too (`routes/triggers.py`: prepared
before the rate limit, started after): `triggers.workflow_inputs` maps the payload to inputs by JSON
Pointer, checks each against the declared schema (`ports.value_mismatch`), and lists the sender's
strings, objects, arrays and `$delivery` as the run's `untrusted_inputs`
(`0044_workflow_run_untrusted`). `runs.fences_prompts` (any untrusted input, or a `trigger:` run)
makes `kinds.render_prompt(fenced=)` wrap every value an agent step is given in a per-prompt
`data-<hex>` tag and append `FENCE_NOTE`.
`next_run_at_ms` reads the grammar in `jobs/schedule.py` — seconds, `every:30s` / `@every 5m`, the
`@daily`-style macros and five-field cron in UTC — and a schedule outside it is refused, never
guessed. An empty schedule is manual only: the sweep skips it and only `POST /jobs/{name}/run` fires it.

## Workflows

An authored workflow (`felix/workflows/`) is a DAG of typed steps, wired output port to input
port. `schema.py:WorkflowSpec` validates structure on parse; `ports.py` holds port types and
`port_mismatch` (advisory: it accepts what it cannot judge); `check.py:check()` resolves every
step's ports and returns them beside its `Problem`s (`ProblemCode` is a closed `Literal` a client
switches on) — it *returns*, because a half-wired draft must save and only a run refuses one.
Step kinds are an open registry (`kinds.py:register_step_kind`, built in: `agent_turn`,
`tool_call`, `approval`, `branch`); a kind's `ports()` is pure, and `kind`/`config` stay open on
parse so a stored row naming an uninstalled kind still loads. A kind that raises is a
`kind_error` on its step, never an exception out of the check. Every step has the implicit control
input `when`; the workflow's inputs are the outputs of the non-step node `trigger`.
`store.py` replaces a definition whole and bumps `version`; the row's `revision`
(`<generation>.<version>`, the generation fixed at creation so a re-created id never reuses one)
is the one `precondition` — a revision string, `ABSENT` (create only), or `None` — matching HTTP's
`If-Match` / `If-None-Match: *`; revisions compare as written on both arms. A miss raises
`WorkflowConflict` with the current row, a Postgres create race included. Contract:
`tests/conformance/test_workflows_store.py`.

**Runs** (`engine.py`, `executors.py`, `runs.py`). `start_run` refuses a definition with problems
or a kind with no executor (`not_runnable`; `RUNNABLE_KINDS` is `executors.EXECUTORS`' keys — a
plugin kind has none), checks the starter against each named manifest's inbound rules
(`NotPermitted`) and **pins** it (`pins_json`; the child's `pin` forces `pin_compile`), caps a
tenant at `MAX_ACTIVE_RUNS_PER_TENANT`, records the starter with `durability/runs.py:record_caller`,
and writes a `workflow_runs` row (definition snapshot) plus an orchestrator fiber
(`kind: workflow_run`, op `workflow`) that calls `advance` and parks `POLL_MS`. `advance` polls
in-flight steps, starts what is ready (at most `MAX_CHILD_STEPS_IN_FLIGHT` agent and tool steps), and saves
under the `version` CAS only when something changed. Each kind's `start`/`poll`/`stop` is its
executor. An `agent_turn` is a child durable chat on `thread_ids.workflow_thread_id`, its fiber id
derived from run and step (a replay or a second claim adopts it, `RunInProgress` included), started
as the starter — or with no context when none was recorded — and bounded by the run's deadline and
the manifest's own TTL; a child's error text is logged and the step gets a written message. An
`tool_call` is a child fiber of kind `workflow_tool` (op `tool`, `tool_step.py`): it resolves the
step's `manifest` as the starter, holds it to the pin (forced), compiles it and calls the named tool
through the governed executor — every wrapper applies, approvals included — after checking the tool
is still bound and its live input schema still takes what the step sends (`schema_drift`) and
screening the arguments as MCP does. `tool_began` is checkpointed before the call; a replayed claim
repeats it only for a `replay_safe` tool. A client tool (`spec.client_tools`) has no client to
answer it and is refused (`step_refusal`). `step_tools` is what `GET /workflows/-/tools` lists for
the editor: the same `runtime.governed_tools` compile, as the caller, less what `step_refusal`
refuses — so the listing, the MCP server and the step agree on a manifest's tools. A tool input
keeps the tool's own argument name (`pullNumber`), not the port-name alphabet. An
`approval` is found by signature (`approvals.store.find_by_signature`, any status) before
`create_pending`; a lapsed or swept one fails the step. Stopping (cancel, expiry, fail-fast) calls
every unfinished step's `stop`: `fibers.stop_fiber` by derived id plus `steer.request_abort`, and
`close_interrupted` for approvals. A buried orchestrator fails its run (`_after_terminal` →
`orchestrator_ended`). `_start_step` and `_bound` fail a step whose outputs pass `MAX_STEP_OUTPUT_BYTES`
(128 KiB) or whose inputs or outputs would take the run's record past `MAX_RUN_RECORD_BYTES` (4 MiB),
and cut a step's error to `MAX_STEP_ERROR_CHARS`. `save_run` clears `auth`/`pins` on the save that
ends a run, and `retention_sweep` deletes finished runs after `FELIX_WORKFLOW_RUN_RETENTION_DAYS`. Contracts: `tests/conformance/test_workflow_runs_store.py`,
`tests/unit/test_workflow_engine.py`, `tests/e2e/test_workflow_runs.py`.

## memory:// and tests

Fibers and webhook delivery use `_memory_fibers` (`reset_memory_fibers`); jobs `_memory_jobs` /
`_memory_runs` (`reset_jobs_for_tests`); push `_memory_subscriptions` (`reset_push_for_tests`);
`tests/conftest.py` calls each reset. Idempotency, waiters, steer and notify go in-process when no
Redis is in use — chosen by Redis, not the DB URL. No worker runs in tests: they call
`resume_due_fibers` / `deliver_due_webhooks` (`tests/e2e/test_durable_resume.py`,
`tests/e2e/test_completion_webhooks.py`). Unit tests are named for the piece (`test_fiber_*`, `test_durable_stream*`, `test_chat_idempotency`,
`test_waiters`, `test_push*`, `test_worker_*`); conformance has the fiber, jobs, idempotency and
cross-replica notify arms in `tests/conformance/` (the last two need `FELIX_CONFORMANCE_REDIS_URL`).

## Changing durable execution

1. New fiber field or status: both claim arms, `_save_fiber`, a migration (**postgres-migrations**);
   a new terminal status also goes in `FIBER_TERMINAL_STATUSES` and `felix_client.RUN_TERMINAL`
   (`tests/unit/test_invariants.py` checks they agree).
2. Write `status` only through `_save_fiber`, so the CAS and `_announce_fiber` run.
3. A new op in `_run_fiber_step` must survive running twice: a lost save replays it.
4. New periodic task: `@broker.task(schedule=...)` + `_instrumented`, and `EXPECTED_SCHEDULES`.
   Cross-tenant sweeps use `rls_bypass`; per-tenant ones bind `rls_tenant`.
5. Route or frame change: `make contract` and read the diff (**api-surface**).
6. Verify: `./scripts/test.sh tests/unit -k "fiber or durable or webhook or idempot or waiter or worker" -q`,
   then the Postgres and Redis conformance arms.
