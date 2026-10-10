---
name: durable-execution
description: How Felix runs work that outlives a request — durable chat runs as fibers (spec.execution.mode durable, 202 + resume_token), the claim/lease/retry loop in the worker, resume and reattach streams and their cursor, signed completion webhooks, cross-process waiters for approvals and client tools, steer/abort, Idempotency-Key on POST /chat and /chat/stream, Web Push for a run waiting on a person, scheduled jobs, and the Taskiq worker and its cron table. Use when editing felix/durability/, felix/jobs/, felix/push/, felix/waiters.py, felix/idempotency.py, felix/steer.py or apps/worker, when a durable run never starts, stalls, repeats a tool call or never reports, when a webhook or push is not delivered, or when adding a periodic task.
allowed-tools: Read Grep Glob Bash(./scripts/test.sh:*)
metadata:
  covers: felix/durability/, felix/jobs/, felix/push/, felix/idempotency.py, felix/waiters.py, felix/steer.py, felix_worker/
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
   and runs `_run_fiber_step` (`sleep`, `stash`, `invoke`, `complete`) until the fiber suspends, at
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
The fiber backstop (`fiber_scheduler`), `webhook_delivery` and `run_scheduled_jobs` run every minute.

Plugin `cron_tasks` become `plugin_<name>` every minute via `_register_plugin_cron_tasks`, which only
the worker's startup hook calls. Each task is wrapped in `_instrumented`. Jobs:
`jobs/scheduler.py:run_due_jobs_all_tenants` binds each tenant with `rls_tenant`,
`jobs/store.py:claim_run` compare-and-sets `next_run_at`, `fire_job` runs as `cron` with no scopes.
`next_run_at_ms` reads seconds, `every:30s` / `@every 5m` and `*/N * * * *` only — any other cron
string fires every 60 s.

## memory:// and tests

Fibers and webhook delivery use `_memory_fibers` (`reset_memory_fibers`); jobs `_memory_jobs` /
`_memory_runs` (`reset_jobs_for_tests`); push `_memory_subscriptions` (`reset_push_for_tests`);
`tests/conftest.py` calls each reset. Idempotency, waiters, steer and notify go in-process when no
Redis is in use — chosen by Redis, not the DB URL. No worker runs in tests: they call
`resume_due_fibers` / `deliver_due_webhooks` (`tests/e2e/test_durable_resume.py`,
`tests/e2e/test_completion_webhooks.py`). Unit tests: `test_fiber_*`, `test_durable_stream*`,
`test_one_durable_run_per_thread`, `test_chat_idempotency`, `test_waiters`,
`test_gated_waits_hear_the_record`, `test_push*`, `test_worker_*`, `test_cron_multi_tenant`.
Conformance: `tests/conformance/test_fiber_store.py`, `tests/conformance/test_jobs_store.py`,
`tests/conformance/test_idempotency_redis.py`, `tests/conformance/test_cross_replica_notify.py`
(needs `FELIX_CONFORMANCE_REDIS_URL`).

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
