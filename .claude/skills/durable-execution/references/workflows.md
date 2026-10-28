# Authored workflows

Detail for the **durable-execution** skill: the definition and its store, runs and their step
executors, and `spec.workflow_authoring`'s agent tools. Read it before changing `felix/workflows/`.

## Definitions and runs

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

## Agent-authored workflows

`spec.workflow_authoring` binds `workflows/agent_tools.py`: `workflow_catalog` (kinds, config
schemas, `WorkflowSpec`'s schema, and a manifest's step tools through `tool_step.step_tools`, less
the authoring tools; serialized by `workflows/present.py`, which the `/workflows` routes share),
`check_workflow` (`engine.check_runnable` + `_AgentWorkflows.check_reach`: `kinds`, `max_steps`,
the 4,000-character agent prompt cap, and `manifests` over `engine.named_manifests`, the one rule
for which steps name a manifest),
`save_workflow`, `list_my_workflows`, `run_workflow`. A saved workflow's `created_by` is
`AuthContext.skill_owner` (issuer-qualified; refused when None) and its `authored_via` is
`agent:<manifest>`. `store.put_workflow(owner=)` checks that pair under the row lock, before the
revision precondition, and raises `WorkflowNotOwned`. A save is also checked against
`engine.admitted_manifests`, the admission a run's start makes. `authored_via` is what made the latest save, so a route save adopts the
workflow. The creator's own save (`writer_owner == created_by`) leaves `owner:agent:<manifest>`
(`store._replaced_via`), which `_AgentWorkflows.holds` lets the agent run but `is_mine` not edit;
anyone else's leaves `""`. A create mints `wf-<hex>` with `precondition=ABSENT`; a replace passes the revision it read.

`save_workflow` and `run_workflow` refuse `RequestContext.unattended` and a workflow thread
(`thread_ids.is_workflow_thread`). `admit` also runs in the approval preview. `run_workflow` calls
`engine.start_run(trigger="agent:<manifest>", untrusted_inputs=<every input>)`, and `StartRefused.message`
is relayed. `max_active_runs` counts the non-terminal runs of the person's workflows from this agent.
`run_approval` adds `workflow_run_rule`. Under `auto` the builder passes that rule object to
`apply_approvals(conditional=)`, matched by identity, so no manifest rule can copy its id. The
wrapper asks `Tool.approval_condition` (`_AgentWorkflows.run_needs_approval`) and gates when it
raises. The condition (`run_reach`) walks the edges from the supplied inputs. A reached `tool_call`
step counts by its tool (`_changes_by_tool`, through `step_tools`); every other kind outside
`_INERT_STEP_KINDS` (`approval`, `branch`) always counts, `agent_turn` included: `step_tools` sees
neither a composite's sub-agents nor a tool that failed to bind, nor memory capture. A tool counts as
non-writing only if it is `read_only and not is_peer` (so `task` counts as writing). A
call `admit_run` refuses also gates. `run_binding` binds an approval to the revision, and `run`
starts that revision. Under `auto` any manifest rule matching `run_workflow` suppresses the harness
rule. `admit_run` holds the run's checks. The rule is waivable and not in `BUILTIN_APPROVALS`.
The preview shows the whole definition as escaped JSON and maps a non-refusal failure to
`unavailable`. With `approval: true` the builder adds the `permission_mode.WORKFLOW_SAVE_APPROVAL` rule, unless a manifest
rule without `when_args` names `save_workflow` literally. `UNWAIVABLE_TOOLS` and `UNWAIVABLE_RULE_IDS`
both hold it.
