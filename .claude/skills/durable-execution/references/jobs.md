# Jobs, scheduled tasks and heartbeats

Detail for the **durable-execution** skill: how a job is claimed and fired, the schedule grammar,
`spec.scheduling`'s agent tasks, and heartbeats. Read it before changing `felix/jobs/` or
`routes/jobs.py` / `routes/heartbeats.py`.

Plugin `cron_tasks` become `plugin_<name>` every minute via `_register_plugin_cron_tasks`, which only
the worker's startup hook calls. Each built-in task is wrapped in `_instrumented`; plugin runners are not. Jobs:
`jobs/scheduler.py:run_due_jobs_all_tenants` binds each tenant with `rls_tenant`,
`jobs/store.py:claim_run` compare-and-sets `next_run_at` and refuses a job whose `last_status` is
`running` unless `last_run_at` is older than `RUNNING_STALE_MS` (6 h: three times a run's
longest wall clock plus a last approval wait), so a run outlasting its interval is not fired beside
itself; refused, the due time stays put and the first tick after the run fires it once.
`POST /jobs/{name}/run` takes the same mark (`claim_manual_run`) and answers `409 {"error":
"job_running"}` while it stands. `fire_job` clears it on every exit, the outer `except` and a
cancellation included (`_clear_running`), through `finish_run`, which compare-and-sets on
`last_run_at` so a run reclaimed as stale cannot clear its reclaimer's mark. `PUT /jobs/{name}` with
`enabled: false` clears a mark (`DISABLED_WHILE_RUNNING`), the operator's override for a dead worker. A firing is unattended (`RequestContext.unattended`, from
`AuthContext.scheduled`), so a gated tool without `allow_unattended` is refused at once rather than
holding the sweep for the rule's ttl. `fire_job` runs as `cron` on behalf of the
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
`data-<hex>` tag and append `FENCE_NOTE`. `WorkflowTarget.revision` is required (`_parse_target`,
so boot fails without it) and passed to `start_run`, so no save -- a person's or an agent's -- changes
what a trigger starts.
`jobs/schedule.py:parse_schedule` reads the grammar — seconds, `every:30s` / `@every 5m`, the
`@daily`-style macros, five-field cron, and a one-shot `at:<ISO-8601 with offset>` — and a
schedule outside it is refused, never guessed. Cron and macros are read in UTC, or in the job's
`timezone` (`0051_job_timezone`, NULL = UTC) on that zone's wall clock: a skipped local time fires at
the first instant after the gap, a repeated one at its first occurrence (fold=0). A zone is refused
beside an interval, `at:` or empty schedule. Every reader of a stored job parses it through
`schedule.job_schedule(job)`, never `parse_schedule(job["schedule"])`, so none reads a zoned job in
UTC; `put_job` treats a changed zone as a changed schedule. `shortest_gap_ms` and
`most_firings_per_utc_day` also sample the days around each offset change in the next year
(`_offset_change_days`), where the gap is shortest and a UTC day can hold an extra firing. An empty schedule is manual only: the sweep skips it and
only `POST /jobs/{name}/run` fires it. A one-shot's `next_run_at` is set by the write that sets its
schedule (`put_job`, refused if past) and its claim clears it, so it fires once; one with no due time
is never swept.

`spec.scheduling` binds `jobs/agent_tools.py`: `schedule_task` writes a job named `task-<hex>` on the
compiling manifest as the caller (`created_by` = `acting_for`, `created_auth` = `creator_auth`), with
`payload.scheduled_by` marking it a task (only this tool writes it; `PUT /jobs` ignores a caller's, keeping the stored one for the same writer); `list_scheduled` / `cancel_scheduled` see only tasks whose
`scheduled_by` names this manifest and whose writer is the caller. Refused: no named caller, no
`jobs:write`, a shared subject (a sub-less API key's `api_key`, a trigger run's `trigger:<id>`, a
machine actor acting for no one; a plugin authenticator handing out a shared subject is the operator's
problem, since the list cannot know it),
scheduled work (`AuthContext.scheduled`: set by `job_auth`, recorded by `auth/recorded.py`, so a
workflow a job starts carries it into its agent steps), the `max_tasks` cap, a recurring
gap under `min_interval_minutes` (a cron's next 200 firings walked), an `at:` over 366 days out. The
same checks (`_AgentTasks.admit`) run in `schedule_task`'s approval preview, so a refused call is denied
with no approval row and no wait. `permission_mode.UNWAIVABLE_TOOLS` holds `schedule_task`, so
no mode waives an approval on it, whichever rule asked. A task records its writer's token expiry in `created_auth.expires_at`;
`fire_job` refuses and disables a job past it (`CREDENTIAL_EXPIRED`). Retention
(`retention._sweep_fired_tasks`, `FELIX_SCHEDULED_TASK_RETENTION_DAYS`) deletes agent tasks that will not run again (a fired one-shot, or disabled on expiry), per tenant,
re-reading each before the delete; a disabled task holds no place under `max_tasks`. With
`approval: true` the builder adds the `permission_mode.SCHEDULING_APPROVAL` rule unless a manifest rule without `when_args`
names `schedule_task` literally (a glob does not count).

**Heartbeats** (`jobs/heartbeat.py`, `routes/heartbeats.py`). `spec.heartbeat` is the operator's;
`PUT /heartbeats/{manifest}` (`heartbeats:write`, `require_person`) resolves the manifest (404), runs
its inbound auth as the firing's caller (`scheduler.job_auth` over the would-be job) and only then
answers `no_heartbeat` (422), so a refused caller learns nothing. `enrol` writes job
`hb-<manifest>-<sha256(person)[:12]>` as the person (`creator_auth`; `expires_at` = the token's `exp`
only when `scheduler.rechecks_at_firing` says no firing can re-check the credential -- not an API
key, not a configured Felix sign-in), `first_run_at` the next slot, payload `{"scheduled_by": {"tool":
"heartbeat", "manifest", "thread_id"}, "fresh_thread": true}` -- the thread the person's conversation
`{tenant}:hb-<16 hex>`, made by `thread_state.claim_thread` (titled, kept across re-enrols while it
exists). `jobs_store.scheduled_by(job, tool)` is the one reader of that record (it must name the
job's own manifest), used by heartbeats, `schedule_task`'s listing, `is_fired_task`, the firing's
`SCHEDULED_FROM_EXTRA` and `PUT /jobs`' carry-forward. `_invoke_job_manifest` asks
`hb.begin_firing` for a `Firing` (None for a plain job): its `prompt` is the *current* checklist,
never the payload; `refusal` when the manifest dropped its heartbeat (disabled, `NO_HEARTBEAT`) --
and `hb.manifest_missing` disables one whose manifest no longer resolves (`NO_MANIFEST`); `adjust`
applies `model` and the lower of `max_turns` and the manifest's own bounds after
`prepare_tenant_invoke`; `after` prunes; `result` records `quiet` (`extras[SENT_THIS_RUN_EXTRA] == 0`)
and the answer cut to 500. `keep_schedule_current` moves a drifted schedule with
`jobs_store.set_schedule` (one conditional UPDATE, never an upsert). `hb.over_daily_cap` (in
`begin_firing`, after the schedule refresh) makes `refusal` a `skipped` result (`reason: daily_cap`)
once `jobs_store.count_runs_since(..., utc_day_start(now))` -- every status but `SKIPPED`, manual runs
included -- reaches `max_firings_per_day` (None, the default, caps nothing); `fire_job` records
`skipped` as its own status rather than `error`, and the job stays enabled. `fire_job`'s cancelled
and failed paths go through `_end_abnormally`, which records the run (`CANCELLED`, under
`asyncio.shield`; `EXECUTION_FAILED`) *before* `_clear_running`, so every firing counts.
`hb.cap_warnings` (via `governance.manifest_warnings`) warns when an explicit cap is below
`schedule.most_firings_per_utc_day`. `record_run` then trims every job to `FELIX_JOB_RUNS_KEEP`
(`trim_runs`: newest by `(started_at, run_id)`, at most `TRIM_BATCH` per call, never a non-skipped run
of the current UTC day, so the cap's count survives a small keep; contract in
`tests/conformance/test_jobs_store.py`). Firing-thread retention is
`prune_firing_threads` over `session/prune.py` (`threads_under`, `delete_threads` -- the one
definition of deleting a thread whole, which retention's memory arm also uses; contract in
`tests/conformance/test_session_prune.py`), keeping the newest `keep_threads` firing-minted threads
and never the conversation. An expired enrolment takes `fire_job`'s credential-expired path with
`hb.CREDENTIAL_EXPIRED`. e2e: `tests/e2e/test_heartbeat.py`.

**Reader's preferences** (`jobs/preferences.py`, `0052_job_preferences`). `jobs.preferences` (text,
at most `PREFERENCES_MAX_BYTES` = 8 KiB of UTF-8, refused past it as `PreferencesTooLong`) with
`preferences_updated_at` / `_by`, written only by `store.set_preferences` (one conditional UPDATE,
never an upsert) from `GET/PUT/DELETE /jobs/{name}/preferences`. All three need the job's writer
(`routes/jobs.py:_is_job_writer`: `created_by == acting_for`, a named person per `require_person`)
or an operator (`is_operator`) -- not `jobs:read` / `jobs:write`, since the text runs on the
writer's scopes; anyone else gets `missing_scopes(SCOPE_ADMIN)` whether the job exists or not, and a
workflow job 422 `preferences_need_a_manifest`. `run_job_now` uses the same helper with
`shared_ok=True`, its long-standing subject-only match. Each write is audited `job.preferences`
(`saved` / `cleared`, payload the job and the byte count, never the text). `put_job` keeps them on
a re-save by the same `created_by` and clears them (`_NO_PREFERENCES`) when the writer changes, on
both arms; the memory arm starts from a copy of the stored row, so any column the writer does not
write is kept as the Postgres UPDATE keeps it. `preferences_required` is the writer's (`JobUpsert`,
false when left out; a heartbeat's enrolment passes the stored value). No tool, manifest field or
`schedule_task` argument sets them. In `_invoke_job_manifest`, `missing_preferences(job)` returns a
`skipped` result (`reason: preferences_missing`, `error: PREFERENCES_MISSING_MESSAGE`, which
`fire_job` writes to `last_error`) before anything resolves, so no model call; `_run_job_turn` calls
`with_preferences(prompt, job)` after `begin_firing` -- on a heartbeat's checklist as on a job's
prompt -- appending the text whole in `fence_fresh(..., PREFERENCES_TAG)` (a region named
`reader_preferences-<12 hex>` per firing) and `PREFERENCES_FRAME`, in the *user* turn (the reader's
authority, screened like the prompt), never the system prompt. `GET /jobs` shows `preferences_set`,
not the text. The watch ignores the skipped runs (`failing_streak`) and a skipping job keeps its
schedule, so it is never `overdue` either: visible on `last_status` / `last_error`, never alerted.
e2e: `tests/e2e/test_job_preferences.py`; contract in `tests/conformance/test_jobs_store.py`.

## Job health watch

`jobs/tick.py`: `run_due_jobs_all_tenants` ends with `record_tick` (Redis key `TICK_KEY`, never moved
backwards, plus an in-process copy) -- unless every tenant's sweep raised, when it stamps nothing and
raises. `scheduler_health` (via `read_tick`) serves `GET /health/scheduler` (`apps/api/.../app.py`, in
`PROBE_PATHS`) and `felix doctor`: `ok` within `FELIX_SCHEDULER_STALE_SECONDS`, else 503 `stale`,
`never`, or `unknown` (Redis in use and unreadable, no local stamp). `jobs/watch.py` is the
`job_health_watch` cron (every 5 min): per tenant under `rls_tenant`, with the tick read once.
`overdue_since` (enabled, not manual, due before `now - FELIX_JOB_OVERDUE_GRACE_SECONDS`; a `running`
mark is no exemption) is a candidate: `_next_health` keeps `OVERDUE_SEEN_AT` and opens `overdue` only
once the tick is `SWEEP_CHANCE_MS` past that sighting. `failing_streak` (the newest
`FELIX_JOB_FAILURE_THRESHOLD` non-skipped runs all `error`) opens `failing`, which stays until a run
since its `since` succeeded (a new streak in the same sweep opens a new incident; counting is by
object identity). State is `jobs.health_json` (`store.set_health`, compare-and-set on the whole
JSONB; `put_job` leaves it); disabling clears everything. A new incident is counted
(`felix_job_health_incident`), audited (`job.health`, `open`/`cleared`, offline as `cron`), and its
alert retried while `pending` or `unrouted` through `destinations/publish.py:publish` -- the
claim/deliver/settle path `post` shares -- under `EditionKey(tenant, FELIX_JOB_ALERT_DESTINATION,
ALERT_MANIFEST="felix:job-health", edition(job, kind, since))`, the destination from
`resolve_harness_destination` (an `operator: true` one for any tenant, which `resolve_destinations`
refuses to every manifest; else one open to the tenant). `not_posted`/`in_flight`/ledger failure stay
`pending` (at most `MAX_ALERT_ATTEMPTS`); unknown is `maybe_posted`. The text is `alert_text`, title
and text through `dest.apply_links`: backticked masked names and `error_class`, never `last_error`.
Tests: `tests/unit/test_job_health_watch.py`, `tests/e2e/test_job_health_watch.py`, the `set_health`
case in `tests/conformance/test_jobs_store.py`.
